"""Single-process background scheduler connected to the persistent Harness."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from deepresearch_agent.agents.deep_research_agent import DeepResearchAgent
from deepresearch_agent.agents.multi_agent.integration.multi_agent_factory import MultiAgentFactory
from deepresearch_agent.agents.multi_agent.executor.worker_coordinator import WorkerCoordinator
from deepresearch_agent.config.settings import ARTIFACT_ROOT, AUTO_RESUME_RUNS
from deepresearch_agent.harness.contracts import SourceMode, WorkflowMode
from deepresearch_agent.harness.event_bus import EventBus
from deepresearch_agent.harness.errors import AppError
from deepresearch_agent.harness.recovery import RecoveryManager
from deepresearch_agent.harness.runtime import HarnessRuntime
from deepresearch_agent.harness.workflow import DeepResearchDriver, PlanExecuteReportDriver
from deepresearch_agent.persistence import ArtifactStore, Database
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
    EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository,
    SessionRepository, MemoryRepository, AuditRepository,
    LearningReviewRepository, SkillRepository,
)
from deepresearch_agent.context import ArtifactEditContextBuilder, ContextBuilder, ContextCompactor
from deepresearch_agent.memory import MemoryExtractor, MemoryService
from deepresearch_agent.sessions import SessionSearchService
from deepresearch_agent.config import settings
from deepresearch_agent.evolution import PromotionPolicy, SkillLearningService, SkillLoader, SkillRegistry
from deepresearch_agent.models.prefix_cache import clear_run_cancelled, mark_run_cancelled, tracker as prefix_tracker
from deepresearch_agent.models.get_models import get_llm_model
from deepresearch_agent.retrieval.router import create_default_router
from deepresearch_agent.retrieval.base import TimeoutBoundProvider
from backend.app.schemas import MessageCreate, RunCreate, SessionCreate


class RunService:
    def __init__(
        self,
        database: Database,
        *,
        workflow_factory: Callable[[Any], Any] | None = None,
        artifact_root=ARTIFACT_ROOT,
        skills_root=settings.SKILLS_ROOT,
    ):
        self.database = database
        self.runs = RunRepository(database)
        self.messages = MessageRepository(database)
        self.events = EventRepository(database)
        self.event_bus = EventBus(self.events)
        self.artifact_store = ArtifactStore(artifact_root)
        self._external_factory = workflow_factory
        self._router = None if workflow_factory else create_default_router()
        self._tasks: dict[str, asyncio.Task] = {}
        self.research_model = None
        from backend.app.services.research_service import ResearchService
        self.research = ResearchService(self)
        memory_repository = MemoryRepository(database)
        memory_service = MemoryService(
            memory_repository, AuditRepository(database),
            user_max_tokens=settings.MEMORY_USER_MAX_TOKENS,
            project_max_tokens=settings.MEMORY_PROJECT_MAX_TOKENS,
            user_max_chars=settings.MEMORY_USER_MAX_CHARS,
            project_max_chars=settings.MEMORY_PROJECT_MAX_CHARS,
        )
        session_repository = SessionRepository(database)
        self.sessions = session_repository
        artifact_repository = ArtifactRepository(database)
        skill_repository = SkillRepository(database)
        self.skill_registry = SkillRegistry(skills_root, skill_repository)
        skill_loader = SkillLoader(self.skill_registry)
        self.context_builder = ContextBuilder(
            messages=self.messages, sessions=session_repository, memory_service=memory_service,
            session_search=SessionSearchService(self.messages, session_repository),
            compactor=ContextCompactor(
                session_repository, self.messages,
                threshold_tokens=min(
                    settings.CONTEXT_COMPRESSION_THRESHOLD_TOKENS,
                    max(100, int(settings.CONTEXT_MAX_TOKENS * settings.CONTEXT_COMPRESSION_THRESHOLD_RATIO)),
                ),
                protect_recent_messages=settings.CONTEXT_PROTECT_RECENT_MESSAGES,
                target_tokens=settings.CONTEXT_COMPRESSION_TARGET_TOKENS,
            ),
            skill_loader=skill_loader,
            artifact_context_builder=ArtifactEditContextBuilder(
                self.runs, artifact_repository, self.artifact_store,
            ),
            max_tokens=settings.CONTEXT_MAX_TOKENS,
            max_chars=settings.CONTEXT_MAX_CHARS,
            recent_turns=settings.CONTEXT_RECENT_TURNS,
            session_search_top_k=settings.SESSION_SEARCH_TOP_K,
            session_search_window=settings.SESSION_SEARCH_WINDOW,
        )
        memory_llm = None
        if (
            settings.MEMORY_BACKGROUND_REVIEW_ENABLED
            and workflow_factory is None
            and settings.OPENAI_API_KEY
            and settings.MEMORY_LLM_MODEL
        ):
            memory_llm = get_llm_model(
                model=settings.MEMORY_LLM_MODEL, temperature=0.0,
                max_tokens=settings.MEMORY_LLM_MAX_OUTPUT_TOKENS,
            )
        self.memory_extractor = None if memory_llm is None else MemoryExtractor(
            memory_service, self.runs, self.messages, ContractRepository(database),
            llm=memory_llm,
            min_confidence=settings.MEMORY_LLM_MIN_CONFIDENCE,
            max_candidates=settings.MEMORY_LLM_MAX_CANDIDATES,
            max_message_chars=settings.MEMORY_LLM_MAX_MESSAGE_CHARS,
        )
        learning_llms = []
        if settings.LEARNING_REVIEW_ENABLED and workflow_factory is None and settings.OPENAI_API_KEY and settings.LEARNING_REVIEW_MODEL:
            learning_llms = [
                get_llm_model(model=settings.LEARNING_REVIEW_MODEL, temperature=0.0, max_tokens=settings.LEARNING_REVIEW_MAX_OUTPUT_TOKENS),
                get_llm_model(model=settings.LEARNING_REVIEW_MODEL, temperature=0.0, max_tokens=settings.LEARNING_REVIEW_MAX_OUTPUT_TOKENS),
            ]
        self.skill_learning = SkillLearningService(
            database, LearningReviewRepository(database), skill_repository, self.skill_registry,
            proposer_llm=learning_llms[0] if learning_llms else None,
            critic_llm=learning_llms[1] if learning_llms else None,
            events=self.event_bus, enabled=settings.LEARNING_REVIEW_ENABLED,
            max_retries=settings.LEARNING_REVIEW_MAX_RETRIES,
        )
        self.skill_promotion = PromotionPolicy(
            skill_repository, self.skill_registry, AuditRepository(database),
            require_real_replay=True, staged=True,
            canary_percent=settings.SKILL_CANARY_INITIAL_PERCENT,
        )

    def schedule(self, run_id: str) -> asyncio.Task:
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return existing
        clear_run_cancelled(run_id)
        task = asyncio.create_task(self._execute(run_id), name=f"run:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(run_id, None))
        return task

    async def _execute(self, run_id: str):
        agents: list[Any] = []
        initial = await self.runs.get(run_id)
        initial_config = json.loads(initial.config_snapshot_json or '{}') if initial else {}
        if initial_config.get('research_required') and not initial_config.get('research_study_id'):
            message = await self.messages.get(initial.trigger_message_id)
            await self.research.create(initial, message.content)

        def base_workflow_factory(context, events=None):
            if self._external_factory is not None:
                return self._external_factory(context, events)
            raw_provider = self._router.for_mode(context.source_mode)
            if context.config_snapshot.get('research_study_id'):
                from deepresearch_agent.research.guard import ResearchProvider
                raw_provider = ResearchProvider(raw_provider, self.research.store, context.run_id,
                    targets=context.config_snapshot.get('research_unit'), events=events,
                    assessor=lambda *args: self.research.call_model(context.run_id, 'assess_sources', *args))
            provider = TimeoutBoundProvider(
                raw_provider,
                timeout_seconds=context.budget_limits.tool_timeout_seconds,
            )
            if context.workflow_mode is WorkflowMode.DEEP_RESEARCH:
                agent = DeepResearchAgent(use_deeper_tool=True, retrieval_provider=provider, run_id=context.run_id)
                agents.append(agent)
                driver = DeepResearchDriver(context, agent, events=events)
            else:
                worker = WorkerCoordinator(
                    retrieval_provider=provider,
                    max_parallel_workers=context.budget_limits.max_concurrency,
                )
                bundle = MultiAgentFactory.create_default_bundle(retrieval_provider=provider, worker=worker)
                driver = PlanExecuteReportDriver(context, bundle.orchestrator, events=events)
            driver.research_provider = raw_provider
            return driver

        def workflow_factory(context, events=None):
            if context.config_snapshot.get('research_study_id'):
                from deepresearch_agent.research.workflow import ResearchWorkflow
                return ResearchWorkflow(context, self.research, base_workflow_factory, events)
            return base_workflow_factory(context, events)

        runtime = HarnessRuntime(
            run_repository=self.runs, message_repository=self.messages,
            event_repository=self.events, checkpoint_repository=CheckpointRepository(self.database),
            evidence_repository=EvidenceRepository(self.database), contract_repository=ContractRepository(self.database),
            trajectory_repository=PlanTaskToolRepository(self.database), workflow_factory=workflow_factory,
            artifact_store=self.artifact_store, artifact_repository=ArtifactRepository(self.database),
            event_bus=self.event_bus,
            context_builder=self.context_builder, memory_extractor=self.memory_extractor, skill_distiller=None,
            prefix_tracker=prefix_tracker,
            research_service=self.research,
        )
        context = None
        try:
            context = await runtime.execute_run(run_id)
            return context
        finally:
            for agent in agents:
                if hasattr(agent, "close"):
                    agent.close()
            run = await self.runs.get(run_id)
            config = json.loads(run.config_snapshot_json or "{}") if run else {}
            if config.get('research_study_id') and context is not None:
                runtime._attach_prefix_usage(context)
                await self.research.store.record_usage(run_id, context.budget_usage.llm_tokens, context.budget_usage.elapsed_seconds)
                if run.status in {'failed', 'budget_exhausted', 'cancelled'}:
                    await self._save_research_report(run, context)
            if not config.get('research_study_id') and not config.get("evaluation_run") and run and run.status not in {"paused", "pausing", "awaiting_scope_approval", "outlining"}:
                await self.skill_learning.enqueue_for_run(run_id)
                snapshot = json.loads(run.model_snapshot_json or "{}") if run else {}
                selected = snapshot.get("skill") if isinstance(snapshot, dict) else None
                if isinstance(selected, dict) and selected.get("status") == "canary":
                    checks = await ContractRepository(self.database).list_for_run(run_id)
                    safety_violation = any(item.kind == "source_match" and item.passed != 1 for item in checks)
                    await self.skill_promotion.observe_run(
                        name=str(selected.get("name")), version=str(selected.get("version")),
                        succeeded=bool(run and run.status == "completed"), safety_violation=safety_violation,
                    )

    async def _save_research_report(self, run, context):
        from deepresearch_agent.research.workflow import ResearchWorkflow
        study = await self.research.store.for_run(run.run_id)
        if study is None or study['approved_revision'] != study['current_revision']:
            return
        try:
            await self.research.store.assert_allowed(run.run_id)
        except AppError:
            return
        matrix = await self.research.store.matrix(study['study_id'])
        content = context.report or ResearchWorkflow.render(study['spec'], matrix, matrix['cells'])
        complete = (run.status == 'completed' and not matrix['counts']['missing'] and not matrix['counts']['stale']
                    and not matrix['counts'].get('pending_retry'))
        await self.research.store.set_report(study['study_id'], study['current_revision'], run.run_id, content, complete)
        await self.event_bus.publish(run.run_id, 'research.review_ready', stage='completed',
            payload={'study_id': study['study_id'], 'complete': complete})

    async def run_evaluation_case(self, *, query: str, source_mode: str, workflow_mode: str, forced_skill=None) -> dict[str, Any]:
        """Execute one isolated real Harness Run for paired Skill evaluation."""
        session = await self.sessions.create(SessionCreate(title="[Skill Eval]"))
        message, run, _ = await self.runs.create_for_user_message(
            MessageCreate(session_id=session.session_id, role="user", content=query, client_message_id=None, metadata={"evaluation": True}),
            RunCreate(
                session_id=session.session_id, trigger_message_id="assigned-atomically",
                source_mode=SourceMode(source_mode), workflow_mode=WorkflowMode(workflow_mode),
                config_snapshot={
                    "evaluation_run": True, "disable_skills": forced_skill is None,
                    "forced_skill": forced_skill, "min_evidence": 1,
                    "report_type": "brief", "deep_research_max_iterations": 1,
                    "schema_version": 1,
                },
                budget=settings.HARNESS_BUDGETS,
            ),
        )
        await self.runs.update_model_snapshot(run.run_id, {
            "llm_model": settings.OPENAI_LLM_MODEL,
            "learning_review_model": settings.LEARNING_REVIEW_MODEL,
            "evaluation_arm": "treatment" if forced_skill else "control",
        })
        await self._execute(run.run_id)
        finished = await self.runs.get(run.run_id)
        checks = await ContractRepository(self.database).list_for_run(run.run_id)
        usage = json.loads(finished.usage_json or "{}") if finished else {}
        check_map = {item.kind: bool(item.passed) for item in checks}
        limits = usage.get("limits", {}) if isinstance(usage, dict) else {}
        used = usage.get("usage", usage) if isinstance(usage, dict) else {}
        return {
            "run_id": run.run_id, "completed": bool(finished and finished.status == "completed"),
            "status": finished.status if finished else "missing",
            "citation_integrity": float(check_map.get("citation_integrity", False)),
            "claim_support": float(check_map.get("claim_support", False)),
            "tool_policy_violations": 0,
            "source_leakage": 0 if check_map.get("source_match", False) else 1,
            "safety_passed": bool(check_map.get("source_match", False)),
            "llm_tokens": int(used.get("llm_tokens", 0) or 0),
            "wall_time_seconds": float(used.get("elapsed_seconds", 0) or 0),
            "checks": check_map,
        }

    async def startup_recovery(self, *, auto_resume: bool = AUTO_RESUME_RUNS) -> list[str]:
        run_ids = await RecoveryManager(self.runs).scan(auto_resume=auto_resume)
        for run_id in run_ids:
            self.schedule(run_id)
        await self.skill_learning.recover()
        return run_ids

    async def cancel(self, run_id: str) -> bool:
        requested = await self.runs.request_cancel(run_id)
        if not requested:
            return False
        mark_run_cancelled(run_id)
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        run = await self.runs.get(run_id)
        if run is not None and run.status not in {'completed', 'partial', 'failed', 'cancelled', 'budget_exhausted'}:
            # Cancellation can arrive after Runtime returned a waiting checkpoint,
            # while its scheduler task is still persisting final accounting.
            await self.runs.update_status(
                run_id, status="cancelled", current_stage="cancelled",
                usage=json.loads(run.usage_json or "{}"),
            )
            await self.event_bus.publish(run_id, "run.cancelled", stage="cancelled", payload={"status": "cancelled"})
        return True

    async def pause(self, run_id: str) -> bool:
        requested = await self.runs.request_pause(run_id)
        if not requested:
            return False
        await self.event_bus.publish(
            run_id, "run.pause_requested", stage="pausing", payload={"status": "pausing"},
        )
        task = self._tasks.get(run_id)
        if task is None or task.done():
            if await self.runs.finalize_pause(run_id):
                run = await self.runs.get(run_id)
                await self.event_bus.publish(
                    run_id, "run.paused", stage="paused",
                    payload={"status": "paused", "resume_from_status": run.current_stage if run else "queued"},
                )
        return True

    async def resume(self, run_id: str, *, clarification: str | None = None) -> bool:
        before = await self.runs.get(run_id)
        study = await self.research.store.for_run(run_id)
        if study:
            await self.research.store.assert_allowed(run_id)
        pending_pause = bool(
            before and json.loads(before.config_snapshot_json or "{}").get("pause_requested")
        )
        cancelling_pending_pause = bool(
            pending_pause and before and before.status != "paused"
        )
        resumed = await self.runs.resume(run_id, clarification=clarification)
        if resumed:
            current = await self.runs.get(run_id)
            await self.event_bus.publish(
                run_id, "run.clarification_received" if clarification else "run.resume_requested",
                stage=current.current_stage if current else "queued",
                payload={
                    "has_clarification": bool(clarification),
                    "status": current.status if current else "queued",
                    "cancelled_pending_pause": cancelling_pending_pause,
                },
            )
            if cancelling_pending_pause:
                return True
            active = self._tasks.get(run_id)
            if active is not None and not active.done():
                loop = asyncio.get_running_loop()
                active.add_done_callback(lambda _task: loop.call_soon(self.schedule, run_id))
            else:
                self.schedule(run_id)
        return resumed

    async def shutdown(self) -> None:
        await self.skill_learning.shutdown()
        active = list(self._tasks.values())
        if not active:
            return
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)

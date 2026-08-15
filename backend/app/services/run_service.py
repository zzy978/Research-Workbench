"""Single-process background scheduler connected to the persistent Harness."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from graphrag_agent.agents.deep_research_agent import DeepResearchAgent
from graphrag_agent.agents.multi_agent.integration.multi_agent_factory import MultiAgentFactory
from graphrag_agent.config.settings import ARTIFACT_ROOT, AUTO_RESUME_RUNS
from graphrag_agent.harness.contracts import SourceMode, WorkflowMode
from graphrag_agent.harness.event_bus import EventBus
from graphrag_agent.harness.recovery import RecoveryManager
from graphrag_agent.harness.runtime import HarnessRuntime
from graphrag_agent.harness.workflow import DeepResearchDriver, PlanExecuteReportDriver
from graphrag_agent.persistence import ArtifactStore, Database
from graphrag_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
    EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository,
    SessionRepository, MemoryRepository, AuditRepository,
    SkillRepository,
)
from graphrag_agent.memory import ContextBuilder, EpisodicMemory, MemoryExtractor, MemoryRetriever, MemoryService, SessionSummarizer
from graphrag_agent.config import settings
from graphrag_agent.evolution import SkillLoader, SkillRegistry, TrajectoryDistiller
from graphrag_agent.retrieval.router import create_default_router


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
        memory_repository = MemoryRepository(database)
        memory_service = MemoryService(memory_repository, AuditRepository(database))
        self.skill_registry = SkillRegistry(skills_root, SkillRepository(database))
        skill_loader = SkillLoader(self.skill_registry)
        self.context_builder = ContextBuilder(
            self.messages, EpisodicMemory(self.messages, self.runs),
            MemoryRetriever(memory_repository, max_chars=settings.SEMANTIC_MEMORY_MAX_CHARS),
            SessionSummarizer(SessionRepository(database), self.messages, threshold_messages=settings.SESSION_SUMMARY_THRESHOLD_MESSAGES),
            skill_loader=skill_loader, max_chars=settings.CONTEXT_MAX_CHARS, recent_turns=settings.CONTEXT_RECENT_TURNS,
        )
        self.memory_extractor = MemoryExtractor(memory_service, self.runs, self.messages, ContractRepository(database))
        self.skill_distiller = TrajectoryDistiller(database, self.skill_registry, self.runs, self.messages, ContractRepository(database))

    def schedule(self, run_id: str) -> asyncio.Task:
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(self._execute(run_id), name=f"run:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(run_id, None))
        return task

    async def _execute(self, run_id: str):
        agents: list[Any] = []

        def workflow_factory(context):
            if self._external_factory is not None:
                return self._external_factory(context)
            provider = self._router.for_mode(context.source_mode)
            if context.workflow_mode is WorkflowMode.DEEP_RESEARCH:
                agent = DeepResearchAgent(use_deeper_tool=True, retrieval_provider=provider, run_id=context.run_id)
                agents.append(agent)
                return DeepResearchDriver(context, agent)
            bundle = MultiAgentFactory.create_default_bundle(retrieval_provider=provider)
            return PlanExecuteReportDriver(context, bundle.orchestrator)

        runtime = HarnessRuntime(
            run_repository=self.runs, message_repository=self.messages,
            event_repository=self.events, checkpoint_repository=CheckpointRepository(self.database),
            evidence_repository=EvidenceRepository(self.database), contract_repository=ContractRepository(self.database),
            trajectory_repository=PlanTaskToolRepository(self.database), workflow_factory=workflow_factory,
            artifact_store=self.artifact_store, artifact_repository=ArtifactRepository(self.database),
            event_bus=self.event_bus,
            context_builder=self.context_builder, memory_extractor=self.memory_extractor, skill_distiller=self.skill_distiller,
        )
        try:
            return await runtime.execute_run(run_id)
        finally:
            for agent in agents:
                if hasattr(agent, "close"):
                    agent.close()

    async def startup_recovery(self, *, auto_resume: bool = AUTO_RESUME_RUNS) -> list[str]:
        run_ids = await RecoveryManager(self.runs).scan(auto_resume=auto_resume)
        for run_id in run_ids:
            self.schedule(run_id)
        return run_ids

    async def cancel(self, run_id: str) -> bool:
        return await self.runs.request_cancel(run_id)

    async def resume(self, run_id: str, *, clarification: str | None = None) -> bool:
        resumed = await self.runs.resume(run_id, clarification=clarification)
        if resumed:
            await self.event_bus.publish(
                run_id, "run.clarification_received" if clarification else "run.resume_requested",
                stage="queued", payload={"has_clarification": bool(clarification)},
            )
            self.schedule(run_id)
        return resumed

    async def shutdown(self) -> None:
        active = list(self._tasks.values())
        if not active:
            return
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)

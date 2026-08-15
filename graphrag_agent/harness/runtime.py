"""Persistent Harness Runtime driving Context -> Plan -> Execute -> Report -> Verify."""

from __future__ import annotations

import inspect
import json
import uuid
from typing import Any, Callable

from graphrag_agent.harness.budgets import BudgetExceeded, BudgetLimits, BudgetManager
from graphrag_agent.harness.checkpoints import CheckpointManager
from graphrag_agent.harness.contracts import ContractEvaluator, RunStatus, SourceMode, WorkflowMode
from graphrag_agent.harness.evidence import EvidenceLedger
from graphrag_agent.harness.event_bus import EventBus
from graphrag_agent.harness.recovery import classify_exception, classify_verification_failures
from graphrag_agent.harness.run_context import RunContext
from graphrag_agent.harness.state_machine import StateMachine
from graphrag_agent.persistence.artifact_store import ArtifactStore
from graphrag_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
    EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository,
)

WorkflowFactory = Callable[[RunContext], Any]


class RunCancelled(RuntimeError):
    pass


class HarnessRuntime:
    def __init__(
        self,
        *,
        run_repository: RunRepository,
        message_repository: MessageRepository,
        event_repository: EventRepository,
        checkpoint_repository: CheckpointRepository,
        evidence_repository: EvidenceRepository,
        contract_repository: ContractRepository,
        workflow_factory: WorkflowFactory,
        trajectory_repository: PlanTaskToolRepository | None = None,
        artifact_store: ArtifactStore | None = None,
        artifact_repository: ArtifactRepository | None = None,
        event_bus: EventBus | None = None,
        context_builder: Any | None = None,
        memory_extractor: Any | None = None,
        skill_distiller: Any | None = None,
        lease_seconds: int = 90,
    ):
        self.runs = run_repository
        self.messages = message_repository
        self.events = event_bus or EventBus(event_repository)
        self.checkpoints = CheckpointManager(checkpoint_repository)
        self.evidence_repository = evidence_repository
        self.evidence_ledger = EvidenceLedger(evidence_repository)
        self.contracts = ContractEvaluator(contract_repository)
        self.trajectory = trajectory_repository
        self.artifact_store = artifact_store
        self.artifact_repository = artifact_repository
        self.workflow_factory = workflow_factory
        self.context_builder = context_builder
        self.memory_extractor = memory_extractor
        self.skill_distiller = skill_distiller
        self.state_machine = StateMachine()
        self.lease_seconds = lease_seconds

    async def execute_run(self, run_id: str) -> RunContext:
        owner = f"harness-{uuid.uuid4().hex}"
        if not await self.runs.acquire_lease(run_id, owner, ttl_seconds=self.lease_seconds):
            raise RuntimeError(f"Run {run_id} 已由其他 Runtime 执行")
        context: RunContext | None = None
        try:
            context = await self._load_context(run_id)
            if context.status is RunStatus.INTERRUPTED:
                await self._transition(context, RunStatus.QUEUED, event_type="run.resumed")
            if context.status in {RunStatus.QUEUED, RunStatus.CONTEXT_BUILDING}:
                if context.status is RunStatus.QUEUED:
                    await self._transition(context, RunStatus.CONTEXT_BUILDING, event_type="run.started")
                if self.context_builder is not None:
                    context = await self.context_builder.build(context)
                    selected_skill = context.context_snapshot.get("selected_skill") or {}
                    await self.runs.update_model_snapshot(run_id, {
                        **context.model_snapshot,
                        "skill": None if not selected_skill else {"name": selected_skill.get("name"), "version": selected_skill.get("version")},
                        "context_policy": {
                            "recent_turns": len(context.context_snapshot.get("recent_messages", [])),
                            "semantic_memory_ids": [item.get("memory_id") for item in context.context_snapshot.get("semantic_memories", [])],
                        },
                    })
                else:
                    context.resolved_query = context.resolved_query or context.original_query
            driver = self.workflow_factory(context)
            if inspect.isawaitable(driver):
                driver = await driver
            if context.status is RunStatus.CONTEXT_BUILDING:
                context.workflow_state = driver.snapshot()
                await self.checkpoints.save(context, "context_building")
                await self.events.publish(run_id, "context.completed", stage="context_building", payload={"query_resolved": True, "used_message_ids": context.used_message_ids, "semantic_memory_count": len(context.context_snapshot.get("semantic_memories", []))})
                await self._transition(context, RunStatus.PLANNING)
            budget = BudgetManager(context.budget_limits, context.budget_usage)

            while not self.state_machine.is_terminal(context.status):
                await self._assert_not_cancelled(context)
                budget.assert_available()

                if context.status in {RunStatus.PLANNING, RunStatus.REPLANNING}:
                    failures = list(context.workflow_state.get("verification_failures", []))
                    await driver.plan(failures or None)
                    context.plan_version += 1
                    context.workflow_state = driver.snapshot()
                    await self._persist_plan(context, driver)
                    await self.checkpoints.save(context, context.status.value)
                    await self.events.publish(run_id, "plan.revised" if failures else "plan.created", stage=context.status.value, payload={"plan_version": context.plan_version})
                    await self._transition(context, RunStatus.EXECUTING)

                elif context.status in {RunStatus.EXECUTING, RunStatus.RETRYING}:
                    await self._assert_not_cancelled(context)
                    try:
                        await driver.execute()
                    except Exception as exc:
                        decision = classify_exception(exc)
                        if decision.action == "retry_same":
                            budget.consume_retry()
                            context.budget_usage = budget.usage
                            if context.status is not RunStatus.RETRYING:
                                await self._transition(context, RunStatus.RETRYING, event_type="run.retrying", error_code=decision.reason, error_message=str(exc))
                            await self._transition(context, RunStatus.EXECUTING, event_type="run.retry_resumed")
                            continue
                        raise
                    await self._assert_not_cancelled(context)
                    await self._persist_execution(context, driver, budget)
                    context.workflow_state = driver.snapshot()
                    context.budget_usage = budget.usage
                    await self.checkpoints.save(context, "executing")
                    await self._transition(context, RunStatus.REPORTING)

                elif context.status is RunStatus.REPORTING:
                    await self._assert_not_cancelled(context)
                    context.report = await driver.report()
                    await self._assert_not_cancelled(context)
                    context.workflow_state = driver.snapshot()
                    await self._save_report_artifact(context)
                    await self.checkpoints.save(context, "reporting")
                    await self.events.publish(run_id, "report.completed", stage="reporting", payload={"characters": len(context.report or "")})
                    await self._transition(context, RunStatus.VERIFYING)

                elif context.status is RunStatus.VERIFYING:
                    evidence = await self.evidence_repository.list_for_run(run_id)
                    verdict = await self.contracts.evaluate(
                        run_id=run_id, source_mode=context.source_mode, report=context.report or "",
                        evidence=evidence, min_evidence=int(context.config_snapshot.get("min_evidence", 1)),
                        required_sections=list(context.config_snapshot.get("required_sections", [])),
                        consistency_passed=driver.report_consistency(),
                    )
                    context.workflow_state = driver.snapshot()
                    context.workflow_state["verification_failures"] = verdict.failures
                    await self.checkpoints.save(context, "verifying")
                    await self.events.publish(run_id, "verification.completed", stage="verifying", payload={"passed": verdict.passed, "failures": verdict.failures})
                    if verdict.passed:
                        self.state_machine.validate(context.status, RunStatus.COMPLETED, contract_passed=True)
                        context.status = RunStatus.COMPLETED
                        context.budget_usage = budget.usage
                        await self.runs.complete_verified(run_id, assistant_content=context.report or "", usage=budget.snapshot())
                        await self.checkpoints.save(context, "completed")
                        await self.events.publish(run_id, "run.completed", stage="completed", payload={"verified": True})
                        if self.memory_extractor is not None:
                            try:
                                candidates = await self.memory_extractor.extract_from_completed_run(run_id)
                                for candidate in candidates:
                                    await self.events.publish(run_id, "memory.candidate_created", stage="completed", payload={"memory_id": candidate.memory_id})
                            except Exception as exc:
                                await self.events.publish(run_id, "memory.extraction_failed", stage="completed", payload={"error": type(exc).__name__})
                        if self.skill_distiller is not None:
                            try:
                                candidate = await self.skill_distiller.distill(run_id)
                                if candidate is not None:
                                    await self.events.publish(run_id, "skill.candidate_created", stage="completed", payload={"candidate_id": candidate.candidate_id, "name": candidate.name, "version": candidate.proposed_version})
                            except Exception as exc:
                                await self.events.publish(run_id, "skill.distillation_failed", stage="completed", payload={"error": type(exc).__name__})
                        break

                    decision = classify_verification_failures(verdict.failures)
                    if verdict.recoverable and decision.action == "repair_report" and await driver.repair_report(verdict.failures):
                        budget.consume_retry()
                        context.budget_usage = budget.usage
                        await self._transition(context, RunStatus.REPORTING, event_type="run.retrying")
                    elif verdict.recoverable and decision.action == "replan":
                        budget.consume_replan()
                        context.budget_usage = budget.usage
                        await self._transition(context, RunStatus.REPLANNING, event_type="plan.replanning")
                    else:
                        await self._transition(context, RunStatus.FAILED, event_type="run.failed", error_message=f"Completion Contract 未通过: {', '.join(verdict.failures)}")

            return context
        except RunCancelled:
            assert context is not None
            if context.status is not RunStatus.CANCELLING:
                await self._transition(context, RunStatus.CANCELLING, event_type="run.cancelling")
            await self._transition(context, RunStatus.CANCELLED, event_type="run.cancelled")
            await self.checkpoints.save(context, "cancelled")
            return context
        except BudgetExceeded as exc:
            assert context is not None
            await self._transition(context, RunStatus.BUDGET_EXHAUSTED, event_type="run.budget_exhausted", error_code="BUDGET_EXHAUSTED", error_message=str(exc))
            await self.checkpoints.save(context, "budget_exhausted")
            return context
        except Exception as exc:
            if context is not None and not self.state_machine.is_terminal(context.status):
                if str(exc) == "needs_user_input":
                    await self._transition(context, RunStatus.NEEDS_USER_INPUT, event_type="run.needs_user_input", error_message=str(exc))
                else:
                    await self._transition(context, RunStatus.FAILED, event_type="run.failed", error_code=type(exc).__name__, error_message=str(exc))
                await self.checkpoints.save(context, context.status.value)
            return context if context is not None else await self._load_context(run_id)
        finally:
            await self.runs.release_lease(run_id, owner)

    async def _load_context(self, run_id: str) -> RunContext:
        run = await self.runs.get(run_id)
        if run is None:
            raise ValueError(f"Run 不存在: {run_id}")
        restored = await self.checkpoints.restore(run_id)
        if restored is not None:
            restored.status = RunStatus(run.status)
            restored.cancellation_requested = bool(run.cancellation_requested)
            restored.config_snapshot.update(json.loads(run.config_snapshot_json or "{}"))
            return restored
        message = await self.messages.get(run.trigger_message_id)
        if message is None:
            raise ValueError(f"Run {run_id} 的触发消息不存在")
        limits = json.loads(run.budget_json or "{}") or {}
        return RunContext(
            run_id=run.run_id, session_id=run.session_id, trigger_message_id=run.trigger_message_id,
            source_mode=SourceMode(run.source_mode), workflow_mode=WorkflowMode(run.workflow_mode),
            status=RunStatus(run.status), original_query=message.content,
            config_snapshot=json.loads(run.config_snapshot_json or "{}"),
            model_snapshot=json.loads(run.model_snapshot_json or "{}"),
            budget_limits=BudgetLimits.model_validate(limits),
        )

    async def _transition(self, context: RunContext, target: RunStatus, *, event_type: str | None = None, error_code: str | None = None, error_message: str | None = None) -> None:
        self.state_machine.validate(context.status, target)
        context.status = target
        await self.runs.update_status(context.run_id, status=target.value, current_stage=target.value, error_code=error_code, error_message=error_message, usage={"limits": context.budget_limits.model_dump(), "usage": context.budget_usage.model_dump()})
        await self.events.publish(context.run_id, event_type or "run.stage_changed", stage=target.value, payload={"status": target.value, "error_code": error_code})

    async def _assert_not_cancelled(self, context: RunContext) -> None:
        run = await self.runs.get(context.run_id)
        if run is not None and run.cancellation_requested:
            context.cancellation_requested = True
            raise RunCancelled("Run 已请求取消")

    async def _persist_plan(self, context: RunContext, driver: Any) -> None:
        if self.trajectory is None:
            return
        record = driver.plan_record()
        if record is None:
            return
        plan, tasks = record
        plan_id = str(plan.get("plan_id") or f"plan_{context.run_id}_{context.plan_version}")
        await self.trajectory.save_plan(run_id=context.run_id, plan_id=plan_id, version=context.plan_version, status=str(plan.get("status", "executing")), plan=plan, tasks=tasks, source_mode=context.source_mode.value)

    async def _persist_execution(self, context: RunContext, driver: Any, budget: BudgetManager) -> None:
        seen_calls: set[str] = set()
        for record in driver.execution_records():
            await self.events.publish(context.run_id, "task.completed", stage="executing", payload={"task_id": record.task_id, "evidence_count": len(record.evidence)})
            for call in record.tool_calls:
                if call.tool_call_id in seen_calls:
                    continue
                seen_calls.add(call.tool_call_id)
                existing = await self.trajectory.get_tool_call(call.tool_call_id) if self.trajectory else None
                if existing is not None and existing.status == "completed":
                    continue
                budget.consume_tool(tavily=call.source_mode == "web")
                if self.trajectory:
                    await self.trajectory.prepare_tool_call(tool_call_id=call.tool_call_id, run_id=context.run_id, task_id=record.task_id, tool_name=call.tool_name, source_mode=context.source_mode.value, args=call.args)
                    await self.trajectory.complete_tool_call(call.tool_call_id, result=call.result, error_code="TOOL_FAILED" if call.status == "failed" else None)
                await self.events.publish(context.run_id, "tool.completed" if call.status != "failed" else "tool.failed", stage="executing", payload={"task_id": record.task_id, "tool_call_id": call.tool_call_id, "tool_name": call.tool_name})

        for task_id, tool_call_id, provider, result in driver.evidence_results():
            saved = await self.evidence_ledger.record_results(run_id=context.run_id, task_id=task_id, tool_call_id=tool_call_id, provider=str(provider), results=[result])
            for item in saved:
                await self.events.publish(context.run_id, "evidence.added", stage="executing", payload={"task_id": task_id, "evidence_id": item.evidence_id, "source_mode": item.source_mode.value})

    async def _save_report_artifact(self, context: RunContext) -> None:
        if self.artifact_store is None:
            return
        artifact = self.artifact_store.write_text(f"{context.run_id}/report/final.md", context.report or "", mime_type="text/markdown; charset=utf-8")
        if self.artifact_repository is not None:
            await self.artifact_repository.record(artifact, run_id=context.run_id)

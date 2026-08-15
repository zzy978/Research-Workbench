"""Adapters that place both legacy research workflows behind one Harness loop."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from graphrag_agent.agents.multi_agent.core.execution_record import ExecutionRecord
from graphrag_agent.agents.multi_agent.core.plan_spec import PlanSpec
from graphrag_agent.agents.multi_agent.core.retrieval_result import RetrievalResult
from graphrag_agent.agents.multi_agent.core.state import PlanExecuteState
from graphrag_agent.agents.multi_agent.orchestrator import MultiAgentOrchestrator
from graphrag_agent.agents.multi_agent.planner.base_planner import PlannerResult
from graphrag_agent.agents.multi_agent.reporter.base_reporter import ReportResult

from .run_context import RunContext


class WorkflowDriver(Protocol):
    async def plan(self, failures: list[str] | None = None) -> None: ...
    async def execute(self) -> None: ...
    async def report(self) -> str: ...
    async def repair_report(self, failures: list[str]) -> bool: ...
    def snapshot(self) -> dict[str, Any]: ...
    def execution_records(self) -> list[ExecutionRecord]: ...
    def evidence_results(self) -> list[tuple[str | None, str | None, str, RetrievalResult]]: ...
    def report_consistency(self) -> bool | None: ...
    def plan_record(self) -> tuple[dict[str, Any], list[dict[str, Any]]] | None: ...


class PlanExecuteReportDriver:
    """Thin staged wrapper around the existing Planner/Worker/Reporter components."""

    def __init__(self, context: RunContext, orchestrator: MultiAgentOrchestrator):
        self.context = context
        self.orchestrator = orchestrator
        self.state = PlanExecuteState(
            session_id=context.session_id, run_id=context.run_id,
            source_mode=context.source_mode.value, workflow_mode=context.workflow_mode.value,
            budget_state={"limits": context.budget_limits.model_dump(), "usage": context.budget_usage.model_dump()},
            context_snapshot=context.config_snapshot, input=context.resolved_query or context.original_query,
        )
        self.planner_result: PlannerResult | None = None
        self.report_result: ReportResult | None = None
        self._restore(context.workflow_state)

    def _restore(self, payload: dict[str, Any]) -> None:
        if not payload:
            return
        raw_state = payload.get("state")
        if raw_state:
            self.state = PlanExecuteState.model_validate(raw_state)
            if isinstance(self.state.plan, dict):
                self.state.plan = PlanSpec.model_validate(self.state.plan)
            self.state.execution_records = [
                item if isinstance(item, ExecutionRecord) else ExecutionRecord.model_validate(item)
                for item in self.state.execution_records
            ]
        if payload.get("planner_result"):
            self.planner_result = PlannerResult.model_validate(payload["planner_result"])
        if payload.get("report_result"):
            self.report_result = ReportResult.model_validate(payload["report_result"])

    async def plan(self, failures: list[str] | None = None) -> None:
        self.planner_result = await asyncio.to_thread(
            self.orchestrator.plan, self.state, assumptions=failures or None,
        )
        if self.planner_result.plan_spec is None:
            if self.planner_result.needs_clarification():
                raise RuntimeError("needs_user_input")
            raise RuntimeError("Planner 未生成可执行 PlanSpec")

    async def execute(self) -> None:
        if self.planner_result is None:
            raise RuntimeError("缺少 PlannerResult")
        records = await asyncio.to_thread(self.orchestrator.execute, self.state, self.planner_result)
        existing = {item.record_id for item in self.state.execution_records}
        self.state.execution_records.extend(item for item in records if item.record_id not in existing)

    async def report(self) -> str:
        self.report_result = await asyncio.to_thread(self.orchestrator.report, self.state)
        return self.report_result.final_report

    async def repair_report(self, failures: list[str]) -> bool:
        if self.report_result is None:
            return False
        report = self.report_result.final_report
        if "required_section" in failures and not report.lstrip().startswith("#"):
            report = "# 研究报告\n\n" + report
        if "source_diversity" in failures and "局限" not in report:
            report += "\n\n## 局限\n\n当前证据来源数量有限，结论应结合更多独立来源复核。"
        self.report_result.final_report = report
        self.state.response = report
        return set(failures).issubset({"required_section", "source_diversity"})

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.model_dump(mode="json"),
            "planner_result": self.planner_result.model_dump(mode="json") if self.planner_result else None,
            "report_result": self.report_result.model_dump(mode="json") if self.report_result else None,
        }

    def execution_records(self) -> list[ExecutionRecord]:
        return list(self.state.execution_records)

    def evidence_results(self) -> list[tuple[str | None, str | None, str, RetrievalResult]]:
        output = []
        for record in self.state.execution_records:
            tool_call_id = record.tool_calls[0].tool_call_id if record.tool_calls else None
            provider = record.tool_calls[0].tool_name if record.tool_calls else "unknown"
            for item in record.evidence:
                result = item if isinstance(item, RetrievalResult) else RetrievalResult.from_dict(item)
                output.append((record.task_id, tool_call_id, provider, result))
        return output

    def report_consistency(self) -> bool | None:
        if not self.report_result or self.report_result.consistency_check is None:
            return None
        return self.report_result.consistency_check.is_consistent

    def plan_record(self) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        if self.planner_result is None or self.planner_result.plan_spec is None:
            return None
        plan = self.planner_result.plan_spec
        return plan.model_dump(mode="json"), [node.model_dump(mode="json") for node in plan.task_graph.nodes]


class DeepResearchDriver:
    """Keeps the existing DeepResearch loop as execute, with deterministic plan/report stages."""

    def __init__(self, context: RunContext, agent: Any):
        self.context, self.agent = context, agent
        research_tool = getattr(agent, "research_tool", None)
        if getattr(research_tool, "deep_research", None) is not None:
            research_tool = research_tool.deep_research
        max_iterations = context.config_snapshot.get("deep_research_max_iterations")
        if max_iterations is not None and research_tool is not None:
            research_tool.max_iterations = max(2, min(int(max_iterations), research_tool.max_iterations))
        payload = context.workflow_state
        self.answer: str | None = payload.get("answer")
        self.results = [RetrievalResult.from_dict(item) for item in payload.get("results", [])]
        self._report: str | None = payload.get("report")
        self._plan = payload.get("plan")

    async def plan(self, failures: list[str] | None = None) -> None:
        self._plan = {
            "plan_id": f"plan_{self.context.run_id}", "version": self.context.plan_version + 1,
            "status": "executing", "source_mode": self.context.source_mode.value,
            "tasks": [{"task_id": f"task_{self.context.run_id}_research", "task_type": "deep_research", "status": "pending", "source_mode": self.context.source_mode.value, "description": self.context.original_query, "parameters": {"query": self.context.resolved_query or self.context.original_query}}],
            "verification_failures": failures or [],
        }

    async def execute(self) -> None:
        if self.answer is None:
            self.answer = await asyncio.to_thread(self.agent.ask, self.context.resolved_query or self.context.original_query, self.context.session_id)
        tool = getattr(self.agent, "research_tool", None)
        provider_results = list(getattr(tool, "provider_results", []) or [])
        if not provider_results and getattr(tool, "deep_research", None) is not None:
            provider_results = list(getattr(tool.deep_research, "provider_results", []) or [])
        if provider_results:
            self.results = provider_results

    async def report(self) -> str:
        body = self.answer or ""
        citations = " ".join(f"[{item.result_id}]" for item in self.results[:5])
        limitation = "\n\n## 局限\n\n当前证据来源数量有限，结论应结合更多独立来源复核。" if len({item.metadata.source_id for item in self.results}) < 2 else ""
        self._report = f"# 深度研究报告\n\n{body}\n\n## 证据引用\n\n{citations}{limitation}".strip()
        return self._report

    async def repair_report(self, failures: list[str]) -> bool:
        if self._report is None:
            return False
        if "source_diversity" in failures and "局限" not in self._report:
            self._report += "\n\n## 局限\n\n当前证据来源数量有限。"
        return set(failures).issubset({"required_section", "source_diversity"})

    def snapshot(self) -> dict[str, Any]:
        return {"answer": self.answer, "report": self._report, "plan": self._plan, "results": [item.to_dict() for item in self.results]}

    def execution_records(self) -> list[ExecutionRecord]:
        task_id = f"task_{self.context.run_id}_research"
        tool = getattr(self.agent, "research_tool", None)
        if getattr(tool, "deep_research", None) is not None:
            tool = tool.deep_research
        calls = list(getattr(tool, "provider_calls", []) or [])
        if not calls and self.results:
            calls = [{"tool_call_id": f"call_{self.context.run_id}_research", "query": self.context.resolved_query or self.context.original_query, "results": self.results}]
        records = []
        tool_name = "tavily_search" if self.context.source_mode.value == "web" else "deep_research"
        for index, call in enumerate(calls):
            results = list(call.get("results", []))
            records.append(ExecutionRecord(
                record_id=f"record_{self.context.run_id}_{index}", task_id=task_id,
                session_id=self.context.session_id, worker_type="deep_research",
                inputs={"query": call.get("query")},
                tool_calls=[{
                    "tool_name": tool_name, "tool_call_id": call["tool_call_id"],
                    "source_mode": self.context.source_mode.value, "args": {"query": call.get("query")},
                    "result": {"result_ids": [item.result_id for item in results]}, "status": "success",
                }], evidence=results,
            ))
        return records

    def evidence_results(self) -> list[tuple[str | None, str | None, str, RetrievalResult]]:
        task_id = f"task_{self.context.run_id}_research"
        provider = getattr(self.agent, "retrieval_provider", None)
        provider_name = getattr(provider, "provider_name", "deep_research")
        tool = getattr(self.agent, "research_tool", None)
        if getattr(tool, "deep_research", None) is not None:
            tool = tool.deep_research
        calls = list(getattr(tool, "provider_calls", []) or [])
        if calls:
            return [(task_id, call["tool_call_id"], provider_name, item) for call in calls for item in call.get("results", [])]
        return [(task_id, f"call_{self.context.run_id}_research", provider_name, item) for item in self.results]

    def report_consistency(self) -> bool | None:
        return None

    def plan_record(self) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        if not self._plan:
            return None
        return self._plan, list(self._plan["tasks"])

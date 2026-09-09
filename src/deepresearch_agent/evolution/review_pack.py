"""Build bounded, traceable ReviewPacks without exposing full raw artifacts."""

from __future__ import annotations

import json

from sqlalchemy import select

from deepresearch_agent.persistence.models import (
    CheckpointModel, ContractCheckModel, EvidenceModel, MessageModel, RunEventModel, RunModel,
    SkillVersionModel, TaskModel, ToolCallModel,
)

from .review_schema import DecisionCard, FailureCard, ReviewPack, TrajectoryEpisode


def _json(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


class ReviewPackBuilder:
    def __init__(self, database):
        self.database = database

    async def build(self, *, review_id: str, run_id: str) -> ReviewPack:
        async with self.database.sessions() as session:
            run = await session.get(RunModel, run_id)
            if run is None:
                raise ValueError(f"Run 不存在: {run_id}")
            message = await session.get(MessageModel, run.trigger_message_id)
            tasks = list((await session.execute(select(TaskModel).where(TaskModel.run_id == run_id))).scalars())
            calls = list((await session.execute(select(ToolCallModel).where(ToolCallModel.run_id == run_id).order_by(ToolCallModel.created_at))).scalars())
            evidence = list((await session.execute(select(EvidenceModel).where(EvidenceModel.run_id == run_id))).scalars())
            checks = list((await session.execute(select(ContractCheckModel).where(ContractCheckModel.run_id == run_id))).scalars())
            events = list((await session.execute(select(RunEventModel).where(RunEventModel.run_id == run_id).order_by(RunEventModel.event_id))).scalars())
            checkpoint = (await session.execute(select(CheckpointModel).where(
                CheckpointModel.run_id == run_id
            ).order_by(CheckpointModel.version.desc()).limit(1))).scalar_one_or_none()

        # Learning failures are not failures of the research being reviewed.
        events = [event for event in events if event.stage != "learning"]
        state = _json(checkpoint.state_json) if checkpoint else {}
        workflow = state.get("workflow_state", {})
        report_metrics = (workflow.get("report_result") or {}).get("report_metrics", {})
        plan = (workflow.get("state") or {}).get("plan") or {}
        nodes = (plan.get("task_graph") or {}).get("nodes", [])
        incomplete = [node.get("task_id") for node in nodes if node.get("status") != "completed"]
        degraded = bool(report_metrics.get("reserved_budget_fallback"))

        evidence_by_call: dict[str, list[EvidenceModel]] = {}
        for item in evidence:
            if item.tool_call_id:
                evidence_by_call.setdefault(item.tool_call_id, []).append(item)
        cards = []
        for call in calls:
            matched = evidence_by_call.get(call.tool_call_id, [])
            cards.append(DecisionCard(
                state_before={"task_id": call.task_id, "source_mode": call.source_mode},
                action={"tool": call.tool_name, "args": _json(call.args_json)},
                observation={
                    "status": call.status, "error_code": call.error_code,
                    "evidence": [{"evidence_id": e.evidence_id, "title": e.title, "source_id": e.source_id, "summary": e.summary[:600]} for e in matched],
                },
                outcome="success" if call.status in {"completed", "success"} else "failure",
                cost={}, trace_refs=[f"tool_call:{call.tool_call_id}"] + [f"evidence:{e.evidence_id}" for e in matched],
            ))
        replans = [event for event in events if event.event_type in {"plan.replanning", "run.replanning"}]
        failures = [event for event in events if "failed" in event.event_type or "error" in event.event_type]
        episode_type = "recovery" if replans and run.status == "completed" else ("success" if run.status == "completed" else "failure")
        if run.status == "completed" and (degraded or incomplete):
            episode_type = "inefficiency"
        refs = [ref for card in cards for ref in card.trace_refs]
        episodes = [TrajectoryEpisode(
            episode_type=episode_type,
            summary=f"{run.workflow_mode}/{run.source_mode} 运行以 {run.status} 结束；工具调用 {len(calls)} 次，证据 {len(evidence)} 条。",
            cards=cards[:40],
            reusable_scope={"workflow_mode": run.workflow_mode, "source_mode": run.source_mode},
            trace_refs=refs[:160],
        )]
        if failures:
            episodes.append(TrajectoryEpisode(
                episode_type="failure", summary="运行中出现失败事件。",
                cards=[], reusable_scope={"event_types": sorted({e.event_type for e in failures})},
                trace_refs=[f"event:{e.event_id}" for e in failures[:30]],
            ))
        contract = {
            check.kind: {"required": bool(check.required), "passed": None if check.passed is None else bool(check.passed), "evidence": _json(check.evidence_json), "ref": f"contract:{check.check_id}"}
            for check in checks
        }
        model_snapshot = _json(run.model_snapshot_json)
        selected = model_snapshot.get("skill")
        loaded = [selected] if isinstance(selected, dict) and selected.get("name") else []
        return ReviewPack(
            review_id=review_id, run_id=run_id,
            user_goal=(message.content if message else "")[:4000],
            workflow_mode=run.workflow_mode, source_mode=run.source_mode,
            terminal_status=run.status, completion_contract=contract,
            context_and_budget_metrics={"budget": _json(run.budget_json), "usage": _json(run.usage_json), "model_snapshot": model_snapshot,
                "report_metrics": report_metrics, "incomplete_task_ids": incomplete,
                "delivery_assessment": "degraded_or_incomplete" if degraded or incomplete else "not_independently_verified"},
            loaded_skills=loaded, episodes=episodes,
            user_corrections=[], candidate_neighbors=[], protected_skills=[],
            artifact_refs=[f"run:{run_id}"] + [f"task:{item.task_id}" for item in tasks] + [f"event:{item.event_id}" for item in events[-20:]],
        )

"""Run control, durable event streaming, evidence and report reads."""

import json

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sse_starlette.sse import EventSourceResponse

from backend.app.dependencies import get_database, get_event_stream, get_run_service
from backend.app.schemas import ClarificationSubmit, RunControl
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository, ContractRepository, EvidenceRepository, MessageRepository, RunRepository,
)

router = APIRouter(prefix="/runs", tags=["runs"])


def run_dict(run):
    return {
        "run_id": run.run_id, "session_id": run.session_id, "trigger_message_id": run.trigger_message_id,
        "source_mode": run.source_mode, "workflow_mode": run.workflow_mode, "status": run.status,
        "current_stage": run.current_stage, "usage": json.loads(run.usage_json or "{}"),
        "error_code": run.error_code, "error_message": run.error_message,
        "cancellation_requested": bool(run.cancellation_requested), "created_at": run.created_at,
        "started_at": run.started_at, "completed_at": run.completed_at, "updated_at": run.updated_at,
    }


async def require_run(database, run_id: str):
    run = await RunRepository(database).get(run_id)
    if run is None:
        raise AppError(ErrorCode.NOT_FOUND, "Run 不存在")
    return run


@router.get("/{run_id}")
async def get_run(run_id: str, database=Depends(get_database)):
    return run_dict(await require_run(database, run_id))


@router.post("/{run_id}/cancel", response_model=RunControl)
async def cancel_run(run_id: str, database=Depends(get_database), run_service=Depends(get_run_service)):
    await require_run(database, run_id)
    if not await run_service.cancel(run_id):
        raise AppError(ErrorCode.CONFLICT, "Run 已结束或当前不能取消")
    return RunControl(run_id=run_id, status="cancelling")


@router.post("/{run_id}/resume", response_model=RunControl)
async def resume_run(run_id: str, database=Depends(get_database), run_service=Depends(get_run_service)):
    await require_run(database, run_id)
    if not await run_service.resume(run_id):
        raise AppError(ErrorCode.CONFLICT, "Run 当前不能恢复")
    return RunControl(run_id=run_id, status="queued")


@router.post("/{run_id}/clarifications", response_model=RunControl)
async def clarify_run(run_id: str, payload: ClarificationSubmit, database=Depends(get_database), run_service=Depends(get_run_service)):
    await require_run(database, run_id)
    if not await run_service.resume(run_id, clarification=payload.content):
        raise AppError(ErrorCode.CONFLICT, "Run 当前不等待澄清")
    return RunControl(run_id=run_id, status="queued")


@router.get("/{run_id}/events")
async def run_events(
    run_id: str, request: Request, after_event_id: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    database=Depends(get_database), event_stream=Depends(get_event_stream),
):
    await require_run(database, run_id)
    cursor = after_event_id
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))
    return EventSourceResponse(event_stream.stream(run_id, after_event_id=cursor), ping=None, headers={"Cache-Control": "no-cache"})


@router.get("/{run_id}/evidence")
async def get_evidence(run_id: str, database=Depends(get_database)):
    await require_run(database, run_id)
    items = await EvidenceRepository(database).list_for_run(run_id)
    return {"items": [
        {"evidence_id": item.evidence_id, "run_id": item.run_id, "task_id": item.task_id, "tool_call_id": item.tool_call_id,
         "source_mode": item.source_mode, "provider": item.provider, "source_id": item.source_id, "title": item.title,
         "summary": item.summary, "metadata": json.loads(item.metadata_json or "{}"), "content_hash": item.content_hash,
         "artifact_id": item.artifact_id, "score": item.score, "created_at": item.created_at}
        for item in items
    ], "total": len(items)}


@router.get("/{run_id}/report")
async def get_report(run_id: str, database=Depends(get_database)):
    run = await require_run(database, run_id)
    message = await MessageRepository(database).get_assistant_for_run(run_id)
    artifact = await ArtifactRepository(database).get_report(run_id)
    checks = await ContractRepository(database).list_for_run(run_id)
    return {
        "run_id": run_id, "status": run.status, "content": message.content if message else None,
        "artifact": None if artifact is None else {"artifact_id": artifact.artifact_id, "relative_path": artifact.relative_path, "sha256": artifact.sha256, "size_bytes": artifact.size_bytes},
        "verification": [
            {"check_id": check.check_id, "kind": check.kind, "required": bool(check.required), "passed": None if check.passed is None else bool(check.passed), "evidence": json.loads(check.evidence_json or "{}")}
            for check in checks
        ],
    }

"""Persistent Session and message commands."""

from fastapi import APIRouter, Depends, Query, Response, status

from backend.app.dependencies import get_chat_service, get_database
from backend.app.schemas import MessageSend, SessionCreate, SessionPatch
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence.repositories import MessageRepository, RunRepository, SessionRepository
from deepresearch_agent.sessions import SessionSearchService

router = APIRouter(prefix="/sessions", tags=["sessions"])


def session_dict(item):
    return {name: getattr(item, name) for name in (
        "session_id", "title", "status", "summary_json", "memory_snapshot_version",
        "memory_snapshot_created_at", "created_at", "updated_at", "archived_at",
    )}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(payload: SessionCreate, database=Depends(get_database)):
    return session_dict(await SessionRepository(database).create(payload))


@router.get("")
async def list_sessions(
    limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
    include_archived: bool = False, database=Depends(get_database),
):
    repository = SessionRepository(database)
    items = await repository.list(limit=limit, offset=offset, include_archived=include_archived)
    total = await repository.count(include_archived=include_archived)
    return {"items": [session_dict(item) for item in items], "total": total, "offset": offset, "limit": limit, "next_offset": offset + len(items) if offset + len(items) < total else None}


@router.get("/search")
async def search_sessions(
    q: str | None = None, session_id: str | None = None, around_message_id: str | None = None,
    limit: int = Query(3, ge=1, le=20), window: int = Query(5, ge=1, le=20),
    detail: str = Query("adaptive", pattern="^(adaptive|full)$"), database=Depends(get_database),
):
    service = SessionSearchService(MessageRepository(database), SessionRepository(database))
    if session_id and around_message_id:
        return await service.around(session_id, around_message_id, window=window)
    if session_id:
        return await service.read(session_id, limit=min(500, limit * 50))
    if q:
        return {"items": [item.to_dict() for item in await service.search(q, limit=limit, window=window, detail=detail)]}
    return {"items": await service.browse(limit=limit)}


@router.get("/{session_id}")
async def get_session(
    session_id: str, message_limit: int = Query(100, ge=1, le=500), message_offset: int = Query(0, ge=0),
    database=Depends(get_database),
):
    item = await SessionRepository(database).get(session_id)
    if item is None:
        raise AppError(ErrorCode.NOT_FOUND, "Session 不存在")
    messages = await MessageRepository(database).list_for_session(session_id, limit=message_limit, offset=message_offset)
    runs = await RunRepository(database).list_for_session(session_id)
    result = session_dict(item)
    result["messages"] = [
        {"message_id": msg.message_id, "session_id": msg.session_id, "run_id": msg.run_id, "role": msg.role, "content": msg.content, "metadata_json": msg.metadata_json, "created_at": msg.created_at}
        for msg in messages
    ]
    result["runs"] = [
        {"run_id": run.run_id, "source_mode": run.source_mode, "workflow_mode": run.workflow_mode, "status": run.status, "current_stage": run.current_stage, "created_at": run.created_at, "updated_at": run.updated_at}
        for run in runs
    ]
    return result


@router.patch("/{session_id}")
async def patch_session(session_id: str, payload: SessionPatch, database=Depends(get_database)):
    repository = SessionRepository(database)
    if await repository.get(session_id) is None:
        raise AppError(ErrorCode.NOT_FOUND, "Session 不存在")
    if payload.title is not None:
        await repository.rename(session_id, payload.title)
    if payload.status == "archived":
        await repository.archive(session_id)
    elif payload.status == "active":
        await repository.restore(session_id)
    return session_dict(await repository.get(session_id))


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: str, database=Depends(get_database)):
    if not await SessionRepository(database).soft_delete(session_id):
        raise AppError(ErrorCode.NOT_FOUND, "Session 不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{session_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def send_message(session_id: str, payload: MessageSend, chat_service=Depends(get_chat_service)):
    return await chat_service.send(session_id, payload)


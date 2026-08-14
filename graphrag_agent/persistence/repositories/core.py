"""Session, message, run and event repositories."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from backend.app.schemas import MessageCreate, RunCreate, RunEventCreate, SessionCreate
from graphrag_agent.harness.errors import AppError, ErrorCode
from graphrag_agent.harness.versioning import EVENT_SCHEMA_VERSION
from graphrag_agent.persistence.database import Database
from graphrag_agent.persistence.models import MessageModel, RunEventModel, RunModel, SessionModel

from .utils import json_text, new_id, utc_now_iso


class SessionRepository:
    def __init__(self, database: Database):
        self.database = database

    async def create(self, data: SessionCreate) -> SessionModel:
        now = utc_now_iso()
        model = SessionModel(session_id=new_id("ses"), title=data.title, status="active", created_at=now, updated_at=now)
        async with self.database.transaction() as session:
            session.add(model)
        return model

    async def get(self, session_id: str, *, include_deleted: bool = False) -> Optional[SessionModel]:
        async with self.database.sessions() as session:
            query = select(SessionModel).where(SessionModel.session_id == session_id)
            if not include_deleted:
                query = query.where(SessionModel.deleted_at.is_(None))
            return (await session.execute(query)).scalar_one_or_none()

    async def list(self, *, limit: int = 30, offset: int = 0, include_archived: bool = False) -> list[SessionModel]:
        limit = max(1, min(limit, 100))
        async with self.database.sessions() as session:
            query = select(SessionModel).where(SessionModel.deleted_at.is_(None))
            if not include_archived:
                query = query.where(SessionModel.status == "active")
            result = await session.execute(query.order_by(SessionModel.updated_at.desc()).limit(limit).offset(max(0, offset)))
            return list(result.scalars())

    async def rename(self, session_id: str, title: str) -> bool:
        if not title.strip():
            raise ValueError("Session 标题不能为空")
        async with self.database.transaction() as session:
            result = await session.execute(update(SessionModel).where(SessionModel.session_id == session_id, SessionModel.deleted_at.is_(None)).values(title=title.strip()[:200], updated_at=utc_now_iso()))
            return result.rowcount == 1

    async def archive(self, session_id: str) -> bool:
        now = utc_now_iso()
        async with self.database.transaction() as session:
            result = await session.execute(update(SessionModel).where(SessionModel.session_id == session_id, SessionModel.deleted_at.is_(None)).values(status="archived", archived_at=now, updated_at=now))
            return result.rowcount == 1

    async def soft_delete(self, session_id: str) -> bool:
        now = utc_now_iso()
        async with self.database.transaction() as session:
            result = await session.execute(update(SessionModel).where(SessionModel.session_id == session_id, SessionModel.deleted_at.is_(None)).values(status="deleted", deleted_at=now, updated_at=now))
            return result.rowcount == 1

    async def search(self, query: str, *, limit: int = 20) -> list[SessionModel]:
        async with self.database.sessions() as session:
            ids = (await session.execute(text("SELECT session_id FROM sessions_fts WHERE sessions_fts MATCH :query LIMIT :limit"), {"query": query, "limit": limit})).scalars().all()
            if not ids:
                return []
            return list((await session.execute(select(SessionModel).where(SessionModel.session_id.in_(ids), SessionModel.deleted_at.is_(None)))).scalars())


class MessageRepository:
    def __init__(self, database: Database):
        self.database = database

    async def append(self, data: MessageCreate) -> tuple[MessageModel, bool]:
        async with self.database.transaction() as session:
            if data.client_message_id:
                existing = (await session.execute(select(MessageModel).where(MessageModel.session_id == data.session_id, MessageModel.client_message_id == data.client_message_id))).scalar_one_or_none()
                if existing:
                    return existing, False
            model = MessageModel(message_id=new_id("msg"), session_id=data.session_id, run_id=data.run_id, client_message_id=data.client_message_id, role=data.role, content=data.content, metadata_json=json_text(data.metadata), created_at=utc_now_iso())
            session.add(model)
            return model, True

    async def list_for_session(self, session_id: str, *, limit: int = 100, offset: int = 0) -> list[MessageModel]:
        async with self.database.sessions() as session:
            result = await session.execute(select(MessageModel).where(MessageModel.session_id == session_id).order_by(MessageModel.created_at).limit(max(1, min(limit, 500))).offset(max(0, offset)))
            return list(result.scalars())

    async def search(self, query: str, *, session_id: Optional[str] = None, limit: int = 20) -> list[MessageModel]:
        clause = "messages_fts MATCH :query"
        params: dict[str, Any] = {"query": query, "limit": limit}
        if session_id:
            clause += " AND session_id = :session_id"
            params["session_id"] = session_id
        async with self.database.sessions() as session:
            ids = (await session.execute(text(f"SELECT message_id FROM messages_fts WHERE {clause} LIMIT :limit"), params)).scalars().all()
            if not ids:
                return []
            return list((await session.execute(select(MessageModel).where(MessageModel.message_id.in_(ids)))).scalars())


class RunRepository:
    ACTIVE_STATUSES = ("queued", "context_building", "planning", "executing", "reporting", "verifying", "retrying", "replanning", "cancelling")

    def __init__(self, database: Database):
        self.database = database

    async def create(self, data: RunCreate) -> RunModel:
        now = utc_now_iso()
        model = RunModel(run_id=new_id("run"), session_id=data.session_id, trigger_message_id=data.trigger_message_id, source_mode=data.source_mode.value, workflow_mode=data.workflow_mode.value, status="queued", config_snapshot_json=json_text(data.config_snapshot), model_snapshot_json="{}", budget_json=json_text(data.budget), usage_json="{}", created_at=now, updated_at=now)
        try:
            async with self.database.transaction() as session:
                session.add(model)
        except IntegrityError as exc:
            raise AppError(ErrorCode.CONFLICT, "该消息已经关联 Run") from exc
        return model

    async def create_for_user_message(self, message: MessageCreate, run: RunCreate) -> tuple[MessageModel, RunModel, bool]:
        """Atomically apply idempotency, user message, queued Run and queued event."""
        if message.role != "user" or message.session_id != run.session_id:
            raise ValueError("原子创建仅接受同一 Session 的 user message")
        async with self.database.transaction() as session:
            if message.client_message_id:
                existing_message = (await session.execute(select(MessageModel).where(MessageModel.session_id == message.session_id, MessageModel.client_message_id == message.client_message_id))).scalar_one_or_none()
                if existing_message:
                    existing_run = (await session.execute(select(RunModel).where(RunModel.trigger_message_id == existing_message.message_id))).scalar_one()
                    if existing_message.content != message.content or existing_run.source_mode != run.source_mode.value or existing_run.workflow_mode != run.workflow_mode.value:
                        raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, "client_message_id 已被不同的消息或运行参数使用")
                    return existing_message, existing_run, False
            now = utc_now_iso()
            message_model = MessageModel(message_id=new_id("msg"), session_id=message.session_id, client_message_id=message.client_message_id, role="user", content=message.content, metadata_json=json_text(message.metadata), created_at=now)
            run_model = RunModel(run_id=new_id("run"), session_id=run.session_id, trigger_message_id=message_model.message_id, source_mode=run.source_mode.value, workflow_mode=run.workflow_mode.value, status="queued", config_snapshot_json=json_text(run.config_snapshot), model_snapshot_json="{}", budget_json=json_text(run.budget), usage_json="{}", created_at=now, updated_at=now)
            event_model = RunEventModel(run_id=run_model.run_id, event_type="run.queued", stage="queued", payload_json=json_text({"run_id": run_model.run_id, "schema_version": EVENT_SCHEMA_VERSION}), schema_version=EVENT_SCHEMA_VERSION, created_at=now)
            session.add(message_model)
            await session.flush()
            session.add(run_model)
            await session.flush()
            session.add(event_model)
            await session.flush()
            return message_model, run_model, True

    async def get(self, run_id: str) -> Optional[RunModel]:
        async with self.database.sessions() as session:
            return (await session.execute(select(RunModel).where(RunModel.run_id == run_id))).scalar_one_or_none()

    async def list_for_session(self, session_id: str, *, limit: int = 50, offset: int = 0) -> list[RunModel]:
        async with self.database.sessions() as session:
            result = await session.execute(select(RunModel).where(RunModel.session_id == session_id).order_by(RunModel.created_at.desc()).limit(max(1, min(limit, 200))).offset(max(0, offset)))
            return list(result.scalars())

    async def update_status(self, run_id: str, *, status: str, current_stage: Optional[str] = None, error_code: Optional[str] = None, error_message: Optional[str] = None) -> bool:
        """Persistence-only update; stage 3 StateMachine will own transition legality."""
        values: dict[str, Any] = {"status": status, "updated_at": utc_now_iso(), "error_code": error_code, "error_message": error_message}
        if current_stage is not None:
            values["current_stage"] = current_stage
        async with self.database.transaction() as session:
            result = await session.execute(update(RunModel).where(RunModel.run_id == run_id).values(**values))
            return result.rowcount == 1

    async def request_cancel(self, run_id: str) -> bool:
        async with self.database.transaction() as session:
            result = await session.execute(update(RunModel).where(RunModel.run_id == run_id, RunModel.status.in_(self.ACTIVE_STATUSES)).values(cancellation_requested=1, updated_at=utc_now_iso()))
            return result.rowcount == 1

    async def acquire_lease(self, run_id: str, owner: str, *, ttl_seconds: int = 60) -> bool:
        now = datetime.now(timezone.utc)
        now_text = now.isoformat().replace("+00:00", "Z")
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
        async with self.database.transaction() as session:
            result = await session.execute(update(RunModel).where(RunModel.run_id == run_id, or_(RunModel.lease_owner.is_(None), RunModel.lease_expires_at < now_text, RunModel.lease_owner == owner)).values(lease_owner=owner, lease_expires_at=expires, updated_at=now_text))
            return result.rowcount == 1

    async def release_lease(self, run_id: str, owner: str) -> bool:
        async with self.database.transaction() as session:
            result = await session.execute(update(RunModel).where(RunModel.run_id == run_id, RunModel.lease_owner == owner).values(lease_owner=None, lease_expires_at=None, updated_at=utc_now_iso()))
            return result.rowcount == 1


class EventRepository:
    def __init__(self, database: Database):
        self.database = database

    async def append(self, data: RunEventCreate) -> RunEventModel:
        model = RunEventModel(run_id=data.run_id, event_type=data.event_type, stage=data.stage, payload_json=json_text({**data.payload, "schema_version": EVENT_SCHEMA_VERSION}), schema_version=EVENT_SCHEMA_VERSION, created_at=utc_now_iso())
        async with self.database.transaction() as session:
            session.add(model)
            await session.flush()
        return model

    async def list_after(self, run_id: str, *, after_event_id: int = 0, limit: int = 500) -> list[RunEventModel]:
        async with self.database.sessions() as session:
            result = await session.execute(select(RunEventModel).where(RunEventModel.run_id == run_id, RunEventModel.event_id > after_event_id).order_by(RunEventModel.event_id).limit(max(1, min(limit, 1000))))
            return list(result.scalars())

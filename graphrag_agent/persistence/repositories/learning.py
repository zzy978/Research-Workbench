"""Stage-1 persistence for later Memory and Skill business services."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select, text

from graphrag_agent.persistence.database import Database
from graphrag_agent.persistence.models import AuditEventModel, EvalRunModel, MemoryModel, SkillCandidateModel, SkillVersionModel

from .utils import json_text, new_id, utc_now_iso


class MemoryRepository:
    def __init__(self, database: Database):
        self.database = database

    async def create_candidate(self, *, content: str, scope: str, kind: str, provenance_refs: list[str], confidence: float, session_id: Optional[str] = None, created_by: str = "agent", supersedes: Optional[str] = None) -> MemoryModel:
        now = utc_now_iso()
        model = MemoryModel(memory_id=new_id("mem"), session_id=session_id, scope=scope, kind=kind, content=content, provenance_json=json_text(provenance_refs), confidence=confidence, valid_from=now, supersedes=supersedes, status="candidate", created_by=created_by, created_at=now, updated_at=now)
        async with self.database.transaction() as session:
            session.add(model)
        return model

    async def search(self, query: str, *, status: Optional[str] = None, limit: int = 20) -> list[MemoryModel]:
        async with self.database.sessions() as session:
            ids = (await session.execute(text("SELECT memory_id FROM memories_fts WHERE memories_fts MATCH :query LIMIT :limit"), {"query": query, "limit": limit})).scalars().all()
            if not ids:
                return []
            statement = select(MemoryModel).where(MemoryModel.memory_id.in_(ids), MemoryModel.deleted_at.is_(None))
            if status:
                statement = statement.where(MemoryModel.status == status)
            return list((await session.execute(statement)).scalars())


class SkillRepository:
    def __init__(self, database: Database):
        self.database = database

    async def add_version(self, *, name: str, version: str, content_path: str, content_hash: str, source_run_ids: list[str], status: str = "candidate") -> SkillVersionModel:
        model = SkillVersionModel(skill_version_id=new_id("skv"), name=name, version=version, status=status, content_path=content_path, content_hash=content_hash, source_run_ids_json=json_text(source_run_ids), created_at=utc_now_iso())
        async with self.database.transaction() as session:
            session.add(model)
        return model

    async def add_candidate(self, *, run_id: str, name: str, proposed_version: str, payload: dict[str, Any]) -> SkillCandidateModel:
        model = SkillCandidateModel(candidate_id=new_id("skc"), run_id=run_id, name=name, proposed_version=proposed_version, status="candidate", payload_json=json_text(payload), created_at=utc_now_iso())
        async with self.database.transaction() as session:
            session.add(model)
        return model

    async def add_eval(self, *, candidate_id: str, status: str, metrics: dict[str, Any]) -> EvalRunModel:
        model = EvalRunModel(eval_run_id=new_id("eval"), candidate_id=candidate_id, status=status, metrics_json=json_text(metrics), created_at=utc_now_iso())
        async with self.database.transaction() as session:
            session.add(model)
        return model


class AuditRepository:
    def __init__(self, database: Database):
        self.database = database

    async def append(self, *, action: str, entity_type: str, entity_id: str, payload: Optional[dict[str, Any]] = None) -> AuditEventModel:
        model = AuditEventModel(action=action, entity_type=entity_type, entity_id=entity_id, payload_json=json_text(payload or {}), created_at=utc_now_iso())
        async with self.database.transaction() as session:
            session.add(model)
            await session.flush()
        return model

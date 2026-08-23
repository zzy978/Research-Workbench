"""Stage-1 persistence for later Memory and Skill business services."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func, select, text, update

from deepresearch_agent.persistence.database import Database
from deepresearch_agent.persistence.models import AuditEventModel, EvalRunModel, MemoryModel, SkillCandidateModel, SkillVersionModel

from .utils import json_text, new_id, utc_now_iso


class MemoryRepository:
    def __init__(self, database: Database):
        self.database = database

    async def create_candidate(self, *, content: str, scope: str, kind: str, provenance_refs: list[str], confidence: float, session_id: Optional[str] = None, created_by: str = "agent", supersedes: Optional[str] = None, expires_at: Optional[str] = None) -> MemoryModel:
        now = utc_now_iso()
        target = scope if scope in {"user", "project"} else None
        model = MemoryModel(memory_id=new_id("mem"), session_id=session_id, target=target, scope=scope, kind=kind, content=content, provenance_json=json_text(provenance_refs), confidence=confidence, valid_from=now, expires_at=expires_at, supersedes=supersedes, status="candidate", created_by=created_by, created_at=now, updated_at=now)
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

    async def get(self, memory_id: str) -> Optional[MemoryModel]:
        async with self.database.sessions() as session:
            return (await session.execute(select(MemoryModel).where(MemoryModel.memory_id == memory_id, MemoryModel.deleted_at.is_(None)))).scalar_one_or_none()

    async def list_available(self, *, session_id: str, now: str, statuses: tuple[str, ...] = ("active",), limit: int = 200) -> list[MemoryModel]:
        async with self.database.sessions() as session:
            statement = select(MemoryModel).where(
                MemoryModel.deleted_at.is_(None), MemoryModel.status.in_(statuses),
                (MemoryModel.expires_at.is_(None) | (MemoryModel.expires_at > now)),
                ((MemoryModel.scope.in_(("project", "domain", "user"))) | (MemoryModel.session_id == session_id)),
            ).order_by(MemoryModel.confidence.desc(), MemoryModel.updated_at.desc()).limit(limit)
            return list((await session.execute(statement)).scalars())

    async def list_curated(self, *, statuses: tuple[str, ...] = ("active",), target: str | None = None, now: str | None = None, limit: int = 200) -> list[MemoryModel]:
        """Return only the bounded user/project store used in prompt snapshots."""
        async with self.database.sessions() as session:
            effective_target = func.coalesce(MemoryModel.target, MemoryModel.scope)
            statement = select(MemoryModel).where(
                MemoryModel.deleted_at.is_(None),
                MemoryModel.status.in_(statuses),
                effective_target.in_(("user", "project")),
            )
            if target is not None:
                statement = statement.where(effective_target == target)
            if now is not None:
                statement = statement.where(MemoryModel.expires_at.is_(None) | (MemoryModel.expires_at > now))
            statement = statement.order_by(effective_target, MemoryModel.updated_at, MemoryModel.memory_id).limit(limit)
            return list((await session.execute(statement)).scalars())

    async def snapshot_version(self) -> int:
        """Monotonic-enough durable version derived from active-store updates."""
        async with self.database.sessions() as session:
            effective_target = func.coalesce(MemoryModel.target, MemoryModel.scope)
            count, latest = (await session.execute(
                select(func.count(MemoryModel.memory_id), func.max(MemoryModel.updated_at)).where(
                    MemoryModel.deleted_at.is_(None), MemoryModel.status == "active",
                    effective_target.in_(("user", "project")),
                )
            )).one()
        if not latest:
            return 0
        digits = "".join(character for character in latest if character.isdigit())[:14]
        return int(digits or 0) * 1000 + int(count or 0)

    async def list_active_for_conflict(self, *, scope: str, kind: str, session_id: Optional[str]) -> list[MemoryModel]:
        async with self.database.sessions() as session:
            statement = select(MemoryModel).where(
                MemoryModel.deleted_at.is_(None), MemoryModel.status == "active",
                MemoryModel.scope == scope, MemoryModel.kind == kind,
            )
            if scope not in {"project", "domain", "user"}:
                statement = statement.where(MemoryModel.session_id == session_id)
            return list((await session.execute(statement)).scalars())

    async def update_lifecycle(self, memory_id: str, **values: Any) -> bool:
        values["updated_at"] = utc_now_iso()
        async with self.database.transaction() as session:
            result = await session.execute(update(MemoryModel).where(MemoryModel.memory_id == memory_id, MemoryModel.deleted_at.is_(None)).values(**values))
            return result.rowcount == 1

    async def soft_delete(self, memory_id: str) -> bool:
        now = utc_now_iso()
        return await self.update_lifecycle(memory_id, deleted_at=now, archived_at=now, status="archived")

    async def mark_provenance_for_review(self, provenance_ref: str) -> list[str]:
        """Return active factual memories to candidate state when a source becomes invalid."""
        async with self.database.transaction() as session:
            rows = list((await session.execute(select(MemoryModel).where(
                MemoryModel.deleted_at.is_(None), MemoryModel.status == "active",
                MemoryModel.kind == "fact", MemoryModel.provenance_json.contains(provenance_ref),
            ))).scalars())
            now = utc_now_iso()
            for row in rows:
                row.status = "candidate"
                row.updated_at = now
            return [row.memory_id for row in rows]


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
        now = utc_now_iso()
        model = EvalRunModel(eval_run_id=new_id("eval"), candidate_id=candidate_id, status=status, metrics_json=json_text(metrics), created_at=now, completed_at=now if status in {"passed", "failed"} else None)
        async with self.database.transaction() as session:
            session.add(model)
        return model

    async def get_candidate(self, candidate_id: str) -> Optional[SkillCandidateModel]:
        async with self.database.sessions() as session:
            return await session.get(SkillCandidateModel, candidate_id)

    async def find_candidate(self, name: str, version: str) -> Optional[SkillCandidateModel]:
        async with self.database.sessions() as session:
            return (await session.execute(select(SkillCandidateModel).where(SkillCandidateModel.name == name, SkillCandidateModel.proposed_version == version).order_by(SkillCandidateModel.created_at.desc()))).scalars().first()

    async def get_version(self, name: str, version: str) -> Optional[SkillVersionModel]:
        async with self.database.sessions() as session:
            return (await session.execute(select(SkillVersionModel).where(SkillVersionModel.name == name, SkillVersionModel.version == version))).scalar_one_or_none()

    async def list_versions(self, *, name: Optional[str] = None, status: Optional[str] = None) -> list[SkillVersionModel]:
        async with self.database.sessions() as session:
            statement = select(SkillVersionModel)
            if name:
                statement = statement.where(SkillVersionModel.name == name)
            if status:
                statement = statement.where(SkillVersionModel.status == status)
            return list((await session.execute(statement.order_by(SkillVersionModel.created_at.desc()))).scalars())

    async def latest_eval(self, candidate_id: str) -> Optional[EvalRunModel]:
        async with self.database.sessions() as session:
            return (await session.execute(select(EvalRunModel).where(EvalRunModel.candidate_id == candidate_id).order_by(EvalRunModel.created_at.desc()))).scalars().first()

    async def update_candidate_status(self, candidate_id: str, status: str) -> bool:
        async with self.database.transaction() as session:
            result = await session.execute(update(SkillCandidateModel).where(SkillCandidateModel.candidate_id == candidate_id).values(status=status))
            return result.rowcount == 1

    async def promote(self, *, candidate_id: str, name: str, version: str, content_hash: str) -> bool:
        async with self.database.transaction() as session:
            candidate = await session.get(SkillCandidateModel, candidate_id)
            target = (await session.execute(select(SkillVersionModel).where(SkillVersionModel.name == name, SkillVersionModel.version == version))).scalar_one_or_none()
            if candidate is None or target is None or candidate.status not in {"candidate", "evaluated"}:
                return False
            await session.execute(update(SkillVersionModel).where(SkillVersionModel.name == name, SkillVersionModel.status == "active").values(status="previous"))
            target.status = "active"
            target.content_hash = content_hash
            candidate.status = "promoted"
            return True

    async def rollback(self, name: str, *, active_hash: str, previous_version: str, previous_hash: str) -> bool:
        async with self.database.transaction() as session:
            active = (await session.execute(select(SkillVersionModel).where(SkillVersionModel.name == name, SkillVersionModel.status == "active"))).scalar_one_or_none()
            previous = (await session.execute(select(SkillVersionModel).where(SkillVersionModel.name == name, SkillVersionModel.version == previous_version, SkillVersionModel.status == "previous"))).scalar_one_or_none()
            if active is None or previous is None:
                return False
            active.status = "candidate"
            active.content_hash = active_hash
            previous.status = "active"
            previous.content_hash = previous_hash
            return True

    async def update_version_hash(self, name: str, version: str, content_hash: str) -> bool:
        async with self.database.transaction() as session:
            result = await session.execute(update(SkillVersionModel).where(SkillVersionModel.name == name, SkillVersionModel.version == version).values(content_hash=content_hash))
            return result.rowcount == 1


class AuditRepository:
    def __init__(self, database: Database):
        self.database = database

    async def append(self, *, action: str, entity_type: str, entity_id: str, payload: Optional[dict[str, Any]] = None) -> AuditEventModel:
        model = AuditEventModel(action=action, entity_type=entity_type, entity_id=entity_id, payload_json=json_text(payload or {}), created_at=utc_now_iso())
        async with self.database.transaction() as session:
            session.add(model)
            await session.flush()
        return model

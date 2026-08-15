"""Phase-4 schemas and durable reads for later Memory/Skill business stages."""

import json

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select, update

from backend.app.dependencies import get_database
from backend.app.schemas import MemoryLifecycleUpdate
from graphrag_agent.harness.errors import AppError, ErrorCode
from graphrag_agent.persistence.models import EvalRunModel, MemoryModel, SkillCandidateModel, SkillVersionModel
from graphrag_agent.persistence.repositories.utils import utc_now_iso

router = APIRouter(tags=["learning"])


@router.get("/memories")
async def list_memories(q: str | None = None, status_filter: str | None = None, database=Depends(get_database)):
    async with database.sessions() as session:
        statement = select(MemoryModel).where(MemoryModel.deleted_at.is_(None))
        if q:
            statement = statement.where(MemoryModel.content.contains(q))
        if status_filter:
            statement = statement.where(MemoryModel.status == status_filter)
        items = list((await session.execute(statement.order_by(MemoryModel.updated_at.desc()).limit(100))).scalars())
    return {"items": [
        {"memory_id": item.memory_id, "session_id": item.session_id, "scope": item.scope, "kind": item.kind,
         "content": item.content, "provenance_refs": json.loads(item.provenance_json or "[]"), "confidence": item.confidence,
         "valid_from": item.valid_from, "expires_at": item.expires_at, "supersedes": item.supersedes,
         "status": item.status, "created_by": item.created_by, "created_at": item.created_at, "updated_at": item.updated_at}
        for item in items
    ], "total": len(items)}


@router.patch("/memories/{memory_id}")
async def patch_memory(memory_id: str, payload: MemoryLifecycleUpdate, database=Depends(get_database)):
    values = {"updated_at": utc_now_iso()}
    if payload.content is not None:
        values["content"] = payload.content
    if payload.status is not None:
        values["status"] = payload.status
    async with database.transaction() as session:
        result = await session.execute(update(MemoryModel).where(MemoryModel.memory_id == memory_id, MemoryModel.deleted_at.is_(None)).values(**values))
    if result.rowcount != 1:
        raise AppError(ErrorCode.NOT_FOUND, "Memory 不存在")
    return {"memory_id": memory_id, **values}


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: str, database=Depends(get_database)):
    now = utc_now_iso()
    async with database.transaction() as session:
        result = await session.execute(update(MemoryModel).where(MemoryModel.memory_id == memory_id, MemoryModel.deleted_at.is_(None)).values(deleted_at=now, updated_at=now))
    if result.rowcount != 1:
        raise AppError(ErrorCode.NOT_FOUND, "Memory 不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/skills")
async def list_skills(database=Depends(get_database)):
    async with database.sessions() as session:
        versions = list((await session.execute(select(SkillVersionModel).order_by(SkillVersionModel.name, SkillVersionModel.created_at.desc()))).scalars())
        candidates = list((await session.execute(select(SkillCandidateModel).order_by(SkillCandidateModel.created_at.desc()))).scalars())
    return {
        "versions": [{"skill_version_id": item.skill_version_id, "name": item.name, "version": item.version, "status": item.status, "source_run_ids": json.loads(item.source_run_ids_json or "[]"), "created_at": item.created_at} for item in versions],
        "candidates": [{"candidate_id": item.candidate_id, "run_id": item.run_id, "name": item.name, "proposed_version": item.proposed_version, "status": item.status, "payload": json.loads(item.payload_json or "{}"), "created_at": item.created_at} for item in candidates],
    }


@router.get("/skills/{name}/versions/{version}")
async def get_skill(name: str, version: str, database=Depends(get_database)):
    async with database.sessions() as session:
        item = (await session.execute(select(SkillVersionModel).where(SkillVersionModel.name == name, SkillVersionModel.version == version))).scalar_one_or_none()
    if item is None:
        raise AppError(ErrorCode.NOT_FOUND, "Skill 版本不存在")
    return {"skill_version_id": item.skill_version_id, "name": item.name, "version": item.version, "status": item.status, "content_path": item.content_path, "content_hash": item.content_hash, "source_run_ids": json.loads(item.source_run_ids_json or "[]"), "created_at": item.created_at}


def phase_seven_required():
    raise AppError(ErrorCode.CONFLICT, "Skill 评测、启用和回滚将在执行计划阶段 7 接入，阶段 4/5 不允许绕过门禁")


@router.post("/skills/{name}/versions/{version}/evaluate")
async def evaluate_skill(name: str, version: str):
    phase_seven_required()


@router.post("/skills/{name}/versions/{version}/promote")
async def promote_skill(name: str, version: str):
    phase_seven_required()


@router.post("/skills/{name}/rollback")
async def rollback_skill(name: str):
    phase_seven_required()


"""Phase-4 schemas and durable reads for later Memory/Skill business stages."""

import json

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select

from backend.app.dependencies import get_database, get_memory_service, get_skill_services
from backend.app.schemas import MemoryCreate, MemoryLifecycleUpdate
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence.models import EvalRunModel, MemoryModel, SkillCandidateModel, SkillVersionModel
from deepresearch_agent.memory import MemoryRejected
from deepresearch_agent.persistence.repositories import MemoryRepository
from deepresearch_agent.evolution import PromotionRejected
from deepresearch_agent.persistence.repositories import AuditRepository

router = APIRouter(tags=["learning"])


@router.get("/memories")
async def list_memories(q: str | None = None, status_filter: str | None = None, target: str | None = None, database=Depends(get_database)):
    if q:
        items = await MemoryRepository(database).search(q, status=status_filter, limit=100)
        if target:
            items = [item for item in items if (item.target or item.scope) == target]
    else:
        async with database.sessions() as session:
            statement = select(MemoryModel).where(MemoryModel.deleted_at.is_(None))
            if status_filter:
                statement = statement.where(MemoryModel.status == status_filter)
            if target:
                statement = statement.where((MemoryModel.target == target) | ((MemoryModel.target.is_(None)) & (MemoryModel.scope == target)))
            items = list((await session.execute(statement.order_by(MemoryModel.updated_at.desc()).limit(100))).scalars())
    return {"items": [
        {"memory_id": item.memory_id, "target": item.target or item.scope, "scope": item.target or item.scope, "kind": item.kind,
         "content": item.content, "provenance_refs": json.loads(item.provenance_json or "[]"), "confidence": item.confidence,
         "valid_from": item.valid_from, "expires_at": item.expires_at, "supersedes": item.supersedes,
         "status": item.status, "created_by": item.created_by, "created_at": item.created_at, "updated_at": item.updated_at}
        for item in items
    ], "total": len(items)}


@router.get("/memories/capacity")
async def memory_capacity(memory_service=Depends(get_memory_service)):
    return {"targets": await memory_service.capacities()}


@router.post("/memories", status_code=status.HTTP_201_CREATED)
async def create_memory(payload: MemoryCreate, memory_service=Depends(get_memory_service)):
    try:
        item = await memory_service.create_candidate(
            target=payload.target, content=payload.content, kind=payload.kind,
            provenance_refs=payload.provenance_refs, created_by="user",
            activate=payload.activate,
        )
    except MemoryRejected as exc:
        raise AppError(ErrorCode.CONFLICT, str(exc)) from exc
    return memory_service.serialize(item)


@router.patch("/memories/{memory_id}")
async def patch_memory(memory_id: str, payload: MemoryLifecycleUpdate, memory_service=Depends(get_memory_service)):
    try:
        item = await memory_service.update(memory_id, content=payload.content, status=payload.status, expires_at=payload.expires_at)
    except MemoryRejected as exc:
        raise AppError(ErrorCode.CONFLICT, str(exc)) from exc
    if item is None:
        raise AppError(ErrorCode.NOT_FOUND, "Memory 不存在")
    return memory_service.serialize(item)


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: str, memory_service=Depends(get_memory_service)):
    if not await memory_service.delete(memory_id):
        raise AppError(ErrorCode.NOT_FOUND, "Memory 不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/skills")
async def list_skills(database=Depends(get_database)):
    async with database.sessions() as session:
        versions = list((await session.execute(select(SkillVersionModel).order_by(SkillVersionModel.name, SkillVersionModel.created_at.desc()))).scalars())
        candidates = list((await session.execute(
            select(SkillCandidateModel)
            .where(SkillCandidateModel.status.in_(("candidate", "evaluated")))
            .order_by(SkillCandidateModel.created_at.desc())
        )).scalars())
    return {
        "versions": [{"skill_version_id": item.skill_version_id, "name": item.name, "version": item.version, "status": item.status, "source_run_ids": json.loads(item.source_run_ids_json or "[]"), "created_at": item.created_at} for item in versions],
        "candidates": [{"candidate_id": item.candidate_id, "run_id": item.run_id, "name": item.name, "proposed_version": item.proposed_version, "status": item.status, "payload": json.loads(item.payload_json or "{}"), "created_at": item.created_at} for item in candidates],
    }


@router.get("/skills/{name}/versions/{version}")
async def get_skill(name: str, version: str, database=Depends(get_database), services=Depends(get_skill_services)):
    async with database.sessions() as session:
        item = (await session.execute(select(SkillVersionModel).where(SkillVersionModel.name == name, SkillVersionModel.version == version))).scalar_one_or_none()
    if item is None:
        raise AppError(ErrorCode.NOT_FOUND, "Skill 版本不存在")
    candidate = await services["repository"].find_candidate(name, version)
    evaluation = await services["repository"].latest_eval(candidate.candidate_id) if candidate else None
    return {"skill_version_id": item.skill_version_id, "name": item.name, "version": item.version, "status": item.status, "content_path": item.content_path, "content_hash": item.content_hash, "content": services["registry"].read_text(item.content_path), "source_run_ids": json.loads(item.source_run_ids_json or "[]"), "evaluation": None if evaluation is None else {"eval_run_id": evaluation.eval_run_id, "status": evaluation.status, "metrics": json.loads(evaluation.metrics_json or "{}")}, "created_at": item.created_at}


@router.post("/skills/{name}/versions/{version}/evaluate")
async def evaluate_skill(name: str, version: str, services=Depends(get_skill_services), database=Depends(get_database)):
    candidate = await services["repository"].find_candidate(name, version)
    if candidate is None:
        raise AppError(ErrorCode.NOT_FOUND, "Skill candidate 不存在")
    result = await services["evaluator"].evaluate(candidate.candidate_id)
    await AuditRepository(database).append(action="skill.evaluated", entity_type="skill", entity_id=f"{name}:{version}", payload={"eval_run_id": result.eval_run_id, "status": result.status})
    return {"eval_run_id": result.eval_run_id, "candidate_id": candidate.candidate_id, "status": result.status, "metrics": json.loads(result.metrics_json or "{}")}


@router.post("/skills/{name}/versions/{version}/promote")
async def promote_skill(name: str, version: str, services=Depends(get_skill_services)):
    try:
        item = await services["promotion"].promote(name=name, version=version, human_approved=True)
    except PromotionRejected as exc:
        raise AppError(ErrorCode.CONFLICT, str(exc)) from exc
    return {"accepted": True, "name": name, "version": item.version, "status": item.status}


@router.post("/skills/{name}/rollback")
async def rollback_skill(name: str, services=Depends(get_skill_services)):
    try:
        item = await services["promotion"].rollback(name)
    except PromotionRejected as exc:
        raise AppError(ErrorCode.CONFLICT, str(exc)) from exc
    return {"accepted": True, "name": name, "version": item.version, "status": item.status}

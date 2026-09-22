"""Version-bound user actions for the research workbench."""
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, model_validator

from backend.app.dependencies import get_run_service
from deepresearch_agent.research.schemas import ResearchSpec


router = APIRouter(prefix='/research', tags=['research'])


class VersionAction(BaseModel):
    revision: int = Field(ge=1)
    fingerprint: str = Field(min_length=64, max_length=64)


class RevisionAction(VersionAction):
    spec: ResearchSpec | None = None
    instruction: str | None = Field(default=None, min_length=1, max_length=10000)

    @model_validator(mode='after')
    def one_edit(self):
        if (self.spec is None) == (self.instruction is None):
            raise ValueError('请提供结构化修改或自然语言修改其中一种')
        return self


class ApprovalAction(VersionAction):
    client_request_id: str = Field(min_length=1, max_length=128)


class CellTarget(BaseModel):
    item_id: str
    field_id: str


class FollowupAction(ApprovalAction):
    cells: list[CellTarget] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=10000)


class AcceptAction(VersionAction):
    report_fingerprint: str = Field(min_length=64, max_length=64)


@router.get('/{study_id}')
async def get_study(study_id: str, service=Depends(get_run_service)):
    study = await service.research.store.get(study_id)
    run = await service.runs.get(study['run_id']) if study.get('run_id') else None
    return {**study, 'run_status': run.status if run else None}


@router.post('/{study_id}/revisions')
async def revise(study_id: str, payload: RevisionAction, service=Depends(get_run_service)):
    return await service.research.revise(study_id, payload)


@router.post('/{study_id}/approve')
async def approve(study_id: str, payload: ApprovalAction, service=Depends(get_run_service)):
    return await service.research.approve(study_id, payload)


@router.get('/{study_id}/matrix')
async def matrix(study_id: str, service=Depends(get_run_service)):
    return await service.research.store.matrix(study_id)


@router.post('/{study_id}/followups')
async def followup(study_id: str, payload: FollowupAction, service=Depends(get_run_service)):
    return await service.research.followup(study_id, payload)


@router.post('/{study_id}/accept')
async def accept(study_id: str, payload: AcceptAction, service=Depends(get_run_service)):
    return await service.research.accept(study_id, payload)


@router.get('/{study_id}/export')
async def export(study_id: str, format: Literal['markdown', 'json'] = Query('markdown'), service=Depends(get_run_service)):
    study = await service.research.store.get(study_id)
    matrix = await service.research.store.matrix(study_id)
    report = study.get('report') or {}
    if format == 'json':
        return {'study': study, 'matrix': matrix, 'report': report}
    from deepresearch_agent.research.workflow import ResearchWorkflow
    content = str(report.get('content') or ResearchWorkflow.render(study['spec'], matrix, matrix['cells']))
    if not report.get('complete'):
        content = '> 部分结果：当前研究仍有缺口，尚未完整定稿。\n\n' + content
    return PlainTextResponse(content, media_type='text/markdown',
        headers={'Content-Disposition': f'attachment; filename="research-{study_id}.md"'})

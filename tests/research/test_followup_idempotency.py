from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from backend.app.api.v1.research import FollowupAction
from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from backend.app.services.research_service import ResearchService
from deepresearch_agent.harness.errors import AppError
from deepresearch_agent.persistence.database import Database
from deepresearch_agent.persistence.models import RunModel
from deepresearch_agent.persistence.repositories import MessageRepository, RunRepository, SessionRepository


@pytest_asyncio.fixture
async def ready(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'followup.db').as_posix()}")
    await db.create_schema()
    session = await SessionRepository(db).create(SessionCreate(title='study'))
    runs = RunRepository(db)
    _, original, _ = await runs.create_for_user_message(
        MessageCreate(session_id=session.session_id, role='user', content='study', client_message_id='initial'),
        RunCreate(session_id=session.session_id, trigger_message_id='assigned-atomically',
                  source_mode='web', workflow_mode='deep_research'))
    await runs.update_status(original.run_id, status='failed', current_stage='failed')
    owner = SimpleNamespace(database=db, runs=runs, messages=MessageRepository(db), schedule=Mock())
    service = ResearchService(owner)
    service._freeze = AsyncMock()
    view = await service.store.create(session.session_id, original.run_id, {
        'title': 'study', 'questions': ['compare'],
        'items': [{'id': 'a', 'name': 'A'}, {'id': 'b', 'name': 'B'}],
        'fields': [{'id': 'common', 'label': 'Common'},
                   {'id': 'only_a', 'label': 'Only A', 'applies_to': ['a']}],
    })
    await service.store.approve(view['study_id'], 1, view['fingerprint'])
    await service.store.link_run(view['study_id'], 1, original.run_id)
    payload = FollowupAction(revision=1, fingerprint=view['fingerprint'], client_request_id='f1',
        cells=[{'item_id': 'a', 'field_id': 'common'}], reason='Verify A')
    yield service, view, payload
    await db.close()


@pytest.mark.asyncio
async def test_replayed_old_followup_does_not_pause_or_schedule_current_run(ready):
    service, view, payload = ready
    first = await service.followup(view['study_id'], payload)
    await service.owner.runs.update_status(first['run_id'], status='failed', current_stage='failed')
    second = await service.followup(view['study_id'], payload.model_copy(update={'client_request_id': 'f2'}))
    await service.owner.runs.update_status(second['run_id'], status='executing', current_stage='executing')
    service._freeze.reset_mock()
    service.owner.schedule.reset_mock()
    replay = await service.followup(view['study_id'], payload)
    assert replay['run_id'] == first['run_id'] and replay['created'] is False
    service._freeze.assert_not_awaited()
    service.owner.schedule.assert_not_called()
    assert (await service.owner.runs.get(second['run_id'])).status == 'executing'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    {'reason': 'A different request'},
    {'cells': [{'item_id': 'b', 'field_id': 'common'}]},
    {'revision': 2},
    {'fingerprint': '0' * 64},
])
async def test_same_id_with_changed_request_is_rejected_before_side_effects(ready, change):
    service, view, payload = ready
    await service.followup(view['study_id'], payload)
    service._freeze.reset_mock()
    service.owner.schedule.reset_mock()
    changed = FollowupAction.model_validate({**payload.model_dump(), **change})
    with pytest.raises(AppError):
        await service.followup(view['study_id'], changed)
    service._freeze.assert_not_awaited()
    service.owner.schedule.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('targets', [
    [{'item_id': 'unknown', 'field_id': 'common'}],
    [{'item_id': 'a', 'field_id': 'unknown'}],
    [{'item_id': 'b', 'field_id': 'only_a'}],
    [{'item_id': 'a', 'field_id': 'common'}] * 2,
])
async def test_invalid_target_does_not_create_orphan_run_or_pause(ready, targets):
    service, view, payload = ready
    async with service.database.sessions() as session:
        before = await session.scalar(select(func.count()).select_from(RunModel))
    with pytest.raises(AppError):
        await service.followup(view['study_id'], FollowupAction.model_validate({**payload.model_dump(), 'cells': targets}))
    async with service.database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RunModel)) == before
    service._freeze.assert_not_awaited()
    service.owner.schedule.assert_not_called()


@pytest.mark.asyncio
async def test_old_request_replay_uses_original_revision_and_reason_after_title_edit(ready):
    service, view, payload = ready
    first = await service.followup(view['study_id'], payload)
    await service.owner.runs.update_status(first['run_id'], status='failed', current_stage='failed')
    spec = {**view['spec'], 'title': 'A new study title'}
    revised = await service.store.revise(view['study_id'], 1, view['fingerprint'], spec)
    await service.store.approve(view['study_id'], 2, revised['fingerprint'])
    service._freeze.reset_mock()
    service.owner.schedule.reset_mock()
    replay = await service.followup(view['study_id'], payload)
    assert replay['run_id'] == first['run_id'] and replay['revision'] == 1
    assert replay['created'] is False
    service._freeze.assert_not_awaited()
    service.owner.schedule.assert_not_called()


@pytest.mark.asyncio
async def test_target_order_does_not_change_idempotent_request(ready):
    service, view, payload = ready
    targets = [{'item_id': 'a', 'field_id': 'common'}, {'item_id': 'b', 'field_id': 'common'}]
    payload = FollowupAction.model_validate({**payload.model_dump(), 'cells': targets})
    first = await service.followup(view['study_id'], payload)
    service._freeze.reset_mock()
    service.owner.schedule.reset_mock()
    replay = await service.followup(view['study_id'], FollowupAction.model_validate({
        **payload.model_dump(), 'cells': list(reversed(targets))}))
    assert replay['run_id'] == first['run_id'] and replay['created'] is False
    service._freeze.assert_not_awaited()
    service.owner.schedule.assert_not_called()


@pytest.mark.asyncio
async def test_retry_repairs_run_created_before_link_failure(ready, monkeypatch):
    service, view, payload = ready
    link_run = service.store.link_run
    monkeypatch.setattr(service.store, 'link_run', AsyncMock(side_effect=RuntimeError('crash before link')))
    with pytest.raises(RuntimeError, match='crash before link'):
        await service.followup(view['study_id'], payload)
    service.owner.schedule.assert_not_called()
    assert len((await service.store.get(view['study_id']))['runs']) == 1
    monkeypatch.setattr(service.store, 'link_run', link_run)
    service._freeze.reset_mock()
    recovered = await service.followup(view['study_id'], payload)
    assert recovered['created'] is False
    service.owner.schedule.assert_called_once_with(recovered['run_id'])
    service._freeze.assert_awaited_once()
    linked = await service.store.for_run(recovered['run_id'])
    assert linked['run_id'] == recovered['run_id']
    service._freeze.reset_mock()
    service.owner.schedule.reset_mock()
    await service.followup(view['study_id'], payload)
    service._freeze.assert_not_awaited()
    service.owner.schedule.assert_not_called()


@pytest.mark.asyncio
async def test_orphan_from_previous_revision_cannot_resume_after_scope_change(ready, monkeypatch):
    service, view, payload = ready
    link_run = service.store.link_run
    monkeypatch.setattr(service.store, 'link_run', AsyncMock(side_effect=RuntimeError('crash before link')))
    with pytest.raises(RuntimeError):
        await service.followup(view['study_id'], payload)
    monkeypatch.setattr(service.store, 'link_run', link_run)
    revised = await service.store.revise(view['study_id'], 1, view['fingerprint'], {**view['spec'], 'title': 'new'})
    await service.store.approve(view['study_id'], 2, revised['fingerprint'])
    service._freeze.reset_mock()
    with pytest.raises(AppError):
        await service.followup(view['study_id'], payload)
    service._freeze.assert_not_awaited()
    service.owner.schedule.assert_not_called()

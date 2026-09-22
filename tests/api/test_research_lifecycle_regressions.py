import asyncio
import copy
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from deepresearch_agent.retrieval.tavily_provider import TavilyProvider
from test_research_workbench import (
    SPEC, FixedResearchModel, WebDriver, research_client, start, wait_run,
)


def approve(client):
    created = start(client)
    run = wait_run(client, created['run_id'], {'awaiting_scope_approval'})
    base = f"/api/v1/research/{run['study_id']}"
    study = client.get(base).json()
    response = client.post(base + '/approve', json={
        'revision': study['current_revision'], 'fingerprint': study['fingerprint'],
        'client_request_id': 'approval',
    })
    assert response.status_code == 200, response.text
    return base, study, response.json()['run_id']


def test_cancel_exports_completed_cells_as_partial_report(research_client, monkeypatch):
    entered_second = threading.Event()
    original = WebDriver.execute

    async def execute(self):
        if self.context.config_snapshot['research_unit']['item_id'] == 'b':
            entered_second.set()
            await asyncio.sleep(30)
        await original(self)

    monkeypatch.setattr(WebDriver, 'execute', execute)
    client = research_client
    base, study, run_id = approve(client)
    assert entered_second.wait(5)
    assert client.post(f'/api/v1/runs/{run_id}/cancel').status_code == 200
    assert wait_run(client, run_id, {'cancelled', 'failed'})['status'] == 'cancelled'
    matrix = client.get(base + '/matrix').json()
    assert matrix['counts']['current'] == 1 and matrix['counts']['missing'] == 1
    exported = client.get(base + '/export')
    assert exported.status_code == 200
    assert 'Alpha' in exported.text and '支持恢复' in exported.text
    assert '部分结果' in exported.text and '尚未调查' in exported.text
    for _ in range(100):
        view = client.get(base).json()
        if view['report'] is not None:
            break
        time.sleep(.02)
    assert view['report'] is not None and view['report']['complete'] is False
    assert client.post(base + '/accept', json={
        'revision': study['current_revision'], 'fingerprint': study['fingerprint'],
        'report_fingerprint': view['report']['fingerprint'],
    }).status_code == 409


def test_followup_api_retry_does_not_pause_newer_run(research_client, monkeypatch):
    client = research_client
    base, study, run_id = approve(client)
    assert wait_run(client, run_id, {'completed', 'failed'})['status'] == 'completed'
    first_request = {'revision': study['current_revision'], 'fingerprint': study['fingerprint'],
        'client_request_id': 'f1', 'cells': [{'item_id': 'a', 'field_id': 'recovery'}], 'reason': '复核 A'}
    first = client.post(base + '/followups', json=first_request)
    assert first.status_code == 200, first.text
    assert wait_run(client, first.json()['run_id'], {'completed', 'failed'})['status'] == 'completed'
    entered = threading.Event()

    async def slow(self):
        entered.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(WebDriver, 'execute', slow)
    second = client.post(base + '/followups', json={**first_request, 'client_request_id': 'f2',
        'cells': [{'item_id': 'b', 'field_id': 'recovery'}], 'reason': '复核 B'})
    assert second.status_code == 200, second.text
    second_id = second.json()['run_id']
    assert entered.wait(5)
    replay = client.post(base + '/followups', json=first_request)
    assert replay.status_code == 200 and replay.json()['run_id'] == first.json()['run_id']
    assert replay.json()['created'] is False
    assert client.get(f'/api/v1/runs/{second_id}').json()['status'] == 'executing'
    assert client.post(f'/api/v1/runs/{second_id}/cancel').status_code == 200
    wait_run(client, second_id, {'cancelled'})


class DiscoveryModel(FixedResearchModel):
    async def draft(self, query, **kwargs):
        spec = copy.deepcopy(SPEC)
        spec['items'] = []
        spec['queries'] = ['discover candidates']
        return spec


def configure_discovery(client, tmp_path, delay):
    class SearchClient:
        def search(self, **kwargs):
            time.sleep(delay)
            return {'results': []}

    service = client.app.state.run_service
    service.research_model = DiscoveryModel()
    provider = TavilyProvider(api_key='', client=SearchClient(), cache_dir=tmp_path / 'cache')
    service._router = SimpleNamespace(for_mode=lambda mode: provider)
    return service


def test_discovery_search_time_is_counted_in_study_budget(research_client, tmp_path):
    client = research_client
    configure_discovery(client, tmp_path, 1.2)
    created = start(client)
    run = wait_run(client, created['run_id'], {'awaiting_scope_approval', 'failed'})
    assert run['status'] == 'awaiting_scope_approval', run
    study = client.get(f"/api/v1/research/{run['study_id']}").json()
    assert study['usage']['discovery_calls'] == 1
    assert study['usage']['active_seconds'] >= 1.1


def test_discovery_search_is_stopped_when_initial_study_budget_expires(research_client, tmp_path, monkeypatch):
    client = research_client
    service = configure_discovery(client, tmp_path, 4)

    async def create_with_one_second_budget(run, query):
        view = await service.research.store.create(run.session_id, run.run_id, {
            'title': query, 'questions': [query],
            'budget': {'max_search_calls': 20, 'max_active_seconds': 1, 'max_llm_tokens': 200000},
        })
        await service.research._configure(run.run_id, view, outline_pending=True)
        return view

    monkeypatch.setattr(service.research, 'create', create_with_one_second_budget)
    started = time.monotonic()
    created = start(client)
    run = wait_run(client, created['run_id'], {'budget_exhausted', 'failed', 'awaiting_scope_approval'})
    assert run['status'] == 'budget_exhausted', run
    assert time.monotonic() - started < 3
    study = client.get(f"/api/v1/research/{run['study_id']}").json()
    assert study['usage']['active_seconds'] >= 1
    assert study['usage']['discovery_calls'] == 1
    assert study['approved_revision'] is None


def test_approval_retry_links_orphan_run_and_finishes(research_client, monkeypatch):
    client = research_client
    service = client.app.state.run_service
    created = start(client)
    outline = wait_run(client, created['run_id'], {'awaiting_scope_approval'})
    base = f"/api/v1/research/{outline['study_id']}"
    assert client.post(f"/api/v1/runs/{outline['run_id']}/cancel").status_code == 200
    wait_run(client, outline['run_id'], {'cancelled'})
    study = client.get(base).json()
    payload = {'revision': study['current_revision'], 'fingerprint': study['fingerprint'],
               'client_request_id': 'approval-after-crash'}
    original_link = service.research.store.link_run
    monkeypatch.setattr(service.research.store, 'link_run', AsyncMock(side_effect=RuntimeError('crash before link')))
    with pytest.raises(RuntimeError, match='crash before link'):
        client.post(base + '/approve', json=payload)
    runs = client.portal.call(service.runs.list_for_session, study['session_id'])
    assert len(runs) == 2
    orphan = next(r for r in runs if r.run_id != outline['run_id'])
    assert client.portal.call(service.research.store.for_run, orphan.run_id) is None
    assert WebDriver.executions == []
    monkeypatch.setattr(service.research.store, 'link_run', original_link)
    replay = client.post(base + '/approve', json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()['run_id'] == orphan.run_id and replay.json()['created'] is False
    assert wait_run(client, orphan.run_id, {'completed', 'failed'})['status'] == 'completed'
    assert len(client.portal.call(service.runs.list_for_session, study['session_id'])) == 2
    assert client.get(base).json()['report']['complete'] is True


def test_initial_approval_retry_recovers_promotion_before_configuration_failure(research_client, monkeypatch):
    client = research_client
    service = client.app.state.run_service
    created = start(client)
    outline = wait_run(client, created['run_id'], {'awaiting_scope_approval'})
    base = f"/api/v1/research/{outline['study_id']}"
    study = client.get(base).json()
    payload = {'revision': study['current_revision'], 'fingerprint': study['fingerprint'],
               'client_request_id': 'promotion-crash'}
    original_configure = service.research._configure
    monkeypatch.setattr(service.research, '_configure', AsyncMock(side_effect=RuntimeError('crash after promotion')))
    with pytest.raises(RuntimeError, match='crash after promotion'):
        client.post(base + '/approve', json=payload)
    pending = client.get(base).json()
    assert pending['approved_revision'] == study['current_revision']
    assert pending['runs'][-1]['purpose'] == 'research'
    assert client.get(f"/api/v1/runs/{outline['run_id']}").json()['status'] == 'awaiting_scope_approval'
    assert WebDriver.executions == []
    monkeypatch.setattr(service.research, '_configure', original_configure)
    replay = client.post(base + '/approve', json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()['run_id'] == outline['run_id'] and replay.json()['created'] is False
    assert wait_run(client, outline['run_id'], {'completed', 'failed'})['status'] == 'completed'
    assert len(client.portal.call(service.runs.list_for_session, study['session_id'])) == 1
    assert client.get(base).json()['report']['complete'] is True


@pytest.mark.parametrize('recovery', ['same_message', 'startup'])
def test_chat_crash_recovers_required_study_without_bypassing_approval(research_client, monkeypatch, recovery):
    client = research_client
    service = client.app.state.run_service
    session_id = client.post('/api/v1/sessions', json={'title': 'crash recovery'}).json()['session_id']
    url = f'/api/v1/sessions/{session_id}/messages'
    payload = {'content': '比较 Alpha 与 Beta', 'client_message_id': 'crashed-message',
               'source_mode': 'web', 'workflow_mode': 'deep_research'}
    original_create = service.research.create
    monkeypatch.setattr(service.research, 'create', AsyncMock(side_effect=RuntimeError('crash before study')))
    with pytest.raises(RuntimeError, match='crash before study'):
        client.post(url, json=payload)
    runs = client.portal.call(service.runs.list_for_session, session_id)
    assert len(runs) == 1
    orphan = runs[0]
    config = json.loads(orphan.config_snapshot_json)
    assert config['research_required'] is True and not config.get('research_study_id')
    assert client.portal.call(service.research.store.for_run, orphan.run_id) is None
    monkeypatch.setattr(service.research, 'create', original_create)
    if recovery == 'same_message':
        replay = client.post(url, json=payload)
        assert replay.status_code in {200, 202}, replay.text
        assert replay.json()['run_id'] == orphan.run_id and replay.json()['created'] is False
    else:
        async def recover():
            return await service.startup_recovery(auto_resume=True)
        assert orphan.run_id in client.portal.call(recover)
    recovered = wait_run(client, orphan.run_id, {'awaiting_scope_approval', 'failed', 'completed'})
    assert recovered['status'] == 'awaiting_scope_approval', recovered
    assert recovered['study_id']
    assert len(client.portal.call(service.runs.list_for_session, session_id)) == 1
    study = client.get(f"/api/v1/research/{recovered['study_id']}").json()
    assert study['approved_revision'] is None
    assert WebDriver.executions == []


@pytest.mark.parametrize('has_study', [False, True])
def test_runtime_fails_closed_without_required_study_or_service(research_client, has_study):
    from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
    from deepresearch_agent.harness.runtime import HarnessRuntime
    from deepresearch_agent.persistence.repositories import (
        CheckpointRepository, ContractRepository, EvidenceRepository,
    )

    client = research_client
    service = client.app.state.run_service
    factory = Mock(side_effect=AssertionError('workflow must not start'))

    async def execute_unready():
        session = await service.sessions.create(SessionCreate(title='unready'))
        _, run, _ = await service.runs.create_for_user_message(
            MessageCreate(session_id=session.session_id, role='user', content='required research', client_message_id='unready'),
            RunCreate(session_id=session.session_id, trigger_message_id='atomic', source_mode='web',
                      workflow_mode='deep_research', config_snapshot={'research_required': True}))
        if has_study:
            await service.research.create(run, 'required research')
        runtime = HarnessRuntime(run_repository=service.runs, message_repository=service.messages,
            event_repository=service.events, checkpoint_repository=CheckpointRepository(service.database),
            evidence_repository=EvidenceRepository(service.database), contract_repository=ContractRepository(service.database),
            workflow_factory=factory, research_service=None if has_study else service.research)
        await runtime.execute_run(run.run_id)
        return await service.runs.get(run.run_id)

    run = client.portal.call(execute_unready)
    assert run.status == 'failed'
    assert '研究范围尚未初始化' in run.error_message
    factory.assert_not_called()

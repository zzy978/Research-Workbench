import time
import uuid
import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from test_api_phase4 import ApiFakeDriver


SPEC = {
    'title': '框架比较', 'questions': ['比较恢复能力'], 'hard_constraints': [],
    'items': [{'id': 'a', 'name': 'Alpha', 'version': '1', 'rationale': '候选'},
              {'id': 'b', 'name': 'Beta', 'version': '1', 'rationale': '候选'}],
    'fields': [{'id': 'recovery', 'label': '恢复', 'description': '恢复机制',
                'evidence_requirement': '官方正文', 'required_access': 'full_text', 'applies_to': []}],
    'scope': {'time_range': '当前版本', 'inclusion': [], 'exclusion': []},
    'source_policy': '官方资料', 'queries': [], 'sections': ['比较结果', '局限'],
    'budget': {'max_search_calls': 20, 'max_active_seconds': 1800, 'max_llm_tokens': 200000},
    'stop_conditions': ['关键字段已有证据或明确缺口'],
}


class FixedResearchModel:
    async def draft(self, query, *, previous=None, instruction=None, discoveries=None):
        return SPEC if previous is None else previous

    async def extract(self, spec, item, fields, results):
        return [{'item_id': item['id'], 'field_id': f['id'], 'status': 'supported',
                 'value': f"{item['name']} 支持恢复", 'reason': '正文说明',
                 'citations': [{'evidence_id': results[0].result_id,
                    'content_hash': results[0].metadata.content_hash,
                    'locator': 'API integrated evidence 1', 'access': 'full_text'}]}
                for f in fields]

    async def synthesize(self, spec, cells):
        return '\n\n'.join(str(c['value']) + ' ['+c['citations'][0]['evidence_id']+']'
                            for c in cells if c.get('citations'))


class WebDriver(ApiFakeDriver):
    executions = []

    def __init__(self, context, events=None):
        super().__init__(context, events)
        for index, result in enumerate(self.results):
            result.source_mode = 'web'
            result.source = 'tavily_search'
            result.metadata.source_id = f'https://example.org/{index}'
            result.metadata.extra['access'] = 'full_text'

    async def execute(self):
        WebDriver.executions.append(self.context.original_query)
        await super().execute()

    def execution_records(self):
        records = super().execution_records()
        for record in records:
            for call in record.tool_calls:
                call.source_mode = 'web'
                call.tool_name = 'tavily_search'
        return records

    def plan_record(self):
        plan, tasks = super().plan_record()
        for task in tasks:
            task['source_mode'] = 'web'
        return plan, tasks


@pytest.fixture
def research_client(tmp_path):
    WebDriver.executions = []
    app = create_app(database_url=f"sqlite+aiosqlite:///{(tmp_path/'research.db').as_posix()}",
                     artifact_root=tmp_path/'artifacts', skills_root=tmp_path/'skills',
                     workflow_factory=WebDriver, auto_resume=False)
    app.state.run_service.research_model = FixedResearchModel()
    with TestClient(app) as client:
        yield client


def wait_run(client, run_id, states):
    for _ in range(250):
        run = client.get(f'/api/v1/runs/{run_id}').json()
        if run['status'] in states:
            return run
        time.sleep(.02)
    raise AssertionError(run)


def start(client, workflow='deep_research'):
    session = client.post('/api/v1/sessions', json={'title': '比较研究'}).json()['session_id']
    return client.post(f'/api/v1/sessions/{session}/messages', json={
        'content': '比较 Alpha 与 Beta 的恢复能力', 'client_message_id': str(uuid.uuid4()),
        'source_mode': 'web', 'workflow_mode': workflow,
    }).json()


def test_web_waits_for_explicit_approval_before_execution(research_client):
    client = research_client
    created = start(client)
    run = wait_run(client, created['run_id'], {'awaiting_scope_approval', 'failed', 'completed'})
    assert run['status'] == 'awaiting_scope_approval'
    assert WebDriver.executions == []
    cancelled = client.post(f"/api/v1/runs/{run['run_id']}/cancel")
    assert cancelled.status_code == 200
    assert client.post(f"/api/v1/runs/{run['run_id']}/resume").status_code == 409
    study = client.get(f"/api/v1/research/{run['study_id']}").json()
    assert study['approved_revision'] is None
    bad = client.post(f"/api/v1/research/{run['study_id']}/approve", json={
        'revision': study['current_revision'], 'fingerprint': '0'*64, 'client_request_id': 'bad'})
    assert bad.status_code == 409
    assert WebDriver.executions == []


@pytest.mark.parametrize('workflow', ['deep_research', 'plan_execute_report'])
def test_approved_study_matrix_increment_and_acceptance(research_client, workflow):
    client = research_client
    created = start(client, workflow)
    run = wait_run(client, created['run_id'], {'awaiting_scope_approval', 'failed', 'completed'})
    assert run['status'] == 'awaiting_scope_approval'
    base = f"/api/v1/research/{run['study_id']}"
    study = client.get(base).json()
    approval = {'revision': study['current_revision'], 'fingerprint': study['fingerprint'], 'client_request_id': 'approve-1'}
    response = client.post(base+'/approve', json=approval)
    assert response.status_code == 200, response.text
    execution_id = response.json()['run_id']
    assert client.post(base+'/approve', json=approval).json()['run_id'] == execution_id
    finished = wait_run(client, execution_id, {'completed', 'failed', 'budget_exhausted'})
    assert finished['status'] == 'completed', finished
    matrix = client.get(base+'/matrix').json()
    assert matrix['counts']['missing'] == 0
    assert len(matrix['cells']) == 2
    study = client.get(base).json()
    assert study['status'] != 'complete'
    accepted = client.post(base+'/accept', json={'revision': study['current_revision'],
        'fingerprint': study['fingerprint'], 'report_fingerprint': study['report']['fingerprint']})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['status'] == 'complete'

    spec = study['spec']
    spec['fields'].append({'id': 'license', 'label': '许可', 'description': '许可条款',
                          'evidence_requirement': '官方正文', 'applies_to': []})
    revision = client.post(base+'/revisions', json={'revision': study['current_revision'],
                           'fingerprint': study['fingerprint'], 'spec': spec})
    assert revision.status_code == 200, revision.text
    newer = revision.json()
    pending = client.get(base+'/matrix').json()
    assert pending['counts']['current'] == 2
    assert pending['counts']['missing'] == 2
    before = len(WebDriver.executions)
    approved = client.post(base+'/approve', json={'revision': newer['current_revision'],
        'fingerprint': newer['fingerprint'], 'client_request_id': 'approve-2'}).json()
    finished = wait_run(client, approved['run_id'], {'completed', 'failed', 'budget_exhausted'})
    assert finished['status'] == 'completed', finished
    assert len(WebDriver.executions) - before == 2
    assert all('license' in q and 'recovery' not in q for q in WebDriver.executions[before:])


def test_approved_budget_stops_long_stage(research_client, monkeypatch):
    async def slow(self):
        await asyncio.sleep(4)
    monkeypatch.setattr(WebDriver, 'execute', slow)
    client = research_client
    created = start(client)
    run = wait_run(client, created['run_id'], {'awaiting_scope_approval'})
    base = f"/api/v1/research/{run['study_id']}"
    study = client.get(base).json()
    spec = study['spec']
    spec['budget']['max_active_seconds'] = 1
    study = client.post(base+'/revisions', json={'revision':study['current_revision'],
        'fingerprint':study['fingerprint'], 'spec':spec}).json()
    started = time.monotonic()
    approved = client.post(base+'/approve', json={'revision':study['current_revision'],
        'fingerprint':study['fingerprint'], 'client_request_id':'short-budget'}).json()
    run = wait_run(client, approved['run_id'], {'budget_exhausted','failed','completed'})
    assert run['status'] == 'budget_exhausted'
    assert time.monotonic()-started < 3


def test_report_recovery_uses_saved_cells_without_researching_again(research_client, monkeypatch):
    async def uncited_summary(*args):
        return '这里是一段没有引用的长结论，应当被检查拒绝。'

    monkeypatch.setattr(FixedResearchModel, 'synthesize', uncited_summary)
    client = research_client
    created = start(client)
    run = wait_run(client, created['run_id'], {'awaiting_scope_approval'})
    base = f"/api/v1/research/{run['study_id']}"
    study = client.get(base).json()
    approved = client.post(base+'/approve', json={'revision':study['current_revision'],
        'fingerprint':study['fingerprint'], 'client_request_id':'recover-prose'}).json()
    finished = wait_run(client, approved['run_id'], {'completed','failed','budget_exhausted'})
    assert finished['status'] == 'completed', finished
    assert len(WebDriver.executions) == 2
    report = client.get(base).json()['report']
    assert report['complete']
    assert '没有引用的长结论' not in report['content']
    assert 'Alpha 支持恢复' in report['content']


def test_success_learning_waits_for_acceptance_and_is_idempotent(research_client, monkeypatch):
    from sqlalchemy import func, select
    from deepresearch_agent.persistence.models import LearningReviewJobModel

    client = research_client
    owner = client.app.state.run_service
    owner.skill_learning.enabled = True
    monkeypatch.setattr(owner.skill_learning, 'schedule', lambda review_id: None)
    created = start(client)
    run = wait_run(client, created['run_id'], {'awaiting_scope_approval'})
    base = f"/api/v1/research/{run['study_id']}"
    study = client.get(base).json()
    approved = client.post(base+'/approve', json={'revision':study['current_revision'],
        'fingerprint':study['fingerprint'], 'client_request_id':'learning'}).json()
    wait_run(client, approved['run_id'], {'completed'})
    assert client.portal.call(owner.skill_learning.enqueue_for_run, approved['run_id']) is None
    study = client.get(base).json()
    acceptance = {'revision':study['current_revision'], 'fingerprint':study['fingerprint'],
                  'report_fingerprint':study['report']['fingerprint']}
    assert client.post(base+'/accept', json=acceptance).status_code == 200
    assert client.post(base+'/accept', json=acceptance).status_code == 200

    async def jobs():
        async with owner.database.sessions() as session:
            return await session.scalar(select(func.count()).select_from(LearningReviewJobModel))

    assert client.portal.call(jobs) == 1


def test_cancel_at_waiting_checkpoint_finishes_scheduler_cleanup(research_client, monkeypatch):
    client = research_client
    owner = client.app.state.run_service
    original = owner.research.store.record_usage

    async def delayed_cleanup(run_id, *args):
        run = await owner.runs.get(run_id)
        if run.status == 'awaiting_scope_approval':
            await asyncio.sleep(30)
        await original(run_id, *args)

    monkeypatch.setattr(owner.research.store, 'record_usage', delayed_cleanup)
    created = start(client)
    run = wait_run(client, created['run_id'], {'awaiting_scope_approval'})
    assert client.post(f"/api/v1/runs/{run['run_id']}/cancel").status_code == 200
    assert client.get(f"/api/v1/runs/{run['run_id']}").json()['status'] == 'cancelled'

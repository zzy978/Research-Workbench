"""Exercise the real scheduler, durable matrix, reports and acceptance gate."""
from collections import Counter

import pytest

from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.harness.errors import ResearchPauseRequested, RunCancelled
from deepresearch_agent.harness.budgets import BudgetExceeded
from test_research_workbench import (
    FixedResearchModel, WebDriver, research_client, start, wait_run,
)


def approve(client):
    created = start(client)
    run = wait_run(client, created['run_id'], {'awaiting_scope_approval'})
    base = f"/api/v1/research/{run['study_id']}"
    study = client.get(base).json()
    spec = study['spec']
    spec['fields'].append({'id': 'license', 'label': '许可'})
    study = client.post(base+'/revisions', json={'revision': study['current_revision'],
        'fingerprint': study['fingerprint'], 'spec': spec}).json()
    approved = client.post(base+'/approve', json={'revision': study['current_revision'],
        'fingerprint': study['fingerprint'], 'client_request_id': 'isolation'}).json()
    return base, approved['run_id']


@pytest.mark.parametrize('recover_at', [2, 3, None])
def test_invalid_locator_retries_extraction_and_defers_only_failed_field(research_client, monkeypatch, recover_at):
    calls = []
    attempts = Counter()
    original = FixedResearchModel.extract

    async def extract(self, spec, item, fields, results):
        cells = await original(self, spec, item, fields, results)
        for cell in cells:
            key = (cell['item_id'], cell['field_id'])
            calls.append(key)
            attempts[key] += 1
            if key == ('a', 'recovery') and (recover_at is None or attempts[key] < recover_at):
                cell['citations'][0]['locator'] = 'fabricated quote'
        return cells

    monkeypatch.setattr(FixedResearchModel, 'extract', extract)
    base, run_id = approve(research_client)
    run = wait_run(research_client, run_id, {'completed', 'partial', 'failed', 'budget_exhausted'})
    assert run['status'] == ('partial' if recover_at is None else 'completed'), run
    assert len(WebDriver.executions) == 2  # extraction never repeats retrieval
    assert attempts[('a', 'license')] == attempts[('b', 'recovery')] == attempts[('b', 'license')] == 1
    assert attempts[('a', 'recovery')] == (recover_at or 3)
    if recover_at != 2:
        assert calls[-1] == ('a', 'recovery')
        assert calls.index(('b', 'license')) < len(calls)-1
    matrix = research_client.get(base+'/matrix').json()
    study = research_client.get(base).json()
    assert study['report']['complete'] is (recover_at is not None)
    if recover_at is None:
        cell = matrix['cells'][0]
        assert cell['status'] == 'pending_retry'
        assert cell['failure']['stage'] == 'validation'
        assert cell['failure']['attempts'] == 3
        assert cell['value'] is None and cell['citations'] == []
        assert matrix['counts']['current'] == 3
        assert matrix['counts']['pending_retry'] == 1
        assert '部分' in study['report']['content'] and '待补查' in study['report']['content']
        assert 'fabricated quote' not in study['report']['content']
        response = research_client.post(base+'/accept', json={'revision': study['current_revision'],
            'fingerprint': study['fingerprint'], 'report_fingerprint': study['report']['fingerprint']})
        assert response.status_code == 409


def test_empty_extraction_is_a_gap_not_a_completed_field(research_client, monkeypatch):
    async def empty(*args):
        return []
    monkeypatch.setattr(FixedResearchModel, 'extract', empty)
    base, run_id = approve(research_client)
    run = wait_run(research_client, run_id, {'completed', 'partial', 'failed'})
    assert run['status'] == 'partial', run
    matrix = research_client.get(base+'/matrix').json()
    assert matrix['counts']['current'] == 0
    assert all(cell['status'] == 'pending_retry' for cell in matrix['cells'])
    assert research_client.get(base).json()['report']['complete'] is False


@pytest.mark.parametrize('fatal', [False, True])
def test_retrieval_failure_isolated_but_auth_stops_all_work(research_client, monkeypatch, fatal):
    original = WebDriver.execute
    attempts = []

    async def execute(self):
        item = self.context.config_snapshot['research_unit']['item_id']
        attempts.append(item)
        if item == 'a':
            self.results = []
            raise AppError(ErrorCode.TAVILY_AUTH_FAILED if fatal else ErrorCode.RETRIEVAL_TIMEOUT,
                           '认证失败' if fatal else '检索超时', retryable=not fatal)
        await original(self)

    monkeypatch.setattr(WebDriver, 'execute', execute)
    base, run_id = approve(research_client)
    run = wait_run(research_client, run_id, {'completed', 'partial', 'failed', 'budget_exhausted'})
    if fatal:
        assert run['status'] == 'failed'
        assert attempts == ['a']
    else:
        assert run['status'] == 'partial', run
        assert attempts == ['a', 'a', 'b', 'a']
        matrix = research_client.get(base+'/matrix').json()
        assert matrix['counts']['current'] == 2
        assert matrix['cells'][0]['failure']['stage'] == 'retrieval'


@pytest.mark.parametrize('pauses', [1, 2])
def test_resume_reuses_retrieval_and_already_saved_fields(research_client, monkeypatch, pauses):
    original = FixedResearchModel.extract
    calls = Counter()

    async def extract(self, spec, item, fields, results):
        key = (item['id'], fields[0]['id'])
        calls[key] += 1
        if key == ('a', 'license') and calls[key] <= pauses:
            raise ResearchPauseRequested()
        return await original(self, spec, item, fields, results)

    monkeypatch.setattr(FixedResearchModel, 'extract', extract)
    base, run_id = approve(research_client)
    async def settle_pause():
        task = research_client.app.state.run_service._tasks.get(run_id)
        if task is not None:
            await task
    for _ in range(pauses):
        run = wait_run(research_client, run_id, {'paused', 'failed', 'partial'})
        assert run['status'] == 'paused', run
        research_client.portal.call(settle_pause)
        assert research_client.get(base+'/matrix').json()['counts']['current'] == 1
        assert research_client.post(f'/api/v1/runs/{run_id}/resume').status_code == 200
    run = wait_run(research_client, run_id, {'completed', 'partial', 'failed'})
    assert run['status'] == 'completed', run
    assert len(WebDriver.executions) == 2
    assert calls[('a', 'recovery')] == calls[('b', 'recovery')] == 1


@pytest.mark.parametrize('kind,expected', [('budget', 'budget_exhausted'), ('cancel', 'cancelled'),
                                         ('persistence', 'failed'), ('model_auth', 'failed')])
def test_global_failures_stop_before_next_field(research_client, monkeypatch, kind, expected):
    class AuthenticationError(RuntimeError):
        status_code = 401

    error = {'budget': BudgetExceeded('llm_tokens', 100, 100), 'cancel': RunCancelled('cancelled'),
             'persistence': AppError(ErrorCode.PERSISTENCE_FAILED, '无法保存'),
             'model_auth': AuthenticationError('unauthorized')}[kind]
    owner = research_client.app.state.run_service
    calls = []

    async def broken(*args, **kwargs):
        calls.append(1)
        raise error

    if kind == 'persistence':
        monkeypatch.setattr(owner.research.store, 'save_cell', broken)
    else:
        monkeypatch.setattr(FixedResearchModel, 'extract', broken)
    base, run_id = approve(research_client)
    run = wait_run(research_client, run_id, {'completed', 'partial', 'failed', 'cancelled', 'budget_exhausted'})
    assert run['status'] == expected, run
    assert len(WebDriver.executions) == 1
    assert calls == [1]
    assert research_client.get(base+'/matrix').json()['counts']['current'] == 0


def test_all_retrievals_fail_still_delivers_partial_report(research_client, monkeypatch):
    attempts = []
    async def failed(self):
        attempts.append(self.context.config_snapshot['research_unit']['item_id'])
        self.results = []
        raise AppError(ErrorCode.RETRIEVAL_FAILED, '检索暂不可用',
                       details={'retries_exhausted': True, 'attempts': 3})

    monkeypatch.setattr(WebDriver, 'execute', failed)
    base, run_id = approve(research_client)
    run = wait_run(research_client, run_id, {'completed', 'partial', 'failed', 'budget_exhausted'})
    assert run['status'] == 'partial', run
    matrix = research_client.get(base+'/matrix').json()
    assert matrix['counts']['pending_retry'] == 4
    assert attempts == ['a', 'b', 'a', 'b']
    assert all(c['value'] is None and c['citations'] == [] for c in matrix['cells'])
    assert research_client.get(base).json()['report']['complete'] is False


def test_unusable_search_results_cannot_complete_a_report_without_cited_cells(research_client, monkeypatch):
    async def not_found(self, spec, item, fields, results):
        return [{'item_id': item['id'], 'field_id': f['id'], 'status': 'not_found',
                 'value': None, 'citations': [], 'reason': '已查资料不包含所需信息'} for f in fields]

    monkeypatch.setattr(FixedResearchModel, 'extract', not_found)
    base, run_id = approve(research_client)
    run = wait_run(research_client, run_id, {'completed', 'partial', 'failed', 'budget_exhausted'})
    assert run['status'] == 'partial', run
    study = research_client.get(base).json()
    assert study['report']['complete'] is False
    assert '部分报告' in study['report']['content']
    assert len(WebDriver.executions) == 2


def test_final_sweep_stops_at_remaining_token_budget(research_client, monkeypatch):
    from deepresearch_agent.models.prefix_cache import get_current_run
    original = FixedResearchModel.extract
    calls = Counter()

    async def extract(self, spec, item, fields, results):
        key = (item['id'], fields[0]['id'])
        calls[key] += 1
        cells = await original(self, spec, item, fields, results)
        if key == ('a', 'recovery'):
            cells[0]['citations'][0]['locator'] = 'invalid quote'
        if key == ('b', 'license'):
            await research_client.app.state.run_service.research.store.record_usage(
                get_current_run(), spec['budget']['max_llm_tokens'], 0)
        return cells

    monkeypatch.setattr(FixedResearchModel, 'extract', extract)
    base, run_id = approve(research_client)
    run = wait_run(research_client, run_id, {'completed', 'partial', 'failed', 'budget_exhausted'})
    assert run['status'] == 'budget_exhausted', run
    matrix = research_client.get(base+'/matrix').json()
    assert matrix['counts']['current'] == 3 and matrix['counts']['pending_retry'] == 1
    assert calls[('a', 'recovery')] == 2
    assert len(WebDriver.executions) == 2

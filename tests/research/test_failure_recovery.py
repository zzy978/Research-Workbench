from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult, RetrievalMetadata
from deepresearch_agent.harness.budgets import BudgetExceeded
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.harness.run_context import RunContext
from deepresearch_agent.persistence.repositories import RunRepository
from deepresearch_agent.research.guard import ResearchProvider
from deepresearch_agent.research.workflow import ResearchWorkflow
from deepresearch_agent.retrieval.base import SearchFilters, ToolCallContext
from test_storage import approved, db


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['retrieval', 'extraction'])
async def test_restored_exhausted_attempt_is_deferred_instead_of_silently_skipped(db, stage):
    store, study, run_id = await approved(db)
    await RunRepository(db).update_status(run_id, status='executing')
    unit = {'item': study['spec']['items'][0], 'fields': study['spec']['fields'], 'task_id': 'task_a'}
    counters = {'task_a:retrieval': 2} if stage == 'retrieval' else {'a:cost:extraction': 2}
    result = RetrievalResult(granularity='DO', evidence='observed', source='tavily_search', source_mode='web',
                             metadata=RetrievalMetadata(source_id='https://a.example', source_type='webpage', content_hash='hash'))
    cache = {} if stage == 'retrieval' else {'task_a': {'retrieved': True,
        'results': [result.to_dict()], 'search_log': [{'query': 'A cost'}]}}
    context = RunContext(run_id=run_id, session_id='session', trigger_message_id=run_id,
        source_mode='web', workflow_mode='deep_research', status='executing', original_query='Q',
        workflow_state={'research_units': [unit], 'research_attempts': counters, 'research_unit_cache': cache})

    class FailedDriver:
        async def plan(self):
            raise AppError(ErrorCode.RETRIEVAL_TIMEOUT, '检索超时')

        def execution_records(self):
            return []

        def evidence_results(self):
            return []

    extraction = AsyncMock(return_value=[])
    driver = ResearchWorkflow(context, SimpleNamespace(store=store, database=db, call_model=extraction),
                              lambda *args: FailedDriver(), SimpleNamespace(publish=AsyncMock()))
    await driver.execute()
    cell = (await store.matrix(study['study_id']))['cells'][0]
    assert cell['status'] == 'pending_retry'
    assert cell['failure']['attempts'] == 3
    assert cell['failure']['stage'] == ('validation' if stage == 'extraction' else 'retrieval')
    assert driver.delivery_status()['incomplete_tasks'] == ['task_a']


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [BudgetExceeded('external_calls', 5, 5),
    AppError(ErrorCode.TAVILY_AUTH_FAILED, '认证失效'), AppError(ErrorCode.PERSISTENCE_FAILED, '无法保存')])
async def test_provider_latches_global_failure_even_if_legacy_driver_swallows_it(db, error):
    store, _, run_id = await approved(db)
    await RunRepository(db).update_status(run_id, status='executing')

    class Provider:
        mode = 'web'
        provider_name = 'fixture'
        requests = 0

        async def search(self, *args, **kwargs):
            self.requests += 1
            if self.requests == 1:
                raise error
            return []

    provider = Provider()
    guarded = ResearchProvider(provider, store, run_id)
    for _ in range(2):
        with pytest.raises(type(error)):
            await guarded.search('Q', top_k=1, search_depth='basic', filters=SearchFilters(),
                                 call_context=ToolCallContext(run_id=run_id, source_mode='web'))
    assert provider.requests == 1
    assert guarded.fatal_failure is error

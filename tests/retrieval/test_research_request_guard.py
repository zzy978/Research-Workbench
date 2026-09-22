import pytest

from deepresearch_agent.harness.budgets import BudgetExceeded
from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.retrieval.base import SearchFilters, ToolCallContext
from deepresearch_agent.retrieval.tavily_provider import TavilyProvider


@pytest.mark.asyncio
async def test_retry_reserves_each_external_attempt_before_network(tmp_path, monkeypatch):
    calls = []

    class Client:
        def search(self, **kwargs):
            calls.append(kwargs['query'])
            raise TimeoutError('transport timeout')

    async def no_sleep(_):
        pass

    monkeypatch.setattr('deepresearch_agent.retrieval.tavily_provider.asyncio.sleep', no_sleep)
    reservations = []

    async def guard():
        if len(reservations) == 1:
            raise BudgetExceeded('external_calls', 2, 1)
        reservations.append(1)

    context = ToolCallContext(run_id='r', source_mode=SourceMode.WEB)
    # Set through object.__setattr__ so baseline reaches behavior rather than an import error.
    object.__setattr__(context, 'before_request', guard)
    provider = TavilyProvider(api_key='', client=Client(), cache_dir=tmp_path)
    with pytest.raises(BudgetExceeded):
        await provider.search('query', top_k=1, search_depth='basic', filters=SearchFilters(), call_context=context)
    assert calls == ['query']


@pytest.mark.asyncio
async def test_snippet_is_not_marked_as_read_body(tmp_path):
    class Client:
        def search(self, **kwargs):
            return {'results': [
                {'url': 'https://example.org/a', 'content': 'search snippet'},
                {'url': 'https://example.org/b', 'content': 'summary', 'raw_content': 'actual body'},
            ]}

    provider = TavilyProvider(api_key='', client=Client(), cache_dir=tmp_path)
    results = await provider.search('query', top_k=2, search_depth='basic', filters=SearchFilters(),
                                    call_context=ToolCallContext(run_id='r', source_mode=SourceMode.WEB))
    access = {r.metadata.source_id: r.metadata.extra.get('access') for r in results}
    assert access == {'https://example.org/a': 'snippet', 'https://example.org/b': 'full_text'}


@pytest.mark.asyncio
async def test_empty_cache_hit_is_audited_without_external_reservation(tmp_path):
    observed = []

    class Client:
        def search(self, **kwargs):
            return {'results': []}

    async def reserve():
        observed.append('attempt')

    async def cached():
        observed.append('cache')

    provider = TavilyProvider(api_key='', client=Client(), cache_dir=tmp_path)
    context = ToolCallContext(run_id='r', source_mode=SourceMode.WEB,
                              before_request=reserve, on_cache_hit=cached)
    for _ in range(2):
        assert await provider.search('query', top_k=1, search_depth='basic',
            filters=SearchFilters(), call_context=context) == []
    assert observed == ['attempt', 'cache']

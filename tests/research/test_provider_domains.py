from types import SimpleNamespace

import pytest

from deepresearch_agent.research.guard import ResearchProvider
from deepresearch_agent.retrieval.base import SearchFilters, ToolCallContext
from deepresearch_agent.harness.contracts import SourceMode


@pytest.mark.asyncio
async def test_provider_cannot_broaden_approved_domain_filter(monkeypatch):
    calls = []

    class Store:
        database = None

        async def assert_allowed(self, *args):
            return {'spec': {'allowed_domains': ['official.example']}}

        async def reserve_request(self, *args):
            calls.append('external')

    class Provider:
        mode = SourceMode.WEB
        provider_name = 'test'

        async def search(self, query, **kwargs):
            assert kwargs['filters'].include_domains == ('official.example',)
            await kwargs['call_context'].before_request()
            return [SimpleNamespace(metadata=SimpleNamespace(source_id=url, extra={})) for url in [
                'https://docs.official.example/a', 'https://official.example.evil.test/a']]

    async def run(*args):
        return SimpleNamespace(cancellation_requested=False, status='executing')

    monkeypatch.setattr('deepresearch_agent.research.guard.RunRepository.get', run)
    results = await ResearchProvider(Provider(), Store(), 'run').search('query', top_k=5,
        search_depth='basic', filters=SearchFilters(include_domains=('evil.test',)),
        call_context=ToolCallContext(run_id='run', source_mode=SourceMode.WEB))
    assert len(results) == 1
    assert results[0].metadata.source_id == 'https://docs.official.example/a'
    assert calls == ['external']

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.harness.errors import AppError
from deepresearch_agent.persistence.repositories import RunRepository
from deepresearch_agent.research.guard import ResearchProvider
from deepresearch_agent.research.intelligence import ResearchModel
from deepresearch_agent.research.workflow import ResearchWorkflow
from deepresearch_agent.retrieval.base import SearchFilters, ToolCallContext
from test_storage import approved, db, spec


def source(title, url, text='abstract'):
    return RetrievalResult(granularity='DO', source='tavily_search', source_mode='web',
        evidence=text, metadata=RetrievalMetadata(source_id=url, title=title, source_type='webpage'))


def test_named_papers_still_require_observed_identity_before_matrix():
    from deepresearch_agent.research.retrieval_quality import needs_discovery
    value = spec(items=[{'id': 'chronos', 'name': 'Chronos', 'version': '2024'}], source_policy='论文原文').model_dump()
    assert needs_discovery(value)


@pytest.mark.asyncio
async def test_empty_candidate_scope_keeps_question_specific_field_applicability(monkeypatch):
    model = ResearchModel()
    value = spec(items=[], fields=[{'id': 'cost', 'label': '费用', 'applies_to': ['q1']}]).model_dump()
    monkeypatch.setattr(model, '_json', AsyncMock(return_value={
        'selections': [{'item_id': 'discovered', 'source_index': 0}]}))
    resolved = await model.resolve_candidates(value, [{'title': 'Observed model', 'url': 'https://arxiv.org/abs/1234.12345'}])
    assert resolved['fields'][0]['applies_to'] == ['discovered']


@pytest.mark.asyncio
async def test_discovery_replaces_categories_with_observed_papers_and_preserves_scope(monkeypatch):
    model = ResearchModel()
    original = spec(items=[{'id': 'tsfm', 'name': '时间序列基础模型（具体实例待核实）'}],
                    fields=[{'id': 'cost', 'label': '费用', 'applies_to': ['tsfm']}]).model_dump()
    papers = [{'title': 'Chronos: Learning the Language of Time Series',
               'url': 'https://arxiv.org/abs/2403.07815v2', 'snippet': 'A pretrained forecasting model.'}]
    monkeypatch.setattr(model, '_json', AsyncMock(return_value={
        'selections': [{'item_id': 'tsfm', 'source_index': 0}]}))
    assert hasattr(model, 'resolve_candidates'), 'missing source-grounded candidate discovery'
    resolved = await model.resolve_candidates(original, papers)
    assert resolved['items'][0]['name'] == papers[0]['title']
    assert papers[0]['url'] in resolved['items'][0]['rationale']
    assert resolved['items'][0]['version'] == 'v2'
    for key in ('questions', 'fields', 'budget', 'hard_constraints', 'scope'):
        assert resolved[key] == original[key]
    assert original['items'][0]['name'].startswith('时间序列基础模型')


@pytest.mark.asyncio
async def test_discovery_rejects_unobserved_source_instead_of_inventing_candidate(monkeypatch):
    model = ResearchModel()
    monkeypatch.setattr(model, '_json', AsyncMock(return_value={
        'selections': [{'item_id': 'a', 'source_index': 99}]}))
    assert hasattr(model, 'resolve_candidates'), 'missing candidate validation'
    with pytest.raises(ValueError):
        await model.resolve_candidates(spec().model_dump(), [{'title': 'Observed', 'url': 'https://a.org'}])


@pytest.mark.asyncio
async def test_search_keeps_topic_rewrites_off_topic_results_and_records_actual_queries(db):
    value = spec(title='2023年以来时间序列预测', items=[{'id': 'tsfm_pretrained', 'name': 'Chronos'}],
                 fields=[{'id': 'method_architecture', 'label': '方法架构'}])
    store, study, rid = await approved(db, value)
    await RunRepository(db).update_status(rid, status='executing')
    calls = []
    class Provider:
        mode = 'web'
        provider_name = 'fixture'
        async def search(self, query, **kwargs):
            calls.append(query)
            await kwargs['call_context'].before_request()
            return [source('TRL SFT Trainer', 'https://huggingface.co/docs/trl')] if len(calls) == 1 else [
                source('Chronos', 'https://arxiv.org/abs/2403.07815')]
    assess = AsyncMock(side_effect=[{'relevant_indices': [], 'query': 'Chronos time series forecasting architecture'},
                                    {'relevant_indices': [0], 'query': ''}])
    assert 'assessor' in __import__('inspect').signature(ResearchProvider).parameters, 'missing relevance gate'
    provider = ResearchProvider(Provider(), store, rid, assessor=assess,
        targets={'item_id': 'tsfm_pretrained', 'field_ids': ['method_architecture']})
    results = await provider.search('针对 item tsfm_pretrained 的 method_architecture', top_k=5,
        search_depth='advanced', filters=SearchFilters(), call_context=ToolCallContext(run_id=rid, source_mode='web'))
    assert len(calls) == 2
    assert all('Chronos' in q and '时间序列预测' in q for q in calls)
    assert all('tsfm_pretrained' not in q and 'method_architecture' not in q for q in calls)
    assert results[0].metadata.title == 'Chronos'
    assert results[0].metadata.extra['search_queries'] == calls
    assert (await store.run_usage(rid))['external_calls'] == 2


@pytest.mark.asyncio
async def test_all_irrelevant_results_are_retrieval_failure_not_negative_evidence(db):
    store, _, rid = await approved(db)
    await RunRepository(db).update_status(rid, status='executing')
    raw = SimpleNamespace(mode='web', provider_name='fixture', search=AsyncMock(return_value=[source('unrelated', 'https://a.org')]))
    assert 'assessor' in __import__('inspect').signature(ResearchProvider).parameters, 'missing relevance gate'
    guarded = ResearchProvider(raw, store, rid, targets={'item_id': 'a', 'field_ids': ['cost']},
        assessor=AsyncMock(return_value={'relevant_indices': [], 'query': 'revised'}))
    with pytest.raises(AppError, match='相关'):
        await guarded.search('A cost', top_k=3, search_depth='basic', filters=SearchFilters(),
                             call_context=ToolCallContext(run_id=rid, source_mode='web'))
    assert raw.search.await_count == 2


def test_unavailable_paper_body_is_not_claimed_as_not_found():
    result = source('Time-LLM', 'https://arxiv.org/abs/2310.01728')
    result.metadata.extra.update(access='snippet', full_text_failure='PDF unavailable')
    cell = {'item_id': 'a', 'field_id': 'method', 'status': 'not_found', 'value': None,
            'reason': 'not in supplied sources', 'citations': []}
    with pytest.raises(AppError, match='正文'):
        ResearchWorkflow._validate_cell([cell], {'id': 'a'},
            {'id': 'method', 'required_access': 'full_text'}, [result])


@pytest.mark.asyncio
async def test_production_lazy_router_exposes_document_retrieval(db, tmp_path):
    from deepresearch_agent.harness.contracts import SourceMode
    from deepresearch_agent.retrieval.router import _LazyProvider
    from deepresearch_agent.retrieval.tavily_provider import TavilyProvider
    calls = []
    class Client:
        def search(self, **kwargs):
            calls.append('search')
            return {'results': [{'title': 'Time-LLM', 'url': 'https://arxiv.org/abs/2310.01728v2', 'content': 'Forecasting.'}]}
        def extract(self, urls, **kwargs):
            calls.append('extract')
            return {'results': [{'url': urls[0], 'raw_content': '# Method\n' + 'Patches and reprogramming. ' * 60
                                + '\n# Experiments\n' + 'Forecasting results. ' * 60}]}
    value = spec(items=[{'id': 'a', 'name': 'Time-LLM'}], fields=[{'id': 'method', 'label': '方法', 'required_access': 'full_text'}])
    store, _, rid = await approved(db, value)
    await RunRepository(db).update_status(rid, status='executing')
    raw = _LazyProvider(mode=SourceMode.WEB, provider_name='tavily',
        factory=lambda: TavilyProvider(api_key='', client=Client(), cache_dir=tmp_path/'cache'))
    provider = ResearchProvider(raw, store, rid, targets={'item_id': 'a', 'field_ids': ['method']},
        assessor=AsyncMock(return_value={'relevant_indices': [0], 'query': ''}))
    results = await provider.search('Time-LLM method', top_k=3, search_depth='basic', filters=SearchFilters(),
        call_context=ToolCallContext(run_id=rid, source_mode='web'))
    assert calls == ['search', 'extract']
    assert all(r.metadata.extra['access'] == 'full_text' and r.metadata.extra['section'] for r in results)
    assert (await store.run_usage(rid))['external_calls'] == 2

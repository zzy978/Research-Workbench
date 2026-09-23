import pytest

from deepresearch_agent.harness.budgets import BudgetExceeded
from deepresearch_agent.retrieval.base import ToolCallContext
from deepresearch_agent.retrieval.tavily_provider import TavilyProvider


BODY = '# Introduction\n' + 'Background. ' * 1400 + '\n## 2 Method\nOur forecasting architecture uses patches.\n' + 'Attention details. ' * 150 + '\n## 3 Experiments\nWe use a chronological split and report MSE 0.123.\n' + 'Dataset details. ' * 150


def landing(provider):
    return provider._map_results({'results': [{'title': 'Time-LLM', 'url': 'https://arxiv.org/abs/2310.01728v2',
        'raw_content': 'Title: Time-LLM\nAbstract: forecasting\nAccess Paper\nSubmission history'}]}, artifact=None)


def test_arxiv_abstract_is_not_paper_full_text(tmp_path):
    provider = TavilyProvider(api_key='', client=object(), cache_dir=tmp_path)
    assert landing(provider)[0].metadata.extra['access'] == 'snippet'


@pytest.mark.asyncio
async def test_html_to_pdf_fallback_gets_sections_beyond_old_character_limit(tmp_path):
    calls, reserved = [], []
    class Client:
        def extract(self, urls, **kwargs):
            calls.extend(urls)
            if '/html/' in urls[0]:
                return {'results': [], 'failed_results': [{'url': urls[0]}]}
            return {'results': [{'url': urls[0], 'raw_content': BODY}]}
    async def reserve():
        reserved.append(1)
    provider = TavilyProvider(api_key='', client=Client(), cache_dir=tmp_path)
    assert hasattr(provider, 'read_documents'), 'missing paper body retrieval'
    results = await provider.read_documents(landing(provider), fields=[{'id': 'dataset_and_split', 'label': '数据集与划分'}],
        call_context=ToolCallContext(run_id='r', source_mode='web', before_request=reserve))
    assert calls == ['https://arxiv.org/html/2310.01728v2', 'https://arxiv.org/pdf/2310.01728v2']
    assert len(reserved) == 2
    assert any('chronological split' in str(r.evidence) for r in results)
    assert all(r.metadata.extra['access'] == 'full_text' for r in results)
    assert all(r.metadata.extra['section'] and r.metadata.extra['document_hash'] for r in results)
    assert all(r.metadata.source_id.endswith('/pdf/2310.01728v2') for r in results)


@pytest.mark.asyncio
async def test_document_fetch_cannot_bypass_budget(tmp_path):
    class Client:
        def extract(self, **kwargs):
            pytest.fail('network must not run after budget denial')
    async def reserve():
        raise BudgetExceeded('external_calls', 20, 20)
    provider = TavilyProvider(api_key='', client=Client(), cache_dir=tmp_path)
    assert hasattr(provider, 'read_documents'), 'missing guarded paper retrieval'
    with pytest.raises(BudgetExceeded):
        await provider.read_documents(landing(provider), fields=[], call_context=ToolCallContext(
            run_id='r', source_mode='web', before_request=reserve))


@pytest.mark.asyncio
async def test_failed_body_fetch_keeps_snippet_and_explicit_gap(tmp_path):
    class Client:
        def extract(self, **kwargs):
            return {'results': [], 'failed_results': [{'error': 'unavailable'}]}
    provider = TavilyProvider(api_key='', client=Client(), cache_dir=tmp_path)
    assert hasattr(provider, 'read_documents'), 'missing explicit body failure'
    result = (await provider.read_documents(landing(provider), fields=[],
        call_context=ToolCallContext(run_id='r', source_mode='web')))[0]
    assert result.metadata.extra['access'] == 'snippet'
    assert result.metadata.extra['full_text_failure']


@pytest.mark.asyncio
async def test_error_page_is_not_cached_as_a_successful_document(tmp_path):
    class Client:
        calls = 0
        def extract(self, urls, **kwargs):
            self.calls += 1
            return {'results': [{'url': urls[0], 'raw_content': 'Document temporarily unavailable'}]}
    client = Client()
    provider = TavilyProvider(api_key='', client=client, cache_dir=tmp_path)
    for _ in range(2):
        await provider.read_documents(landing(provider), fields=[], call_context=ToolCallContext(run_id='r', source_mode='web'))
    assert client.calls == 4


def test_pdf_appendix_sections_are_distinct_from_conclusion():
    from deepresearch_agent.retrieval.documents import section_passages
    text = ('5 CONCLUSION\n' + 'We conclude. ' * 50 + '\nB EXPERIMENTAL DETAILS\n'
            + 'Experimental settings. ' * 50 + '\nB.2 DATASET DETAILS\n'
            + 'The chronological training validation test split is 70:10:20. ' * 50)
    passages = section_passages(text, [{'id': 'dataset_and_split', 'label': '数据集与划分'}])
    assert any(p['section'] == 'DATASET DETAILS' and '70:10:20' in p['text'] for p in passages)
    assert not any(p['section'] == 'CONCLUSION' and '70:10:20' in p['text'] for p in passages)


def test_pdf_numeric_table_rows_do_not_become_section_headings():
    from deepresearch_agent.retrieval.documents import section_passages
    text = 'B.1 DATASETS\nWe split data chronologically.\nC.1 w/o Dataset Context 0.402 0.417 0.298 0.331\nResults.'
    passages = section_passages(text, [{'id': 'dataset', 'label': '数据集'}])
    assert all(p['section'] == 'DATASETS' for p in passages)


def test_publisher_landing_pages_follow_observed_pdf_links():
    from deepresearch_agent.retrieval.documents import document_urls, is_paper_url, is_landing_page
    url = 'https://proceedings.mlr.press/v202/example23a.html'
    assert is_paper_url(url)
    assert document_urls(url, '[Download PDF](example23a/example23a.pdf)')[0] == 'https://proceedings.mlr.press/v202/example23a/example23a.pdf'
    assert is_landing_page('https://dl.acm.org/doi/abs/10.1145/123')
    assert document_urls(url, '[PDF](javascript:alert)')[0] == url


@pytest.mark.asyncio
async def test_cached_document_still_checks_cancellation_and_preserves_artifacts(tmp_path):
    from deepresearch_agent.harness.errors import RunCancelled
    from deepresearch_agent.persistence.artifact_store import ArtifactStore
    from deepresearch_agent.retrieval.base import SearchFilters
    class Client:
        calls = 0
        def extract(self, urls, **kwargs):
            self.calls += 1
            return {'results': [{'url': urls[0], 'raw_content': BODY}]}
    client = Client()
    provider = TavilyProvider(api_key='', client=client, cache_dir=tmp_path/'cache', artifact_store=ArtifactStore(tmp_path/'artifacts'))
    context = ToolCallContext(run_id='r', source_mode='web', tool_call_id='call')
    fields = [{'id': 'dataset', 'label': '数据集'}]
    first = await provider.read_documents(landing(provider), fields=fields, call_context=context)
    path = tmp_path/'artifacts'/first[0].metadata.extra['artifact_path']
    original = path.read_bytes()
    await provider.read_documents(landing(provider), fields=fields, call_context=context)
    assert client.calls == 1
    assert path.read_bytes() == original
    async def cancelled():
        raise RunCancelled('cancelled')
    with pytest.raises(RunCancelled):
        await provider.read_documents(landing(provider), fields=fields,
            call_context=ToolCallContext(run_id='r', source_mode='web', on_cache_hit=cancelled))
    assert await provider.read_documents(landing(provider), fields=fields, call_context=context,
        filters=SearchFilters(include_domains=('example.org',))) == []

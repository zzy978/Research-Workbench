"""Opt-in live smoke test, isolated from the user's studies and application DB."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deepresearch_agent.config.settings import TAVILY_API_KEY
from deepresearch_agent.harness.evidence import EvidenceLedger
from deepresearch_agent.persistence.artifact_store import ArtifactStore
from deepresearch_agent.research.intelligence import ResearchModel
from deepresearch_agent.research.retrieval_quality import search_query
from deepresearch_agent.research.schemas import ResearchSpec
from deepresearch_agent.research.workflow import ResearchWorkflow
from deepresearch_agent.retrieval.base import SearchFilters, ToolCallContext
from deepresearch_agent.retrieval.tavily_provider import TavilyProvider


async def verify(output):
    output.mkdir(parents=True, exist_ok=True)
    run_id = 'retrieval_live_' + uuid4().hex
    provider = TavilyProvider(api_key=TAVILY_API_KEY, cache_dir=output/'cache',
                              artifact_store=ArtifactStore(output/'artifacts'))
    model = ResearchModel()
    calls = 0

    async def reserve():
        nonlocal calls
        if calls >= 8:
            raise RuntimeError('Live probe exceeded eight external requests')
        calls += 1

    context = ToolCallContext(run_id=run_id, source_mode='web', before_request=reserve)
    spec = ResearchSpec.model_validate({'title': '2023年以来基于大语言模型的时间序列预测',
        'questions': ['具体模型的方法机制是什么？'],
        'items': [{'id': 'llm_forecast', 'name': '基于大语言模型的时间序列预测方法（具体实例待核实）'}],
        'fields': [{'id': 'method_architecture', 'label': '方法与架构', 'required_access': 'full_text'}],
        'allowed_domains': ['arxiv.org'],
        'queries': ['time series forecasting large language models reprogramming'],
    }).model_dump(mode='json')
    discoveries = await provider.search(spec['queries'][0], top_k=3, search_depth='advanced',
        filters=SearchFilters(include_domains=('arxiv.org',)), call_context=context)
    spec = await model.resolve_candidates(spec, [{'title': r.metadata.title, 'url': r.metadata.source_id,
        'snippet': str(r.evidence)[:1500]} for r in discoveries])
    assert '身份来源：' in spec['items'][0]['rationale'], 'No concrete candidate was discovered'
    item, field = spec['items'][0], spec['fields'][0]
    query = search_query(spec, item, [field], 'item llm_forecast method_architecture')
    results = await provider.search(query, top_k=3, search_depth='advanced',
        filters=SearchFilters(include_domains=('arxiv.org',)), call_context=context)
    assessment = await model.assess_sources(spec, item, [field], query, results)
    results = [results[i] for i in assessment['relevant_indices']]
    assert results, 'No relevant source returned'
    documents = await provider.read_documents(results[:1], fields=[field], filters=SearchFilters(include_domains=('arxiv.org',)),
                                              call_context=context)
    assert any(r.metadata.extra.get('access') == 'full_text' for r in documents), 'No paper body retrieved'
    EvidenceLedger().assign(run_id=run_id, task_id='probe', tool_call_id=None, provider='tavily', results=documents)
    cells = await model.extract(spec, item, [field], documents)
    cell = ResearchWorkflow._validate_cell(cells, item, field, documents)
    assert cell['status'] == 'supported' and cell.get('citations'), 'No grounded method evidence extracted'
    record = {'ok': True, 'run_id': run_id, 'external_requests': calls,
        'candidate': item, 'query': query, 'documents': [r.to_dict() for r in documents], 'cell': cell}
    path = output/'result.json'
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'ok': True, 'external_requests': calls, 'candidate': item['name'],
                      'sections': [r.metadata.extra.get('section') for r in documents],
                      'status': cell['status'], 'result': str(path.resolve())}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True, help='Use configured model and Tavily APIs')
    parser.add_argument('--output', type=Path, required=True, help='Isolated output directory')
    args = parser.parse_args()
    asyncio.run(verify(args.output))

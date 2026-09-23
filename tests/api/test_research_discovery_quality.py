from copy import deepcopy
from types import SimpleNamespace

from test_research_workbench import SPEC, FixedResearchModel, research_client, start, wait_run
from deepresearch_agent.research.retrieval_quality import apply_candidates
from deepresearch_agent.retrieval.tavily_provider import TavilyProvider


class CategoryModel(FixedResearchModel):
    async def draft(self, query, **kwargs):
        spec = deepcopy(SPEC)
        spec['items'] = [{'id': 'a', 'name': '时间序列基础模型（具体实例待核实）'}]
        spec['queries'] = ['time series forecasting foundation models']
        return spec

    async def resolve_candidates(self, spec, sources):
        return apply_candidates(spec, sources, [{'item_id': 'a', 'source_index': 0}])


def test_category_is_resolved_before_scope_approval(research_client, tmp_path):
    class Client:
        def search(self, **kwargs):
            return {'results': [{'title': 'Chronos: Learning the Language of Time Series',
                'url': 'https://arxiv.org/abs/2403.07815v2', 'content': 'Pretrained time series forecasting.'}]}
    service = research_client.app.state.run_service
    service.research_model = CategoryModel()
    provider = TavilyProvider(api_key='', client=Client(), cache_dir=tmp_path/'search')
    service._router = SimpleNamespace(for_mode=lambda mode: provider)
    created = start(research_client)
    run = wait_run(research_client, created['run_id'], {'awaiting_scope_approval', 'failed'})
    assert run['status'] == 'awaiting_scope_approval', run
    study = research_client.get('/api/v1/research/' + run['study_id']).json()
    assert study['spec']['items'][0]['name'].startswith('Chronos:')
    assert study['approved_revision'] is None
    assert study['usage']['discovery_calls'] == 1
    assert study['spec']['fields'] == SPEC['fields']


def test_unresolved_category_cannot_start_evidence_extraction(research_client):
    service = research_client.app.state.run_service
    service.research_model = CategoryModel()
    service._router = None
    created = start(research_client)
    run = wait_run(research_client, created['run_id'], {'awaiting_scope_approval', 'failed'})
    study = research_client.get('/api/v1/research/' + run['study_id']).json()
    response = research_client.post('/api/v1/research/' + run['study_id'] + '/approve', json={
        'revision': study['current_revision'], 'fingerprint': study['fingerprint'], 'client_request_id': 'approve'})
    assert response.status_code == 409
    assert '具体论文' in response.text

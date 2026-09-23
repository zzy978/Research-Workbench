import json
from types import SimpleNamespace

import pytest

from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.research.failures import CellValidationError, failure_detail
from deepresearch_agent.research.intelligence import ResearchModel
from deepresearch_agent.research.workflow import ResearchWorkflow


ITEM = {'id': 'react', 'name': 'ReAct'}
FIELD = {'id': 'capability_evidence', 'label': '能力与评测证据', 'required_access': 'full_text'}
SPEC = {'questions': ['AI Agent 发展历程'], 'hard_constraints': []}


def evidence():
    return RetrievalResult(granularity='DO', evidence='Verified benchmark result.', source='tavily_search',
        source_mode='web', metadata=RetrievalMetadata(source_id='https://example.org/paper',
            source_type='webpage', content_hash='actual-hash', extra={'access': 'full_text'}))


def content(result):
    return {'status': 'supported', 'value': '已报告评测结果', 'reason': '原文结果',
            'citations': [{'evidence_id': result.result_id, 'locator': 'Verified benchmark result.'}]}


class JsonLLM:
    model_name = 'fixture'

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


@pytest.mark.asyncio
async def test_single_content_gets_host_identity_and_verified_metadata(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = evidence()
    llm = JsonLLM(content(source))
    cells = await ResearchModel(llm).extract(SPEC, ITEM, [FIELD], [source])
    assert len(cells) == 1
    assert cells[0]['item_id'] == ITEM['id']
    assert cells[0]['field_id'] == FIELD['id']
    assert cells[0]['citations'][0]['content_hash'] == 'actual-hash'
    assert cells[0]['citations'][0]['access'] == 'full_text'
    assert 'JSON Schema' in llm.calls[0][1][1]
    assert not (tmp_path / 'data/research_errors').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad,expected_path', [
    ({'cells': []}, 'status'),
    ({'item_id': 'other'}, 'item_id'),
    ({'status': 'pending_retry'}, 'status'),
    ({'citations': 'not a list'}, 'citations'),
])
async def test_schema_failure_preserves_expected_actual_and_raw_output(tmp_path, monkeypatch, bad, expected_path):
    monkeypatch.chdir(tmp_path)
    source = evidence()
    payload = bad if 'cells' in bad else content(source) | bad
    with pytest.raises(CellValidationError) as caught:
        await ResearchModel(JsonLLM(payload)).extract(SPEC, ITEM, [FIELD], [source])
    detail = failure_detail(caught.value, 'validation', 1)
    issue = next(i for i in detail['validation_errors'] if i['path'] == expected_path)
    assert 'expected' in issue and 'actual' in issue
    assert '要求' in str(caught.value) and '实际' in str(caught.value)
    logs = list((tmp_path / 'data/research_errors').glob('*.json'))
    assert len(logs) == 1
    log = json.loads(logs[0].read_text(encoding='utf-8'))
    assert log['operation'] == 'extract'
    assert log['parsed_response'] == payload
    assert json.loads(log['raw_response']) == payload
    assert log['validation_errors'] == detail['validation_errors']


@pytest.mark.asyncio
async def test_bad_quote_logs_exact_failure_and_feedback_reaches_next_prompt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = evidence()
    payload = content(source)
    payload['citations'][0]['locator'] = 'fabricated quote'
    llm = JsonLLM(payload)
    model = ResearchModel(llm)
    with pytest.raises(CellValidationError) as caught:
        await model.extract(SPEC, ITEM, [FIELD], [source])
    detail = failure_detail(caught.value, 'validation', 1)
    issue = detail['validation_errors'][0]
    assert issue['path'] == 'citations.0.locator'
    assert issue['actual'] == 'fabricated quote'
    assert source.result_id in str(issue['expected'])
    log = json.loads(next((tmp_path / 'data/research_errors').glob('*.json')).read_text(encoding='utf-8'))
    assert log['parsed_response'] == payload
    llm.payload = content(source)
    cells = await model.extract(SPEC | {'_extraction_feedback': detail}, ITEM, [FIELD], [source])
    assert cells[0]['status'] == 'supported'
    assert json.dumps(detail, ensure_ascii=False) in llm.calls[1][1][1]


def test_host_validation_reports_wrong_identity_and_cardinality():
    source = evidence()
    cell = content(source) | {'item_id': 'other', 'field_id': FIELD['id']}
    with pytest.raises(CellValidationError) as caught:
        ResearchWorkflow._validate_cell([cell], ITEM, FIELD, [source])
    issue = caught.value.details['validation_errors'][0]
    assert issue == {'path': 'item_id', 'expected': 'react', 'actual': 'other'}
    with pytest.raises(CellValidationError) as caught:
        ResearchWorkflow._validate_cell([], ITEM, FIELD, [source])
    assert caught.value.details['validation_errors'][0] == {'path': 'cells.length', 'expected': 1, 'actual': 0}


@pytest.mark.asyncio
async def test_abstract_cannot_be_promoted_to_full_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = evidence()
    source.metadata.extra['access'] = 'snippet'
    with pytest.raises(CellValidationError) as caught:
        await ResearchModel(JsonLLM(content(source))).extract(SPEC, ITEM, [FIELD], [source])
    assert caught.value.details['validation_errors'][0]['expected'] == 'full_text'
    assert caught.value.details['validation_errors'][0]['actual'] == 'snippet'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,path', [
    ('unknown_source', 'citations.0.evidence_id'),
    ('empty_citations', 'conclusion'),
    ('negative_with_value', 'value'),
    ('conflict_one_source', 'citations.independent_sources'),
    ('wrong_locator_type', 'citations.0.locator'),
    ('multiple_cells', '$'),
])
async def test_invalid_evidence_and_shapes_are_rejected_with_specific_feedback(tmp_path, monkeypatch, kind, path):
    monkeypatch.chdir(tmp_path)
    source = evidence()
    payload = content(source)
    if kind == 'unknown_source':
        payload['citations'][0]['evidence_id'] = 'invented'
    elif kind == 'empty_citations':
        payload['citations'] = []
    elif kind == 'negative_with_value':
        payload['status'] = 'not_found'
    elif kind == 'conflict_one_source':
        payload['status'] = 'conflict'
    elif kind == 'wrong_locator_type':
        payload['citations'][0]['locator'] = 123
    else:
        payload = [payload, payload]
    with pytest.raises(CellValidationError) as caught:
        await ResearchModel(JsonLLM(payload)).extract(SPEC, ITEM, [FIELD], [source])
    assert caught.value.details['validation_errors'][0]['path'] == path


@pytest.mark.asyncio
async def test_invalid_json_is_logged_and_can_be_used_as_retry_feedback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class BrokenLLM:
        async def ainvoke(self, messages):
            return SimpleNamespace(content='{"status":')

    with pytest.raises(CellValidationError) as caught:
        await ResearchModel(BrokenLLM()).extract(SPEC, ITEM, [FIELD], [evidence()])
    issue = failure_detail(caught.value, 'validation', 1)['validation_errors'][0]
    assert issue['path'] == '$'
    assert issue['actual']['text_near_error'] == '{"status":'
    log = json.loads(next((tmp_path / 'data/research_errors').glob('*.json')).read_text(encoding='utf-8'))
    assert log['raw_response'] == '{"status":'


@pytest.mark.asyncio
async def test_extractor_rejects_batch_before_calling_model():
    llm = JsonLLM({})
    with pytest.raises(CellValidationError):
        await ResearchModel(llm).extract(SPEC, ITEM, [FIELD, FIELD], [evidence()])
    assert llm.calls == []


@pytest.mark.asyncio
async def test_quote_from_unshown_tail_is_rejected_without_mutating_cached_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = evidence()
    source.evidence = 'x' * 12000 + 'Verified benchmark result.'
    llm = JsonLLM(content(source))
    with pytest.raises(CellValidationError, match='证据定位'):
        await ResearchModel(llm).extract(SPEC, ITEM, [FIELD], [source])
    assert source.evidence.endswith('Verified benchmark result.')
    shown = json.loads(llm.calls[0][1][1].split('\n来源: ')[1])
    assert len(shown[0]['text']) == 12000

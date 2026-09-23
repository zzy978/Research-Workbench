import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from deepresearch_agent.models.prefix_cache import set_current_run
from deepresearch_agent.research.intelligence import ResearchModel


class FakeLLM:
    model_name = 'test-model'

    def __init__(self, content):
        self.content = content

    async def ainvoke(self, messages):
        return SimpleNamespace(content=self.content)


@pytest.mark.asyncio
async def test_invalid_reference_preserves_full_response_and_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payload = {'questions': ['问题'], 'items': [{'id': 'timesnet', 'name': 'TimesNet'}],
               'fields': [{'id': 'metrics', 'label': '指标', 'applies_to': ['TimesNet']}]}
    raw = '```json\n' + json.dumps(payload, ensure_ascii=False) + '\n```'
    set_current_run('run_diagnostic_test')
    try:
        with pytest.raises(ValidationError):
            await ResearchModel(FakeLLM(raw)).draft('比较模型')
    finally:
        set_current_run(None)
    paths = list((tmp_path / 'data/research_errors').glob('*.json'))
    assert len(paths) == 1
    log = json.loads(paths[0].read_text(encoding='utf-8'))
    assert log['raw_response'] == raw
    assert log['parsed_response'] == payload
    assert log['run_id'] == 'run_diagnostic_test'
    assert log['model'] == 'test-model'
    assert log['operation'] == 'draft'
    assert '比较模型' in log['prompt']
    assert log['validation_errors'][0]['input'] == payload
    assert '字段适用范围含未知对象' in log['traceback']


@pytest.mark.asyncio
async def test_invalid_json_saved_without_overwriting_previous_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for _ in range(2):
        with pytest.raises(json.JSONDecodeError):
            await ResearchModel(FakeLLM('{broken')).draft('问题')
    paths = list((tmp_path / 'data/research_errors').glob('*.json'))
    assert len(paths) == 6
    for path in paths:
        log = json.loads(path.read_text(encoding='utf-8'))
        assert log['raw_response'] == '{broken'
        assert log['parsed_response'] is None
        assert log['error_type'] == 'JSONDecodeError'


@pytest.mark.asyncio
async def test_log_write_failure_does_not_replace_original_error(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'data').write_text('blocks directory creation')
    with pytest.raises(json.JSONDecodeError):
        await ResearchModel(FakeLLM('{broken')).draft('问题')
    assert '无法保存研究错误日志' in caplog.text


@pytest.mark.asyncio
async def test_valid_outline_does_not_create_error_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = await ResearchModel(FakeLLM('{"questions": ["问题"]}')).draft('问题')
    assert result['questions'] == ['问题']
    assert not (tmp_path / 'data/research_errors').exists()


class SequenceLLM:
    model_name = 'test-model'

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(content=response)


@pytest.mark.asyncio
async def test_missing_scope_brace_is_repaired_and_validated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    valid = '{"questions":["对比"],"scope":{"exclusion":[]},"source_policy":"官方来源"}'
    broken = valid.replace('[]}', '[]')
    llm = SequenceLLM([broken, valid])
    result = await ResearchModel(llm).draft('对比')
    assert result['scope']['exclusion'] == []
    assert result['source_policy'] == '官方来源'
    repair = llm.calls[1][1][1]
    assert 'JSONDecodeError' in repair
    assert '不得改变' in repair
    assert json.dumps(broken, ensure_ascii=False) in repair
    assert len(list((tmp_path / 'data/research_errors').glob('*.json'))) == 1


@pytest.mark.asyncio
async def test_repairs_stop_after_two_attempts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    llm = SequenceLLM(['{first', '{second', '{third', '{unused'])
    with pytest.raises(json.JSONDecodeError):
        await ResearchModel(llm).draft('问题')
    assert len(llm.calls) == 3
    logs = [json.loads(p.read_text(encoding='utf-8')) for p in (tmp_path / 'data/research_errors').glob('*.json')]
    assert sorted(log['attempt'] for log in logs) == [1, 2, 3]
    assert len({log['call_id'] for log in logs}) == 1


@pytest.mark.asyncio
async def test_repaired_json_still_requires_full_schema_validation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    invalid = json.dumps({'questions': ['Q'], 'fields': [
        {'id': 'f', 'label': 'F', 'applies_to': ['unknown']}]})
    llm = SequenceLLM(['{broken', invalid])
    with pytest.raises(ValidationError, match='字段适用范围含未知对象'):
        await ResearchModel(llm).draft('问题')
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_provider_errors_are_not_json_retries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    llm = SequenceLLM([RuntimeError('provider unavailable')])
    with pytest.raises(RuntimeError, match='provider unavailable'):
        await ResearchModel(llm).draft('问题')
    assert len(llm.calls) == 1

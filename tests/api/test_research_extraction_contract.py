"""Run the production extractor through the scheduler and durable matrix."""
import json
from collections import Counter
from types import SimpleNamespace

import pytest

from deepresearch_agent.research.intelligence import ResearchModel
from test_research_failure_isolation import approve
from test_research_workbench import FixedResearchModel, WebDriver, research_client, wait_run


@pytest.mark.parametrize('recover', [True, False])
def test_detailed_validation_feedback_reaches_real_extractor(research_client, monkeypatch, tmp_path, recover):
    monkeypatch.chdir(tmp_path)
    attempts = Counter()
    feedback = []

    class LLM:
        model_name = 'contract-fixture'

        async def ainvoke(self, messages):
            prompt = messages[1][1]
            item = json.loads(prompt.split('\n对象: ')[1].split('\n当前字段: ')[0])
            field = json.loads(prompt.split('\n当前字段: ')[1].split('\n上次抽取失败原因')[0])
            key = (item['id'], field['id'])
            attempts[key] += 1
            source = json.loads(prompt.split('\n来源: ')[1])[0]
            payload = {'status': 'supported', 'value': '有原文依据', 'reason': '原文报告',
                       'citations': [{'evidence_id': source['evidence_id'], 'locator': 'API integrated evidence 1'}]}
            if key == ('a', 'recovery'):
                if attempts[key] > 1:
                    marker = prompt.split('\n上次抽取失败原因')[1].split(': ', 1)[1].split('\n来源: ')[0]
                    feedback.append(json.loads(marker))
                if not recover or attempts[key] == 1:
                    payload['status'] = 'pending_retry'
            return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))

    model = ResearchModel(LLM())

    async def extract(self, *args):
        return await model.extract(*args)

    monkeypatch.setattr(FixedResearchModel, 'extract', extract)
    base, run_id = approve(research_client)
    run = wait_run(research_client, run_id, {'completed', 'partial', 'failed', 'budget_exhausted'})
    assert run['status'] == ('completed' if recover else 'partial'), run
    assert attempts[('a', 'recovery')] == (2 if recover else 3)
    assert len(WebDriver.executions) == 2
    assert attempts[('a', 'license')] == attempts[('b', 'recovery')] == attempts[('b', 'license')] == 1
    issue = feedback[0]['validation_errors'][0]
    assert issue['path'] == 'status'
    assert 'supported' in issue['expected']['enum']
    assert issue['actual'] == 'pending_retry'
    matrix = research_client.get(base+'/matrix').json()
    cell = next(c for c in matrix['cells'] if c['item_id'] == 'a' and c['field_id'] == 'recovery')
    assert cell['status'] == ('supported' if recover else 'pending_retry')
    if not recover:
        assert cell['failure']['validation_errors'][0] == issue
        assert cell['failure']['stage'] == 'validation'
        assert cell['failure']['attempts'] == 3
        assert cell['value'] is None and cell['citations'] == []
    logs = [json.loads(p.read_text(encoding='utf-8')) for p in (tmp_path/'data/research_errors').glob('*.json')]
    assert len(logs) == (1 if recover else 3)
    assert all(log['run_id'] == run_id and log['operation'] == 'extract' for log in logs)
    assert all(log['validation_errors'][0] == issue for log in logs)

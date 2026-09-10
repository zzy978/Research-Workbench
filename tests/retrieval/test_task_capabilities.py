import json
from types import SimpleNamespace

import pytest

from deepresearch_agent.config import settings
from deepresearch_agent.agents.multi_agent.core.plan_spec import TaskGraph, TaskNode
from deepresearch_agent.agents.multi_agent.planner.task_decomposer import TaskDecomposer
from deepresearch_agent.agents.multi_agent.planner.plan_reviewer import PlanReviewer


class ReplyLLM:
    def __init__(self, reply):
        self.reply, self.prompt = reply, None

    def bind(self, **kwargs):
        return self

    def invoke(self, prompt):
        self.prompt = prompt
        return SimpleNamespace(content=json.dumps(self.reply))


@pytest.mark.parametrize('mode,backend,task', [('web','hybrid','web_search'), ('graphrag','hybrid','hybrid_search')])
def test_prompt_and_reviewer_only_advertise_actual_backend(monkeypatch, mode, backend, task):
    monkeypatch.setattr(settings, 'PRIVATE_RETRIEVAL_BACKEND', backend)
    llm = ReplyLLM({'nodes':[{'task_type':task,'description':'Find original sources'}]})
    graph = TaskDecomposer(llm=llm).decompose('Compare sources', source_mode=mode).task_graph
    assert task in llm.prompt
    assert 'local_search' not in llm.prompt and 'global_search' not in llm.prompt
    llm = ReplyLLM({'problem_statement':{'original_query':'Compare sources'}})
    PlanReviewer(llm=llm).review(original_query='Compare sources',refined_query=None,
        task_graph=graph,assumptions=[],source_mode=mode)
    assert task in llm.prompt
    assert 'global_search' not in llm.prompt


def test_new_hybrid_plan_rejects_graph_path_instead_of_relabeling(monkeypatch):
    monkeypatch.setattr(settings, 'PRIVATE_RETRIEVAL_BACKEND', 'hybrid')
    llm = ReplyLLM({'nodes':[{'task_type':'chain_exploration','description':'Trace causal graph paths'}]})
    with pytest.raises(ValueError, match='chain_exploration'):
        TaskDecomposer(llm=llm).decompose('Trace paths')


@pytest.mark.parametrize('task_type,depends_on', [('global_search', []), ('invented_tool', []), ('global_search', ['missing'])])
def test_review_cannot_reintroduce_unsupported_task(monkeypatch, task_type, depends_on):
    monkeypatch.setattr(settings, 'PRIVATE_RETRIEVAL_BACKEND', 'hybrid')
    llm = ReplyLLM({'problem_statement':{'original_query':'Compare sources'},
        'task_graph':{'nodes':[{'task_type':task_type,'description':'Global community summary','depends_on':depends_on}]}})
    with pytest.raises(ValueError, match=task_type):
        PlanReviewer(llm=llm).review(original_query='Compare sources',refined_query=None,
            task_graph=TaskGraph(nodes=[TaskNode(task_type='hybrid_search',description='Retrieve')]),assumptions=[])


def test_fusion_retrieves_again_after_corpus_changes(monkeypatch):
    from deepresearch_agent.agents import fusion_agent as module
    state = {'answer':'before update','calls':0}
    def process(*args, **kwargs):
        state['calls'] += 1
        return {'response':state['answer']}
    monkeypatch.setattr(module, 'MultiAgentFacade', lambda **kw: SimpleNamespace(process_query=process))
    agent = module.FusionGraphRAGAgent()
    assert agent.ask('query','session-A') == 'before update'
    state['answer'] = 'after update'
    assert agent.ask('query','session-A') == 'after update'
    assert agent.ask('query','session-B') == 'after update'
    assert state['calls'] == 3


@pytest.mark.asyncio
async def test_fusion_stream_uses_current_sources(monkeypatch):
    from deepresearch_agent.agents import fusion_agent as module
    state = {'answer':'first answer'}
    monkeypatch.setattr(module, 'MultiAgentFacade', lambda **kw: SimpleNamespace(
        process_query=lambda *a, **k: {'response':state['answer']}))
    agent = module.FusionGraphRAGAgent()
    assert agent.ask('query') == 'first answer'
    state['answer'] = 'updated answer'
    assert ''.join([part async for part in agent.ask_stream('query')]) == 'updated answer'


@pytest.mark.parametrize('configured,graph_support,task', [('hybrid',True,'chain_exploration'), ('graphrag',False,'hybrid_search')])
def test_injected_provider_controls_planning_capabilities(monkeypatch, configured, graph_support, task):
    from deepresearch_agent.agents.multi_agent.planner.base_planner import BasePlanner
    from deepresearch_agent.agents.multi_agent.planner.clarifier import ClarificationResult
    from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
    monkeypatch.setattr(settings, 'PRIVATE_RETRIEVAL_BACKEND', configured)
    class PlannerLLM:
        def bind(self, **kwargs):
            return self
        def invoke(self, prompt):
            if '审校' in prompt:
                response = {'problem_statement':{'original_query':'比较原文证据'}}
            else:
                response = {'nodes':[{'task_type':task,'description':'比较原文证据'}]}
            assert f'- {task}:' in prompt
            if not graph_support:
                assert 'chain_exploration' not in prompt
            return SimpleNamespace(content=json.dumps(response))
    planner = BasePlanner(llm=PlannerLLM(),
        clarifier=SimpleNamespace(analyze=lambda context: ClarificationResult(needs_clarification=False)),
        retrieval_provider=SimpleNamespace(mode='graphrag', supports_graph=graph_support))
    result = planner.generate_plan(PlanExecuteState(input='比较原文证据'))
    assert result.plan_spec.task_graph.nodes[0].task_type == task


def test_legacy_translation_records_original_strategy(monkeypatch):
    monkeypatch.setattr(settings, 'PRIVATE_RETRIEVAL_BACKEND', 'hybrid')
    graph = object.__new__(TaskDecomposer)._build_task_graph({'nodes':[
        {'task_type':'chain_exploration','description':'Old graph task'}]})
    assert graph.nodes[0].task_type == 'hybrid_search'
    assert graph.nodes[0].parameters['legacy_task_type'] == 'chain_exploration'

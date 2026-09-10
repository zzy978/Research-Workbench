from types import SimpleNamespace
import sys

import pytest

from deepresearch_agent.config import settings
from deepresearch_agent.harness.contracts import SourceMode


def test_hybrid_backend_preserves_multi_round_research_tasks(monkeypatch):
    from deepresearch_agent.config import settings
    from deepresearch_agent.agents.multi_agent.planner.task_decomposer import TaskDecomposer
    from deepresearch_agent.agents.multi_agent.planner.base_planner import BasePlanner
    from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec, ProblemStatement
    monkeypatch.setattr(settings, "PRIVATE_RETRIEVAL_BACKEND", "hybrid")
    decomposer = object.__new__(TaskDecomposer)
    graph = decomposer._build_task_graph({"nodes": [
        {"task_type": "deep_research", "description": "Investigate multiple questions"},
        {"task_type": "deeper_research", "description": "Continue research"},
    ]})
    assert [n.task_type for n in graph.nodes] == ["deep_research", "deep_research"]
    plan = PlanSpec(problem_statement=ProblemStatement(original_query="research"), task_graph=graph)
    BasePlanner._enforce_source_mode(plan, "graphrag")
    assert [n.task_type for n in plan.task_graph.nodes] == ["deep_research", "deep_research"]
from deepresearch_agent.agents.multi_agent.planner.task_decomposer import TaskDecomposer


@pytest.mark.parametrize("strategy", ["local_search", "global_search", "naive_search", "chain_exploration"])
def test_default_private_planner_normalizes_graph_strategies(monkeypatch, strategy):
    monkeypatch.setattr(settings, "PRIVATE_RETRIEVAL_BACKEND", "hybrid", raising=False)
    decomposer = object.__new__(TaskDecomposer)
    graph = decomposer._build_task_graph({"nodes": [{"task_type": strategy, "description": "查找原文"}]})
    assert graph.nodes[0].task_type == "hybrid_search"


def test_explicit_graph_planner_keeps_specialist(monkeypatch):
    monkeypatch.setattr(settings, "PRIVATE_RETRIEVAL_BACKEND", "graphrag", raising=False)
    graph = object.__new__(TaskDecomposer)._build_task_graph(
        {"nodes": [{"task_type": "chain_exploration", "description": "查找关系"}]}
    )
    assert graph.nodes[0].task_type == "chain_exploration"


def test_hybrid_deep_tool_initializes_without_graph(monkeypatch):
    from deepresearch_agent.search.tool import deep_research_tool as module

    def initialize(self, **kwargs):
        assert kwargs["enable_vector_cache"] is False
        self.llm = SimpleNamespace()

    monkeypatch.setattr(module.BaseSearchTool, "__init__", initialize)
    provider = SimpleNamespace(mode=SourceMode.GRAPHRAG, provider_name="hybrid", supports_graph=False)
    tool = module.DeepResearchTool(provider=provider)
    assert tool._graph_keywords is None


def test_hybrid_agent_disables_deeper_and_graph_cache(monkeypatch):
    from deepresearch_agent.agents import deep_research_agent as module

    seen = {}
    graph_calls = []
    def forbidden(**kwargs):
        graph_calls.append(kwargs)
        raise AssertionError("graph specialist must not be constructed")
    monkeypatch.setitem(sys.modules, "deepresearch_agent.search.tool.deeper_research_tool", SimpleNamespace(DeeperResearchTool=forbidden))
    monkeypatch.setattr(module.BaseAgent, "__init__", lambda self, **kw: seen.update(kw))
    monkeypatch.setattr(module, "DeepResearchTool", lambda **kw: SimpleNamespace(get_thinking_stream_tool=lambda: None))
    provider = SimpleNamespace(mode=SourceMode.GRAPHRAG, provider_name="hybrid", supports_graph=False)
    agent = module.DeepResearchAgent(retrieval_provider=provider)
    assert not agent.use_deeper_tool
    assert graph_calls == []
    assert seen["enable_vector_cache"] is False
    agent.is_deeper_tool(True)
    assert graph_calls == []


def test_default_deep_tool_resolves_router_provider(monkeypatch):
    from deepresearch_agent.search.tool import deep_research_tool as module
    from deepresearch_agent.retrieval import router
    provider = SimpleNamespace(mode=SourceMode.GRAPHRAG, supports_graph=False)
    monkeypatch.setattr(router, "create_default_router", lambda: router.RetrievalRouter({SourceMode.GRAPHRAG: provider}))
    def initialize(self, **kwargs):
        self.llm = SimpleNamespace()
    monkeypatch.setattr(module.BaseSearchTool, "__init__", initialize)
    assert module.DeepResearchTool().retrieval_provider is provider


@pytest.mark.parametrize("strategy", ["hybrid_search", "local_search", "chain_exploration"])
def test_real_executor_preserves_original_evidence_without_graph(strategy):
    from deepresearch_agent.agents.multi_agent.executor.retrieval_executor import RetrievalExecutor
    from deepresearch_agent.agents.multi_agent.core.plan_spec import TaskNode, PlanExecutionSignal
    from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
    from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult, RetrievalMetadata

    class Provider:
        mode = SourceMode.GRAPHRAG
        provider_name = "hybrid"
        supports_graph = False

        async def search(self, query, **kwargs):
            assert kwargs["filters"].strategy == "hybrid_search"
            return [RetrievalResult(granularity="Chunk", evidence="Original document passage.",
                source="hybrid_search", metadata=RetrievalMetadata(source_id="manual.txt#0", source_type="chunk",
                extra={"char_start": 0, "char_end": 26, "document_id": "manual.txt"}))]

    task = TaskNode(task_id="task_1", task_type=strategy, description="Find original document")
    state = PlanExecuteState(input=task.description)
    signal = PlanExecutionSignal(plan_id="p1", version=1, execution_mode="sequential", tasks=[task.model_dump()],
                                execution_sequence=[task.task_id], assumptions=[], acceptance_criteria={})
    result = RetrievalExecutor(provider=Provider(), tool_registry={}, extra_tool_factories={}).execute_task(task, state, signal)
    assert result.success
    evidence = result.record.evidence[0]
    assert evidence.evidence == "Original document passage."
    assert evidence.metadata.extra["char_end"] == 26
    assert evidence.result_id.startswith("ev_")
    assert state.execution_context.intermediate_results[task.task_id]["answer"] == evidence.evidence


def test_reflection_reuses_validation_without_graph(monkeypatch):
    from deepresearch_agent.agents.multi_agent.executor import reflector
    from deepresearch_agent.agents.multi_agent.core.plan_spec import TaskNode, PlanExecutionSignal
    from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
    created = []
    class Validator:
        def __init__(self, *, enable_graph):
            assert enable_graph is False
            created.append(self)
        def validate(self, *args, **kwargs):
            return {"validation": {"passed": True}}
    monkeypatch.setattr(reflector, "AnswerValidationTool", Validator)
    executor = reflector.ReflectionExecutor()
    monkeypatch.setattr(executor, "_resolve_query_answer", lambda *args, **kwargs: ("query", "answer", None))
    task = TaskNode(task_type="reflection", description="Review answer")
    state = PlanExecuteState(input="query")
    signal = PlanExecutionSignal(plan_id="p1", version=1, execution_mode="sequential", tasks=[task.model_dump()],
                                execution_sequence=[task.task_id], assumptions=[], acceptance_criteria={})
    assert executor.execute_task(task, state, signal).success
    assert executor.execute_task(task, state, signal).success
    assert len(created) == 1


@pytest.mark.parametrize("mode", [SourceMode.GRAPHRAG, SourceMode.WEB])
def test_non_graph_tool_cannot_reuse_or_write_answer_cache(monkeypatch, mode):
    import asyncio
    from deepresearch_agent.search.tool import deep_research_tool as module
    from deepresearch_agent.cache_manager.manager import CacheManager

    old_cache = CacheManager(memory_only=True, enable_vector_similarity=False)
    old_cache.set("deep:query", "Stale private graph answer that must never be reused.")
    def initialize(self, **kwargs):
        self.llm = SimpleNamespace()
        self.cache_manager = old_cache
        self.performance_metrics = {}
    monkeypatch.setattr(module.BaseSearchTool, "__init__", initialize)
    tool = module.DeepResearchTool(provider=SimpleNamespace(mode=mode, supports_graph=False))
    tool.validator = SimpleNamespace(validate=lambda *args: {"passed": True})
    tool.thinking = lambda query: {"answer": "Fresh original evidence answer.", "reference": {}}
    assert tool.search("query") == "Fresh original evidence answer."
    assert tool.cache_manager.get("deep:query", skip_validation=True) is None
    async def thinking_stream(query):
        yield {"answer": "Fresh streamed original evidence.", "thinking": ""}
    tool.thinking_stream = thinking_stream
    async def collect():
        return "".join([chunk async for chunk in tool.search_stream("query")])
    assert "Fresh streamed original evidence." in asyncio.run(collect())
    assert tool.cache_manager.get("deep:query", skip_validation=True) is None


@pytest.mark.parametrize("mode", [SourceMode.GRAPHRAG, SourceMode.WEB])
def test_non_graph_agent_disables_all_answer_cache_storage(monkeypatch, mode):
    import asyncio
    from deepresearch_agent.agents import deep_research_agent as module
    from deepresearch_agent.cache_manager.manager import CacheManager
    from langchain_core.messages import HumanMessage, AIMessage
    seen = {}
    def initialize(self, **kwargs):
        seen.update(kwargs)
        self.cache_manager = CacheManager(memory_only=True, enable_vector_similarity=False)
        self.global_cache_manager = CacheManager(memory_only=True, enable_vector_similarity=False)
        for cache in (self.cache_manager, self.global_cache_manager):
            cache.set("query", "Stale private graph answer that must never be reused.")
        self._log_execution = lambda *args: None
    monkeypatch.setattr(module.BaseAgent, "__init__", initialize)
    monkeypatch.setattr(module, "DeepResearchTool", lambda **kw: SimpleNamespace(get_thinking_stream_tool=lambda: None))
    agent = module.DeepResearchAgent(retrieval_provider=SimpleNamespace(mode=mode, supports_graph=False))
    fresh = "Fresh original evidence answer."
    state = {"messages": [HumanMessage(content="query"), AIMessage(content="search"), AIMessage(content=fresh)]}
    assert agent._generate_node(state)["messages"][0].content == fresh
    agent._log_performance = lambda *args: None
    agent._extract_keywords = lambda query: {"high_level": [], "low_level": []}
    agent.default_recursion_limit = 10
    agent.graph = SimpleNamespace(stream=lambda *args, **kwargs: iter([{}]))
    agent.memory = SimpleNamespace(get=lambda config: {"channel_values": {"messages": [AIMessage(content=fresh)]}})
    assert agent.check_fast_cache("query") is None
    assert agent.ask("query") == fresh
    async def search_stream(query):
        yield "Fresh streamed agent answer."
    agent.research_tool.search_stream = search_stream
    async def collect():
        return "".join([chunk async for chunk in agent.ask_stream("query")])
    assert asyncio.run(collect()) == "Fresh streamed agent answer."
    for cache in (agent.cache_manager, agent.global_cache_manager):
        cache.set("query", fresh)
        assert cache.get("query", skip_validation=True) is None
        assert cache.get_fast("query") is None
    assert seen["memory_only"] is True


def test_explicit_graph_agent_preserves_legacy_cache(monkeypatch):
    from deepresearch_agent.agents import deep_research_agent as module
    legacy_cache = object()
    def initialize(self, **kwargs):
        assert kwargs["memory_only"] is False
        assert kwargs["enable_vector_cache"] is None
        self.cache_manager = self.global_cache_manager = legacy_cache
    monkeypatch.setattr(module.BaseAgent, "__init__", initialize)
    monkeypatch.setattr(module, "DeepResearchTool", lambda **kw: SimpleNamespace(get_thinking_stream_tool=lambda: None))
    agent = module.DeepResearchAgent(use_deeper_tool=False,
        retrieval_provider=SimpleNamespace(mode=SourceMode.GRAPHRAG, supports_graph=True))
    assert agent.cache_manager is agent.global_cache_manager is legacy_cache


@pytest.mark.parametrize("actual_provider,expected", [("hybrid_rag", "hybrid_rag"), (None, "local_search")])
def test_plan_driver_preserves_actual_provider_in_evidence_ledger(actual_provider, expected):
    from deepresearch_agent.harness.workflow import PlanExecuteReportDriver
    from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult, RetrievalMetadata
    result = RetrievalResult(result_id="chunk", granularity="Chunk", evidence="Original evidence", source="local_search", score=0.8,
        metadata=RetrievalMetadata(source_id="doc", source_type="chunk", extra={"provider": actual_provider} if actual_provider else {}))
    driver = object.__new__(PlanExecuteReportDriver)
    driver.state = SimpleNamespace(execution_records=[SimpleNamespace(
        task_id="task", tool_calls=[SimpleNamespace(tool_call_id="call", tool_name="local_search")],
        evidence=[result])])
    assert driver.evidence_results()[0][2] == expected

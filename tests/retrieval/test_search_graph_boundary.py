"""Graph connections belong to graph tools, not provider-driven research."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.search.tool import base


@pytest.fixture
def offline_models(monkeypatch):
    monkeypatch.setattr(base, "get_llm_model", lambda: SimpleNamespace())
    monkeypatch.setattr(base, "get_embeddings_model", lambda: SimpleNamespace())
    from deepresearch_agent.cache_manager import manager
    monkeypatch.setitem(manager.CACHE_SETTINGS, "enable_vector_similarity", False)


def forbid_graph(monkeypatch):
    def unavailable():
        pytest.fail("A provider-driven tool tried to open Neo4j")
    monkeypatch.setitem(sys.modules, "deepresearch_agent.config.neo4jdb",
                        SimpleNamespace(get_db_manager=unavailable))


def test_generic_search_tool_runs_without_graph_backend(offline_models, monkeypatch, tmp_path):
    forbid_graph(monkeypatch)
    from deepresearch_agent.cache_manager import manager

    monkeypatch.setitem(manager.CACHE_SETTINGS, "enable_vector_similarity", True)

    def no_vector_cache():
        pytest.fail("A generic tool tried to initialize the optional vector cache")

    monkeypatch.setattr(manager, "get_cache_embedding_provider", no_vector_cache)

    class PlainSearch(base.BaseSearchTool):
        def _setup_chains(self):
            pass

        def extract_keywords(self, query):
            return {}

        def search(self, query):
            return query

    with PlainSearch(cache_dir=str(tmp_path)) as tool:
        assert tool.search("ordinary query") == "ordinary query"
        assert not tool.cache_manager.enable_vector_similarity


@pytest.mark.parametrize("supports_graph", [False, True])
def test_deep_research_uses_supplied_provider_without_opening_graph(
    offline_models, monkeypatch, supports_graph,
):
    from deepresearch_agent.search.tool.deep_research_tool import DeepResearchTool
    from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult, RetrievalMetadata

    forbid_graph(monkeypatch)

    class Provider:
        mode = SourceMode.GRAPHRAG

        async def search(self, query, **kwargs):
            assert query == "original question"
            assert kwargs["call_context"].run_id == "boundary-run"
            return [RetrievalResult(
                result_id="evidence-1", granularity="Chunk", evidence="Original source passage",
                source="hybrid_search", source_mode="graphrag",
                metadata=RetrievalMetadata(source_id="document-1", source_type="chunk"),
            )]

    provider = Provider()
    provider.supports_graph = supports_graph
    with DeepResearchTool(provider=provider, run_id="boundary-run") as tool:
        result = asyncio.run(tool._async_search("original question"))
        assert result["chunks"][0]["text"] == "Original source passage"
        assert tool.provider_calls[0]["results"][0].result_id == "evidence-1"


def test_merge_preserves_evidence_from_both_rounds_without_mutating_inputs():
    from deepresearch_agent.search.tool.reasoning import results

    first = {"chunks": [{"chunk_id": "a", "text": "first"}],
             "doc_aggs": [{"doc_id": "one"}], "entities": ["original"]}
    second = {"chunks": [{"chunk_id": "a", "text": "duplicate"},
                         {"chunk_id": "b", "text": "second"}],
              "doc_aggs": [{"doc_id": "one"}, {"doc_id": "two"}], "entities": ["new"]}
    merged = results.merge_search_results(first, second)
    assert merged["chunks"] == [{"chunk_id": "a", "text": "first"}, {"chunk_id": "b", "text": "second"}]
    assert merged["doc_aggs"] == [{"doc_id": "one"}, {"doc_id": "two"}]
    assert merged["entities"] == ["original", "new"]
    assert first["entities"] == ["original"]
    assert len(first["chunks"]) == 1


@pytest.mark.parametrize("module_name,class_name", [
    ("local_search_tool", "LocalSearchTool"),
    ("global_search_tool", "GlobalSearchTool"),
    ("hybrid_tool", "HybridSearchTool"),
    ("naive_search_tool", "NaiveSearchTool"),
])
def test_graph_specialists_keep_vector_query_and_text_fallback(monkeypatch, module_name, class_name):
    import importlib
    import pandas as pd
    module = importlib.import_module(f"deepresearch_agent.search.tool.{module_name}")
    tool = object.__new__(getattr(module, class_name))
    tool.embeddings = SimpleNamespace(embed_query=lambda query: [0.2, 0.8])
    tool.default_vector_limit = tool.default_text_limit = 5

    def execute(cypher, params):
        if "db.index.vector.queryNodes" in cypher:
            assert params == {"embedding": [0.2, 0.8], "limit": 2}
            raise RuntimeError("Vector index unavailable")
        assert "MATCH (e:__Entity__)" in cypher
        assert params == {"query": "subject", "limit": 2}
        return pd.DataFrame({"id": ["entity-a", "entity-b"]})

    monkeypatch.setitem(sys.modules, "deepresearch_agent.config.neo4jdb",
                        SimpleNamespace(get_db_manager=lambda: SimpleNamespace(execute_query=execute)))
    tool.driver = SimpleNamespace(execute_query=lambda cypher, parameters_, result_transformer_:
                                  execute(cypher, parameters_))
    assert tool.vector_search("subject", 2) == ["entity-a", "entity-b"]


def test_graph_keywords_keep_legacy_extraction_and_cache(offline_models, monkeypatch):
    from deepresearch_agent.search.tool.deep_research_tool import DeepResearchTool

    class Keywords:
        closed = False
        queries = []

        def extract_keywords(self, query):
            self.queries.append(query)
            return {"high_level": ["legacy topic"], "low_level": [query]}

        def close(self):
            self.closed = True

    keywords = Keywords()
    monkeypatch.setitem(sys.modules, "deepresearch_agent.search.tool.hybrid_tool",
                        SimpleNamespace(HybridSearchTool=lambda: keywords))
    provider = SimpleNamespace(mode=SourceMode.GRAPHRAG, supports_graph=True)
    with DeepResearchTool(provider=provider) as tool:
        assert tool.extract_keywords("subject") == {"high_level": ["legacy topic"], "low_level": ["subject"]}
        tool.extract_keywords("subject")
    assert keywords.queries == ["subject"]
    assert keywords.closed


def test_merge_keeps_first_round_metadata_even_when_it_has_no_chunks():
    from deepresearch_agent.search.tool.reasoning.results import merge_search_results
    first = {"chunks": [], "doc_aggs": [{"doc_id": "one"}], "entities": ["original"]}
    second = {"chunks": [{"text": "anonymous source"}], "entities": ["new"]}
    merged = merge_search_results(first, second)
    assert merged == {"chunks": [{"text": "anonymous source"}], "doc_aggs": [{"doc_id": "one"}],
                      "entities": ["original", "new"]}
    merged["chunks"][0]["text"] = "changed"
    assert second["chunks"][0]["text"] == "anonymous source"


def test_graph_base_owns_connection_and_closes_it(offline_models, monkeypatch, tmp_path):
    import pandas as pd
    from deepresearch_agent.search.tool.graph_base import GraphSearchTool

    class Connection:
        closed = False

        def close(self):
            self.closed = True

    connection = Connection()
    manager = SimpleNamespace(get_graph=lambda: connection, get_driver=lambda: object(),
                              execute_query=lambda cypher, params: pd.DataFrame({"id": ["entity"]}))
    monkeypatch.setitem(sys.modules, "deepresearch_agent.config.neo4jdb",
                        SimpleNamespace(get_db_manager=lambda: manager))

    class GraphSearch(GraphSearchTool):
        def _setup_chains(self):
            pass

        def extract_keywords(self, query):
            return {}

        def search(self, query):
            return self.text_search(query)

    with GraphSearch(cache_dir=str(tmp_path)) as tool:
        assert tool.search("subject") == ["entity"]
        assert not connection.closed
    assert connection.closed


def test_deep_research_import_does_not_load_graph_reasoning_modules():
    import os
    from pathlib import Path
    import subprocess

    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import importlib.abc
import sys

class NoGraph(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {
            'deepresearch_agent.search.tool.reasoning.community_enhance',
            'deepresearch_agent.search.tool.reasoning.kg_builder',
            'deepresearch_agent.search.tool.hybrid_tool',
            'deepresearch_agent.config.neo4jdb',
        }:
            raise AssertionError('Unexpected graph dependency: ' + fullname)

sys.meta_path.insert(0, NoGraph())
from deepresearch_agent.search.tool import DeepResearchTool
from deepresearch_agent.search.tool.reasoning import QueryGenerator, ThinkingEngine
assert DeepResearchTool and QueryGenerator and ThinkingEngine
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(source)}, timeout=60)
    assert result.returncode == 0, result.stderr

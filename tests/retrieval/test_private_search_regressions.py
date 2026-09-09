from unittest.mock import AsyncMock, MagicMock

import pytest

from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.retrieval.graphrag_provider import GraphRAGProvider
from deepresearch_agent.search.tool.deep_research_tool import DeepResearchTool
from deepresearch_agent.search.tool.deeper_research_tool import DeeperResearchTool


def evidence(text):
    return RetrievalResult(
        granularity="Chunk", evidence=text, source="hybrid_search", source_mode="graphrag",
        metadata=RetrievalMetadata(source_id="document-1", source_type="chunk"),
    )


@pytest.mark.parametrize("query", ["什么是急性脑血管病", "急性脑血管病是什么？"])
def test_pdf_compatibility_characters_match_without_changing_source(query):
    item = evidence("急性脑⾎管病⼜称脑卒中，主要包括出⾎性和缺⾎性脑⾎管病。")
    original = item.evidence
    assert GraphRAGProvider._filter_relevant(query, [item]) == [item]
    assert item.evidence == original


def test_normalization_does_not_accept_other_disease_subtypes():
    unrelated = evidence("出⾎性脑⾎管病以脑出血为主要表现。")
    assert GraphRAGProvider._filter_relevant("什么是缺血性脑血管病", [unrelated]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_class", [DeepResearchTool, DeeperResearchTool])
async def test_one_iteration_reaches_actual_search(tool_class, monkeypatch):
    # Run the real generator up to its first provider call, without external APIs.
    class SearchReached(Exception):
        pass

    tool = MagicMock()
    inner = tool if tool_class is DeepResearchTool else tool.deep_research
    inner.max_iterations = 1
    inner.all_retrieved_info = []
    inner.thinking_engine.has_executed_query.return_value = False
    inner.thinking_engine.executed_search_queries = []
    inner.thinking_engine.remove_result_tags.return_value = ""
    inner._async_search = AsyncMock(side_effect=SearchReached)
    tool.query_generator.generate_sub_queries.return_value = ["急性脑血管病"]
    tool.extract_keywords.return_value = {}
    tool._async_enhance_search = AsyncMock(return_value={})
    tool._progress = AsyncMock()
    monkeypatch.setattr("deepresearch_agent.search.tool.deeper_research_tool.complexity_estimate", lambda _: 0)
    with pytest.raises(SearchReached):
        async for _ in tool_class.thinking_stream(tool, "急性脑血管病"):
            pass
    inner._async_search.assert_awaited_once_with("急性脑血管病")

"""Search tool package with lazy public exports for source isolation."""

from importlib import import_module


_EXPORTS = {
    "BaseSearchTool": ("graphrag_agent.search.tool.base", "BaseSearchTool"),
    "LocalSearchTool": ("graphrag_agent.search.tool.local_search_tool", "LocalSearchTool"),
    "GlobalSearchTool": ("graphrag_agent.search.tool.global_search_tool", "GlobalSearchTool"),
    "HybridSearchTool": ("graphrag_agent.search.tool.hybrid_tool", "HybridSearchTool"),
    "NaiveSearchTool": ("graphrag_agent.search.tool.naive_search_tool", "NaiveSearchTool"),
    "DeepResearchTool": ("graphrag_agent.search.tool.deep_research_tool", "DeepResearchTool"),
    "DeeperResearchTool": ("graphrag_agent.search.tool.deeper_research_tool", "DeeperResearchTool"),
    "ChainOfExplorationTool": ("graphrag_agent.search.tool.chain_exploration_tool", "ChainOfExplorationTool"),
    "HypothesisGeneratorTool": ("graphrag_agent.search.tool.hypothesis_tool", "HypothesisGeneratorTool"),
    "AnswerValidationTool": ("graphrag_agent.search.tool.validation_tool", "AnswerValidationTool"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value

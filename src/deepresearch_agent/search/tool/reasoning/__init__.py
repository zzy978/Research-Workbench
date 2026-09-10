"""按需导出推理组件，通用研究流程不加载图专用模块。"""

from importlib import import_module


_EXPORTS = {
    "extract_between": "nlp",
    "extract_from_templates": "nlp",
    "extract_sentences": "nlp",
    "kb_prompt": "prompts",
    "num_tokens_from_string": "prompts",
    "ThinkingEngine": "thinking",
    "AnswerValidator": "validator",
    "DualPathSearcher": "search",
    "QueryGenerator": "search",
    "CommunityAwareSearchEnhancer": "community_enhance",
    "DynamicKnowledgeGraphBuilder": "kg_builder",
    "EvidenceChainTracker": "evidence",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value

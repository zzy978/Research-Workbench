"""Private hybrid RAG (or explicit legacy GraphRAG) and Web retrieval.

The private source retains the historical ``graphrag`` API value. Backend
selection belongs to configuration and does not change a run's source policy.
"""

from .base import RetrievalProvider, SearchDepth, SearchFilters, SourceMode, ToolCallContext
from .router import RetrievalRouter, create_default_router

__all__ = ["RetrievalProvider", "RetrievalRouter", "SearchDepth", "SearchFilters", "SourceMode", "ToolCallContext", "create_default_router"]

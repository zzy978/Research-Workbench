"""Answer caching policy for provider-backed research entry points."""

from deepresearch_agent.cache_manager.backends.base import CacheStorageBackend
from deepresearch_agent.cache_manager.manager import CacheManager


class _DisabledAnswerStorage(CacheStorageBackend):
    """Keep the cache API and answer validation without retaining any answer."""

    def get(self, key):
        return None

    def set(self, key, value):
        pass

    def delete(self, key):
        return False

    def clear(self):
        pass


def create_uncached_answer_manager() -> CacheManager:
    # Query-only caches lack source identity and index revision. Until those
    # participate in the key, every provider-backed answer must retrieve afresh.
    return CacheManager(
        storage_backend=_DisabledAnswerStorage(),
        memory_only=True,
        enable_vector_similarity=False,
    )

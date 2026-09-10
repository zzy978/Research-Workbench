"""Source-preserving hybrid retrieval; generation belongs to the research workflow."""
from __future__ import annotations

import asyncio
import hashlib
import math
from pathlib import Path
import threading
import unicodedata

from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.harness.policies import SourcePolicy
from deepresearch_agent.retrieval.hybrid_index import HybridIndex
from deepresearch_agent.retrieval.task_capabilities import task_capabilities, adapt_legacy_task


class HybridRAGProvider:
    mode = SourceMode.GRAPHRAG  # Legacy storage/API value for the private corpus.
    provider_name = "hybrid_rag"
    supports_graph = False

    def __init__(self, *, index_dir, embeddings, reranker, embedding_identity: str,
                 candidate_k: int = 30, rerank_k: int = 20, min_score: float = 0.01):
        if candidate_k < 1 or rerank_k < 1 or not 0 <= min_score <= 1:
            raise ValueError("检索候选数或重排阈值无效")
        self.index_dir = Path(index_dir)
        self.embeddings, self.reranker = embeddings, reranker
        self.embedding_identity = embedding_identity
        self.candidate_k, self.rerank_k, self.min_score = candidate_k, rerank_k, min_score
        self._index = None
        self._lock = threading.Lock()

    async def search(self, query, *, top_k, search_depth, filters, call_context):
        if call_context.source_mode != self.mode:
            raise AppError(ErrorCode.SOURCE_POLICY_VIOLATION, "私有库检索不能用于联网信息源")
        strategy = filters.strategy or "hybrid_search"
        SourcePolicy().assert_tool_allowed(self.mode, strategy)
        if adapt_legacy_task(strategy, task_capabilities(self.mode, provider=self)) != "hybrid_search":
            raise ValueError("混合检索不支持此工具")
        if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= self.rerank_k:
            raise ValueError(f"top_k 必须在 1 到 {self.rerank_k} 之间")
        if not query.strip():
            return []
        return await asyncio.to_thread(self._search, query, top_k, strategy)

    def _search(self, query: str, top_k: int, requested_strategy: str):
        try:
            # Reload only when the published corpus version changes. Each call pins its snapshot.
            with self._lock:
                manifest = HybridIndex.read_manifest(self.index_dir, embedding_identity=self.embedding_identity)
                if self._index is None or self._index.version != manifest["version"]:
                    self._index = HybridIndex.load(self.index_dir, embedding_identity=self.embedding_identity)
                index = self._index
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AppError(ErrorCode.SOURCE_UNAVAILABLE,
                           "私有库索引缺失、损坏或与向量模型不匹配，请运行 build_rag_index.py") from exc
        try:
            matching_query = unicodedata.normalize("NFKC", query)
            candidates = index.search(matching_query, self.embeddings.embed_query(matching_query),
                                      candidate_k=max(top_k, self.candidate_k))[:self.rerank_k]
            scores = self.reranker.score(matching_query, [unicodedata.normalize("NFKC", item["chunk"]["text"]) for item in candidates])
            if len(scores) != len(candidates) or any(not math.isfinite(s) or not 0 <= s <= 1 for s in scores):
                raise ValueError("重排必须为每条候选返回 0–1 有限分数")
        except Exception as exc:
            raise AppError(ErrorCode.RETRIEVAL_FAILED,
                           "混合检索或重排失败，请检查 Embedding 服务和本地 Reranker 模型；未降级到其他信息源") from exc
        ranked = sorted(zip(candidates, scores), key=lambda pair: -pair[1])
        results = []
        for item, score in ranked:
            if score < self.min_score:
                continue
            chunk = item["chunk"]
            extra = {k: v for k, v in item.items() if k != "chunk"}
            extra.update({k: chunk[k] for k in ("source_path", "start_char", "end_char")})
            extra.update(provider=self.provider_name, strategy="hybrid_search", requested_strategy=requested_strategy,
                         index_version=index.version, chunk_id=chunk["id"], rerank_score=score,
                         reranker=getattr(self.reranker, "model_name", type(self.reranker).__name__))
            results.append(RetrievalResult(
                result_id=chunk["id"], granularity="Chunk", evidence=chunk["text"],
                source="hybrid_search", source_mode=self.mode.value, score=score,
                metadata=RetrievalMetadata(source_id="doc_" + hashlib.sha256(chunk["source_path"].encode("utf-8")).hexdigest(), source_type="chunk",
                                           title=chunk["source_path"], content_hash=chunk["content_hash"], extra=extra),
            ))
            if len(results) == top_k:
                break
        return results


def create_hybrid_provider():
    from deepresearch_agent.config import settings
    from deepresearch_agent.models.get_models import get_embeddings_model
    from deepresearch_agent.retrieval.reranker import CrossEncoderReranker
    return HybridRAGProvider(
        index_dir=settings.RAG_INDEX_DIR, embeddings=get_embeddings_model(),
        embedding_identity=settings.RAG_EMBEDDING_IDENTITY,
        candidate_k=settings.RAG_CANDIDATE_K, rerank_k=settings.RAG_RERANK_K,
        min_score=settings.RAG_MIN_RERANK_SCORE,
        reranker=CrossEncoderReranker(settings.RERANKER_MODEL, device=settings.RERANKER_DEVICE,
                                     batch_size=settings.RERANKER_BATCH_SIZE,
                                     max_length=settings.RERANKER_MAX_LENGTH,
                                     cache_dir=settings.MODEL_CACHE_DIR / "reranker"),
    )

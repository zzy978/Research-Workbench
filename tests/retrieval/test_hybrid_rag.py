"""Exercise retrieval with hand-authored vectors; no paid API or model download."""
import pytest

from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.retrieval.base import SearchFilters, ToolCallContext


class Embeddings:
    def embed_documents(self, texts):
        return [[1., 0.] if "语义" in text else [0., 1.] for text in texts]

    def embed_query(self, query):
        return [1., 0.]


class Reranker:
    def score(self, query, texts):
        return [0.95 if "ZX-219" in text else 0.3 for text in texts]


def build(tmp_path, documents=None, **kwargs):
    from deepresearch_agent.retrieval.hybrid_index import HybridIndex
    return HybridIndex.build(
        tmp_path, documents or [("semantic.md", "语义近似段落"), ("manual.md", "ZX-219 精确型号操作说明")],
        embeddings=Embeddings(), embedding_identity="test-model", chunk_size=80, overlap=10,
        **kwargs,
    )


def test_complementary_recall_and_deduplication(tmp_path):
    index = build(tmp_path)
    candidates = index.search("ZX-219", [1., 0.], candidate_k=1)
    assert {item["chunk"]["source_path"] for item in candidates} == {"semantic.md", "manual.md"}
    candidates = index.search("语义", [1., 0.], candidate_k=2)
    assert len({item["chunk"]["id"] for item in candidates}) == len(candidates)


@pytest.mark.asyncio
async def test_rerank_precedes_top_k_and_preserves_original_evidence(tmp_path):
    from deepresearch_agent.retrieval.hybrid_provider import HybridRAGProvider
    build(tmp_path)
    provider = HybridRAGProvider(index_dir=tmp_path, embeddings=Embeddings(), reranker=Reranker(),
                                 embedding_identity="test-model", candidate_k=1, rerank_k=2)
    results = await provider.search("ZX-219", top_k=1, search_depth="basic", filters=SearchFilters(),
                                    call_context=ToolCallContext("run", SourceMode.GRAPHRAG))
    assert len(results) == 1
    result = results[0]
    assert result.evidence == "ZX-219 精确型号操作说明"
    assert result.score == 0.95
    assert result.metadata.extra["source_path"] == "manual.md"
    assert result.metadata.extra["rerank_score"] == 0.95
    assert result.metadata.extra["bm25_rank"] == 1
    assert result.metadata.source_id


def test_chunk_locations_and_stable_ids(tmp_path):
    text = "第一段含有脑⾎管病。\n\n" + "第二段说明。" * 30
    index = build(tmp_path, [("folder/a.md", text)])
    ids = [c["id"] for c in index.chunks]
    assert all(text[c["start_char"]:c["end_char"]] == c["text"] for c in index.chunks)
    assert index.chunks[0]["start_char"] == 0
    assert index.chunks[-1]["end_char"] == len(text)
    assert len(ids) > 1
    assert [c["id"] for c in build(tmp_path, [("folder/a.md", text)]).chunks] == ids


def test_rebuild_removes_deleted_documents_and_failed_build_preserves_snapshot(tmp_path):
    from deepresearch_agent.retrieval.hybrid_index import HybridIndex
    old = build(tmp_path)
    new = build(tmp_path, [("new.md", "replacement")])
    assert {c["source_path"] for c in new.chunks} == {"new.md"}
    assert {c["source_path"] for c in old.chunks} == {"semantic.md", "manual.md"}
    with pytest.raises(ValueError, match="empty|空"):
        HybridIndex.build(tmp_path, [], embeddings=Embeddings(), embedding_identity="test-model")
    assert HybridIndex.load(tmp_path, embedding_identity="test-model").version == new.version


def test_wrong_embedding_model_and_dimension_are_rejected(tmp_path):
    from deepresearch_agent.retrieval.hybrid_index import HybridIndex
    index = build(tmp_path)
    with pytest.raises(ValueError, match="模型|identity"):
        HybridIndex.load(tmp_path, embedding_identity="different")
    with pytest.raises(ValueError, match="维度|dimension"):
        index.search("query", [1., 2., 3.], candidate_k=1)
    with pytest.raises(ValueError):
        index.search("query", [float("nan"), 0.], candidate_k=1)


@pytest.mark.asyncio
async def test_no_cross_source_fallback_or_hidden_rerank_failure(tmp_path):
    from deepresearch_agent.retrieval.hybrid_provider import HybridRAGProvider
    class BrokenReranker:
        def score(self, query, texts):
            raise RuntimeError("secret backend details")
    build(tmp_path)
    provider = HybridRAGProvider(index_dir=tmp_path, embeddings=Embeddings(), reranker=BrokenReranker(),
                                 embedding_identity="test-model")
    with pytest.raises(AppError) as caught:
        await provider.search("query", top_k=1, search_depth="basic", filters=SearchFilters(),
                              call_context=ToolCallContext("run", SourceMode.WEB))
    assert caught.value.code == ErrorCode.SOURCE_POLICY_VIOLATION
    with pytest.raises(AppError) as caught:
        await provider.search("query", top_k=1, search_depth="basic", filters=SearchFilters(),
                              call_context=ToolCallContext("run", SourceMode.GRAPHRAG))
    assert caught.value.code == ErrorCode.RETRIEVAL_FAILED
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_missing_index_and_blank_query(tmp_path):
    from deepresearch_agent.retrieval.hybrid_provider import HybridRAGProvider
    provider = HybridRAGProvider(index_dir=tmp_path, embeddings=Embeddings(), reranker=Reranker(),
                                 embedding_identity="test-model")
    args = dict(top_k=1, search_depth="basic", filters=SearchFilters(),
                call_context=ToolCallContext("run", SourceMode.GRAPHRAG))
    assert await provider.search("  ", **args) == []
    with pytest.raises(AppError) as caught:
        await provider.search("query", **args)
    assert caught.value.code == ErrorCode.SOURCE_UNAVAILABLE


def test_default_router_selects_hybrid_and_preserves_explicit_graph(monkeypatch):
    from deepresearch_agent.config import settings
    from deepresearch_agent.retrieval.router import create_default_router
    monkeypatch.setattr(settings, "PRIVATE_RETRIEVAL_BACKEND", "hybrid", raising=False)
    assert create_default_router().for_mode("graphrag").provider_name == "hybrid_rag"
    monkeypatch.setattr(settings, "PRIVATE_RETRIEVAL_BACKEND", "graphrag")
    assert create_default_router().for_mode("graphrag").provider_name == "graphrag"


def test_ingestion_rejects_unreadable_files_instead_of_publishing_partial_corpus(tmp_path):
    from deepresearch_agent.retrieval.ingestion import read_corpus
    (tmp_path / "good.md").write_text("有效文档", encoding="utf-8")
    (tmp_path / "broken.pdf").write_text("not a PDF", encoding="utf-8")
    with pytest.raises(ValueError, match="broken.pdf"):
        read_corpus(tmp_path)


def test_ingestion_preserves_relative_paths(tmp_path):
    from deepresearch_agent.retrieval.ingestion import read_corpus
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "same.md").write_text("first", encoding="utf-8")
    (tmp_path / "same.md").write_text("second", encoding="utf-8")
    assert dict(read_corpus(tmp_path)) == {"a/same.md": "first", "same.md": "second"}


def test_failed_embedding_batch_does_not_publish_a_new_version(tmp_path):
    from deepresearch_agent.retrieval.hybrid_index import HybridIndex
    index = build(tmp_path)
    class BadEmbeddings:
        def embed_documents(self, texts):
            return [[float("nan"), 0.] for _ in texts]
    with pytest.raises(ValueError):
        HybridIndex.build(tmp_path, [("x", "a")], embeddings=BadEmbeddings(), embedding_identity="test-model")
    assert HybridIndex.load(tmp_path, embedding_identity="test-model").version == index.version


def test_reranker_applies_sigmoid_and_keeps_model_loaded(monkeypatch):
    import torch
    from deepresearch_agent.retrieval.reranker import CrossEncoderReranker
    class Model:
        def __init__(self, name, **kwargs):
            assert kwargs["trust_remote_code"] is False
        def predict(self, pairs, *, activation_fn, **kwargs):
            assert pairs == [("q", "a"), ("q", "b")]
            return activation_fn(torch.tensor([0., 2.])).numpy()
    monkeypatch.setattr("sentence_transformers.CrossEncoder", Model)
    reranker = CrossEncoderReranker("local-model")
    assert reranker.score("q", ["a", "b"]) == pytest.approx([0.5, 0.880797])
    assert reranker.score("q", []) == []


@pytest.mark.asyncio
async def test_chunks_from_one_document_are_one_independent_source(tmp_path):
    from deepresearch_agent.retrieval.hybrid_provider import HybridRAGProvider
    build(tmp_path, [("one.md", "语义相关的同一份资料。" * 30)])
    provider = HybridRAGProvider(index_dir=tmp_path, embeddings=Embeddings(), reranker=Reranker(),
                                 embedding_identity="test-model")
    results = await provider.search("语义", top_k=3, search_depth="basic", filters=SearchFilters(),
                                    call_context=ToolCallContext("run", SourceMode.GRAPHRAG))
    assert len(results) == 3
    assert len({r.metadata.source_id for r in results}) == 1
    assert len({r.metadata.extra["chunk_id"] for r in results}) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["[]", "null", "123", '"bad"'])
async def test_invalid_manifest_type_is_source_unavailable(tmp_path, payload):
    from deepresearch_agent.retrieval.hybrid_provider import HybridRAGProvider
    (tmp_path / "current.json").write_text(payload, encoding="utf-8")
    provider = HybridRAGProvider(index_dir=tmp_path, embeddings=Embeddings(), reranker=Reranker(),
                                 embedding_identity="test-model")
    with pytest.raises(AppError) as caught:
        await provider.search("query", top_k=1, search_depth="basic", filters=SearchFilters(),
                              call_context=ToolCallContext("run", SourceMode.GRAPHRAG))
    assert caught.value.code == ErrorCode.SOURCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_pdf_compatibility_characters_normalize_only_model_inputs(tmp_path):
    from deepresearch_agent.retrieval.hybrid_index import HybridIndex
    from deepresearch_agent.retrieval.hybrid_provider import HybridRAGProvider
    original = "急性脑⾎管病⼜称脑卒中。"
    class NormalizedEmbeddings:
        def embed_documents(self, texts):
            assert texts == ["急性脑血管病又称脑卒中。"]
            return [[1., 0.]]
        def embed_query(self, query):
            assert query == "脑血管病"
            return [1., 0.]
    class NormalizedReranker:
        def score(self, query, texts):
            assert query == "脑血管病"
            assert texts == ["急性脑血管病又称脑卒中。"]
            return [0.9]
    model = NormalizedEmbeddings()
    HybridIndex.build(tmp_path, [("book.pdf", original)], embeddings=model, embedding_identity="test-model")
    provider = HybridRAGProvider(index_dir=tmp_path, embeddings=model, reranker=NormalizedReranker(),
                                 embedding_identity="test-model")
    results = await provider.search("脑⾎管病", top_k=1, search_depth="basic", filters=SearchFilters(),
                                    call_context=ToolCallContext("run", SourceMode.GRAPHRAG))
    assert results[0].evidence == original


@pytest.mark.asyncio
async def test_live_provider_picks_up_rebuilt_corpus(tmp_path):
    from deepresearch_agent.retrieval.hybrid_provider import HybridRAGProvider
    build(tmp_path)
    provider = HybridRAGProvider(index_dir=tmp_path, embeddings=Embeddings(), reranker=Reranker(),
                                 embedding_identity="test-model")
    args = dict(top_k=1, search_depth="basic", filters=SearchFilters(),
                call_context=ToolCallContext("run", SourceMode.GRAPHRAG))
    before = (await provider.search("ZX-219", **args))[0]
    build(tmp_path, [("replacement.md", "replacement document")])
    after = (await provider.search("ZX-219", **args))[0]
    assert before.metadata.extra["index_version"] != after.metadata.extra["index_version"]
    assert after.evidence == "replacement document"
    assert after.metadata.extra["source_path"] == "replacement.md"


@pytest.mark.asyncio
async def test_configured_rerank_threshold_can_return_no_evidence(tmp_path):
    from deepresearch_agent.retrieval.hybrid_provider import HybridRAGProvider
    build(tmp_path)
    provider = HybridRAGProvider(index_dir=tmp_path, embeddings=Embeddings(), reranker=Reranker(),
                                 embedding_identity="test-model", min_score=0.99)
    assert await provider.search("query", top_k=2, search_depth="basic", filters=SearchFilters(),
                                 call_context=ToolCallContext("run", SourceMode.GRAPHRAG)) == []


def test_concurrent_workers_share_one_lazy_model_provider():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from deepresearch_agent.retrieval.router import _LazyProvider
    entered, release = threading.Event(), threading.Event()
    def factory():
        entered.set()
        assert release.wait(2)
        return object()
    provider = _LazyProvider(mode=SourceMode.GRAPHRAG, provider_name="hybrid_rag", factory=factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(provider._get)
        assert entered.wait(2)
        second_started = threading.Event()
        def second_get():
            second_started.set()
            return provider._get()
        second = pool.submit(second_get)
        assert second_started.wait(2)
        # Both workers are now racing to initialize an expensive provider.
        release.set()
        assert first.result() is second.result()


@pytest.mark.asyncio
async def test_default_provider_does_not_promote_very_low_rerank_scores(tmp_path):
    from deepresearch_agent.retrieval.hybrid_provider import HybridRAGProvider
    class Unrelated:
        def score(self, query, texts):
            return [0.001 for _ in texts]
    build(tmp_path)
    provider = HybridRAGProvider(index_dir=tmp_path, embeddings=Embeddings(), reranker=Unrelated(),
                                 embedding_identity="test-model")
    assert await provider.search("unrelated", top_k=2, search_depth="basic", filters=SearchFilters(),
                                 call_context=ToolCallContext("run", SourceMode.GRAPHRAG)) == []

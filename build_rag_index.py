"""Build the default private RAG corpus without creating or modifying a graph."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def main():
    parser = argparse.ArgumentParser(description="构建私有库向量 + BM25 索引")
    parser.add_argument("--files", type=Path, help="原始文档目录，默认 files/")
    parser.add_argument("--index-dir", type=Path, help="索引目录，默认 RAG_INDEX_DIR")
    parser.add_argument("--prepare-reranker", action="store_true", help="仅下载/加载重排模型并测试推理，不构建索引")
    args = parser.parse_args()
    # Initialize the native runtime before model adapters load pandas on Windows.
    if sys.platform == "win32":
        import torch  # noqa: F401
    from deepresearch_agent.config import settings
    if args.prepare_reranker:
        from deepresearch_agent.retrieval.reranker import CrossEncoderReranker
        reranker = CrossEncoderReranker(settings.RERANKER_MODEL, device=settings.RERANKER_DEVICE,
                                       batch_size=settings.RERANKER_BATCH_SIZE,
                                       max_length=settings.RERANKER_MAX_LENGTH,
                                       cache_dir=settings.MODEL_CACHE_DIR / "reranker")
        score = reranker.score("什么是向量检索？", ["向量检索按语义相似度寻找相关文本。"])
        print(f"重排模型已就绪：{settings.RERANKER_MODEL}；测试分数={score[0]:.4f}")
        return
    from deepresearch_agent.retrieval.ingestion import read_corpus
    from deepresearch_agent.retrieval.hybrid_index import HybridIndex
    from deepresearch_agent.models.get_models import get_embeddings_model
    documents = read_corpus(args.files or settings.FILES_DIR)
    print(f"开始向量化：{len(documents)} 个文件；{sum(len(text) for _, text in documents)} 个字符", flush=True)
    index = HybridIndex.build(args.index_dir or settings.RAG_INDEX_DIR, documents,
                              embeddings=get_embeddings_model(), embedding_identity=settings.RAG_EMBEDDING_IDENTITY,
                              chunk_size=settings.RAG_CHUNK_SIZE, overlap=settings.RAG_CHUNK_OVERLAP,
                              batch_size=settings.OPENAI_EMBEDDING_BATCH_SIZE)
    print(f"索引已发布：{index.manifest['document_count']} 个文档，{len(index.chunks)} 个片段，"
          f"{index.manifest['dimension']} 维，版本 {index.version}")
    print(f"索引目录：{(args.index_dir or settings.RAG_INDEX_DIR).resolve()}")


if __name__ == "__main__":
    main()

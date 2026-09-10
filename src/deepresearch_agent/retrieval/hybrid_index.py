"""Immutable local corpus snapshots: original chunks, exact cosine and BM25."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import unicodedata
import uuid

import jieba
import numpy as np


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()
    # Keep identifiers whole while segmenting Chinese consistently at index/query time.
    return [token for part in re.findall(r"[a-z0-9_]+(?:[-.][a-z0-9_]+)*|[\u3400-\u9fff]+", text)
            for token in (jieba.lcut(part, HMM=False) if re.search(r"[\u3400-\u9fff]", part) else [part])]


def split_documents(documents, *, chunk_size: int, overlap: int) -> list[dict]:
    if not 0 <= overlap < chunk_size:
        raise ValueError("分块要求 0 <= overlap < chunk_size")
    chunks, paths = [], set()
    for path, text in sorted(documents):
        path = str(path).replace("\\", "/")
        if path in paths:
            raise ValueError("文档路径重复")
        paths.add(path)
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"文档为空或无法提取文字: {path}")
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            if end < len(text):
                # Prefer a sentence/paragraph boundary, but always make forward progress.
                lower = start + max(overlap + 1, chunk_size // 2)
                boundaries = [text.rfind(sep, lower, end) for sep in ("\n", "。", "！", "？", ". ", "; ")]
                boundary = max(boundaries)
                if boundary >= lower:
                    end = boundary + 1
            content = text[start:end]
            if content.strip():
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
                chunk_id = hashlib.sha256(f"{path}\0{start}\0{end}\0{digest}".encode("utf-8")).hexdigest()
                chunks.append(dict(id=chunk_id, source_path=path, text=content,
                                   start_char=start, end_char=end, content_hash=digest))
            if end == len(text):
                break
            start = end - overlap
    if not chunks:
        raise ValueError("不能发布空索引 (empty corpus)")
    return chunks


def _vectors(values, *, count: int, dimension: int | None = None):
    vectors = np.asarray(values, dtype="float32")
    if vectors.ndim != 2 or vectors.shape[0] != count or not vectors.shape[1]:
        raise ValueError("Embedding 返回数量或维度不正确")
    if dimension is not None and vectors.shape[1] != dimension:
        raise ValueError("Embedding dimension 与索引维度不一致，请重建索引")
    if not np.isfinite(vectors).all() or (np.linalg.norm(vectors, axis=1) == 0).any():
        raise ValueError("Embedding 含非有限值或零向量")
    vectors = np.ascontiguousarray(vectors)
    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors


class HybridIndex:
    def __init__(self, manifest: dict, chunks: list[dict], vectors):
        self.manifest, self.chunks, self._vectors = manifest, chunks, vectors
        self.version = manifest["version"]
        self._postings = defaultdict(list)
        self._lengths = []
        for i, chunk in enumerate(chunks):
            tokens = tokenize(chunk["text"])
            self._lengths.append(len(tokens))
            for token, count in Counter(tokens).items():
                self._postings[token].append((i, count))
        self._average_length = sum(self._lengths) / len(chunks) or 1.0

    @classmethod
    def build(cls, root, documents, *, embeddings, embedding_identity: str,
              chunk_size: int = 800, overlap: int = 120, batch_size: int = 10):
        if batch_size < 1 or not embedding_identity:
            raise ValueError("batch_size 和 embedding_identity 必须有效")
        chunks = split_documents(documents, chunk_size=chunk_size, overlap=overlap)
        batches, dimension = [], None
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            vectors = _vectors(embeddings.embed_documents([unicodedata.normalize("NFKC", c["text"]) for c in batch]),
                               count=len(batch), dimension=dimension)
            dimension = vectors.shape[1]
            batches.append(vectors)
        index = np.concatenate(batches)
        root = Path(root)
        version = uuid.uuid4().hex
        directory = root / version
        directory.mkdir(parents=True)
        chunk_bytes = json.dumps(chunks, ensure_ascii=False).encode("utf-8")
        vector_file = io.BytesIO()
        np.save(vector_file, index, allow_pickle=False)
        vector_bytes = vector_file.getvalue()
        (directory / "chunks.json").write_bytes(chunk_bytes)
        (directory / "vectors.npy").write_bytes(vector_bytes)
        manifest = dict(schema_version=1, version=version, embedding_identity=embedding_identity,
                        dimension=dimension, chunk_count=len(chunks),
                        document_count=len({c["source_path"] for c in chunks}),
                        chunk_size=chunk_size, overlap=overlap,
                        chunks_sha256=hashlib.sha256(chunk_bytes).hexdigest(),
                        vectors_sha256=hashlib.sha256(vector_bytes).hexdigest())
        # A reader sees either complete old or complete new files, including on Windows.
        pending = root / f".current-{version}.tmp"
        pending.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        os.replace(pending, root / "current.json")
        return cls(manifest, chunks, index)

    @staticmethod
    def read_manifest(root, *, embedding_identity: str) -> dict:
        manifest = json.loads((Path(root) / "current.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("索引清单必须是 JSON 对象")
        if manifest.get("schema_version") != 1 or not re.fullmatch(r"[a-f0-9]{32}", manifest.get("version", "")):
            raise ValueError("索引格式无效，请重新构建")
        if manifest.get("embedding_identity") != embedding_identity:
            raise ValueError("Embedding 模型 identity 与索引不一致，请重新构建")
        if manifest.get("chunk_count", 0) < 1 or manifest.get("dimension", 0) < 1:
            raise ValueError("索引为空或维度无效")
        for name in ("chunks.json", "vectors.npy"):
            if not (Path(root) / manifest["version"] / name).is_file():
                raise ValueError("索引快照文件缺失，请重新构建")
        return manifest

    @classmethod
    def load(cls, root, *, embedding_identity: str):
        manifest = cls.read_manifest(root, embedding_identity=embedding_identity)
        directory = Path(root) / manifest["version"]
        chunk_bytes = (directory / "chunks.json").read_bytes()
        vector_bytes = (directory / "vectors.npy").read_bytes()
        if (hashlib.sha256(chunk_bytes).hexdigest() != manifest["chunks_sha256"] or
                hashlib.sha256(vector_bytes).hexdigest() != manifest["vectors_sha256"]):
            raise ValueError("索引文件校验失败，请重新构建")
        chunks = json.loads(chunk_bytes)
        index = np.load(io.BytesIO(vector_bytes), allow_pickle=False)
        if len(chunks) != manifest["chunk_count"] or index.shape != (len(chunks), manifest["dimension"]):
            raise ValueError("索引内容数量或维度不一致")
        return cls(manifest, chunks, index)

    def search(self, query: str, query_vector, *, candidate_k: int) -> list[dict]:
        if candidate_k < 1:
            raise ValueError("candidate_k 必须为正数")
        vector = _vectors([query_vector], count=1, dimension=self._vectors.shape[1])
        scores = self._vectors @ vector[0]
        indices = np.argsort(-scores, kind="stable")[:candidate_k]
        vector_hits = [(int(i), float(scores[i])) for i in indices]
        bm25_scores = defaultdict(float)
        for token in set(tokenize(query)):
            postings = self._postings.get(token, [])
            idf = math.log(1 + (len(self.chunks) - len(postings) + 0.5) / (len(postings) + 0.5))
            for i, frequency in postings:
                norm = 1.5 * (0.25 + 0.75 * self._lengths[i] / self._average_length)
                bm25_scores[i] += idf * frequency * 2.5 / (frequency + norm)
        lexical_hits = sorted(bm25_scores.items(), key=lambda item: (-item[1], item[0]))[:candidate_k]
        combined = {}
        for name, hits in (("vector", vector_hits), ("bm25", lexical_hits)):
            for rank, (i, score) in enumerate(hits, 1):
                item = combined.setdefault(i, dict(chunk=self.chunks[i], rrf_score=0.0))
                item[f"{name}_rank"], item[f"{name}_score"] = rank, score
                item["rrf_score"] += 1.0 / (60 + rank)
        return sorted(combined.values(), key=lambda item: (-item["rrf_score"], item["chunk"]["id"]))

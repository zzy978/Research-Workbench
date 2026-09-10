import importlib
import json
from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest

health_module = importlib.import_module("backend.app.api.v1.health")


@pytest.fixture
def configured(monkeypatch, tmp_path):
    for name, value in {
        "PRIVATE_RETRIEVAL_BACKEND": "hybrid", "RAG_INDEX_DIR": tmp_path,
        "RAG_EMBEDDING_IDENTITY": "test-embedding", "EMBEDDING_API_KEY": "test-key",
        "OPENAI_EMBEDDINGS_MODEL": "test-model", "RERANKER_MODEL": "model/repo",
        "TAVILY_API_KEY": "", "OPENAI_API_KEY": "", "NEO4J_URI": "neo4j://invalid:7687",
        "NEO4J_USERNAME": "neo4j", "NEO4J_PASSWORD": "test",
    }.items():
        monkeypatch.setattr(health_module.settings, name, value)

    async def forbidden(*args):
        pytest.fail("Hybrid health must not probe Neo4j or call external services")
    monkeypatch.setattr(health_module, "_tcp_status", forbidden)
    return tmp_path


def write_manifest(root, **changes):
    manifest = dict(schema_version=1, version="a" * 32, embedding_identity="test-embedding", chunk_count=2, dimension=2)
    manifest.update(changes)
    (root / "current.json").write_text(json.dumps(manifest), encoding="utf-8")
    snapshot = root / ("a" * 32)
    snapshot.mkdir(exist_ok=True)
    for name in ("chunks.json", "vectors.npy"):
        (snapshot / name).write_bytes(b"not loaded by health")


@pytest.mark.asyncio
async def test_hybrid_capabilities_missing_index_is_unavailable(configured):
    result = await health_module.capabilities()
    assert result["sources"]["graphrag"]["available"] is False
    assert result["sources"]["graphrag"]["backend"] == "hybrid"
    assert result["sources"]["web"] == {"available": False, "reason": "TAVILY_API_KEY 未配置"}


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{}, {"embedding_identity": "old"}, {"chunk_count": 0}])
async def test_hybrid_manifest_readiness(configured, changes):
    write_manifest(configured, **changes)
    result = (await health_module.capabilities())["sources"]["graphrag"]
    assert result["available"] is (not changes)
    assert result["check_level"] == "configuration_and_files_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["EMBEDDING_API_KEY", "OPENAI_EMBEDDINGS_MODEL", "RERANKER_MODEL"])
async def test_hybrid_missing_model_configuration(configured, monkeypatch, name):
    write_manifest(configured)
    monkeypatch.setattr(health_module.settings, name, "")
    assert (await health_module.capabilities())["sources"]["graphrag"]["available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["malformed", "wrong_type", "missing_snapshot"])
async def test_hybrid_broken_index_is_reported_without_endpoint_failure(configured, broken):
    write_manifest(configured)
    if broken == "missing_snapshot":
        (configured / ("a" * 32) / "vectors.npy").unlink()
    else:
        (configured / "current.json").write_text("[1]" if broken == "wrong_type" else "{", encoding="utf-8")
    result = (await health_module.capabilities())["sources"]["graphrag"]
    assert result["available"] is False
    assert result["reason"]


@pytest.mark.asyncio
async def test_legacy_capabilities_preserves_tcp_check(configured, monkeypatch):
    monkeypatch.setattr(health_module.settings, "PRIVATE_RETRIEVAL_BACKEND", "graphrag")
    calls = []
    async def tcp(uri, port):
        calls.append((uri, port))
        return "healthy", None
    monkeypatch.setattr(health_module, "_tcp_status", tcp)
    result = await health_module.capabilities()
    assert result["sources"]["graphrag"]["available"] is True
    assert calls == [("neo4j://invalid:7687", 7687)]


@pytest.mark.asyncio
async def test_health_hybrid_skips_neo4j(configured):
    write_manifest(configured)
    class Connection:
        async def execute(self, statement):
            pass
    @asynccontextmanager
    async def connect():
        yield Connection()
    result = await health_module.health(SimpleNamespace(engine=SimpleNamespace(connect=connect)))
    assert result["components"]["private_retrieval"]["status"] == "healthy"
    assert result["components"]["neo4j"]["check_level"] == "not_required"

"""Health and capability discovery without spending model/search quota."""

import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from sqlalchemy import text

from backend.app.dependencies import get_database
from deepresearch_agent.config import settings
from deepresearch_agent.retrieval.hybrid_index import HybridIndex

router = APIRouter(tags=["system"])


async def _tcp_status(uri: str, default_port: int) -> tuple[str, str | None]:
    parsed = urlparse(uri)
    host, port = parsed.hostname, parsed.port or default_port
    if not host:
        return "unavailable", "地址未配置"
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.75)
        writer.close()
        await writer.wait_closed()
        return "healthy", None
    except Exception:
        return "degraded", f"无法连接 {host}:{port}"


async def _private_status() -> dict:
    backend = settings.PRIVATE_RETRIEVAL_BACKEND
    if backend == "graphrag":
        configured = bool(settings.NEO4J_URI and settings.NEO4J_USERNAME and settings.NEO4J_PASSWORD)
        status, reason = await _tcp_status(settings.NEO4J_URI, 7687) if configured else ("unavailable", "Neo4j 未配置")
        return {"backend": backend, "status": status, "configured": configured, "reason": reason, "check_level": "tcp_only"}
    configured = bool(settings.EMBEDDING_API_KEY and settings.OPENAI_EMBEDDINGS_MODEL and settings.RERANKER_MODEL)
    result = {
        "backend": backend, "status": "unavailable", "configured": configured,
        "reason": "Embedding 或 Reranker 未配置", "check_level": "configuration_and_files_only",
        "inference_checked": False,
    }
    if not configured:
        return result
    try:
        manifest = await asyncio.to_thread(HybridIndex.read_manifest, settings.RAG_INDEX_DIR,
                                          embedding_identity=settings.RAG_EMBEDDING_IDENTITY)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        result["reason"] = "RAG 索引缺失、格式无效或 Embedding 配置不匹配，请运行 build_rag_index.py 重建"
    else:
        result.update(status="healthy", reason=None, index_version=manifest["version"], chunk_count=manifest["chunk_count"])
    return result


@router.get("/health")
async def health(database=Depends(get_database)):
    sqlite_status = "healthy"
    try:
        async with database.engine.connect() as connection:
            await connection.execute(text("CREATE TEMP TABLE IF NOT EXISTS health_probe(value INTEGER)"))
            await connection.execute(text("DROP TABLE health_probe"))
    except Exception:
        sqlite_status = "unavailable"
    llm_configured = bool(settings.OPENAI_API_KEY and settings.OPENAI_LLM_MODEL)
    neo4j_configured = bool(settings.NEO4J_URI and settings.NEO4J_USERNAME and settings.NEO4J_PASSWORD)
    tavily_configured = bool(settings.TAVILY_API_KEY)
    llm_status, llm_reason = await _tcp_status(settings.OPENAI_BASE_URL, 443) if llm_configured else ("unavailable", "LLM 未配置")
    private = await _private_status()
    neo4j = private if settings.PRIVATE_RETRIEVAL_BACKEND == "graphrag" else {
        "status": "not_required", "configured": neo4j_configured,
        "reason": "hybrid 后端不依赖 Neo4j", "check_level": "not_required",
    }
    tavily_status, tavily_reason = (("healthy", None) if tavily_configured else ("unavailable", "TAVILY_API_KEY 未配置"))
    overall = "healthy" if sqlite_status == "healthy" and llm_status == "healthy" and (private["status"] == "healthy" or tavily_status == "healthy") else "degraded"
    return {
        "status": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "components": {
            "api": {"status": "healthy", "check_level": "functional"},
            "sqlite": {"status": sqlite_status, "check_level": "read_write"},
            "llm": {"status": llm_status, "configured": llm_configured, "reason": llm_reason, "check_level": "tcp_only"},
            "neo4j": neo4j,
            "private_retrieval": private,
            "tavily": {"status": tavily_status, "configured": tavily_configured, "reason": tavily_reason, "check_level": "configuration_only"},
        },
    }


@router.get("/capabilities")
async def capabilities():
    web = bool(settings.TAVILY_API_KEY)
    private = await _private_status()
    return {
        "sources": {
            "graphrag": {"available": private["status"] == "healthy", "reason": private["reason"],
                         "backend": private["backend"], "check_level": private["check_level"]},
            "web": {"available": web, "reason": None if web else "TAVILY_API_KEY 未配置"},
        },
        "workflows": ["deep_research", "plan_execute_report"],
        "default_source_mode": "graphrag",
        "default_budget": settings.HARNESS_BUDGETS,
        "features": {"sse": True, "memory_management": True, "skill_management": True},
        "fastapi_workers": settings.workers,
    }

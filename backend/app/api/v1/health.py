"""Health and capability discovery."""

from fastapi import APIRouter, Depends
from sqlalchemy import text

from backend.app.dependencies import get_database
from graphrag_agent.config import settings

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(database=Depends(get_database)):
    sqlite_status = "healthy"
    try:
        async with database.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        sqlite_status = "unavailable"
    llm_configured = bool(settings.OPENAI_API_KEY and settings.OPENAI_LLM_MODEL)
    neo4j_configured = bool(settings.NEO4J_URI and settings.NEO4J_USERNAME and settings.NEO4J_PASSWORD)
    tavily_configured = bool(settings.TAVILY_API_KEY)
    overall = "healthy" if sqlite_status == "healthy" and llm_configured and (neo4j_configured or tavily_configured) else "degraded"
    return {
        "status": overall,
        "components": {
            "api": {"status": "healthy"},
            "sqlite": {"status": sqlite_status},
            "llm": {"status": "healthy" if llm_configured else "unavailable", "configured": llm_configured},
            "neo4j": {"status": "healthy" if neo4j_configured else "unavailable", "configured": neo4j_configured},
            "tavily": {"status": "healthy" if tavily_configured else "unavailable", "configured": tavily_configured},
        },
    }


@router.get("/capabilities")
async def capabilities():
    web = bool(settings.TAVILY_API_KEY)
    graph = bool(settings.NEO4J_URI and settings.NEO4J_USERNAME and settings.NEO4J_PASSWORD)
    return {
        "sources": {
            "graphrag": {"available": graph, "reason": None if graph else "Neo4j 未配置"},
            "web": {"available": web, "reason": None if web else "TAVILY_API_KEY 未配置"},
        },
        "workflows": ["deep_research", "plan_execute_report"],
        "default_source_mode": "graphrag",
        "default_budget": settings.HARNESS_BUDGETS,
        "features": {"sse": True, "memory_management": True, "skill_management": True},
        "fastapi_workers": settings.workers,
    }

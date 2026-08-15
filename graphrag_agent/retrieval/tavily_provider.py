"""Tavily implementation with bounded retry, cache and stable Web evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tavily import TavilyClient

from graphrag_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from graphrag_agent.config.settings import CACHE_DIR
from graphrag_agent.harness.contracts import SourceMode
from graphrag_agent.harness.errors import AppError, ErrorCode
from graphrag_agent.harness.policies import SourcePolicy
from graphrag_agent.persistence.artifact_store import ArtifactStore
from graphrag_agent.retrieval.base import SearchDepth, SearchFilters, ToolCallContext
from graphrag_agent.retrieval.web_utils import content_hash, domain_from_url, normalize_content, normalize_url


class TavilyProvider:
    mode = SourceMode.WEB
    provider_name = "tavily"

    def __init__(
        self,
        *,
        api_key: str,
        client: Optional[Any] = None,
        artifact_store: Optional[ArtifactStore] = None,
        cache_dir: str | Path | None = None,
        default_depth: SearchDepth = "advanced",
        default_max_results: int = 5,
        timeout_seconds: int = 45,
        cache_ttl_seconds: int = 86400,
        policy: Optional[SourcePolicy] = None,
    ) -> None:
        if not api_key and client is None:
            raise AppError(ErrorCode.TAVILY_API_KEY_MISSING, "联网搜索未配置 TAVILY_API_KEY")
        self._client = client or TavilyClient(api_key=api_key)
        self._artifact_store = artifact_store
        self._cache_dir = Path(cache_dir or (CACHE_DIR / "tavily")).resolve()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._default_depth = default_depth
        self._default_max_results = default_max_results
        self._timeout_seconds = timeout_seconds
        self._cache_ttl_seconds = cache_ttl_seconds
        self._policy = policy or SourcePolicy()

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        search_depth: SearchDepth,
        filters: SearchFilters,
        call_context: ToolCallContext,
    ) -> list[RetrievalResult]:
        self._policy.assert_tool_allowed(call_context.source_mode, "tavily_search")
        args = self._policy.sanitize_web_arguments({
            "query": query,
            "top_k": top_k or self._default_max_results,
            "search_depth": search_depth or self._default_depth,
            "include_domains": list(filters.include_domains),
            "exclude_domains": list(filters.exclude_domains),
            "start_date": filters.start_date,
            "end_date": filters.end_date,
            "topic": filters.topic,
        })
        cached = self._read_cache(args)
        if cached is None:
            raw = await self._search_with_retry(args)
            self._write_cache(args, raw)
        else:
            raw = cached
        artifact = self._store_raw(call_context, raw)
        return self._map_results(raw, artifact=artifact)[: int(args["top_k"])]

    async def _search_with_retry(self, args: dict[str, Any]) -> dict[str, Any]:
        call_args = {
            "query": args["query"],
            "search_depth": args["search_depth"],
            "max_results": args["top_k"],
            "include_answer": False,
            "include_raw_content": "markdown",
            "timeout": self._timeout_seconds,
        }
        for name in ("include_domains", "exclude_domains", "start_date", "end_date", "topic"):
            if args.get(name):
                call_args[name] = args[name]
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(self._client.search, **call_args)
                if not isinstance(response, dict):
                    raise TypeError("Tavily 返回值不是对象")
                return response
            except Exception as exc:  # Tavily SDK and requests expose several error families
                status = self._status_code(exc)
                name = type(exc).__name__.lower()
                if status in {401, 403} or any(token in name for token in ("invalidapikey", "forbidden", "missingapikey")):
                    raise AppError(ErrorCode.TAVILY_AUTH_FAILED, "Tavily 认证失败，请检查后端 API Key") from exc
                if status == 429 or "limit" in name or "ratelimit" in name:
                    if attempt == 2:
                        raise AppError(ErrorCode.TAVILY_RATE_LIMITED, "Tavily 请求频率受限", retryable=True) from exc
                    await asyncio.sleep(self._retry_delay(exc, attempt))
                    continue
                if status is not None and status < 500:
                    raise AppError(ErrorCode.RETRIEVAL_FAILED, "Tavily 请求失败", details={"status_code": status}) from exc
                is_timeout = "timeout" in name
                if attempt == 2:
                    code = ErrorCode.RETRIEVAL_TIMEOUT if is_timeout else ErrorCode.RETRIEVAL_FAILED
                    raise AppError(code, "Tavily 检索超时" if is_timeout else "Tavily 检索暂时不可用", retryable=True) from exc
                await asyncio.sleep((2 ** (attempt + 1)) + random.uniform(0, 0.25))
        raise AssertionError("unreachable")

    @staticmethod
    def _status_code(exc: Exception) -> Optional[int]:
        value = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        return value or getattr(response, "status_code", None)

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int) -> float:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", {}) or {}
        raw = headers.get("Retry-After") or headers.get("retry-after")
        try:
            return max(0.0, float(raw)) if raw is not None else float(2 ** (attempt + 1))
        except (TypeError, ValueError):
            return float(2 ** (attempt + 1))

    def _map_results(self, raw: dict[str, Any], *, artifact: Optional[dict[str, str]]) -> list[RetrievalResult]:
        deduped: dict[tuple[str, str], RetrievalResult] = {}
        retrieved_at = datetime.now(timezone.utc)
        for item in raw.get("results", []) or []:
            try:
                url = normalize_url(str(item.get("url") or ""))
            except ValueError:
                continue
            body = str(item.get("raw_content") or item.get("content") or "")
            normalized = normalize_content(body)
            digest = content_hash(normalized)
            published = self._parse_datetime(item.get("published_date") or item.get("published_at"))
            score = min(1.0, max(0.0, float(item.get("score") or 0.5)))
            extra = {
                "untrusted_external_content": True,
                "content_truncated": len(normalized) > 12000,
            }
            if artifact:
                extra.update(artifact)
            metadata = RetrievalMetadata(
                source_id=url,
                source_type="webpage",
                title=item.get("title"),
                url=url,
                domain=domain_from_url(url),
                published_at=published,
                retrieved_at=retrieved_at,
                timestamp=published or retrieved_at,
                content_hash=digest,
                confidence=score,
                extra=extra,
            )
            result = RetrievalResult(
                granularity="DO",
                evidence=normalized[:12000],
                metadata=metadata,
                source="tavily_search",
                source_mode="web",
                score=score,
            )
            key = (url, digest)
            prior = deduped.get(key)
            if prior is None or result.score > prior.score:
                deduped[key] = result
        return list(deduped.values())

    def _store_raw(self, context: ToolCallContext, raw: dict[str, Any]) -> Optional[dict[str, str]]:
        if self._artifact_store is None:
            return None
        call_id = context.tool_call_id or hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()[:16]
        stored = self._artifact_store.write_text(
            f"{context.run_id}/tavily/{call_id}.json",
            json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str),
            mime_type="application/json",
        )
        return {"artifact_id": stored.artifact_id, "artifact_path": stored.relative_path}

    def _cache_key(self, args: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _read_cache(self, args: dict[str, Any]) -> Optional[dict[str, Any]]:
        path = self._cache_dir / f"{self._cache_key(args)}.json"
        if not path.is_file():
            return None
        ttl = 3600 if args.get("topic") == "news" or any(word in str(args["query"]).lower() for word in ("today", "latest", "最新", "今天")) else self._cache_ttl_seconds
        if time.time() - path.stat().st_mtime > ttl:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, args: dict[str, Any], raw: dict[str, Any]) -> None:
        path = self._cache_dir / f"{self._cache_key(args)}.json"
        temporary = path.with_suffix(f".{time.time_ns()}.tmp")
        temporary.write_text(json.dumps(raw, ensure_ascii=False, default=str), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


__all__ = ["TavilyProvider"]

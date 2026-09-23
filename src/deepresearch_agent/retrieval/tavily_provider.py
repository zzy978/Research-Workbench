"""Tavily implementation with bounded retry, cache and stable Web evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tavily import TavilyClient

from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalMetadata, RetrievalResult
from deepresearch_agent.config.settings import CACHE_DIR
from deepresearch_agent.harness.contracts import SourceMode
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.harness.policies import SourcePolicy
from deepresearch_agent.persistence.artifact_store import ArtifactStore
from deepresearch_agent.retrieval.base import SearchDepth, SearchFilters, ToolCallContext
from deepresearch_agent.retrieval.web_utils import content_hash, domain_from_url, normalize_content, normalize_url
from .documents import document_results, document_urls, has_paper_body, is_landing_page, is_paper_url


class TavilyProvider:
    mode = SourceMode.WEB
    provider_name = "tavily"
    _secret_pattern = re.compile(r"(?<![A-Za-z0-9_])(?:sk|tvly)-[A-Za-z0-9_-]{8,}|\bBearer\s+[A-Za-z0-9._-]{8,}|\bapi[_ -]?key\s*[:=]\s*\S+", re.I)

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
            raw = self._sanitize_external(await self._search_with_retry(args, before_request=call_context.before_request))
            self._write_cache(args, raw)
        else:
            raw = self._sanitize_external(cached)
            if call_context.on_cache_hit is not None:
                await call_context.on_cache_hit()
        artifact = self._store_raw(call_context, raw)
        results = self._map_results(raw, artifact=artifact, query=str(args["query"]))[: int(args["top_k"])]
        for result in results:
            result.metadata.extra['cache_hit'] = cached is not None
        return results

    async def _search_with_retry(self, args: dict[str, Any], *, before_request=None) -> dict[str, Any]:
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
            # A denied reservation is not a transport failure and must not be retried.
            if before_request is not None:
                await before_request()
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
                        raise AppError(ErrorCode.TAVILY_RATE_LIMITED, "Tavily 请求频率受限", retryable=True,
                                       details={'retries_exhausted': True, 'attempts': 3}) from exc
                    await asyncio.sleep(self._retry_delay(exc, attempt))
                    continue
                if status is not None and status < 500:
                    raise AppError(ErrorCode.RETRIEVAL_FAILED, "Tavily 请求失败", details={"status_code": status}) from exc
                is_timeout = "timeout" in name
                if attempt == 2:
                    code = ErrorCode.RETRIEVAL_TIMEOUT if is_timeout else ErrorCode.RETRIEVAL_FAILED
                    raise AppError(code, "Tavily 检索超时" if is_timeout else "Tavily 检索暂时不可用", retryable=True,
                                   details={'retries_exhausted': True, 'attempts': 3}) from exc
                await asyncio.sleep((2 ** (attempt + 1)) + random.uniform(0, 0.25))
        raise AssertionError("unreachable")

    async def read_documents(self, results, *, fields, call_context, filters=None):
        """Fetch accepted paper HTML/PDF through Extract, with the same request guard as Search."""
        self._policy.assert_tool_allowed(call_context.source_mode, 'tavily_search')
        output = []
        for source in results:
            domain = domain_from_url(source.metadata.source_id)
            if filters and (filters.include_domains and not any(domain == d or domain.endswith('.' + d) for d in filters.include_domains)
                            or any(domain == d or domain.endswith('.' + d) for d in filters.exclude_domains)):
                continue
            extra = source.metadata.extra
            required = any(f.get('required_access') == 'full_text' for f in fields)
            if extra.get('section') or (not is_paper_url(source.metadata.source_id) and
                                       (not required or extra.get('access') == 'full_text')):
                output.append(source)
                continue
            fetched = []
            failure = '正文获取失败或内容不包含可识别的方法与实验章节'
            for url in document_urls(source.metadata.source_id, str(source.evidence)):
                domain = domain_from_url(url)
                if filters and (filters.include_domains and not any(domain == d or domain.endswith('.' + d) for d in filters.include_domains)
                                or any(domain == d or domain.endswith('.' + d) for d in filters.exclude_domains)):
                    continue
                args = {'operation': 'extract', 'url': url}
                raw = self._read_cache(args)
                if raw is None:
                    # Reservation errors must escape before any transport exception handling.
                    if call_context.before_request:
                        await call_context.before_request()
                    try:
                        raw = self._sanitize_external(await asyncio.to_thread(self._client.extract,
                            urls=[url], extract_depth='advanced', format='markdown',
                            timeout=min(30, self._timeout_seconds)))
                    except Exception as exc:
                        status = self._status_code(exc)
                        if status in {401, 403} or any(token in type(exc).__name__.lower() for token in ('invalidapikey', 'forbidden', 'missingapikey')):
                            raise AppError(ErrorCode.TAVILY_AUTH_FAILED, 'Tavily 正文获取认证失败') from exc
                        continue
                elif call_context.on_cache_hit:
                    await call_context.on_cache_hit()
                if not isinstance(raw, dict):
                    continue
                request_id = (call_context.tool_call_id or 'read') + '_extract_' + content_hash(url + json.dumps(raw, sort_keys=True))[:16]
                artifact = self._store_raw(replace(call_context, tool_call_id=request_id), raw)
                for entry in raw.get('results', []):
                    # Do not accept another URL/redirect as evidence for this requested paper.
                    try:
                        actual_url = normalize_url(entry.get('url') or '')
                    except ValueError:
                        continue
                    if actual_url != normalize_url(url):
                        continue
                    body = str(entry.get('raw_content') or '')
                    if not body.strip() or (is_paper_url(url) and not has_paper_body(body)):
                        continue
                    fetched.extend(document_results(source, url, body, fields, artifact))
                if fetched:
                    # An HTTP 200 error/landing page is not a usable body and must remain retryable.
                    self._write_cache(args, raw)
                    break
            if fetched:
                output.extend(fetched)
            else:
                unresolved = source.model_copy(deep=True)
                unresolved.metadata.extra.update(access='snippet', full_text_failure=failure)
                output.append(unresolved)
        return output

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

    def _map_results(self, raw: dict[str, Any], *, artifact: Optional[dict[str, str]], query: str = "") -> list[RetrievalResult]:
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
                "access": "full_text" if item.get("raw_content") and len(normalized) <= 12000
                    and not is_landing_page(url) and (not is_paper_url(url) or has_paper_body(body)) else "snippet",
                "body_available": bool(item.get("raw_content")),
            }
            domain = domain_from_url(url)
            query_terms = {
                token for token in re.findall(r"[a-z][a-z0-9-]{3,}", query.lower())
                if token not in {"basic", "advanced", "search", "depth", "official", "documentation", "document", "docs"}
            }
            brand_match = any(token in domain.lower().split(".") for token in query_terms)
            documentation_domain = domain.lower().startswith(("docs.", "help.", "developer."))
            public_authority = domain.lower().endswith((".gov", ".gov.cn", ".edu", ".edu.cn"))
            extra["authority_rank"] = 3 if brand_match and documentation_domain else 2 if documentation_domain or public_authority else 0
            if artifact:
                extra.update(artifact)
            metadata = RetrievalMetadata(
                source_id=url,
                source_type="webpage",
                title=item.get("title"),
                url=url,
                domain=domain,
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
        def authority(item: RetrievalResult) -> tuple[int, float]:
            return int((item.metadata.extra or {}).get("authority_rank") or 0), item.score

        return sorted(deduped.values(), key=authority, reverse=True)

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
        ttl = 3600 if args.get("topic") == "news" or any(word in str(args.get("query", "")).lower() for word in ("today", "latest", "最新", "今天")) else self._cache_ttl_seconds
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

    @classmethod
    def _sanitize_external(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls._secret_pattern.sub("[REDACTED]", value)
        if isinstance(value, list):
            return [cls._sanitize_external(item) for item in value]
        if isinstance(value, dict):
            sanitized = {}
            for key, item in value.items():
                if key.lower() == "url" and isinstance(item, str):
                    try:
                        sanitized[key] = normalize_url(item)
                    except ValueError:
                        sanitized[key] = ""
                else:
                    sanitized[key] = cls._sanitize_external(item)
            return sanitized
        return value

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

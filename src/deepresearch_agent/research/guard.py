"""Host-owned retrieval boundary shared by both existing workflow drivers."""
from dataclasses import replace
from urllib.parse import urlsplit

from deepresearch_agent.harness.errors import AppError, ErrorCode, RunCancelled
from deepresearch_agent.persistence.repositories import RunRepository
from .failures import is_global_failure
from .retrieval_quality import search_query


class ResearchProvider:
    def __init__(self, provider, store, run_id, *, phase='research', targets=None, events=None, assessor=None):
        self.provider = provider
        self.store, self.run_id, self.phase = store, run_id, phase
        self.targets = targets or []
        self.events = events
        self.mode = provider.mode
        self.provider_name = provider.provider_name
        self.supports_graph = False
        self.last_failure = None
        self.fatal_failure = None
        self.assessor = assessor

    async def search(self, query, *, top_k, search_depth, filters, call_context):
        if self.fatal_failure is not None:
            raise self.fatal_failure
        try:
            result = await self._search(query, top_k=top_k, search_depth=search_depth,
                                        filters=filters, call_context=call_context)
            self.last_failure = None
            return result
        except Exception as exc:
            self.last_failure = exc
            if is_global_failure(exc):
                self.fatal_failure = exc
            raise

    async def _search(self, query, *, top_k, search_depth, filters, call_context):
        study = await self.store.assert_allowed(self.run_id, self.phase)
        spec = study['spec']
        target = self.targets if isinstance(self.targets, dict) else {}
        item = next((i for i in spec.get('items', []) if i['id'] == target.get('item_id')), None)
        fields = [f for f in spec.get('fields', []) if f['id'] in target.get('field_ids', [])]
        if item:
            query = search_query(spec, item, fields, query)
        queries = []
        for attempt in range(2 if item and self.assessor else 1):
            queries.append(query)
            result = await self._request(query, top_k=top_k, search_depth=search_depth,
                                         filters=filters, call_context=call_context)
            if item and self.assessor:
                assessment = await self.assessor(spec, item, fields, query, result)
                result = [result[i] for i in assessment['relevant_indices']]
                if not result:
                    if self.events:
                        await self.events.publish(self.run_id, 'research.search_rejected', stage=self.phase,
                            payload={'query': query, 'targets': self.targets, 'reason': '来源与研究对象不相关'})
                    query = search_query(spec, item, fields, assessment.get('query', ''), retry=True)
                    continue
            # Only fetch accepted sources, using the same cancellation/budget/domain guards.
            if item and hasattr(self.provider, 'read_documents'):
                result = await self._request(query, top_k=top_k, search_depth=search_depth,
                    filters=filters, call_context=call_context, documents=result, fields=fields)
            for entry in result:
                entry.metadata.extra['search_queries'] = list(queries)
            return result
        raise AppError(ErrorCode.RETRIEVAL_FAILED, '搜索结果与研究对象不相关，改写查询后仍未取得相关来源', retryable=True)

    async def _request(self, query, *, top_k, search_depth, filters, call_context, documents=None, fields=None):
        study = await self.store.assert_allowed(self.run_id, self.phase)
        domains = study['spec'].get('allowed_domains') or list(filters.include_domains)
        if domains:
            # A model cannot broaden the user-approved source boundary.
            filters = replace(filters, include_domains=tuple(domains))
        run = await RunRepository(self.store.database).get(self.run_id)
        if run is None or run.cancellation_requested or run.status in {'failed', 'partial', 'cancelled', 'budget_exhausted', 'completed', 'paused'}:
            raise RunCancelled('研究已取消')
        cache_hit = False
        if self.events and documents is None:
            await self.events.publish(self.run_id, 'research.search_requested', stage=self.phase,
                payload={'query': query, 'targets': self.targets})

        async def cached():
            nonlocal cache_hit
            current = await RunRepository(self.store.database).get(self.run_id)
            if current is None or current.cancellation_requested or current.status in {'failed', 'partial', 'cancelled', 'budget_exhausted', 'completed', 'paused'}:
                raise RunCancelled('研究已取消')
            cache_hit = True
            if self.events:
                await self.events.publish(self.run_id, 'research.cache_hit', stage=self.phase,
                    payload={'query': query, 'targets': self.targets})

        async def reserve():
            current = await RunRepository(self.store.database).get(self.run_id)
            if current is None or current.cancellation_requested or current.status in {'failed', 'partial', 'cancelled', 'budget_exhausted', 'completed', 'paused'}:
                raise RunCancelled('研究已取消')
            await self.store.reserve_request(self.run_id, self.phase)
            if self.events:
                await self.events.publish(self.run_id, 'research.request_started', stage=self.phase,
                    payload={'query': query, 'targets': self.targets, 'phase': self.phase})

        guarded = replace(call_context, run_id=self.run_id, before_request=reserve, on_cache_hit=cached)
        # Production Web provider invokes the guard for EVERY SDK retry.
        if documents is None:
            result = await self.provider.search(query, top_k=top_k, search_depth=search_depth,
                                               filters=filters, call_context=guarded)
        else:
            result = await self.provider.read_documents(documents, fields=fields or [], filters=filters,
                                                       call_context=guarded)
        await self.store.assert_allowed(self.run_id, self.phase)
        if domains:
            def permitted(item):
                host = (urlsplit(item.metadata.source_id).hostname or '').lower()
                return any(host == domain or host.endswith('.'+domain) for domain in domains)
            result = [item for item in result if permitted(item)]
        for item in result:
            item.metadata.extra['research_targets'] = self.targets
        if self.events:
            await self.events.publish(self.run_id, 'research.search_completed' if documents is None else 'research.documents_completed', stage=self.phase,
                payload={'query': query, 'targets': self.targets, 'result_count': len(result),
                         'cache_hit': cache_hit})
        return result

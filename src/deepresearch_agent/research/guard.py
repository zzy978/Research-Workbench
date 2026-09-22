"""Host-owned retrieval boundary shared by both existing workflow drivers."""
from dataclasses import replace
from urllib.parse import urlsplit

from deepresearch_agent.harness.errors import RunCancelled
from deepresearch_agent.persistence.repositories import RunRepository


class ResearchProvider:
    def __init__(self, provider, store, run_id, *, phase='research', targets=None, events=None):
        self.provider = provider
        self.store, self.run_id, self.phase = store, run_id, phase
        self.targets = targets or []
        self.events = events
        self.mode = provider.mode
        self.provider_name = provider.provider_name
        self.supports_graph = False

    async def search(self, query, *, top_k, search_depth, filters, call_context):
        study = await self.store.assert_allowed(self.run_id, self.phase)
        domains = study['spec'].get('allowed_domains') or []
        if domains:
            # A model cannot broaden the user-approved source boundary.
            filters = replace(filters, include_domains=tuple(domains))
        run = await RunRepository(self.store.database).get(self.run_id)
        if run is None or run.cancellation_requested or run.status in {'failed', 'cancelled', 'budget_exhausted', 'completed', 'paused'}:
            raise RunCancelled('研究已取消')
        cache_hit = False
        if self.events:
            await self.events.publish(self.run_id, 'research.search_requested', stage=self.phase,
                payload={'query': query, 'targets': self.targets})

        async def cached():
            nonlocal cache_hit
            cache_hit = True
            if self.events:
                await self.events.publish(self.run_id, 'research.cache_hit', stage=self.phase,
                    payload={'query': query, 'targets': self.targets})

        async def reserve():
            current = await RunRepository(self.store.database).get(self.run_id)
            if current is None or current.cancellation_requested or current.status in {'failed', 'cancelled', 'budget_exhausted', 'completed', 'paused'}:
                raise RunCancelled('研究已取消')
            await self.store.reserve_request(self.run_id, self.phase)
            if self.events:
                await self.events.publish(self.run_id, 'research.request_started', stage=self.phase,
                    payload={'query': query, 'targets': self.targets, 'phase': self.phase})

        guarded = replace(call_context, run_id=self.run_id, before_request=reserve, on_cache_hit=cached)
        # Production Web provider invokes the guard for EVERY SDK retry.
        result = await self.provider.search(query, top_k=top_k, search_depth=search_depth,
                                           filters=filters, call_context=guarded)
        await self.store.assert_allowed(self.run_id, self.phase)
        if domains:
            def permitted(item):
                host = (urlsplit(item.metadata.source_id).hostname or '').lower()
                return any(host == domain or host.endswith('.'+domain) for domain in domains)
            result = [item for item in result if permitted(item)]
        for item in result:
            item.metadata.extra['research_targets'] = self.targets
        if self.events:
            await self.events.publish(self.run_id, 'research.search_completed', stage=self.phase,
                payload={'query': query, 'targets': self.targets, 'result_count': len(result),
                         'cache_hit': cache_hit})
        return result

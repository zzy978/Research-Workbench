"""User commands for versioned studies, independent from model-authored text."""
from __future__ import annotations

import asyncio
import json
import time

from backend.app.schemas import MessageCreate, RunCreate
from deepresearch_agent.harness.contracts import SourceMode, WorkflowMode
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence.models import RunModel
from deepresearch_agent.persistence.repositories import RunRepository
from deepresearch_agent.research.schemas import ResearchSpec
from deepresearch_agent.research.storage import ResearchStore
from deepresearch_agent.research.intelligence import ResearchModel
from deepresearch_agent.research.guard import ResearchProvider
from deepresearch_agent.research.retrieval_quality import needs_discovery
from deepresearch_agent.retrieval.base import SearchFilters, ToolCallContext


class ResearchService:
    def __init__(self, owner):
        self.owner = owner
        self.database = owner.database
        self.store = ResearchStore(self.database)
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def model(self):
        return self.owner.research_model or ResearchModel()

    def lock(self, study_id):
        return self._locks.setdefault(study_id, asyncio.Lock())

    async def call_model(self, run_id, method, *args, **kwargs):
        from deepresearch_agent.harness.budgets import BudgetExceeded
        from deepresearch_agent.models.prefix_cache import tracker, get_current_run, set_current_run
        study = await self.store.for_run(run_id)
        for metric, key in [('llm_tokens', 'max_llm_tokens'), ('active_seconds', 'max_active_seconds')]:
            if study['usage'][metric] >= study['spec']['budget'][key]:
                raise BudgetExceeded(metric, study['usage'][metric], study['spec']['budget'][key])
        baseline = await self.store.run_usage(run_id)
        def tokens():
            value = (tracker.run_snapshot(run_id) or {}).get('totals', {})
            return int(value.get('input_tokens', 0)) + int(value.get('output_tokens', 0))
        before, started, prior_run = tokens(), time.monotonic(), get_current_run()
        set_current_run(run_id)
        try:
            return await getattr(self.model, method)(*args, **kwargs)
        finally:
            set_current_run(prior_run)
            await self.store.record_usage(run_id, baseline['llm_tokens'] + max(0, tokens()-before),
                                          baseline['active_seconds'] + time.monotonic()-started)

    async def create(self, run, query):
        existing = await self.store.for_run(run.run_id)
        if existing:
            await self._configure(run.run_id, existing, outline_pending=existing['current_revision'] == 1)
            return existing
        spec = ResearchSpec.model_validate({
            'title': query[:160], 'questions': [query], 'items': [],
            'fields': [{'id': 'answer', 'label': '问题证据', 'description': '回答研究问题',
                        'evidence_requirement': '可定位的一手资料', 'applies_to': []}],
        })
        study = await self.store.create(run.session_id, run.run_id, spec)
        await self._configure(run.run_id, study, outline_pending=True)
        return study

    async def _configure(self, run_id, study, *, outline_pending=False):
        used = await self.store.run_usage(run_id)
        async with self.database.transaction() as session:
            run = await session.get(RunModel, run_id)
            config = json.loads(run.config_snapshot_json)
            config.update(research_study_id=study['study_id'], research_revision=study['current_revision'],
                          research_outline_pending=outline_pending, min_evidence=1)
            run.config_snapshot_json = json.dumps(config, ensure_ascii=False)
            limits = json.loads(run.budget_json or '{}')
            allowance, total = study['spec']['budget'], study['usage']
            # This Run retains its consumed allowance; other Runs never reset it.
            limits.update(
                max_llm_tokens=max(1, int(allowance['max_llm_tokens'] - total['llm_tokens'] + used['llm_tokens'])),
                wall_time_seconds=max(1, int(allowance['max_active_seconds'] - total['active_seconds'] + used['active_seconds'])),
                max_tavily_calls=max(1, allowance['max_search_calls']),
            )
            run.budget_json = json.dumps(limits)

    async def prepare_outline(self, context):
        study = await self.store.for_run(context.run_id)
        if study is None:
            return True
        if study['approved_revision'] == study['current_revision']:
            await self.store.assert_allowed(context.run_id)
            return True
        if context.config_snapshot.get('research_outline_pending'):
            spec = await self.call_model(context.run_id, 'draft', context.original_query)
            spec = ResearchSpec.model_validate(spec).model_dump(mode='json')
            discoveries = []
            # Broad method families and unknown identities are not concrete candidates.
            if needs_discovery(spec) and self.owner._router is not None:
                provider = ResearchProvider(self.owner._router.for_mode(SourceMode.WEB), self.store,
                    context.run_id, phase='outline', events=self.owner.event_bus)
                for query in (spec.get('queries') or [context.original_query])[:3]:
                    from deepresearch_agent.harness.budgets import BudgetExceeded
                    try:
                        results = await provider.search(query, top_k=5, search_depth='advanced',
                            filters=SearchFilters(include_domains=tuple(spec.get('allowed_domains', []))),
                            call_context=ToolCallContext(run_id=context.run_id, source_mode=SourceMode.WEB))
                        discoveries.extend({'title': r.metadata.title, 'url': r.metadata.source_id,
                                            'snippet': str(r.evidence)[:1500]} for r in results)
                    except BudgetExceeded:
                        break
                    except AppError as exc:
                        await self.owner.event_bus.publish(context.run_id, 'research.discovery_unavailable',
                            stage='outline', payload={'code': exc.code.value})
                        break
                if discoveries:
                    spec = await self.call_model(context.run_id, 'resolve_candidates', spec, discoveries)
            study = await self.store.revise(study['study_id'], study['current_revision'], study['fingerprint'], spec)
            await self.store.link_run(study['study_id'], study['current_revision'], context.run_id, purpose='outline')
            await self._configure(context.run_id, study)
            context.config_snapshot.update(research_outline_pending=False, research_revision=study['current_revision'])
            used = await self.store.run_usage(context.run_id)
            context.budget_usage.llm_tokens = used['llm_tokens']
            context.budget_usage.elapsed_seconds = used['active_seconds']
        await self.owner.event_bus.publish(context.run_id, 'research.outline_ready', stage='outline',
            payload={'study_id': study['study_id'], 'revision': study['current_revision'], 'fingerprint': study['fingerprint']})
        return False

    async def _freeze(self, study):
        run = await self.owner.runs.get(study.get('run_id')) if study.get('run_id') else None
        if run and run.status in RunRepository.ACTIVE_STATUSES:
            await self.owner.pause(run.run_id)
            task = self.owner._tasks.get(run.run_id)
            if task and not task.done():
                done, _ = await asyncio.wait({task}, timeout=5)
                if not done:
                    raise AppError(ErrorCode.CONFLICT, '正在保存当前进度，请稍后重试修改')

    async def _current_for_edit(self, study_id, payload):
        current = await self.store.get(study_id)
        if payload.revision != current['current_revision'] or payload.fingerprint != current['fingerprint']:
            raise AppError(ErrorCode.CONFLICT, '研究范围已更新，请刷新后修改')
        return current

    async def _save_revision(self, current, payload, spec):
        study_id = current['study_id']
        revised = await self.store.revise(study_id, payload.revision, payload.fingerprint, spec)
        run = await self.owner.runs.get(current['run_id']) if current.get('run_id') else None
        if run and run.status == 'awaiting_scope_approval':
            await self.store.link_run(study_id, revised['current_revision'], run.run_id, purpose='outline')
            await self._configure(run.run_id, revised)
        if run:
            await self.owner.event_bus.publish(run.run_id, 'research.outline_ready', stage='outline',
                payload={'study_id': study_id, 'revision': revised['current_revision']})
        return await self.store.get(study_id)

    async def restore(self, study_id, payload):
        async with self.lock(study_id):
            current = await self._current_for_edit(study_id, payload)
            if payload.source_revision == current['current_revision']:
                raise AppError(ErrorCode.CONFLICT, '所选版本已是当前版本，无需回退')
            source = await self.store.get_revision(study_id, payload.source_revision)
            await self._freeze(current)
            return await self._save_revision(current, payload, source['spec'])

    async def revise(self, study_id, payload):
        async with self.lock(study_id):
            current = await self._current_for_edit(study_id, payload)
            await self._freeze(current)
            spec = payload.spec
            if spec is None:
                spec = await self.call_model(current['run_id'], 'draft', current['spec']['title'], previous=current['spec'], instruction=payload.instruction)
            return await self._save_revision(current, payload, spec)

    async def approve(self, study_id, payload):
        async with self.lock(study_id):
            existing = await self.store.get(study_id)
            if needs_discovery(existing['spec']) and existing['spec']['items']:
                raise AppError(ErrorCode.CONFLICT, '研究对象仍是待核实的分类，请先修订为有来源的具体论文、模型或产品再确认范围')
            running = self.owner._tasks.get(existing.get('run_id'))
            current_run = await self.owner.runs.get(existing.get('run_id'))
            if existing['approved_revision'] != existing['current_revision'] and running and not running.done() and current_run and current_run.status in {'outlining', 'context_building'}:
                raise AppError(ErrorCode.CONFLICT, '研究大纲尚在生成，请稍后确认')
            study = await self.store.approve(study_id, payload.revision, payload.fingerprint)
            matching = [r for r in study.get('runs', []) if r['revision'] == payload.revision and r['purpose'] == 'research']
            if matching:
                run = await self.owner.runs.get(matching[-1]['run_id'])
                if run.run_id == study['run_id'] and run.status == 'awaiting_scope_approval':
                    pending = self.owner._tasks.get(run.run_id)
                    if pending and not pending.done():
                        await asyncio.shield(pending)
                    await self._configure(run.run_id, study)
                    await self.owner.runs.update_status(run.run_id, status='queued', current_stage='queued')
                    run = await self.owner.runs.get(run.run_id)
                if run.run_id == study['run_id'] and run.status == 'queued':
                    self.owner.schedule(run.run_id)
                return self._accepted(study, run, False)
            current = await self.owner.runs.get(study['run_id']) if study.get('run_id') else None
            if current and current.status == 'awaiting_scope_approval':
                # The waiting checkpoint is visible just before the old task exits.
                if running and not running.done():
                    await asyncio.shield(running)
                run, created = current, False
                await self.store.link_run(study_id, payload.revision, run.run_id, purpose='research')
                await self._configure(run.run_id, study)
                await self.owner.runs.update_status(run.run_id, status='queued', current_stage='queued')
                run = await self.owner.runs.get(run.run_id)
            else:
                run, created = await self._new_run(study, 'research', f'approve:{payload.revision}')
            await self.owner.event_bus.publish(run.run_id, 'research.scope_approved', stage='planning',
                payload={'study_id': study_id, 'revision': payload.revision})
            self.owner.schedule(run.run_id)
            return self._accepted(study, run, created)

    async def followup(self, study_id, payload):
        from sqlalchemy import select
        from deepresearch_agent.research.schemas import effective_items
        from deepresearch_agent.research.storage import RevisionModel

        async with self.lock(study_id):
            study = await self.store.get(study_id)
            targets = [c.model_dump() for c in payload.cells]
            target_keys = {(c['item_id'], c['field_id']) for c in targets}
            client_id = f'research:{study_id}:followup:{payload.client_request_id}'
            previous = await self.owner.messages.get_by_client_id(study['session_id'], client_id)
            if previous is not None:
                async with self.database.sessions() as session:
                    run = await session.scalar(select(RunModel).where(RunModel.trigger_message_id == previous.message_id))
                config = json.loads(run.config_snapshot_json) if run else {}
                previous_revision = config.get('research_revision')
                async with self.database.sessions() as session:
                    revision = await session.get(RevisionModel, (study_id, previous_revision)) if previous_revision else None
                previous_targets = config.get('research_targets') or []
                previous_keys = {(c['item_id'], c['field_id']) for c in previous_targets}
                linked = run is not None and any(r['run_id'] == run.run_id and r['purpose'] == 'followup'
                             and r['revision'] == payload.revision for r in study['runs'])
                expected_content = (f"{json.loads(revision.spec_json)['title']}\n{payload.reason}".strip()
                                    if revision is not None else None)
                if (run is None or config.get('research_study_id') != study_id
                        or previous_revision != payload.revision or revision is None
                        or revision.fingerprint != payload.fingerprint
                        or previous_keys != target_keys or len(target_keys) != len(targets)
                        or previous.content != expected_content):
                    raise AppError(ErrorCode.CONFLICT, '请求标识已用于不同的补充研究，请使用新的请求标识')
                if not linked:
                    if (study['current_revision'] != payload.revision
                            or study['approved_revision'] != payload.revision
                            or study['approved_fingerprint'] != payload.fingerprint):
                        raise AppError(ErrorCode.CONFLICT, '原补充研究尚未关联且范围已更新，不能恢复旧请求')
                    applicable = {(item['id'], field['id']) for item in effective_items(study['spec'])
                                  for field in study['spec']['fields']
                                  if not field['applies_to'] or item['id'] in field['applies_to']}
                    if not target_keys or not target_keys <= applicable:
                        raise AppError(ErrorCode.CONFLICT, '补充研究目标不属于当前适用范围')
                    await self._freeze(study)
                    await self.store.link_run(study_id, payload.revision, run.run_id, purpose='followup', targets=targets)
                    self.owner.schedule(run.run_id)
                return self._accepted({**study, 'current_revision': previous_revision}, run, False)
            if payload.revision != study['approved_revision'] or payload.fingerprint != study['approved_fingerprint']:
                raise AppError(ErrorCode.CONFLICT, '请先确认当前研究范围')
            applicable = {(item['id'], field['id']) for item in effective_items(study['spec'])
                          for field in study['spec']['fields']
                          if not field['applies_to'] or item['id'] in field['applies_to']}
            if len(target_keys) != len(targets) or not target_keys or not target_keys <= applicable:
                raise AppError(ErrorCode.CONFLICT, '补充研究目标必须是当前范围内适用且不重复的单元')
            await self._freeze(study)
            run, created = await self._new_run(study, 'followup', 'followup:'+payload.client_request_id,
                targets=targets, reason=payload.reason)
            if created:
                self.owner.schedule(run.run_id)
            return self._accepted(study, run, created)

    async def _new_run(self, study, purpose, key, *, targets=None, reason=''):
        original = await self.owner.runs.get(study['runs'][0]['run_id'])
        limits = json.loads(original.budget_json)
        allowance, used = study['spec']['budget'], study['usage']
        limits.update(max_llm_tokens=max(1, allowance['max_llm_tokens']-int(used.get('llm_tokens', 0))),
                      max_tavily_calls=max(1, allowance['max_search_calls']),
                      wall_time_seconds=max(1, int(allowance['max_active_seconds']-used.get('active_seconds', 0))))
        message, run, created = await self.owner.runs.create_for_user_message(
            MessageCreate(session_id=study['session_id'], role='user',
                content=f"{study['spec']['title']}\n{reason}".strip(), client_message_id=f"research:{study['study_id']}:{key}"),
            RunCreate(session_id=study['session_id'], trigger_message_id='assigned-atomically',
                source_mode=SourceMode.WEB, workflow_mode=WorkflowMode(original.workflow_mode), budget=limits,
                config_snapshot={'research_study_id': study['study_id'], 'research_revision': study['current_revision'],
                                 'research_targets': targets, 'min_evidence': 1, 'schema_version': 1}))
        if await self.store.for_run(run.run_id) is None:
            await self.store.link_run(study['study_id'], study['current_revision'], run.run_id, purpose=purpose, targets=targets)
        return run, created

    async def accept(self, study_id, payload):
        async with self.lock(study_id):
            previous = await self.store.get(study_id)
            view = await self.store.accept(study_id, payload.revision, payload.fingerprint, payload.report_fingerprint)
            if not previous.get('acceptance'):
                await self.owner.event_bus.publish(view['run_id'], 'research.accepted', stage='completed',
                    payload={'study_id': study_id, 'revision': payload.revision})
            await self.owner.skill_learning.enqueue_for_run(view['run_id'])
            return view

    @staticmethod
    def _accepted(study, run, created):
        return {'study_id': study['study_id'], 'revision': study['current_revision'],
                'run_id': run.run_id, 'status': run.status, 'created': created}

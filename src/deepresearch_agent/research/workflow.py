"""Research units around the existing drivers, with durable evidence boundaries."""
from __future__ import annotations

import json

from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionRecord
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult, RetrievalMetadata
from deepresearch_agent.harness.checkpoints import CheckpointManager
from deepresearch_agent.harness.evidence import EvidenceLedger
from deepresearch_agent.harness.errors import ResearchPauseRequested, RunCancelled
from deepresearch_agent.persistence.models import EvidenceModel
from deepresearch_agent.persistence.repositories import EvidenceRepository, CheckpointRepository, PlanTaskToolRepository, RunRepository
from .failures import CellValidationError, failure_detail, is_global_failure


class ResearchWorkflow:
    def __init__(self, context, service, factory, events):
        self.context, self.service, self.factory, self.events = context, service, factory, events
        self.store = service.store
        saved = context.workflow_state
        self.units = saved.get('research_units', [])
        self.done = set(saved.get('research_done', []))
        self.records = [ExecutionRecord.model_validate(r) for r in saved.get('research_records', [])]
        self.results = [RetrievalResult.from_dict(r) for r in saved.get('research_results', [])]
        self.aliases = dict(saved.get('research_aliases', {}))
        self._report = saved.get('research_report')
        self._complete = saved.get('research_execution_complete', False)
        self._conservative_report = saved.get('research_conservative_report', False)
        self.pending = dict(saved.get('research_pending', {}))
        self.cache = dict(saved.get('research_unit_cache', {}))
        self.finished_fields = set(saved.get('research_finished_fields', []))
        self.repaired = set(saved.get('research_repaired', []))
        self.attempts = dict(saved.get('research_attempts', {}))
        self.ledger = EvidenceLedger(EvidenceRepository(service.database))

    async def plan(self, failures=None):
        study = await self.store.assert_allowed(self.context.run_id)
        matrix = await self.store.matrix(study['study_id'])
        targets = self.context.config_snapshot.get('research_targets')
        force = {(c['item_id'], c['field_id']) for c in targets or []}
        wanted = force if targets is not None else {(c['item_id'], c['field_id']) for c in matrix['cells']
                  if c['status'] in {'missing', 'stale', 'pending_retry'}}
        if failures and not matrix['counts']['missing'] and not matrix['counts']['stale'] and set(failures) <= {'citation_integrity', 'claim_support', 'required_section', 'report_consistency', 'source_diversity'}:
            # A prose defect cannot justify repeating already-covered searches.
            self._conservative_report = True
            wanted = set()
        self.units = []
        for item in matrix['items']:
            fields = [f for f in matrix['fields'] if (item['id'], f['id']) in wanted]
            if fields:
                self.units.append({'item': item, 'fields': fields, 'task_id': f"task_{self.context.run_id}_{item['id']}"})
        await self._reuse(matrix)

    async def _reuse(self, matrix):
        for cell in matrix['cells']:
            if cell['status'] in {'missing', 'stale', 'pending_retry'}:
                continue
            for citation in cell.get('citations', []):
                old_id = citation['evidence_id']
                if old_id in self.aliases:
                    continue
                async with self.service.database.sessions() as session:
                    row = await session.get(EvidenceModel, old_id)
                if row is None:
                    continue
                metadata = json.loads(row.metadata_json or '{}')
                metadata.update(source_id=row.source_id, content_hash=row.content_hash)
                metadata.setdefault('source_type', 'webpage')
                metadata.setdefault('extra', {}).update(origin_run_id=row.run_id, origin_evidence_id=old_id)
                result = RetrievalResult(granularity='DO', evidence=row.summary,
                    metadata=RetrievalMetadata.model_validate(metadata), source='tavily_search', source_mode='web')
                await self.ledger.record_results(run_id=self.context.run_id, task_id=None, tool_call_id=None,
                                                provider=row.provider, results=[result])
                self.aliases[old_id] = result.result_id
                if result.result_id not in {r.result_id for r in self.results}:
                    self.results.append(result)

    async def execute(self):
        study = await self.store.assert_allowed(self.context.run_id)
        for unit in self.units:
            if unit['task_id'] in self.done:
                continue
            await self._check_control()
            await self._unit(study, unit)
            self.done.add(unit['task_id'])
            await self._checkpoint()
        # A single bounded sweep, after every ordinary object has had a turn.
        for unit in self.units:
            fields = [f for f in unit['fields'] if self._key(unit, f) in self.pending
                      and self._key(unit, f) not in self.repaired]
            if fields:
                await self._check_control()
                await self._unit(study, {**unit, 'fields': fields}, repair=True)
                self.repaired.update(self._key(unit, f) for f in fields)
                await self._checkpoint()
        self._complete = True

    @staticmethod
    def _key(unit, field):
        return unit['item']['id'] + ':' + field['id']

    async def _checkpoint(self):
        self.context.workflow_state = self.snapshot()
        await CheckpointManager(CheckpointRepository(self.service.database)).save(self.context, 'research_unit')

    async def _check_control(self):
        run = await RunRepository(self.service.database).get(self.context.run_id)
        if run is None or run.cancellation_requested:
            raise RunCancelled('研究已取消')
        if json.loads(run.config_snapshot_json).get('pause_requested'):
            raise ResearchPauseRequested()
        await self.store.assert_allowed(self.context.run_id)

    async def _save_cell(self, study, cell):
        await self.store.save_cell(study['study_id'], study['current_revision'], self.context.run_id, cell)
        await self.events.publish(self.context.run_id, 'research.cell_updated', stage='executing',
            payload={'study_id': study['study_id'], 'item_id': cell['item_id'],
                     'field_id': cell['field_id'], 'status': cell['status']})

    async def _defer(self, study, unit, field, exc, stage, attempts):
        key = self._key(unit, field)
        detail = failure_detail(exc, stage, attempts)
        self.pending[key] = detail
        await self._save_cell(study, {'item_id': unit['item']['id'], 'field_id': field['id'],
            'status': 'pending_retry', 'value': None, 'citations': [], 'reason': detail['reason'],
            'failure': detail, 'search_log': self.cache.get(unit['task_id'], {}).get('search_log', [])})
        await self._checkpoint()

    async def _unit(self, study, unit, *, repair=False):
        task_id = unit['task_id']
        cache = self.cache.get(task_id)
        if cache is None or not cache.get('retrieved'):
            counter = task_id + ':retrieval'
            limit = 3 if repair else 2
            while (self.attempts.get(counter, 0) < limit
                   and (repair or not cache or not cache.get('retries_exhausted'))):
                await self._check_control()
                self.attempts[counter] = self.attempts.get(counter, 0) + 1
                await self._checkpoint()
                failure = await self._retrieve(study, unit, self.attempts[counter])
                if failure is None:
                    break
                if isinstance(failure, ResearchPauseRequested):
                    self.attempts[counter] -= 1
                    await self._checkpoint()
                    raise failure
                if is_global_failure(failure):
                    raise failure
                for field in unit['fields']:
                    await self._defer(study, unit, field, failure, 'retrieval', self.attempts[counter])
                # Tavily already used its own transport retries. Do not multiply
                # them by restarting the whole driver in the immediate phase.
                if repair or getattr(failure, 'details', {}).get('retries_exhausted'):
                    break
            cache = self.cache.get(task_id, {})
            if not cache.get('retrieved'):
                for field in unit['fields']:
                    if self._key(unit, field) not in self.pending:
                        await self._defer(study, unit, field, CellValidationError('上次检索被中断，未取得可核验结果'),
                                          'retrieval', max(1, self.attempts.get(counter, 0)))
                await self._checkpoint()
                return
        raw_results = [RetrievalResult.from_dict(r) for r in cache['results']]
        for field in unit['fields']:
            key = self._key(unit, field)
            if key in self.finished_fields:
                continue
            counter = key + ':extraction'
            limit = 3 if repair else 2
            while self.attempts.get(counter, 0) < limit:
                await self._check_control()
                self.attempts[counter] = self.attempts.get(counter, 0) + 1
                await self._checkpoint()
                stage = 'extraction'
                cells = None
                try:
                    self._require_available_body(field, raw_results)
                    if raw_results:
                        extraction_spec = {**study['spec'], '_extraction_feedback': self.pending.get(key)}
                        cells = await self.service.call_model(self.context.run_id, 'extract', extraction_spec,
                                                             unit['item'], [field], raw_results)
                    else:
                        cells = [{'item_id': unit['item']['id'], 'field_id': field['id'], 'status': 'not_found',
                                  'value': None, 'reason': '本次指定范围内检索未取得可用证据', 'citations': []}]
                    stage = 'validation'
                    cell = self._validate_cell(cells, unit['item'], field, raw_results)
                    cell['search_log'] = cache['search_log']
                except Exception as exc:
                    if isinstance(exc, ResearchPauseRequested):
                        self.attempts[counter] -= 1
                        await self._checkpoint()
                        raise
                    if is_global_failure(exc):
                        raise
                    if isinstance(exc, CellValidationError):
                        if stage == 'validation':
                            self._log_cell_failure(exc, unit, field, cells, raw_results, self.attempts[counter])
                        stage = 'validation'
                    await self._defer(study, unit, field, exc, stage, self.attempts[counter])
                    continue
                # Persistence and scope conflicts must never be swallowed as a
                # model defect; only typed evidence validation is recoverable.
                try:
                    await self._save_cell(study, cell)
                except CellValidationError as exc:
                    if not exc.details.get('validation_errors'):
                        from .extraction import validation_error
                        exc = validation_error(exc.message, 'cell', exc.message, cell)
                    self._log_cell_failure(exc, unit, field, cells, raw_results, self.attempts[counter])
                    await self._defer(study, unit, field, exc, 'validation', self.attempts[counter])
                    continue
                self.finished_fields.add(key)
                self.pending.pop(key, None)
                await self._checkpoint()
                break
            if key not in self.finished_fields and key not in self.pending:
                await self._defer(study, unit, field, CellValidationError('上次抽取被中断，未取得可核验结果'),
                                  'extraction', max(1, self.attempts.get(counter, 0)))

    def _log_cell_failure(self, exc, unit, field, cells, results, attempt):
        from .diagnostics import save_model_failure
        save_model_failure(operation='validate_cell', model=None,
            prompt={'item': unit['item'], 'field': field, 'sources': [r.to_dict() for r in results]},
            raw_response=None, parsed_response=cells, error=exc, attempt=attempt,
            run_id=self.context.run_id)

    async def _retrieve(self, study, unit, attempt):
        item, fields = unit['item'], unit['fields']
        query = '\n'.join([
            '请仅调查 item 对象与 fields 指定字段，优先版本对应官方正文，主动核对限制和反例。'
            'questions 只是决策背景，其中出现的其他对象不能成为本单元的新调查目标。',
            json.dumps({'questions': study['spec']['questions'], 'hard_constraints': study['spec']['hard_constraints'],
                        'scope': study['spec']['scope'], 'source_policy': study['spec']['source_policy'],
                        'stop_conditions': study['spec']['stop_conditions'],
                        'item': item, 'fields': fields}, ensure_ascii=False),
        ])
        child = self.context.model_copy(deep=True)
        child.original_query = child.resolved_query = child.model_input = query
        child.workflow_state = {}
        child.config_snapshot['research_unit'] = {'item_id': item['id'], 'field_ids': [f['id'] for f in fields]}
        driver = self.factory(child, self.events)
        await self.events.publish(self.context.run_id, 'task.started', stage='executing',
                                  payload={'task_id': unit['task_id'], 'description': item['name']})
        failure = None
        try:
            await driver.plan()
            await driver.execute()
        except Exception as exc:
            failure = exc
        provider = getattr(driver, 'research_provider', None)
        failure = getattr(provider, 'fatal_failure', None) or failure or getattr(provider, 'last_failure', None)
        records = driver.execution_records()
        trajectory = PlanTaskToolRepository(self.service.database)
        for record in records:
            record.task_id = unit['task_id']
            record.record_id = unit['task_id'] + '_' + str(attempt) + '_' + record.record_id
            for call in record.tool_calls:
                call.tool_call_id = unit['task_id'] + '_' + str(attempt) + '_' + call.tool_call_id
                await trajectory.prepare_tool_call(tool_call_id=call.tool_call_id, run_id=self.context.run_id,
                    task_id=record.task_id, tool_name=call.tool_name, source_mode='web', args=call.args)
                await trajectory.complete_tool_call(call.tool_call_id, result=call.result,
                    error_code='TOOL_FAILED' if call.status == 'failed' else None)
            self.records.append(record)
        raw_results = []
        for _, _, provider, result in driver.evidence_results():
            await self.ledger.record_results(run_id=self.context.run_id, task_id=unit['task_id'],
                tool_call_id=records[0].tool_calls[0].tool_call_id if records and records[0].tool_calls else None,
                provider=str(provider), results=[result])
            if result.result_id not in {r.result_id for r in raw_results}:
                raw_results.append(result)
            if result.result_id not in {r.result_id for r in self.results}:
                self.results.append(result)
        if failure is None and (any(call.status == 'failed' for record in records for call in record.tool_calls)
                                or not any(record.tool_calls for record in records)):
            failure = ValueError('当前单元检索未成功，不能标为未找到')
        actual_queries = list(dict.fromkeys(q for r in raw_results for q in r.metadata.extra.get('search_queries', [])))
        self.cache[unit['task_id']] = {'retrieved': failure is None,
            'retries_exhausted': bool(getattr(failure, 'details', {}).get('retries_exhausted')),
            'results': [r.to_dict() for r in raw_results],
            'search_log': ([{'query': q} for q in actual_queries] if actual_queries else
                           [call.args for record in records for call in record.tool_calls])}
        await self._checkpoint()
        return failure

    @staticmethod
    def _require_available_body(field, results):
        from .extraction import require_available_body
        require_available_body(field, results)

    @staticmethod
    def _validate_cell(cells, item, field, raw_results):
        from .extraction import validate_bound_cell
        return validate_bound_cell(cells, item, field, raw_results)

    async def report(self):
        study = await self.store.assert_allowed(self.context.run_id)
        matrix = await self.store.matrix(study['study_id'])
        if matrix['counts'].get('pending_retry') or not any(c.get('citations') for c in matrix['cells']):
            self._conservative_report = True
        cells = json.loads(json.dumps(matrix['cells']))
        for cell in cells:
            for citation in cell.get('citations', []):
                citation['evidence_id'] = self.aliases.get(citation['evidence_id'], citation['evidence_id'])
        if self._conservative_report:
            names = {item['id']: item['name'] for item in matrix['items']}
            labels = {field['id']: field['label'] for field in matrix['fields']}
            summary = '\n\n'.join(
                f"- {names[c['item_id']]} 的 {labels[c['field_id']]}（{'推断' if c['status'] == 'inference' else '来源冲突' if c['status'] == 'conflict' else '有证据支持'}）："
                + str(c['value']).replace('\n', ' ') + ' '
                + ' '.join(dict.fromkeys('['+ref['evidence_id']+']' for ref in c['citations']))
                for c in cells if c['status'] in {'supported', 'inference', 'conflict'} and c.get('citations')
            )
        else:
            try:
                summary = await self.service.call_model(self.context.run_id, 'synthesize', study['spec'], cells)
            except ValueError:
                self._conservative_report = True
                return await self.report()
        self._report = self.render(study['spec'], matrix, cells, summary)
        return self._report

    @staticmethod
    def render(spec, matrix, cells, summary=''):
        def clean(value):
            return str(value).replace('|', '\\|').replace('\n', ' ')
        names = {i['id']: i['name'] for i in matrix['items']}
        labels = {f['id']: f['label'] for f in matrix['fields']}
        statuses = {'supported': '有证据支持', 'inference': '推断', 'conflict': '存在冲突',
                    'not_found': '未找到', 'not_applicable': '不适用', 'missing': '尚未调查', 'stale': '需要复查',
                    'pending_retry': '待补查'}
        incomplete = (any(c['status'] in {'missing', 'stale', 'pending_retry'} for c in cells)
                      or not any(c.get('citations') for c in cells))
        lines = ['# '+spec['title']+('（部分报告）' if incomplete else ''), '', summary, '', '## 比较结果', '', '| 对象 | 字段 | 结果 | 证据状态 |', '|---|---|---|---|']
        for cell in cells:
            refs = ' '.join('['+c['evidence_id']+']' for c in cell.get('citations', []))
            value = cell.get('value')
            if value is None:
                value = cell.get('reason') or statuses[cell['status']]
            lines.append(f"| {clean(names[cell['item_id']])} | {clean(labels[cell['field_id']])} | {clean(value)} {refs} | {statuses[cell['status']]} |")
        lines += ['', '## 局限', '', '结论受当前版本、检索时间和可访问资料范围限制。推断与冲突需结合原始资料判断；未找到不代表不存在。']
        if incomplete:
            lines += ['', '本报告为部分结果，仍有证据缺口或尚未完成核查的字段，不能视为全部完成。']
        return '\n'.join(lines)

    async def repair_report(self, failures):
        return False

    def snapshot(self):
        return {'research_units': self.units, 'research_done': sorted(self.done),
                'research_records': [r.model_dump(mode='json') for r in self.records],
                'research_results': [r.to_dict() for r in self.results], 'research_aliases': self.aliases,
                'research_report': self._report, 'research_execution_complete': self._complete,
                'research_pending': self.pending, 'research_unit_cache': self.cache,
                'research_finished_fields': sorted(self.finished_fields), 'research_repaired': sorted(self.repaired),
                'research_attempts': self.attempts,
                'research_conservative_report': self._conservative_report}

    def execution_records(self):
        return self.records

    def evidence_results(self):
        return [(None, None, 'research', r) for r in self.results]

    def plan_record(self):
        tasks = [{'task_id': u['task_id'], 'task_type': 'deep_research', 'source_mode': 'web',
                  'description': u['item']['name'], 'status': 'pending_retry' if any(self._key(u, f) in self.pending for f in u['fields'])
                  else 'completed' if u['task_id'] in self.done else 'pending'} for u in self.units]
        return {'plan_id': f'plan_{self.context.run_id}', 'version': self.context.plan_version,
                'status': 'executing', 'tasks': tasks}, tasks

    def report_consistency(self):
        return bool(self._report)

    def evidence_card_coverage(self):
        return None

    def report_metrics(self):
        return {}

    def delivery_status(self):
        return {'incomplete_tasks': [u['task_id'] for u in self.units if u['task_id'] not in self.done
                or any(self._key(u, f) in self.pending for f in u['fields'])], 'budget_limited': False}

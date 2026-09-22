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
        self.ledger = EvidenceLedger(EvidenceRepository(service.database))

    async def plan(self, failures=None):
        study = await self.store.assert_allowed(self.context.run_id)
        matrix = await self.store.matrix(study['study_id'])
        targets = self.context.config_snapshot.get('research_targets')
        force = {(c['item_id'], c['field_id']) for c in targets or []}
        wanted = force if targets is not None else {(c['item_id'], c['field_id']) for c in matrix['cells']
                  if c['status'] in {'missing', 'stale'}}
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
            if cell['status'] in {'missing', 'stale'}:
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
            run = await RunRepository(self.service.database).get(self.context.run_id)
            if run.cancellation_requested:
                raise RunCancelled('研究已取消')
            if json.loads(run.config_snapshot_json).get('pause_requested'):
                raise ResearchPauseRequested()
            if unit['task_id'] in self.done:
                continue
            await self.store.assert_allowed(self.context.run_id)
            await self._unit(study, unit)
            self.done.add(unit['task_id'])
            self.context.workflow_state = self.snapshot()
            await CheckpointManager(CheckpointRepository(self.service.database)).save(self.context, 'research_unit')
        self._complete = True

    async def _unit(self, study, unit):
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
        await driver.plan()
        failure = None
        try:
            await driver.execute()
        except Exception as exc:
            failure = exc
        records = driver.execution_records()
        trajectory = PlanTaskToolRepository(self.service.database)
        for record in records:
            record.task_id = unit['task_id']
            record.record_id = unit['task_id'] + '_' + record.record_id
            for call in record.tool_calls:
                call.tool_call_id = unit['task_id'] + '_' + call.tool_call_id
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
        self.context.workflow_state = self.snapshot()
        if failure:
            raise failure
        requested_fields = {f['id'] for f in fields}
        if not raw_results:
            if not any(record.tool_calls for record in records):
                raise ValueError('当前单元没有实际检索记录，不能标为未找到')
            cells = [{'item_id': item['id'], 'field_id': f['id'], 'status': 'not_found', 'value': None,
                      'reason': '本次指定范围内检索未取得可用证据', 'citations': []} for f in fields]
        else:
            cells = await self.service.call_model(self.context.run_id, 'extract', study['spec'], item, fields, raw_results)
        sources = {r.result_id: r for r in raw_results}
        for cell in cells:
            if cell.get('item_id') != item['id'] or cell.get('field_id') not in requested_fields:
                raise ValueError('抽取结果试图改变已批准的对象或字段')
            for citation in cell.get('citations', []):
                source = sources.get(citation.get('evidence_id'))
                locator = str(citation.get('locator') or '').strip()
                if source is None or not locator or locator not in str(source.evidence):
                    raise ValueError('证据定位不在实际读取的来源中')
                citation['content_hash'] = source.metadata.content_hash
                citation['access'] = source.metadata.extra.get('access', 'snippet')
            field = next(f for f in fields if f['id'] == cell['field_id'])
            if field.get('required_access') == 'full_text' and any(c['access'] != 'full_text' for c in cell.get('citations', [])):
                cell.update(status='not_found', value=None, citations=[],
                            reason='已检索到相关片段，但尚未取得满足本字段要求的正文证据')
            cell['search_log'] = [call.args for record in records for call in record.tool_calls]
            await self.store.save_cell(study['study_id'], study['current_revision'], self.context.run_id, cell)
            await self.events.publish(self.context.run_id, 'research.cell_updated', stage='executing',
                payload={'study_id': study['study_id'], 'item_id': item['id'], 'field_id': cell['field_id']})

    async def report(self):
        study = await self.store.assert_allowed(self.context.run_id)
        matrix = await self.store.matrix(study['study_id'])
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
                    'not_found': '未找到', 'not_applicable': '不适用', 'missing': '尚未调查', 'stale': '需要复查'}
        lines = ['# '+spec['title'], '', summary, '', '## 比较结果', '', '| 对象 | 字段 | 结果 | 证据状态 |', '|---|---|---|---|']
        for cell in cells:
            refs = ' '.join('['+c['evidence_id']+']' for c in cell.get('citations', []))
            value = cell.get('value')
            if value is None:
                value = cell.get('reason') or statuses[cell['status']]
            lines.append(f"| {clean(names[cell['item_id']])} | {clean(labels[cell['field_id']])} | {clean(value)} {refs} | {statuses[cell['status']]} |")
        lines += ['', '## 局限', '', '结论受当前版本、检索时间和可访问资料范围限制。推断与冲突需结合原始资料判断；未找到不代表不存在。']
        if matrix['counts']['missing'] or matrix['counts']['stale']:
            lines += ['', '本报告为部分结果，仍有尚未调查或需要复查的字段。']
        return '\n'.join(lines)

    async def repair_report(self, failures):
        return False

    def snapshot(self):
        return {'research_units': self.units, 'research_done': sorted(self.done),
                'research_records': [r.model_dump(mode='json') for r in self.records],
                'research_results': [r.to_dict() for r in self.results], 'research_aliases': self.aliases,
                'research_report': self._report, 'research_execution_complete': self._complete,
                'research_conservative_report': self._conservative_report}

    def execution_records(self):
        return self.records

    def evidence_results(self):
        return [(None, None, 'research', r) for r in self.results]

    def plan_record(self):
        tasks = [{'task_id': u['task_id'], 'task_type': 'deep_research', 'source_mode': 'web',
                  'description': u['item']['name'], 'status': 'completed' if u['task_id'] in self.done else 'pending'} for u in self.units]
        return {'plan_id': f'plan_{self.context.run_id}', 'version': self.context.plan_version,
                'status': 'executing', 'tasks': tasks}, tasks

    def report_consistency(self):
        return bool(self._report)

    def evidence_card_coverage(self):
        return None

    def report_metrics(self):
        return {}

    def delivery_status(self):
        return {'incomplete_tasks': [u['task_id'] for u in self.units if u['task_id'] not in self.done], 'budget_limited': False}

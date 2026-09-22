"""Opt-in, isolated real-provider research comparison (never uses the application DB)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
import uuid

os.environ['LEARNING_REVIEW_ENABLED'] = 'false'
os.environ['MEMORY_BACKGROUND_REVIEW_ENABLED'] = 'false'


CASES = [
    {'id': 'checkpoint', 'objects': ['LangGraph', 'AutoGen'], 'field': 'checkpoint',
     'label': '持久化检查点', 'question': '比较 LangGraph 与 AutoGen 是否支持持久化检查点；仅依据官方文档，区分原生能力与需要自行实现的部分。'},
    {'id': 'license', 'objects': ['FastAPI', 'Flask'], 'field': 'license',
     'label': '开源许可证', 'question': '比较 FastAPI 与 Flask 当前开源许可证，仅根据官方仓库 LICENSE 文件，不作法律建议。'},
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='明确启用真实模型和检索调用')
    parser.add_argument('--limit', type=int, default=2)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--baseline', action='store_true', help='另外运行同预算旧研究链路')
    parser.add_argument('--case', choices=[c['id'] for c in CASES], help='只运行指定固定案例')
    parser.add_argument('--output', default='.local-run/workbench/live')
    args = parser.parse_args()
    if not args.live:
        parser.error('真实调用需要 --live；使用公开资料，不会读取生产会话')
    from fastapi.testclient import TestClient
    from backend.app.main import create_app
    from backend.app.schemas import MessageCreate, RunCreate
    from deepresearch_agent.harness import SourceMode, WorkflowMode
    from deepresearch_agent.config import settings

    root = Path(args.output).resolve() / time.strftime('%Y%m%d-%H%M%S')
    root.mkdir(parents=True)
    app = create_app(database_url=f'sqlite+aiosqlite:///{(root/"eval.db").as_posix()}',
                     artifact_root=root/'artifacts', skills_root=root/'skills', auto_resume=False)
    results = []
    with TestClient(app) as client:
        def wait(run_id, waiting=False):
            deadline = time.monotonic()+args.timeout
            prior = None
            while time.monotonic() < deadline:
                run = client.get(f'/api/v1/runs/{run_id}').json()
                if run['status'] != prior:
                    print(run_id, run['status'], run.get('error_message') or '', flush=True)
                    prior = run['status']
                if run['status'] in {'completed', 'failed', 'budget_exhausted', 'cancelled'} or waiting and run['status']=='awaiting_scope_approval':
                    return run
                time.sleep(2)
            client.post(f'/api/v1/runs/{run_id}/cancel')
            raise TimeoutError(f'case timeout: {run_id}')

        for case in [c for c in CASES[:args.limit] if args.case is None or c['id'] == args.case]:
            session = client.post('/api/v1/sessions', json={'title': case['question'][:80]}).json()['session_id']
            created = client.post(f'/api/v1/sessions/{session}/messages', json={
                'content': case['question'], 'source_mode':'web', 'workflow_mode':'deep_research',
                'client_message_id': uuid.uuid4().hex}).json()
            first = wait(created['run_id'], waiting=True)
            if first['status'] != 'awaiting_scope_approval':
                results.append({'case':case['id'], 'arm':'workbench', 'run':first})
                continue
            base = '/api/v1/research/'+first['study_id']
            study = client.get(base).json()
            spec = study['spec']
            spec.update(items=[{'id':f'item_{i}', 'name':name, 'version':'当前官方版本', 'rationale':'比较对象'} for i,name in enumerate(case['objects'])],
                fields=[{'id':case['field'], 'label':case['label'], 'description':case['label'],
                         'evidence_requirement':'官方正文', 'required_access': 'full_text', 'applies_to':[]}],
                allowed_domains=['github.com', 'fastapi.tiangolo.com', 'flask.palletsprojects.com'] if case['id']=='license' else ['docs.langchain.com', 'langchain-ai.github.io', 'microsoft.github.io', 'learn.microsoft.com'],
                budget={'max_search_calls':12, 'max_active_seconds':args.timeout, 'max_llm_tokens':200000})
            edited = client.post(base+'/revisions', json={'revision':study['current_revision'],
                'fingerprint':study['fingerprint'], 'spec':spec})
            edited.raise_for_status()
            study = edited.json()
            approved = client.post(base+'/approve', json={'revision':study['current_revision'],
                'fingerprint':study['fingerprint'], 'client_request_id':uuid.uuid4().hex})
            approved.raise_for_status()
            run = wait(approved.json()['run_id'])
            time.sleep(.5)
            bundle = client.get(base+'/export?format=json').json()
            result = {'case':case['id'], 'arm':'workbench', 'model':settings.OPENAI_LLM_MODEL,
                      'run':run, 'study':bundle['study'], 'matrix':bundle['matrix'],
                      'semantic_support':'requires_human_review'}
            results.append(result)
            (root/(case['id']+'-workbench.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
            if args.baseline:
                owner = app.state.run_service
                async def baseline():
                    _, old, _ = await owner.runs.create_for_user_message(
                        MessageCreate(session_id=session,role='user',content=case['question'],client_message_id=uuid.uuid4().hex),
                        RunCreate(session_id=session,trigger_message_id='assigned-atomically',source_mode=SourceMode.WEB,
                            workflow_mode=WorkflowMode.DEEP_RESEARCH,
                            config_snapshot={'min_evidence':1,'deep_research_max_iterations':2,'evaluation_run':True},
                            budget={**settings.HARNESS_BUDGETS,'max_tavily_calls':12,'max_tool_calls':12,'max_llm_tokens':200000,'wall_time_seconds':args.timeout}))
                    owner.schedule(old.run_id)
                    return old.run_id
                old_id = client.portal.call(baseline)
                old = wait(old_id)
                results.append({'case':case['id'],'arm':'legacy','model':settings.OPENAI_LLM_MODEL,
                                'run':old,'report':client.get(f'/api/v1/runs/{old_id}/report').json(),
                                'semantic_support':'requires_human_review'})
            (root/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
    (root/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
    print('RESULTS',root/'results.json',flush=True)


if __name__ == '__main__':
    main()

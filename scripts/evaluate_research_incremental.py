"""Continue an isolated live evaluation with one new field; preserve its budget."""
import argparse
import json
import os
from pathlib import Path
import time

os.environ['LEARNING_REVIEW_ENABLED'] = 'false'
os.environ['MEMORY_BACKGROUND_REVIEW_ENABLED'] = 'false'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', help='evaluate_research_workbench 输出目录')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--max-active-seconds', type=int, help='明确修订并确认新的项目总时间预算')
    parser.add_argument('--max-llm-tokens', type=int, help='明确修订并确认新的项目总 Token 预算')
    args = parser.parse_args()
    root = Path(args.directory).resolve()
    safe_root = Path('.local-run/workbench/live').resolve()
    if not args.live or not root.is_relative_to(safe_root):
        parser.error('需要 --live，且仅允许延续 .local-run/workbench/live 下的隔离评测')
    from fastapi.testclient import TestClient
    from backend.app.main import create_app

    baseline = json.loads((root/'license-workbench.json').read_text(encoding='utf8'))
    app = create_app(database_url=f'sqlite+aiosqlite:///{(root/"eval.db").as_posix()}',
                     artifact_root=root/'artifacts', skills_root=root/'skills', auto_resume=False)
    with TestClient(app) as client:
        base = '/api/v1/research/'+baseline['study']['study_id']
        before = client.get(base).json()
        spec = before['spec']
        old_budget = dict(spec['budget'])
        if args.max_active_seconds is not None:
            spec['budget']['max_active_seconds'] = args.max_active_seconds
        if args.max_llm_tokens is not None:
            spec['budget']['max_llm_tokens'] = args.max_llm_tokens
        if spec['budget'] != old_budget:
            print('EXPLICIT BUDGET REVISION', old_budget, '->', spec['budget'], flush=True)
        if not any(f['id'] == 'repository_url' for f in spec['fields']):
            spec['fields'].append({'id':'repository_url','label':'官方仓库地址',
                'description':'官方文档指向的源代码仓库 URL', 'evidence_requirement':'官方来源',
                'required_access':'snippet', 'applies_to':[spec['items'][0]['id']]})
        response = client.post(base+'/revisions', json={'revision':before['current_revision'],
            'fingerprint':before['fingerprint'], 'spec':spec})
        response.raise_for_status()
        revised = response.json()
        response = client.post(base+'/approve', json={'revision':revised['current_revision'],
            'fingerprint':revised['fingerprint'], 'client_request_id':'incremental-repository-url'})
        response.raise_for_status()
        run_id = response.json()['run_id']
        deadline = time.monotonic()+600
        previous = None
        while time.monotonic() < deadline:
            run = client.get('/api/v1/runs/'+run_id).json()
            if run['status'] != previous:
                print(run_id, run['status'], run.get('error_message') or '', flush=True)
                previous = run['status']
            if run['status'] in {'completed','failed','cancelled','budget_exhausted'}:
                break
            time.sleep(2)
        else:
            client.post('/api/v1/runs/'+run_id+'/cancel')
            raise TimeoutError(run_id)
        time.sleep(.5)
        bundle = client.get(base+'/export?format=json').json()
        after = bundle['study']['usage']
        result = {'run':run, 'model':baseline['model'], 'before_usage':before['usage'],
            'after_usage':after, 'delta':{k:after[k]-before['usage'][k] for k in after},
            'budget_increased':spec['budget'] != old_budget, 'previous_budget':old_budget,
            'reused_cells':sum(bool(c.get('origin_run_id') and c['origin_run_id'] != run_id) for c in bundle['matrix']['cells']),
            **bundle, 'semantic_support':'requires_human_review'}
        target = root/f'incremental-r{revised["current_revision"]}.json'
        target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
        print('RESULTS',target,flush=True)


if __name__ == '__main__':
    main()

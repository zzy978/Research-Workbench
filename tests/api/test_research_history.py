"""History reads and restores must preserve immutable research specifications."""
from copy import deepcopy
import asyncio
import threading

from test_research_workbench import WebDriver, research_client, start, wait_run


def setup_history(client):
    created = start(client)
    run = wait_run(client, created['run_id'], {'awaiting_scope_approval'})
    base = f"/api/v1/research/{run['study_id']}"
    original = client.get(base).json()
    spec = deepcopy(original['spec'])
    spec['title'] = '修订后的课题'
    spec['scope']['exclusion'] = ['排除未公开资料']
    response = client.post(base + '/revisions', json={**version(original), 'spec': spec})
    assert response.status_code == 200, response.text
    return base, original, client.get(base).json()


def version(study):
    return {'revision': study['current_revision'], 'fingerprint': study['fingerprint']}


def test_history_reads_full_immutable_specs_without_mutating_current(research_client):
    client = research_client
    base, original, current = setup_history(client)
    response = client.get(base + '/revisions')
    assert response.status_code == 200
    history = response.json()
    assert [row['revision'] for row in history] == list(range(current['current_revision'], 0, -1))
    for study in (original, current):
        detail = client.get(base + f"/revisions/{study['current_revision']}")
        assert detail.status_code == 200
        assert detail.json()['spec'] == study['spec']
        assert detail.json()['fingerprint'] == study['fingerprint']
    assert client.get(base).json() == current
    assert client.get(base + '/revisions/9999').status_code == 404
    assert client.get('/api/v1/research/missing/revisions').status_code == 404
    assert client.get(base + '/revisions/0').status_code == 422


def test_restore_creates_new_unapproved_version_and_rejects_stale_retry(research_client, monkeypatch):
    client = research_client
    base, original, current = setup_history(client)

    async def no_model(*args, **kwargs):
        raise AssertionError('Restoring saved content must not call a model')

    service = client.app.state.run_service.research
    call_model = service.call_model
    monkeypatch.setattr(service, 'call_model', no_model)
    payload = {**version(current), 'source_revision': original['current_revision']}
    response = client.post(base + '/restore', json=payload)
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored['current_revision'] == current['current_revision'] + 1
    assert restored['spec'] == original['spec']
    assert restored['fingerprint'] == original['fingerprint']
    assert restored['status'] == 'draft'
    assert restored['approved_revision'] is None
    assert restored['acceptance'] is None
    assert restored['report'] is None
    assert restored['usage'] == current['usage']
    assert client.get(base + f"/revisions/{current['current_revision']}").json()['spec'] == current['spec']
    assert client.post(base + '/restore', json=payload).status_code == 409
    assert client.post(base + '/restore', json={**version(restored), 'source_revision': 9999}).status_code == 404
    assert client.post(base + '/restore', json={**version(restored), 'source_revision': restored['current_revision']}).status_code == 409
    assert client.get(base).json()['current_revision'] == restored['current_revision']
    # Waiting outline must be rebound so approval executes the restored version.
    assert restored['runs'][-1]['revision'] == restored['current_revision']
    monkeypatch.setattr(service, 'call_model', call_model)
    approved = client.post(base + '/approve', json={**version(restored), 'client_request_id': 'restored'})
    assert approved.status_code == 200, approved.text
    assert approved.json()['revision'] == restored['current_revision']
    assert wait_run(client, approved.json()['run_id'], {'completed', 'failed'})['status'] == 'completed'


def test_saved_diff_includes_actual_before_and_after(research_client):
    base, original, current = setup_history(research_client)
    changes = {row['path']: row for row in current['diff']['changes']}
    assert changes['title'] == {'path': 'title', 'before': original['spec']['title'], 'after': current['spec']['title']}
    assert changes['scope']['before'] == original['spec']['scope']
    assert changes['scope']['after'] == current['spec']['scope']


def test_restore_clears_approval_acceptance_and_report(research_client):
    client = research_client
    base, original, current = setup_history(client)
    response = client.post(base + '/approve', json={**version(current), 'client_request_id': 'before-restore'})
    run_id = response.json()['run_id']
    assert wait_run(client, run_id, {'completed', 'failed'})['status'] == 'completed'
    finished = client.get(base).json()
    response = client.post(base + '/accept', json={**version(finished), 'report_fingerprint': finished['report']['fingerprint']})
    assert response.status_code == 200, response.text
    assert response.json()['acceptance'] is not None
    response = client.post(base + '/restore', json={**version(finished), 'source_revision': original['current_revision']})
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored['approved_revision'] is None and restored['approved_fingerprint'] is None
    assert restored['acceptance'] is None and restored['report'] is None
    assert restored['status'] == 'draft'
    # Final usage can be flushed after the completed status becomes visible.
    assert all(restored['usage'][key] >= value for key, value in finished['usage'].items())
    assert client.get(base + f"/revisions/{current['current_revision']}").json()['spec'] == current['spec']


def test_restore_pauses_active_run_and_invalid_request_does_not(research_client, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    async def slow(self):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.02)

    monkeypatch.setattr(WebDriver, 'execute', slow)
    client = research_client
    owner = client.app.state.run_service
    pause = owner.pause

    async def pause_and_release(run_id):
        result = await pause(run_id)
        release.set()
        return result

    monkeypatch.setattr(owner, 'pause', pause_and_release)
    base, original, current = setup_history(client)
    response = client.post(base + '/approve', json={**version(current), 'client_request_id': 'active-restore'})
    run_id = response.json()['run_id']
    try:
        assert entered.wait(5)
        assert client.post(base + '/restore', json={**version(current), 'source_revision': 9999}).status_code == 404
        assert client.post(base + '/restore', json={**version(original), 'source_revision': 1}).status_code == 409
        assert client.get(f'/api/v1/runs/{run_id}').json()['status'] == 'executing'
        response = client.post(base + '/restore', json={**version(current), 'source_revision': original['current_revision']})
    finally:
        release.set()
    assert response.status_code == 200, response.text
    assert wait_run(client, run_id, {'paused', 'failed'})['status'] == 'paused'
    assert response.json()['spec'] == original['spec']
    assert client.post(f'/api/v1/runs/{run_id}/resume').status_code == 409


def test_restore_waits_for_checkpoint_without_creating_partial_revision(research_client, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    async def held(self):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(.02)

    monkeypatch.setattr(WebDriver, 'execute', held)
    client = research_client
    base, original, current = setup_history(client)
    response = client.post(base + '/approve', json={**version(current), 'client_request_id': 'held-restore'})
    run_id = response.json()['run_id']
    payload = {**version(current), 'source_revision': original['current_revision']}
    try:
        assert entered.wait(5)
        response = client.post(base + '/restore', json=payload)
        assert response.status_code == 409
        assert '正在保存当前进度' in response.json()['error']['message']
        assert client.get(base).json()['current_revision'] == current['current_revision']
    finally:
        release.set()
    assert wait_run(client, run_id, {'paused', 'failed'})['status'] == 'paused'
    response = client.post(base + '/restore', json=payload)
    assert response.status_code == 200, response.text
    assert response.json()['current_revision'] == current['current_revision'] + 1

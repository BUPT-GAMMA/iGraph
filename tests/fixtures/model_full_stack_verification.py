"""Opt-in acceptance against local Compose and model_verification_server.

Usage: python ... USER SESSION
Only a dedicated `model-verification-*` account is accepted. Retains the small
synthetic test dataset/session for browser checks; remove the test user afterward.
"""
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
import httpx


def main(uid, sid):
    assert uid.startswith('model-verification-'), 'Use a disposable verification account'
    client = httpx.Client(base_url='http://localhost:21108', headers={'X-User-ID': uid}, timeout=90)
    def request(method, path, expected=200, **kwargs):
        response = client.request(method, path, **kwargs)
        assert response.status_code == expected, (path, response.status_code, response.text[:500])
        return response.json()
    base = f'/api/sessions/{sid}'
    models = request('GET', '/api/models')['models']
    a = next(item for item in models if item['display_name'] == 'Verification A')
    b = next(item for item in models if item['display_name'] == 'Verification B')
    # The synthetic endpoint accepts the complete toolset. A deliberately small
    # window is tested separately as an expected MODEL_CONTEXT_TOO_LARGE error.
    if b['context_window'] < 131072:
        b = request('POST', '/api/models', json={'config_id': b['config_id'], 'revision': b['revision'],
            'upstream_model_id': 'model-b', 'display_name': 'Verification B',
            'base_url': 'http://host.docker.internal:21119/b/v1', 'context_window': 131072})['model']
    config_revision = b['revision']
    selection = request('GET', base + '/model')
    if selection['model_revision'] == 0:
        assert selection['model'] == a['config_id'], 'Adding B must not switch A'
    elif selection['model'] != a['config_id']:
        selection = request('PATCH', base + '/model', json={'model': a['config_id'], 'model_revision': selection['model_revision']})
    initial_revision = selection['model_revision']
    b_revision = initial_revision + 1
    test = request('POST', '/api/models/test', json={'config_id': b['config_id'], 'revision': config_revision})
    assert test['success']
    dataset = request('POST', '/api/personal-datasets', data={'name': 'Model verification graph'},
                      files={'file': ('model-verification.edgelist', b'1 2\n2 3\n3 1\n', 'text/plain')})['dataset']
    request('PATCH', base + '/dataset', json={'active_dataset_id': dataset['dataset_id']})
    before = request('POST', base + '/graph/visualize', json={})
    choice = request('PATCH', base + '/model', json={'model': b['config_id'], 'model_revision': initial_revision})
    assert choice['model_revision'] == b_revision
    request('PATCH', base + '/model', expected=409, json={'model': a['config_id'], 'model_revision': initial_revision})
    after = request('POST', base + '/graph/visualize', json={})
    for key in ('nodes', 'edges', 'graph_version', 'num_nodes', 'num_edges'):
        assert before.get(key) == after.get(key), key
    print('PASS: connection test, add without switching, stale selection rejection, graph unchanged', flush=True)

    def chat(message, revision):
        response = client.post(base + '/chat/stream', json={'message': message, 'model_revision': revision, 'start_request_id': str(uuid.uuid4())})
        assert response.status_code == 200, response.text[:500]
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
        assert not [event for event in events if event.get('type') == 'error'], events
        return next(event for event in reversed(events) if event.get('type') == 'done')
    done = chat('Synthetic follow-up using B', b_revision)
    assert done['model_snapshot']['model_id'] == 'model-b'
    messages = request('GET', base + '/messages')['messages']
    answers = [item for item in messages if item['role'] == 'assistant']
    assert answers[0]['model_snapshot']['config_id'] == a['config_id']
    assert answers[-1]['model_snapshot']['config_id'] == b['config_id']
    calls = httpx.get('http://localhost:21119').json()
    assert any(item['model'] == 'model-b' and 'assistant' in item['roles'] for item in calls)
    print('PASS: model B receives previous history; historical answers retain actual model identity', flush=True)
    request('PUT', '/api/models/default', json={'config_id': b['config_id']})
    second = request('POST', '/api/sessions', json={'initial_message_preview': 'Default verification'})
    assert second['model'] == b['config_id']
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(chat, 'verification-slow', b_revision)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = client.patch(base + '/model', json={'model': b['config_id'], 'model_revision': b_revision})
            if response.status_code == 409:
                break
            time.sleep(.1)
        else:
            raise AssertionError('Run did not acquire selection lock')
        request('PATCH', base + '/model', expected=409, json={'model': a['config_id'], 'model_revision': b_revision})
        blocked = request('DELETE', f"/api/models/{b['config_id']}?revision={config_revision}", expected=409)
        assert sid in blocked['detail']['sessions']
        request('POST', '/api/models', json={'config_id': b['config_id'], 'revision': config_revision, 'upstream_model_id': 'model-b-edited',
            'display_name': 'Verification B', 'base_url': 'http://host.docker.internal:21119/edited/v1'})
        request('PATCH', '/api/models/state', json={'config_id': b['config_id'], 'revision': config_revision + 1, 'enabled': False})
        assert future.result()['model_snapshot']['model_id'] == 'model-b'
    assert request('GET', base + '/model')['model_unavailable_reason'] == 'MODEL_DISABLED'
    response = client.post(base + '/chat/stream', json={'message': 'Must not send', 'model_revision': b_revision, 'start_request_id': str(uuid.uuid4())})
    assert response.status_code >= 400 and 'MODEL_DISABLED' in response.text
    request('DELETE', f"/api/models/{b['config_id']}?revision={config_revision + 2}")
    assert request('GET', base + '/model')['model_unavailable_reason'] == 'MODEL_REMOVED'
    assert request('GET', base + '/messages')['messages']
    response = client.post('/api/sessions', json={})
    assert response.status_code >= 400, 'Deleted default must not silently fall back'
    request('PATCH', base + '/model', json={'model': a['config_id'], 'model_revision': b_revision})
    assert request('GET', f"/api/sessions/{second['session_id']}/model")['model_unavailable_reason'] == 'MODEL_REMOVED'
    request('PUT', '/api/models/default', json={'config_id': a['config_id']})
    assert request('GET', base + '/model')['model_revision'] == b_revision + 1
    print('PASS: in-flight switch/delete blocked, edit/disable freeze current run, delete/recover, independent sessions, no fallback', flush=True)
    client.close()


if __name__ == '__main__':
    main(*sys.argv[1:])

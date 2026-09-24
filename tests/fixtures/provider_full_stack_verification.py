"""Opt-in provider acceptance against local Compose and the synthetic endpoint.

Usage: python tests/fixtures/provider_full_stack_verification.py model-verification-USER
Creates and removes only its own provider, models and conversation.
"""
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx


def main(uid):
    assert uid.startswith('model-verification-')
    with httpx.Client(base_url='http://localhost:21108', headers={'X-User-ID': uid}, timeout=90) as client:
        def request(method, path, expected=200, **kwargs):
            response = client.request(method, path, **kwargs)
            assert response.status_code == expected, (path, response.status_code, response.text[:300])
            return response.json()
        provider = request('POST', '/api/model-providers', json={'name': 'Provider full-stack check',
            'base_url': 'http://host.docker.internal:21119/provider/v1', 'api_key': 'synthetic-key'})['provider']
        pid, sid = provider['provider_id'], None
        try:
            discovered = request('POST', '/api/model-providers/discover', json=provider)
            assert len(discovered['models']) == 4
            batch = {**provider, 'models': [{'id': 'model-a', 'name': 'Provider A'}, {'id': 'model-b', 'name': 'Provider B'}]}
            assert request('POST', '/api/model-providers/import', json=batch)['added_count'] == 2
            assert request('POST', '/api/model-providers/import', json=batch)['added_count'] == 0
            own = [m for m in request('GET', '/api/models')['models'] if m.get('provider_id') == pid]
            a = next(m for m in own if m['upstream_model_id'] == 'model-a')
            created = request('POST', '/api/sessions', json={'model': a['config_id']})
            sid = created['session_id']
            base = f'/api/sessions/{sid}'
            selection = request('GET', base + '/model')
            def slow_run():
                r = client.post(base + '/chat/stream', json={'message': 'verification-slow',
                    'start_request_id': str(uuid.uuid4()), 'model_revision': selection['model_revision']})
                assert r.status_code == 200, r.text[:300]
                events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith('data: {')]
                assert not [e for e in events if e.get('type') == 'error']
                return next(e for e in reversed(events) if e.get('type') == 'done')
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(slow_run)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    response = client.patch(base + '/model', json={'model': a['config_id'], 'model_revision': selection['model_revision']})
                    if response.status_code == 409:
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError('No active run registered')
                provider = request('POST', '/api/model-providers', json={**provider,
                    'base_url': 'http://host.docker.internal:21119/updated/v1', 'api_key': 'rotated-synthetic', 'enabled': False})['provider']
                assert request('GET', base + '/model')['model_unavailable_reason'] == 'MODEL_PROVIDER_DISABLED'
                rejected = request('DELETE', f"/api/model-providers/{pid}?revision={provider['revision']}", expected=409)
                assert rejected['detail']['code'] == 'MODEL_PROVIDER_IN_USE'
                assert len([m for m in request('GET', '/api/models')['models'] if m.get('provider_id') == pid]) == 2
                done = future.result()
                assert done['model_snapshot']['model_id'] == 'model-a'
            calls = httpx.get('http://localhost:21119').json()
            assert any(c['path'] == '/provider/v1/chat/completions' and c['model'] == 'model-a' for c in calls)
            assert not any(c['path'] == '/updated/v1/chat/completions' for c in calls)
            request('POST', '/api/model-providers/import', expected=409, json=batch)
            print('PASS: discovery, atomic bulk import, duplicate retry, shared-provider edit/disable during real Agent run, frozen endpoint')
        finally:
            if sid:
                request('DELETE', f'/api/sessions/{sid}', expected=202)
            current = next(p for p in request('GET', '/api/model-providers')['providers'] if p['provider_id'] == pid)
            request('DELETE', f"/api/model-providers/{pid}?revision={current['revision']}")
            assert not [m for m in request('GET', '/api/models')['models'] if m.get('provider_id') == pid]
            print('PASS: active-run deletion rejected; populated provider deleted atomically after run completion')


if __name__ == '__main__':
    main(sys.argv[1])

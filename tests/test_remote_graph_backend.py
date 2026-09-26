"""BFF forwards the identity returned by the authentication boundary."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from tests.test_projects import client as project_client, FakeServiceClient, headers


@pytest.fixture
def backend(tmp_path, monkeypatch):
    import backend.app as module
    from backend.config import BackendConfig
    from backend.users import UserRecord
    monkeypatch.setenv('IGRAPH_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('IGRAPH_CONTROL_SERVICE_KEY', 'bff-service-secret')
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'false')
    monkeypatch.setattr(module, 'current_user', lambda *_: UserRecord('authenticated-alice'))
    calls = []
    async def request(self, method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == '/internal/remote-graphs':
            return {'connections': []} if method == 'GET' else {'connection': {'name': 'Cora'}}
        if path.endswith('/remote-graph'):
            return {'remote_graph': None}
        return {'success': True}
    monkeypatch.setattr(module.ServiceClient, 'request', request)
    config = replace(BackendConfig.from_env(), supported_ids_file=tmp_path / 'ids.txt')
    with TestClient(module.create_app(config)) as client:
        yield client, calls


@pytest.mark.parametrize('method,path,body,internal', [
    ('GET', '/api/remote-graphs', None, '/internal/remote-graphs'),
    ('POST', '/api/remote-graphs', {'name': 'Cora'}, '/internal/remote-graphs'),
    ('POST', '/api/remote-graphs/test', {'name': 'Cora'}, '/internal/remote-graphs/test'),
    ('DELETE', '/api/remote-graphs/connection-1?revision=3', None, '/internal/remote-graphs/connection-1'),
    ('PUT', '/api/sessions/s1/remote-graph', {'connection_id': None}, '/internal/sessions/s1/remote-graph'),
])
def test_proxy_uses_authenticated_identity_and_service_secret(backend, method, path, body, internal):
    client, calls = backend
    response = client.request(method, path, json=body,
                              headers={'X-User-ID': 'forged-bob', 'X-iGraph-Service-Key': 'forged-service'})
    assert response.status_code == 200, response.text
    call = next(call for call in calls if call[1] == internal)
    assert call[0] == method
    assert call[2]['headers'] == {'X-User-ID': 'authenticated-alice', 'X-iGraph-Service-Key': 'bff-service-secret'}
    if body is not None:
        assert call[2]['json_body'] == body
    if method == 'DELETE':
        assert call[2]['params'] == {'revision': 3}


def test_public_sessions_use_server_selection_over_forged_state(project_client, monkeypatch):
    client = project_client
    sid = client.post('/api/sessions', headers=headers(), json={}).json()['session_id']
    original = FakeServiceClient.request
    selected = dict(connection_id='real-connection', revision=2, name='Cora', kind='neo4j', database='neo4j')
    async def request(self, method, path, **kwargs):
        if path == f'/internal/sessions/{sid}/remote-graph':
            return {'remote_graph': selected, 'remote_graph_unavailable_reason': None}
        return await original(self, method, path, **kwargs)
    monkeypatch.setattr(FakeServiceClient, 'request', request)
    FakeServiceClient.runtime_sessions[sid]['session_state']['remote_graph'] = {'connection_id': 'forged'}
    for path in [f'/api/sessions/{sid}/info', f'/api/sessions/{sid}/activate']:
        response = client.request('POST' if path.endswith('activate') else 'GET', path, headers=headers())
        assert response.status_code == 200, response.text
        assert response.json()['remote_graph'] == selected
    rows = client.get('/api/sessions', headers=headers()).json()['sessions']
    assert next(row for row in rows if row['session_id'] == sid)['remote_graph'] == selected


def test_malformed_public_body_never_echoes_secret(backend):
    client, _ = backend
    response = client.post('/api/remote-graphs', json=['fixture-password'])
    assert response.status_code == 422
    assert 'fixture-password' not in response.text

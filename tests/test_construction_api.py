"""Public construction routes preserve dataset ownership and reusable results."""
from pathlib import Path

import pytest
from fastapi import HTTPException

from test_projects import FakeServiceClient, client, headers


@pytest.fixture
def engine(monkeypatch):
    states, calls = {}, []
    original = FakeServiceClient.request

    async def request(self, method, path, *, headers, json_body=None, **kwargs):
        if not self.is_runtime and path.startswith('/construction/'):
            calls.append((method, path, headers, json_body))
            dataset_id = path.split('/')[2]
            key = (headers['X-User-ID'], dataset_id)
            if method == 'POST' and path.endswith('/build'):
                states[key] = dict(dataset_id=dataset_id, status='algorithm_unavailable', revision=0,
                                   node_count=0, edge_count=0, message='算法待接入')
            if method == 'POST' and path.endswith('/commit'):
                if json_body['base_revision'] != states[key]['revision']:
                    raise HTTPException(409, {'code': 'REVISION_CONFLICT'})
                states[key]['revision'] += 1
            if method == 'DELETE':
                states.pop(key, None)
                return {'success': True}
            return states.get(key, dict(dataset_id=dataset_id, status='not_built', revision=0))
        if not self.is_runtime and path.endswith('/graph/load_construction'):
            calls.append((method, path, headers, json_body))
            self.graph_loaded[path.split('/')[2]] = True
            return {'success': True}
        return await original(self, method, path, headers=headers, json_body=json_body, **kwargs)

    monkeypatch.setattr(FakeServiceClient, 'request', request)
    return states, calls


@pytest.mark.parametrize('extension', ['sql', 'csv', 'txt'])
def test_raw_upload_saved_without_activation_or_fake_graph(client, engine, extension):
    session = client.post('/api/sessions', headers=headers(), json={}).json()['session_id']
    result = client.post('/api/personal-datasets', headers=headers(),
        files={'file': (f'source.{extension}', b'raw source')}, data={'session_id': session})
    assert result.status_code == 200
    payload = result.json()
    dataset = payload['dataset']
    assert dataset['status'] == 'source_ready'
    assert payload['activated_session_id'] is None
    assert payload['construction_required'] is True
    assert 'storage_path' not in dataset
    endpoint = f"/api/personal-datasets/{dataset['dataset_id']}/construction"
    assert client.get(endpoint, headers=headers('alice')).status_code == 404
    response = client.post(endpoint + '/build', headers=headers(), json={'options': {}, 'source_path': '/etc/passwd'})
    assert response.status_code == 200
    assert response.json()['status'] == 'algorithm_unavailable'
    saved_path = Path(engine[1][-1][3]['source_path'])
    assert saved_path.parent.name == dataset['dataset_id']
    assert saved_path.read_bytes() == b'raw source'
    assert engine[1][-1][2]['X-iGraph-Service-Key'] == 'test-project-key'
    stored = client.get('/api/personal-datasets', headers=headers()).json()['datasets'][0]
    assert stored['status'] == 'source_ready'
    assert stored['metadata']['construction']['status'] == 'algorithm_unavailable'
    assert client.patch(f'/api/sessions/{session}/dataset', headers=headers(),
        json={'active_dataset_id': dataset['dataset_id']}).status_code == 409


def test_saved_graph_activates_by_dataset_and_conflicts_do_not_overwrite(client, engine, tmp_path):
    states, calls = engine
    dataset = client.post('/api/personal-datasets', headers=headers(),
        files={'file': ('source.csv', b'a,b')}).json()['dataset']
    dataset_id = dataset['dataset_id']
    endpoint = f'/api/personal-datasets/{dataset_id}/construction'
    states[('admin', dataset_id)] = dict(dataset_id=dataset_id, status='ready', revision=1,
                                       node_count=2, edge_count=1, directed=True)
    assert client.get(endpoint, headers=headers()).json()['revision'] == 1
    session = client.post('/api/sessions', headers=headers(),
        json={'active_dataset_id': dataset_id}).json()['session_id']
    assert client.post(f'/api/sessions/{session}/activate', headers=headers()).status_code == 200
    assert any(path.endswith('/load_construction') and body == {'dataset_id': dataset_id}
               for _, path, _, body in calls)
    edit = {'base_revision': 1, 'operations': [{'op': 'reverse_edges', 'edge_ids': ['e1']}]}
    assert client.post(endpoint + '/commit', headers=headers(), json=edit).status_code == 200
    assert client.post(endpoint + '/commit', headers=headers(), json=edit).status_code == 409
    stored = client.get('/api/personal-datasets', headers=headers()).json()['datasets'][0]
    assert stored['metadata']['construction']['revision'] == 2
    assert next(tmp_path.rglob('source.csv')).read_bytes() == b'a,b'


def test_project_scope_and_delete_remove_construction(client, engine):
    project = client.post('/api/projects', headers=headers(), json={'name': 'Project'}).json()['project']
    scope = f"/api/projects/{project['project_id']}/datasets"
    dataset = client.post(scope, headers=headers(), files={'file': ('source.sql', b'SELECT 1;')}).json()['dataset']
    endpoint = f"{scope}/{dataset['dataset_id']}/construction"
    assert client.get(f"/api/personal-datasets/{dataset['dataset_id']}/construction", headers=headers()).status_code == 404
    assert client.post(endpoint + '/build', headers=headers(), json={}).status_code == 200
    saved_path = Path(engine[1][-1][3]['source_path'])
    assert client.delete(f"{scope}/{dataset['dataset_id']}", headers=headers()).status_code == 200
    assert ('admin', dataset['dataset_id']) not in engine[0]
    assert not saved_path.exists()


def test_source_preview_bff_preserves_dataset_ownership_and_scope(client, engine):
    project = client.post('/api/projects', headers=headers(), json={'name': 'Preview'}).json()['project']
    scope = f"/api/projects/{project['project_id']}/datasets"
    dataset = client.post(scope, headers=headers(), files={'file': ('source.csv', b'id,name\n1,Alice\n')}).json()['dataset']
    endpoint = f"{scope}/{dataset['dataset_id']}/construction/source-preview"
    assert client.get(endpoint, headers=headers('alice')).status_code == 404
    assert client.get(f"/api/personal-datasets/{dataset['dataset_id']}/construction/source-preview", headers=headers()).status_code == 404
    result = client.get(endpoint, headers={**headers(), 'X-iGraph-Construction-Scope': 'personal'})
    assert result.status_code == 200
    method, path, forwarded, body = engine[1][-1]
    assert method == 'GET' and path.endswith('/source-preview') and body is None
    assert forwarded['X-iGraph-Construction-Scope'] == f"project:{project['project_id']}"

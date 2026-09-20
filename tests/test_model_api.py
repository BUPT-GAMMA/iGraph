import json
from types import SimpleNamespace

import httpx
import pytest
from agno.db.sqlite import SqliteDb
from agno.session.agent import AgentSession
from agno.run.agent import RunOutput
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from agent_runtime.model_api import create_model_router
from agent_runtime.model_catalog import ModelCatalog
from agent_runtime.execution_store import ExecutionStore, ControlError


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv('IGRAPH_CONTROL_SERVICE_KEY', 'model-api-test-only')
    monkeypatch.setenv('QWEN_API_KEY', 'deployment-secret')
    monkeypatch.setenv('QWEN_BASE_URL', 'https://test.example/v1')
    store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/controls.db', connect_args={'check_same_thread': False}))
    store.migrate()
    catalog = ModelCatalog(store, SimpleNamespace(llm_api_key='', llm_base_url=''))
    db = SqliteDb(db_file=str(tmp_path / 'sessions.db'))
    db.upsert_session(AgentSession(session_id='A', agent_id='igraph-agent', user_id='alice',
        session_data={'session_state': {'model': 'qwen3.6-plus'}}, runs=[], created_at=1, updated_at=1))
    app = FastAPI()
    @app.exception_handler(ControlError)
    async def error(request, exc):
        return JSONResponse({'detail': {'code': exc.code}}, status_code=exc.status)
    app.include_router(create_model_router(catalog, db))
    with TestClient(app) as client:
        client.headers.update({'X-User-ID': 'alice', 'X-iGraph-Service-Key': 'model-api-test-only'})
        assert client.post('/internal/model-catalog/bootstrap', json={'allowed': ['qwen3.6-plus']}).status_code == 200
        yield client, catalog
    db.close()


def add(client, **extra):
    result = client.post('/internal/model-catalog', json={
        'upstream_model_id': 'local-model', 'display_name': 'Local', 'base_url': 'http://localhost:1234/v1', **extra})
    assert result.status_code == 200, result.text
    return result.json()['model']


def test_management_requires_service_identity_and_never_returns_secrets(api):
    client, catalog = api
    assert client.get('/internal/model-catalog', headers={'X-iGraph-Service-Key': ''}).status_code == 403
    row = add(client, api_key='private-secret')
    assert 'private-secret' not in json.dumps(row)
    assert 'deployment-secret' not in client.get('/internal/model-catalog').text
    assert client.get('/internal/sessions/A/model-selection', headers={'X-User-ID': 'bob'}).status_code == 404


def test_full_disable_delete_restore_flow(api):
    client, catalog = api
    row = add(client)
    cid = row['config_id']
    chosen = client.patch('/internal/sessions/A/model-selection', json={'model': cid, 'model_revision': 0})
    assert chosen.status_code == 200
    assert chosen.json()['model_revision'] == 1
    assert client.patch('/internal/sessions/A/model-selection', json={'model': 'qwen3.6-plus', 'model_revision': 0}).status_code == 409
    assert client.patch('/internal/model-catalog/state', json={'config_id': cid, 'revision': 1, 'enabled': False}).status_code == 200
    assert client.get('/internal/sessions/A/model-selection').json()['model_unavailable_reason'] == 'MODEL_DISABLED'
    assert client.delete(f'/internal/model-catalog/{cid}?revision=2').status_code == 200
    assert client.get('/internal/sessions/A/model-selection').json()['model_unavailable_reason'] == 'MODEL_REMOVED'
    assert client.patch('/internal/sessions/A/model-selection', json={'model': 'qwen3.6-plus', 'model_revision': 1}).status_code == 200


def test_private_run_resolution_keeps_original_revision_for_paused_resume(api):
    client, catalog = api
    row = add(client, api_key='old-key')
    catalog.select('alice', 'A', row['config_id'], 0)
    run, _ = catalog.store.register('alice', 'A', 'req', 'fp', capture_model=lambda conn: catalog.freeze(conn, 'alice', 'A'))
    catalog.store.acknowledge('alice', 'A', run['run_id'], 1, run_status='paused', terminal_persisted=False)
    catalog.save('alice', {'api_key': 'new-key', 'base_url': 'http://localhost:4321/v1'}, row['config_id'], 1)
    path = f"/internal/sessions/A/runs/{run['run_id']}/model-config"
    assert client.get(path, headers={'X-iGraph-Service-Key': ''}).status_code == 403
    assert client.get(path, headers={'X-User-ID': 'bob'}).status_code == 404
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()['api_key'] == 'old-key'
    assert response.json()['base_url'] == 'http://localhost:1234/v1'
    catalog.store.acknowledge('alice', 'A', run['run_id'], 1, run_status='completed', terminal_persisted=True, agent_stopped=True, preparation_stopped=True)
    assert client.get(path).status_code == 409


def test_pre_run_ingestion_uses_current_session_selection(api):
    client, catalog = api
    row = add(client, api_key='session-key')
    catalog.select('alice', 'A', row['config_id'], 0)
    response = client.get('/internal/sessions/A/model-config')
    assert response.status_code == 200
    assert response.json()['model_id'] == 'local-model'
    assert client.get('/internal/sessions/A/model-config', headers={'X-User-ID': 'bob'}).status_code == 404
    catalog.set_enabled('alice', row['config_id'], False, 1)
    assert client.get('/internal/sessions/A/model-config').json()['detail']['code'] == 'MODEL_DISABLED'


def test_user_deletion_removes_configurations_and_credentials(api):
    client, catalog = api
    add(client, api_key='removed-secret')
    response = client.delete('/internal/model-catalog/user/all')
    assert response.status_code == 200
    assert catalog.list('alice')['models'] == []


def test_provenance_preserves_only_public_per_run_audit_fields(api):
    client, catalog = api
    # Locate the route's native DB through its fixture closure is unnecessary:
    # use a separately owned native run in a small router instance.
    db = SimpleNamespace(get_session=lambda **kwargs: AgentSession(session_id='A', user_id='alice', agent_id='igraph-agent',
        runs=[RunOutput(run_id='old', metadata={'model_snapshot': {'config_id': 'A', 'display_name': 'Old A'}, 'internal': 'not-for-client'})]))
    app = FastAPI(); app.include_router(create_model_router(catalog, db))
    with TestClient(app) as other:
        response = other.get('/internal/sessions/A/model-provenance', headers=dict(client.headers))
    assert response.status_code == 200
    assert response.json()['runs']['old']['model_snapshot']['display_name'] == 'Old A'
    assert 'not-for-client' not in response.text


def test_connection_test_uses_only_synthetic_input_and_selected_endpoint(api, monkeypatch):
    client, catalog = api
    row = add(client, api_key='private-secret')
    captured = []
    original = httpx.AsyncClient
    def respond(request):
        captured.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
    monkeypatch.setattr('agent_runtime.model_api.httpx.AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(respond)))
    response = client.post('/internal/model-catalog/test', json={'config_id': row['config_id'], 'revision': 1})
    assert response.status_code == 200
    assert captured[0].url == 'http://localhost:1234/v1/chat/completions'
    body = json.loads(captured[0].content)
    assert body['messages'] == [{'role': 'user', 'content': 'Reply OK.'}]
    assert body['model'] == 'local-model' and body['tools']
    assert 'private-secret' not in response.text


def test_connection_error_does_not_expose_provider_body(api, monkeypatch):
    client, catalog = api
    row = add(client)
    original = httpx.AsyncClient
    monkeypatch.setattr('agent_runtime.model_api.httpx.AsyncClient', lambda **kwargs: original(**kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text='private upstream information'))))
    response = client.post('/internal/model-catalog/test', json={'config_id': row['config_id'], 'revision': 1})
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'MODEL_AUTH_FAILED'
    assert 'private upstream' not in response.text


@pytest.mark.parametrize('url', ['ftp://example.org', 'http://', 'http://name:password@example.org/v1', 'http://example.org/v1?api_key=secret'])
def test_invalid_or_credential_bearing_urls_rejected(api, url):
    client, _ = api
    result = client.post('/internal/model-catalog', json={'upstream_model_id': 'local', 'base_url': url})
    assert result.status_code == 422

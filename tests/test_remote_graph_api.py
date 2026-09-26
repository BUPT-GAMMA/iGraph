"""Service boundary tests with real catalog, sessions, and ownership checks."""
from types import SimpleNamespace

import pytest
from agno.db.sqlite import SqliteDb
from agno.session.agent import AgentSession
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from agent_runtime.execution_control import ExecutionController
from agent_runtime.execution_scope import RunCancellationManager
from agent_runtime.execution_store import ControlError, ExecutionStore
from agent_runtime.remote_graph.catalog import RemoteGraphCatalog
from agent_runtime.remote_graph.secrets import SecretBox


FORM = dict(name='Cora', uri='bolt://db.example:7687', database='neo4j',
            username='reader', password='fixture-password')


@pytest.fixture
def api(tmp_path, monkeypatch):
    from agent_runtime.remote_graph.api import create_remote_graph_router
    monkeypatch.setenv('IGRAPH_CONTROL_SERVICE_KEY', 'remote-api-service-only')
    store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/catalog.db',
                                         connect_args={'check_same_thread': False}))
    store.migrate()
    catalog = RemoteGraphCatalog(store, SecretBox(Fernet.generate_key().decode()))
    db = SqliteDb(db_file=str(tmp_path / 'sessions.db'))
    for uid in ['alice', 'bob']:
        db.upsert_session(AgentSession(session_id=uid + '-session', user_id=uid,
            agent_id='igraph-agent', session_data={'session_state': {'active_dataset_id': 'local-A'}},
            runs=[], created_at=1, updated_at=1))
    controller = ExecutionController(db, store, RunCancellationManager())
    class Executor:
        error = None
        async def test_connection(self, uid, body):
            if self.error:
                raise ControlError(self.error, 422)
            # Resolve edited credentials through the production persistence boundary.
            resolved = catalog.resolve_write(uid, body)
            assert resolved.password.get_secret_value() == 'fixture-password'
            return dict(success=True, readable=True, read_only=True, database=resolved.public.database)
    executor = Executor()
    app = FastAPI()
    @app.exception_handler(ControlError)
    async def error(request, exc):
        return JSONResponse({'detail': {'code': exc.code}}, status_code=exc.status)
    app.include_router(create_remote_graph_router(catalog, controller, executor))
    with TestClient(app, headers={'X-User-ID': 'alice', 'X-iGraph-Service-Key': 'remote-api-service-only'}) as client:
        yield SimpleNamespace(client=client, catalog=catalog, db=db, executor=executor)
    db.close()


def create(api):
    response = api.client.post('/internal/remote-graphs', json=FORM)
    assert response.status_code == 200, response.text
    assert 'fixture-password' not in response.text
    return response.json()['connection']


def test_service_auth_and_owner_isolation(api):
    client = api.client
    assert client.get('/internal/remote-graphs', headers={'X-iGraph-Service-Key': ''}).status_code == 403
    item = create(api)
    assert client.get('/internal/remote-graphs', headers={'X-User-ID': 'bob'}).json() == {'connections': []}
    assert client.delete(f"/internal/remote-graphs/{item['connection_id']}?revision=1", headers={'X-User-ID': 'bob'}).status_code == 404
    assert client.put('/internal/sessions/bob-session/remote-graph',
        headers={'X-User-ID': 'bob'}, json={'connection_id': item['connection_id']}).status_code == 404
    assert client.put('/internal/sessions/bob-session/remote-graph',
        json={'connection_id': item['connection_id']}).status_code == 404


def test_test_does_not_save_and_save_requires_verified_readonly(api):
    result = api.client.post('/internal/remote-graphs/test', json=FORM)
    assert result.json() == dict(success=True, readable=True, read_only=True, database='neo4j')
    assert api.catalog.list('alice') == []
    api.executor.error = 'REMOTE_GRAPH_READ_ONLY_REQUIRED'
    assert api.client.post('/internal/remote-graphs', json=FORM).status_code == 422
    assert api.catalog.list('alice') == []


def test_select_rechecks_readonly_access_before_activating(api):
    item = create(api)
    api.executor.error = 'REMOTE_GRAPH_READ_ONLY_REQUIRED'
    response = api.client.put('/internal/sessions/alice-session/remote-graph', json={'connection_id': item['connection_id']})
    assert response.status_code == 422
    assert api.client.get('/internal/sessions/alice-session/remote-graph').json()['remote_graph'] is None


def test_edit_blank_secret_cas_and_ownership(api):
    item = create(api)
    form = {**FORM, 'connection_id': item['connection_id'], 'revision': 1, 'password': None}
    assert api.client.post('/internal/remote-graphs/test', json=form).status_code == 200
    assert api.catalog.list('alice')[0].revision == 1
    assert api.client.post('/internal/remote-graphs/test', headers={'X-User-ID': 'bob'}, json=form).status_code == 404
    assert api.client.post('/internal/remote-graphs', json=form).json()['connection']['revision'] == 2
    assert api.client.post('/internal/remote-graphs', json=form).status_code == 409


def test_selection_refresh_deleted_reference_and_unselect_preserve_local_graph(api):
    item = create(api)
    path = '/internal/sessions/alice-session/remote-graph'
    result = api.client.put(path, json={'connection_id': item['connection_id']})
    binding = result.json()['remote_graph']
    assert binding['revision'] == 1
    assert api.client.get(path).json()['remote_graph'] == binding
    assert api.db.get_session(session_id='alice-session', user_id='alice').session_data['session_state']['active_dataset_id'] == 'local-A'
    assert api.client.delete(f"/internal/remote-graphs/{item['connection_id']}?revision=1").status_code == 200
    missing = api.client.get(path).json()
    assert missing['remote_graph'] == binding
    assert missing['remote_graph_unavailable_reason'] == 'REMOTE_GRAPH_NOT_FOUND'
    with api.catalog.store.transaction('alice', 'alice-session') as conn:
        assert api.catalog.freeze(conn, 'alice', 'alice-session').model_dump() == binding
    assert api.client.put(path, json={'connection_id': None}).json()['remote_graph'] is None


@pytest.mark.parametrize('patch', [
    {'uri': 'bolt://reader:fixture-password@db.example:7687'},
    {'name': '', 'password': 'fixture-password'},
    {'user_id': 'bob'},
])
def test_rejects_invalid_dto_without_echoing_credentials(api, patch):
    response = api.client.post('/internal/remote-graphs', json={**FORM, **patch})
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'REMOTE_GRAPH_INVALID_CONFIG'
    assert 'fixture-password' not in response.text
    assert api.catalog.list('alice') == []

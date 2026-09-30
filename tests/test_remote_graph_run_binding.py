import pytest

from agent_runtime.execution_store import ControlError
from tests.test_remote_graph_catalog import catalog, body, binding


def test_old_binding_loads_as_neo4j():
    from agent_runtime.remote_graph.contracts import RemoteBinding
    old = dict(connection_id='old', revision=7, name='Old graph', database='neo4j')
    assert RemoteBinding.model_validate(old).kind == 'neo4j'


def test_old_saved_selection_loads_as_neo4j(catalog):
    from agent_runtime.remote_graph.catalog import selections
    item = catalog.save('alice', body())
    old = dict(connection_id=item.connection_id, revision=1, name='Cora', database='neo4j')
    with catalog.store.transaction('alice', 's1') as conn:
        conn.execute(selections.insert().values(user_id='alice', session_id='s1', binding=old))
    assert catalog.selection('alice', 's1')['remote_graph']['kind'] == 'neo4j'


def test_falkordb_run_freezes_kind(catalog):
    item = catalog.save('alice', body(kind='falkordb', uri='redis://db.example:6379',
                                      database='cora', username=''))
    catalog.select('alice', 's1', item.connection_id)
    run, _ = catalog.store.register('alice', 's1', 'request-1', 'body',
        capture_remote_graph=lambda conn: catalog.freeze(conn, 'alice', 's1'))
    assert run['state']['remote_graph_binding']['kind'] == 'falkordb'


def test_binding_is_frozen_on_idempotent_registration(catalog):
    item = catalog.save('alice', body())
    catalog.select('alice', 's1', item.connection_id)
    first, _ = catalog.store.register('alice', 's1', 'request-1', 'same-body',
        capture_remote_graph=lambda conn: catalog.freeze(conn, 'alice', 's1'))
    catalog.select('alice', 's1', None)
    second, created = catalog.store.register('alice', 's1', 'request-1', 'same-body',
        capture_remote_graph=lambda conn: catalog.freeze(conn, 'alice', 's1'))
    assert not created
    assert second['state']['remote_graph_binding'] == first['state']['remote_graph_binding']
    assert first['state']['remote_graph_binding'] == binding(item).model_dump()
    assert 'fixture-secret' not in str(first) and 'bolt://' not in str(first)


def test_old_run_rejects_changed_connection_and_next_run_gets_new_revision(catalog):
    item = catalog.save('alice', body())
    catalog.select('alice', 's1', item.connection_id)
    first, _ = catalog.store.register('alice', 's1', 'r1', 'same-body',
        capture_remote_graph=lambda conn: catalog.freeze(conn, 'alice', 's1'))
    updated = catalog.save('alice', body(connection_id=item.connection_id, revision=1, password='new'))
    with pytest.raises(ControlError, match='REMOTE_GRAPH_CHANGED'):
        catalog.resolve('alice', binding(item))
    catalog.store.acknowledge('alice', 's1', first['run_id'], 1, preparation_stopped=True,
        agent_stopped=True, terminal_persisted=True, run_status='completed')
    second, _ = catalog.store.register('alice', 's1', 'r2', 'next-body',
        capture_remote_graph=lambda conn: catalog.freeze(conn, 'alice', 's1'))
    assert second['state']['remote_graph_binding'] == binding(updated).model_dump()
    assert catalog.store.get('alice', 's1', run_id=first['run_id'])['state']['remote_graph_binding']['revision'] == 1


def test_deleted_selection_and_missing_key_do_not_block_registration(catalog):
    from agent_runtime.remote_graph.secrets import SecretBox
    item = catalog.save('alice', body())
    catalog.select('alice', 's1', item.connection_id)
    catalog.delete('alice', item.connection_id, 1)
    catalog.secret_box = SecretBox('')
    run, created = catalog.store.register('alice', 's1', 'r1', 'body',
        capture_remote_graph=lambda conn: catalog.freeze(conn, 'alice', 's1'))
    assert created and run['state']['remote_graph_binding']['connection_id'] == item.connection_id
    ordinary, created = catalog.store.register('alice', 'ordinary', 'r2', 'body')
    assert created and ordinary['state'].get('remote_graph_binding') is None


def test_native_start_and_continue_receive_saved_binding_not_current_selection(tmp_path, monkeypatch):
    from agno.agent import AgentFactory
    from agno.db.sqlite import SqliteDb
    from agno.factory import FactoryPermissionError
    from agno.session.agent import AgentSession
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from agent_runtime.config import AgentRuntimeConfig
    monkeypatch.setenv('GRAPH_ENGINE_MCP_URL', 'http://mcp.example/mcp')
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'true')
    monkeypatch.setenv('IGRAPH_CONTROL_SERVICE_KEY', 'binding-test-only')
    monkeypatch.setenv('IGRAPH_REMOTE_GRAPH_SECRET_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('IGRAPH_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('AGENT_RUNTIME_TRACING', 'false')
    from agent_runtime import os_app
    db = SqliteDb(db_file=str(tmp_path / 'native.db'))
    db.upsert_session(AgentSession(session_id='s1', user_id='alice', agent_id='igraph-agent',
        session_data={'session_state': {}}, runs=[], created_at=1, updated_at=1))
    observed = []
    def factory(*args, **kwargs):
        def inspect_context(ctx):
            observed.append(ctx.request.state.igraph_remote_graph_binding)
            raise FactoryPermissionError('stop after inspecting trusted context')
        return AgentFactory(id='igraph-agent', name='binding test', db=db, factory=inspect_context)
    monkeypatch.setattr(os_app, 'create_db', lambda _: db)
    monkeypatch.setattr(os_app, 'create_igraph_factory', factory)
    app = os_app.create_app(AgentRuntimeConfig.from_env())
    with TestClient(app, headers={'X-User-ID': 'alice', 'X-iGraph-Service-Key': 'binding-test-only'}) as client:
        controller = app.state.execution_controller
        catalog = controller.remote_graph_catalog
        first = catalog.save('alice', body())
        second = catalog.save('alice', body(name='Other', database='other'))
        catalog.select('alice', 's1', first.connection_id)
        row, _ = controller.store.register('alice', 's1', 'r1', 'body',
            capture_remote_graph=lambda conn: catalog.freeze(conn, 'alice', 's1'))
        catalog.select('alice', 's1', second.connection_id)
        for suffix in ['', f"/{row['run_id']}/continue"]:
            response = client.post('/agents/igraph-agent/runs' + suffix,
                data={'message': 'hi', 'session_id': 's1', 'run_id': row['run_id'], 'user_id': 'alice',
                      'stream': 'false', 'session_state': '{"remote_graph_binding":{"connection_id":"forged"}}'})
            assert response.status_code == 403, response.text
            assert observed[-1] == binding(first).model_dump()
    db.close()

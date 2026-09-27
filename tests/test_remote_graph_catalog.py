"""Real storage tests for ownership, encryption and optimistic concurrency."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select

from agent_runtime.execution_store import ControlError, ExecutionStore


@pytest.fixture
def catalog(tmp_path):
    from agent_runtime.remote_graph.catalog import RemoteGraphCatalog
    from agent_runtime.remote_graph.secrets import SecretBox
    store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/connections.db',
                                         connect_args={'check_same_thread': False}))
    store.migrate()
    return RemoteGraphCatalog(store, SecretBox(Fernet.generate_key().decode()))


def body(**kwargs):
    from agent_runtime.remote_graph.contracts import ConnectionWrite
    return ConnectionWrite(**(dict(name='Cora', uri='bolt://db.example:7687',
                                  database='neo4j', username='reader',
                                  password='fixture-secret') | kwargs))


def binding(item):
    from agent_runtime.remote_graph.contracts import RemoteBinding
    return RemoteBinding(**item.model_dump())


def test_missing_kind_is_neo4j(catalog):
    item = catalog.save('alice', body())
    assert item.kind == 'neo4j'
    assert catalog.list('alice')[0].kind == 'neo4j'


def test_unknown_kind_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        body(kind='unknown')


def test_kind_cannot_change_on_edit(catalog):
    item = catalog.save('alice', body())
    with pytest.raises(ControlError, match='REMOTE_GRAPH_INVALID_CONFIG'):
        catalog.save('alice', body(connection_id=item.connection_id, revision=1,
                                   kind='falkordb', uri='redis://db.example:6379',
                                   database='other'))
    assert catalog.list('alice')[0].kind == 'neo4j'


def test_kind_cannot_change_on_test_form(catalog):
    item = catalog.save('alice', body())
    with pytest.raises(ControlError, match='REMOTE_GRAPH_INVALID_CONFIG'):
        catalog.resolve_write('alice', body(connection_id=item.connection_id, revision=1,
                                            kind='falkordb', uri='redis://db.example:6379',
                                            database='other'))


def test_binding_kind_must_match_saved_connection(catalog):
    from agent_runtime.remote_graph.contracts import RemoteBinding
    item = catalog.save('alice', body(kind='falkordb', uri='redis://db.example:6379',
                                      database='cora', username=''))
    forged = RemoteBinding(connection_id=item.connection_id, revision=1, name=item.name,
                           kind='neo4j', database=item.database)
    with pytest.raises(ControlError, match='REMOTE_GRAPH_CHANGED'):
        catalog.resolve('alice', forged)


def test_neo4j_still_requires_username():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        body(username='')


def test_falkordb_kind_survives_save_select_freeze_and_blank_password_edit(catalog):
    item = catalog.save('alice', body(kind='falkordb', uri='rediss://db.example:6379',
                                      database='cora', username=''))
    assert item.kind == 'falkordb'
    assert catalog.list('alice')[0].kind == 'falkordb'
    assert catalog.selection_form('alice', item.connection_id).kind == 'falkordb'
    assert catalog.select('alice', 'session', item.connection_id).kind == 'falkordb'
    with catalog.store.transaction('alice', 'session') as conn:
        assert catalog.freeze(conn, 'alice', 'session').kind == 'falkordb'
    edited = catalog.save('alice', body(connection_id=item.connection_id, revision=1,
                                        kind='falkordb', uri='rediss://db.example:6379',
                                        database='cora', username='', password=''))
    assert edited.kind == 'falkordb'
    assert catalog.resolve('alice', binding(edited)).password.get_secret_value() == 'fixture-secret'


@pytest.mark.parametrize('uri', [
    'redis://user:secret@db.example:6379', 'redis://db.example:6379/0',
    'redis://db.example:6379?db=1', 'rediss://db.example:6379#other',
    'bolt://db.example:7687',
])
def test_falkordb_rejects_embedded_credentials_and_target_options(uri):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        body(kind='falkordb', uri=uri, database='cora')


def test_owner_revision_and_secret(catalog):
    from agent_runtime.remote_graph.catalog import connections
    item = catalog.save('alice', body())
    assert item.revision == 1
    assert 'fixture-secret' not in item.model_dump_json()
    assert catalog.list('alice') == [item]
    assert catalog.list('bob') == []
    with pytest.raises(ControlError, match='REMOTE_GRAPH_NOT_FOUND'):
        catalog.resolve('bob', binding(item))
    assert catalog.resolve('alice', binding(item)).password.get_secret_value() == 'fixture-secret'
    with catalog.store.engine.connect() as conn:
        row = dict(conn.execute(select(connections)).mappings().one())
    assert 'fixture-secret' not in str(row)
    assert row['password_ciphertext'] != 'fixture-secret'


@pytest.mark.parametrize('password', [None, ''])
def test_blank_edit_preserves_secret_and_invalidates_old_revision(catalog, password):
    item = catalog.save('alice', body())
    edited = catalog.save('alice', body(connection_id=item.connection_id, revision=1,
                                        name='Renamed', password=password))
    assert edited.revision == 2 and edited.name == 'Renamed'
    assert catalog.resolve('alice', binding(edited)).password.get_secret_value() == 'fixture-secret'
    with pytest.raises(ControlError, match='REMOTE_GRAPH_CHANGED'):
        catalog.resolve('alice', binding(item))
    with pytest.raises(ControlError, match='REMOTE_GRAPH_CHANGED'):
        catalog.save('alice', body(connection_id=item.connection_id, revision=1))


def test_cas_has_exactly_one_winner(catalog):
    item = catalog.save('alice', body())
    def edit(name):
        try:
            return catalog.save('alice', body(connection_id=item.connection_id, revision=1, name=name)).revision
        except ControlError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(edit, ['first', 'second']))
    assert results.count(2) == 1
    assert results.count('REMOTE_GRAPH_CHANGED') == 1


def test_delete_checks_owner_and_revision(catalog):
    item = catalog.save('alice', body())
    with pytest.raises(ControlError, match='REMOTE_GRAPH_NOT_FOUND'):
        catalog.delete('bob', item.connection_id, 1)
    with pytest.raises(ControlError, match='REMOTE_GRAPH_CHANGED'):
        catalog.delete('alice', item.connection_id, 2)
    catalog.delete('alice', item.connection_id, 1)
    assert catalog.list('alice') == []
    with pytest.raises(ControlError, match='REMOTE_GRAPH_NOT_FOUND'):
        catalog.resolve('alice', binding(item))


@pytest.mark.parametrize('password', [None, ''])
def test_create_requires_password(catalog, password):
    with pytest.raises(ControlError, match='REMOTE_GRAPH_INVALID_CONFIG'):
        catalog.save('alice', body(password=password))
    assert catalog.list('alice') == []


def test_wrong_or_missing_key_fails_closed_without_disabling_store(catalog):
    from agent_runtime.remote_graph.catalog import RemoteGraphCatalog
    from agent_runtime.remote_graph.secrets import SecretBox
    item = catalog.save('alice', body())
    for key in ['', 'invalid', Fernet.generate_key().decode()]:
        other = RemoteGraphCatalog(catalog.store, SecretBox(key))
        with pytest.raises(ControlError, match='REMOTE_GRAPH_SECRET_UNAVAILABLE'):
            other.resolve('alice', binding(item))
    unavailable = RemoteGraphCatalog(catalog.store, SecretBox(''))
    with pytest.raises(ControlError, match='REMOTE_GRAPH_SECRET_UNAVAILABLE'):
        unavailable.save('alice', body())
    run, created = catalog.store.register('alice', 'session', 'request', 'digest')
    assert created and run['run_id']


def test_update_other_users_connection_is_not_found(catalog):
    item = catalog.save('alice', body())
    with pytest.raises(ControlError, match='REMOTE_GRAPH_NOT_FOUND'):
        catalog.save('bob', body(connection_id=item.connection_id, revision=1))


def test_rotated_password_is_encrypted_and_binding_has_no_location(catalog):
    item = catalog.save('alice', body())
    item = catalog.save('alice', body(connection_id=item.connection_id, revision=1, password='new-secret'))
    assert catalog.resolve('alice', binding(item)).password.get_secret_value() == 'new-secret'
    assert set(binding(item).model_dump()) == {'connection_id', 'revision', 'name', 'kind', 'database'}


def test_secret_key_config_is_optional_and_not_in_repr(monkeypatch):
    from agent_runtime.config import AgentRuntimeConfig
    monkeypatch.setenv('GRAPH_ENGINE_MCP_URL', 'http://mcp:21110/mcp')
    monkeypatch.delenv('IGRAPH_REMOTE_GRAPH_SECRET_KEY', raising=False)
    assert AgentRuntimeConfig.from_env().remote_graph_secret_key == ''
    monkeypatch.setenv('IGRAPH_REMOTE_GRAPH_SECRET_KEY', 'deployment-secret')
    config = AgentRuntimeConfig.from_env()
    assert config.remote_graph_secret_key == 'deployment-secret'
    assert 'deployment-secret' not in repr(config)

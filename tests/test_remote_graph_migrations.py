"""Legacy connection schema stays readable without touching existing secrets."""
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text


OLD_TABLE = '''
CREATE TABLE igraph_remote_graph_connections (
    user_id VARCHAR NOT NULL,
    connection_id VARCHAR NOT NULL,
    revision INTEGER NOT NULL,
    name VARCHAR NOT NULL,
    uri VARCHAR NOT NULL,
    database VARCHAR NOT NULL,
    username VARCHAR NOT NULL,
    password_ciphertext TEXT NOT NULL,
    PRIMARY KEY (user_id, connection_id)
)
'''


def legacy_engine(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path}/legacy.db')
    with engine.begin() as conn:
        conn.execute(text(OLD_TABLE))
        conn.execute(text('''INSERT INTO igraph_remote_graph_connections
            (user_id, connection_id, revision, name, uri, database, username, password_ciphertext)
            VALUES ('alice', 'connection-1', 17, 'Legacy', 'bolt://db.example:7687',
                    'neo4j', 'reader', 'gAAAAA-opaque-ciphertext')'''))
    return engine


def identity(engine):
    with engine.connect() as conn:
        return conn.execute(text('''SELECT connection_id, revision, password_ciphertext
            FROM igraph_remote_graph_connections''')).one()


def test_migration_keeps_ciphertext_id_and_revision(tmp_path):
    from agent_runtime.remote_graph.migrations import migrate_remote_graph_connections
    engine = legacy_engine(tmp_path)
    before = identity(engine)
    migrate_remote_graph_connections(engine)
    after = identity(engine)
    assert after == before
    assert after.password_ciphertext.encode() == b'gAAAAA-opaque-ciphertext'
    with engine.connect() as conn:
        assert conn.execute(text('SELECT kind FROM igraph_remote_graph_connections')).scalar_one() == 'neo4j'
    engine.dispose()


def test_migration_is_idempotent(tmp_path):
    from agent_runtime.remote_graph.migrations import migrate_remote_graph_connections
    engine = legacy_engine(tmp_path)
    migrate_remote_graph_connections(engine)
    migrate_remote_graph_connections(engine)
    assert identity(engine).revision == 17
    assert [col['name'] for col in inspect(engine).get_columns('igraph_remote_graph_connections')].count('kind') == 1
    engine.dispose()


@pytest.mark.skipif(not os.environ.get('IGRAPH_TEST_POSTGRES_URL'), reason='isolated PostgreSQL URL unavailable')
def test_concurrent_postgres_migration_in_isolated_schema():
    from agent_runtime.remote_graph.migrations import migrate_remote_graph_connections
    url = os.environ['IGRAPH_TEST_POSTGRES_URL']
    schema = 'test_remote_graph_migration_' + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        with engine.begin() as conn:
            conn.execute(text(OLD_TABLE))
            conn.execute(text('''INSERT INTO igraph_remote_graph_connections
                (user_id, connection_id, revision, name, uri, database, username, password_ciphertext)
                VALUES ('alice', 'connection-1', 17, 'Legacy', 'bolt://db.example:7687',
                        'neo4j', 'reader', 'gAAAAA-opaque-ciphertext')'''))
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(migrate_remote_graph_connections, [engine, engine]))
        assert identity(engine).password_ciphertext.encode() == b'gAAAAA-opaque-ciphertext'
        assert identity(engine).revision == 17
        with engine.connect() as conn:
            assert conn.execute(text('SELECT kind FROM igraph_remote_graph_connections')).scalar_one() == 'neo4j'
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()

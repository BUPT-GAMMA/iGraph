"""Opt-in real Neo4j/Compose acceptance. Never substitute mocks for live gates."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest


def require_env(request, *names):
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        reason = 'Live remote graph prerequisites missing: ' + ', '.join(missing)
        if request.config.getoption('--require-live'):
            pytest.fail(reason, pytrace=False)
        pytest.skip(reason)
    return {name: os.environ[name] for name in names}


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture(scope='session')
def remote_live(request):
    names = ['IGRAPH_TEST_NEO4J_' + name for name in ('URI', 'DATABASE', 'USER', 'PASSWORD', 'READER_USER', 'READER_PASSWORD', 'DEDICATED')]
    values = require_env(request, *names, 'IGRAPH_TEST_CORA_MANIFEST', 'IGRAPH_TEST_CORA_INPUT', 'IGRAPH_TEST_EVIDENCE_DIR')
    if values['IGRAPH_TEST_NEO4J_DEDICATED'] != '1':
        pytest.fail('Use a dedicated test database and set IGRAPH_TEST_NEO4J_DEDICATED=1', pytrace=False)
    manifest = json.loads(Path(values['IGRAPH_TEST_CORA_MANIFEST']).read_text())
    source_hash = hashlib.sha256(Path(values['IGRAPH_TEST_CORA_INPUT']).read_bytes()).hexdigest()
    assert manifest['source_sha256'] == source_hash, 'Manifest does not match original input'
    evidence = Path(values['IGRAPH_TEST_EVIDENCE_DIR']); evidence.mkdir(parents=True, exist_ok=True)
    from neo4j import GraphDatabase, __version__
    uri, database = values['IGRAPH_TEST_NEO4J_URI'], values['IGRAPH_TEST_NEO4J_DATABASE']
    admin = GraphDatabase.driver(uri, auth=(values['IGRAPH_TEST_NEO4J_USER'], values['IGRAPH_TEST_NEO4J_PASSWORD']))
    reader = GraphDatabase.driver(uri, auth=(values['IGRAPH_TEST_NEO4J_READER_USER'], values['IGRAPH_TEST_NEO4J_READER_PASSWORD']))
    try:
        reader.verify_connectivity()
        with admin.session(database=database) as session:
            components = session.run('CALL dbms.components() YIELD name, versions, edition RETURN name, versions, edition').data()
        assert components and all(item['edition'] == 'enterprise' for item in components), 'Requires database-enforced reader permissions; Community is not an acceptance substitute'
        (evidence / 'versions.json').write_text(json.dumps({'driver': __version__, 'server': components, 'source_sha256': source_hash}, indent=2))
        yield SimpleNamespace(values=values, manifest=manifest, evidence=evidence, uri=uri, database=database, reader=reader, admin=admin)
    finally:
        admin.close(); reader.close()


@pytest.fixture
def remote_executor(remote_live, tmp_path):
    from cryptography.fernet import Fernet
    from sqlalchemy import create_engine
    from agent_runtime.execution_store import ExecutionStore
    from agent_runtime.remote_graph.catalog import RemoteGraphCatalog
    from agent_runtime.remote_graph.contracts import ConnectionWrite, RemoteBinding
    from agent_runtime.remote_graph.secrets import SecretBox
    from agent_runtime.remote_graph.neo4j import RemoteGraphExecutor
    store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/catalog.db')); store.migrate()
    catalog = RemoteGraphCatalog(store, SecretBox(Fernet.generate_key().decode()))
    form = ConnectionWrite(name='Cora live acceptance', uri=remote_live.uri, database=remote_live.database,
                           username=remote_live.values['IGRAPH_TEST_NEO4J_READER_USER'], password=remote_live.values['IGRAPH_TEST_NEO4J_READER_PASSWORD'])
    connection = catalog.save('live-owner', form)
    settings = SimpleNamespace(remote_graph_timeout_seconds=30, remote_graph_max_rows=10000,
        remote_graph_max_bytes=10 * 1024 * 1024, remote_graph_preview_rows=100, remote_graph_preview_bytes=64 * 1024,
        remote_graph_allowed_hosts=(urlsplit(remote_live.uri).hostname,),
        remote_graph_allowed_cidrs=tuple(filter(None, os.environ.get('IGRAPH_TEST_NEO4J_ALLOWED_CIDRS', '').split(','))))
    return SimpleNamespace(executor=RemoteGraphExecutor(catalog, settings), form=form,
                           binding=RemoteBinding(**connection.model_dump()), root=tmp_path / 'Ephemeral' / 'remote_graph')

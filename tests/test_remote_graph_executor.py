"""Async driver boundary: real catalog, explicit transactions, bounded consumption."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from neo4j.exceptions import AuthError, CypherSyntaxError, ClientError, SessionExpired
from sqlalchemy import create_engine

from agent_runtime.execution_store import ControlError, ExecutionStore
from agent_runtime.remote_graph.catalog import RemoteGraphCatalog
from agent_runtime.remote_graph.contracts import ConnectionWrite, QueryRequest, RemoteBinding
from agent_runtime.remote_graph.secrets import SecretBox


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def service(tmp_path, monkeypatch):
    from agent_runtime.remote_graph.executor import RemoteGraphExecutor
    store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/connections.db'))
    store.migrate()
    catalog = RemoteGraphCatalog(store, SecretBox(Fernet.generate_key().decode()))
    form = ConnectionWrite(name='Cora', uri='bolt://db.example:7687', database='neo4j', username='reader', password='fixture-password')
    item = catalog.save('alice', form)
    driver = Driver()
    monkeypatch.setattr('agent_runtime.remote_graph.neo4j.AsyncGraphDatabase.driver', lambda *args, **kwargs: driver)
    settings = SimpleNamespace(remote_graph_timeout_seconds=30, remote_graph_max_rows=2,
        remote_graph_max_bytes=1024, remote_graph_preview_rows=1, remote_graph_preview_bytes=512,
        remote_graph_allowed_hosts=('db.example',), remote_graph_allowed_cidrs=())
    executor = RemoteGraphExecutor(catalog, settings)
    return SimpleNamespace(executor=executor, driver=driver, catalog=catalog, form=form,
                           binding=RemoteBinding(**item.model_dump()), root=tmp_path / 'Ephemeral' / 'remote_graph')


@pytest.mark.anyio
@pytest.mark.parametrize('query', [
    'MATCH (n:Paper {cora_id: $id}) RETURN n.cora_id',
    'RETURN {secret_value: "never-echo-this-value"}',
    'MATCH (n) RETURN n{.cora_id}',
])
async def test_falkor_rejection_explains_map_limit_without_echoing_input(service, query):
    form = ConnectionWrite(name='Falkor', kind='falkordb', uri='redis://db.example:6379',
                           database='test', username='', password='fixture-password')
    item = service.catalog.save('alice', form)
    binding = RemoteBinding(**item.model_dump())
    result = await service.executor.query('alice', binding, QueryRequest(query=query, parameters={'id': 0}))
    assert not result.success
    assert result.error_code == 'REMOTE_GRAPH_QUERY_REJECTED'
    assert result.error_message and 'WHERE' in result.error_message
    assert 'map' in result.error_message.lower()
    assert 'never-echo-this-value' not in result.error_message
    assert 'fixture-password' not in result.error_message
    assert result.source == binding


def test_arbitrary_control_errors_are_not_exposed_as_query_hints():
    from agent_runtime.remote_graph.executor import RemoteGraphExecutor
    error = ControlError('REMOTE_GRAPH_QUERY_REJECTED', 422)
    error.safe_message = 'unexpected-driver-secret'
    assert RemoteGraphExecutor._error(None, error) == ('REMOTE_GRAPH_QUERY_REJECTED', None)


@pytest.mark.parametrize('query', ['CALL { RETURN 1 }', 'MATCH (n) WHERE EXISTS { MATCH (n)--() } RETURN n'])
def test_falkor_subquery_rejection_does_not_misdiagnose_a_map(query):
    from agent_runtime.remote_graph.falkordb import check_query
    from agent_runtime.remote_graph.executor import RemoteGraphExecutor
    with pytest.raises(ControlError) as error:
        check_query(query)
    assert RemoteGraphExecutor._error(None, error.value) == ('REMOTE_GRAPH_QUERY_REJECTED', None)


class Result:
    def __init__(self, driver, records, columns, *, user=False):
        self.driver, self.records, self.columns, self.user = driver, iter(records), columns, user
    def keys(self):
        return self.columns
    def __aiter__(self):
        return self
    async def __anext__(self):
        if self.user:
            await self.driver.block('consume')
        try:
            return next(self.records)
        except StopIteration:
            raise StopAsyncIteration
    async def consume(self):
        return SimpleNamespace(query_type=self.driver.query_type)


class Driver:
    def __init__(self):
        self.closed = False
        self.sessions = []
        self.calls = []
        self.query_type = 'r'
        self.rows = [{'n': 1}, {'n': 2}, {'n': 3}]
        self.privileges = [dict(access='GRANTED', action=a, graph='*') for a in ['access', 'match']]
        self.error = None
        self.metadata_error = None
        self.schema = False
        self.stage = None
        self.entered = asyncio.Event()
    async def block(self, stage):
        if self.stage == stage:
            self.entered.set()
            await asyncio.Event().wait()
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        self.closed = True
    def session(self, **kwargs):
        session = Session(self, kwargs)
        self.sessions.append(session)
        return session


class Session:
    def __init__(self, driver, options):
        self.driver, self.options = driver, options
        self.cancelled = self.closed = False
    async def begin_transaction(self, **kwargs):
        assert kwargs['timeout'] <= 30
        await self.driver.block('connect')
        return Transaction(self.driver)
    def cancel(self):
        self.cancelled = True
    async def close(self):
        self.closed = True


class Transaction:
    def __init__(self, driver):
        self.driver = driver
    async def run(self, query, parameters=None):
        self.driver.calls.append((query, parameters))
        if query.startswith('SHOW USER PRIVILEGES'):
            if self.driver.metadata_error:
                raise self.driver.metadata_error
            if isinstance(self.driver.error, AuthError):
                raise self.driver.error
            return Result(self.driver, self.driver.privileges, ['access', 'action', 'graph'])
        if query.startswith('SHOW FUNCTIONS'):
            return Result(self.driver, [{'name': 'count', 'isBuiltIn': True}], ['name', 'isBuiltIn'])
        if query.startswith('EXPLAIN '):
            return Result(self.driver, [], [])
        if self.driver.schema and query.startswith('CALL '):
            if query.startswith('CALL db.labels'):
                return Result(self.driver, [{'label': 'Paper'}], ['label'])
            if query.startswith('CALL db.relationshipTypes'):
                return Result(self.driver, [{'relationshipType': 'LINKS_TO'}], ['relationshipType'])
            if query.startswith('CALL db.schema.nodeTypeProperties'):
                return Result(self.driver, [{'nodeLabels': ['Paper'], 'propertyName': 'title', 'propertyTypes': ['String']}],
                              ['nodeLabels', 'propertyName', 'propertyTypes'])
            return Result(self.driver, [{'relType': ':`LINKS_TO`', 'propertyName': 'weight', 'propertyTypes': ['Long']}],
                          ['relType', 'propertyName', 'propertyTypes'])
        await self.driver.block('execute')
        if self.driver.error:
            raise self.driver.error
        return Result(self.driver, self.driver.rows, ['n'], user=True)
    async def close(self):
        pass


@pytest.mark.anyio
async def test_query_passes_parameters_and_distinguishes_preview_from_saved_limit(service):
    result = await service.executor.query('alice', service.binding,
        QueryRequest(query='RETURN $value AS n', parameters={'value': 1}, save_result=True), service.root)
    assert result.success and result.rows == [[1]] and result.returned_rows == 2
    assert result.truncated and result.preview_truncated
    assert ('RETURN $value AS n', {'value': 1}) in service.driver.calls
    assert service.driver.closed and all(s.closed for s in service.driver.sessions)
    assert all(s.options['default_access_mode'] == 'READ' for s in service.driver.sessions)
    assert result.artifact_path.startswith('Ephemeral/remote_graph/')
    artifact = service.root / result.artifact_path.split('/')[-1]
    payload = json.loads(artifact.read_text())
    assert payload['rows'] == [[1], [2]] and payload['truncated']
    assert payload['parameters'] == {'value': 1}
    assert payload['source']['revision'] == 1 and payload['query_time']
    assert 'fixture-password' not in artifact.read_text() + result.model_dump_json()


@pytest.mark.anyio
@pytest.mark.parametrize('stage', ['connect', 'execute', 'consume'])
async def test_cancel_cleans_session_and_driver_and_propagates(service, stage):
    service.driver.stage = stage
    task = asyncio.create_task(service.executor.query('alice', service.binding,
        QueryRequest(query='RETURN 1', save_result=True), service.root))
    await asyncio.wait_for(service.driver.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.driver.closed
    assert any(s.cancelled for s in service.driver.sessions)
    assert all(s.closed for s in service.driver.sessions)
    assert not list(service.root.glob('*'))


@pytest.mark.anyio
async def test_write_plan_and_untrusted_procedure_never_execute(service):
    service.driver.query_type = 'rw'
    result = await service.executor.query('alice', service.binding, QueryRequest(query='MATCH (n) SET n.x = 1 RETURN n'))
    assert result.error_code == 'REMOTE_GRAPH_QUERY_REJECTED'
    assert not any(q.startswith('MATCH') for q, _ in service.driver.calls)
    result = await service.executor.query('alice', service.binding, QueryRequest(query='CALL evil.write()'))
    assert result.error_code == 'REMOTE_GRAPH_QUERY_REJECTED'
    assert not any(q.startswith('CALL evil') for q, _ in service.driver.calls)


@pytest.mark.anyio
@pytest.mark.parametrize('mode, expected', [('readonly', 'verified_read_only'), ('write', 'not_read_only'), ('unavailable', 'unknown'), ('expired', 'unknown'), ('empty', 'unknown')])
async def test_connection_and_query_allow_writable_or_unknown_permissions(service, mode, expected):
    if mode == 'write':
        service.driver.privileges.append(dict(access='GRANTED', action='write', graph='*'))
    elif mode == 'unavailable':
        service.driver.metadata_error = ClientError('permissions unavailable')
    elif mode == 'expired':
        service.driver.metadata_error = SessionExpired('metadata session expired')
    elif mode == 'empty':
        service.driver.privileges = []
    result = await service.executor.test_connection('alice', service.form)
    assert result == dict(success=True, connection_usable=True, database_read_only=expected, database='neo4j')
    queried = await service.executor.query('alice', service.binding, QueryRequest(query='RETURN 1'))
    assert queried.success
    assert not any(q.startswith(('CREATE', 'SET', 'DELETE')) for q, _ in service.driver.calls)


@pytest.mark.anyio
async def test_syntax_error_preserves_position_but_not_password_or_address(service):
    service.driver.error = CypherSyntaxError('bad fixture-password at db.example (line 2, column 9)')
    result = await service.executor.query('alice', service.binding, QueryRequest(query='RETURN invalid'))
    assert result.error_code == 'REMOTE_GRAPH_QUERY_ERROR'
    assert 'line 2, column 9' in result.error_message
    assert 'fixture-password' not in result.model_dump_json() and 'db.example' not in result.model_dump_json()


@pytest.mark.anyio
async def test_revision_change_fails_before_contacting_driver(service):
    service.catalog.save('alice', service.form.model_copy(update={'connection_id': service.binding.connection_id, 'revision': 1}))
    result = await service.executor.query('alice', service.binding, QueryRequest(query='RETURN 1'))
    assert result.error_code == 'REMOTE_GRAPH_CHANGED'
    assert service.driver.calls == []


@pytest.mark.anyio
async def test_timeout_cancels_inflight_work(service):
    service.executor.settings.remote_graph_timeout_seconds = .02
    service.driver.stage = 'execute'
    result = await service.executor.query('alice', service.binding, QueryRequest(query='RETURN 1'))
    assert result.error_code == 'REMOTE_GRAPH_TIMEOUT'
    assert service.driver.closed and any(s.cancelled for s in service.driver.sessions)


@pytest.mark.anyio
async def test_describe_timeout_keeps_http_408(service):
    service.executor.settings.remote_graph_timeout_seconds = .02
    service.driver.stage = 'connect'
    with pytest.raises(ControlError) as error:
        await service.executor.describe('alice', service.binding)
    assert error.value.code == 'REMOTE_GRAPH_TIMEOUT' and error.value.status == 408


@pytest.mark.anyio
async def test_user_defined_function_cannot_bypass_procedure_allowlist(service):
    result = await service.executor.query('alice', service.binding, QueryRequest(query='RETURN evil()'))
    assert result.error_code == 'REMOTE_GRAPH_QUERY_REJECTED'
    assert not any(q == 'RETURN evil()' for q, _ in service.driver.calls)


@pytest.mark.anyio
async def test_describe_preserves_actual_types_and_normalizes_relationship_names(service):
    service.driver.schema = True
    result = await service.executor.describe('alice', service.binding, labels=['Paper'], relationship_types=['LINKS_TO'])
    assert result['labels'] == ['Paper'] and result['relationship_types'] == ['LINKS_TO']
    assert {'relationship_type': 'LINKS_TO', 'name': 'weight', 'types': ['Long']} in result['properties']
    assert {'node_labels': ['Paper'], 'name': 'title', 'types': ['String']} in result['properties']
    assert result['coverage'] == {'sampled': False, 'truncated': False}


@pytest.mark.anyio
async def test_auth_errors_do_not_leak_credentials(service):
    service.driver.error = AuthError('fixture-password db.example')
    result = await service.executor.query('alice', service.binding, QueryRequest(query='RETURN 1'))
    assert result.error_code == 'REMOTE_GRAPH_AUTH_FAILED'
    assert 'fixture-password' not in result.model_dump_json()


@pytest.mark.anyio
async def test_cancel_atomic_write_removes_partial_file(service, monkeypatch):
    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr('agent_runtime.remote_graph.executor.asyncio.sleep', stop)
    with pytest.raises(asyncio.CancelledError):
        await service.executor.query('alice', service.binding, QueryRequest(query='RETURN 1', save_result=True), service.root)
    assert not list(service.root.glob('*'))


@pytest.mark.anyio
async def test_schema_filters_are_applied_before_result_budgets(service):
    service.driver.schema = True
    await service.executor.describe('alice', service.binding, labels=['Paper'], relationship_types=['LINKS_TO'])
    queries = [(q, p) for q, p in service.driver.calls if q.startswith('CALL ')]
    # Database-side predicates must narrow rows before query() applies row and preview budgets.
    for query, params in queries:
        assert 'WHERE' in query[:query.index('RETURN')], query
        assert params['labels'] == ['Paper'] and params['types'] == ['LINKS_TO']


@pytest.mark.anyio
async def test_requested_relationship_beyond_default_preview_can_be_discovered(service, monkeypatch):
    from agent_runtime.remote_graph.adapters import AdapterRows
    from agent_runtime.remote_graph.neo4j import Neo4jAdapter
    from agent_runtime.remote_graph.results import bounded_rows
    rows = [[f':`Other{i}`', 'weight', ['Long']] for i in range(100)] + [[':`Wanted`', 'weight', ['Long']]]
    async def schema_query(self, ctx, tx, request):
        if request.query.startswith('CALL db.schema.relTypeProperties'):
            selected = rows
            if 'WHERE' in request.query.split('RETURN')[0]:
                selected = [row for row in rows if row[0] in request.parameters['encoded_types']]
            bounded = bounded_rows(selected, max_rows=ctx.max_rows, max_bytes=ctx.max_bytes)
            return AdapterRows(['relType', 'propertyName', 'propertyTypes'], bounded.rows,
                               bounded.returned_rows, bounded.truncated)
        return AdapterRows([], [], 0, False)
    monkeypatch.setattr(Neo4jAdapter, '_execute_tx', schema_query)
    async def fail_public_query(*args, **kwargs):
        pytest.fail('describe must not call the public identity query executor')
    monkeypatch.setattr(service.executor, 'query', fail_public_query)
    result = await service.executor.describe('alice', service.binding, relationship_types=['Wanted'])
    assert result['properties'] == [{'relationship_type': 'Wanted', 'name': 'weight', 'types': ['Long']}]
    assert not result['coverage']['truncated']

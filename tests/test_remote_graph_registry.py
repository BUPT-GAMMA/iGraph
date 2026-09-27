"""Product dispatch is explicit and follows the frozen source kind."""
import pytest

from agent_runtime.execution_store import ControlError
from agent_runtime.remote_graph.contracts import RemoteBinding


def test_unknown_kind_never_falls_back():
    from agent_runtime.remote_graph.registry import get_adapter

    with pytest.raises(ControlError, match='REMOTE_GRAPH_INVALID_CONFIG'):
        get_adapter('unregistered')


@pytest.mark.anyio
async def test_unknown_kind_query_never_uses_catalog(monkeypatch):
    from agent_runtime.remote_graph.executor import RemoteGraphExecutor
    from agent_runtime.remote_graph.contracts import QueryRequest
    from types import SimpleNamespace

    class Catalog:
        def resolve(self, uid, binding):
            pytest.fail('unknown kind must be rejected before resolving credentials')

    binding = RemoteBinding.model_construct(connection_id='id', revision=1,
        name='Unknown', kind='unregistered', database='graph')
    settings = SimpleNamespace(remote_graph_timeout_seconds=2, remote_graph_max_rows=10,
        remote_graph_max_bytes=1000, remote_graph_preview_rows=10, remote_graph_preview_bytes=1000,
        remote_graph_allowed_hosts=(), remote_graph_allowed_cidrs=())
    result = await RemoteGraphExecutor(Catalog(), settings).query('owner', binding, QueryRequest(query='RETURN 1'))
    assert not result.success and result.error_code == 'REMOTE_GRAPH_INVALID_CONFIG'


def test_disabled_kind_is_not_executable_or_selectable():
    from agent_runtime.remote_graph.registry import get_adapter, list_database_types

    available = list_database_types()
    assert {entry['kind'] for entry in available} == {'neo4j'}
    assert available[0]['display_name'] == 'Neo4j'
    assert available[0]['query_language'] == 'cypher'
    assert available[0]['target_label'] == 'Database'
    assert available[0]['username_required'] is True
    assert available[0]['support_status'] == 'verified'
    assert available[0]['verified_versions'] == '5.26.31 community'
    assert available[0]['uri_example'].startswith('bolt+s://')
    with pytest.raises(ControlError, match='REMOTE_GRAPH_INVALID_CONFIG'):
        get_adapter('falkordb')


def test_disabled_registered_adapter_cannot_execute(monkeypatch):
    from agent_runtime.remote_graph import registry

    registered = registry._REGISTRY['neo4j']
    monkeypatch.setitem(registry._REGISTRY, 'neo4j', {**registered, 'enabled': False})
    with pytest.raises(ControlError, match='REMOTE_GRAPH_INVALID_CONFIG'):
        registry.get_adapter('neo4j')
    assert registry.list_database_types() == []


@pytest.mark.anyio
async def test_dispatch_uses_frozen_kind(monkeypatch):
    from agent_runtime.remote_graph import registry
    from agent_runtime.remote_graph.executor import RemoteGraphExecutor
    from agent_runtime.remote_graph.contracts import QueryRequest
    from types import SimpleNamespace

    seen = []

    class Adapter:
        async def execute(self, ctx, request):
            from agent_runtime.remote_graph.adapters import AdapterRows
            seen.append((ctx.connection.public.kind, request.query))
            return AdapterRows(columns=['source'], rows=[['falkordb']], returned_rows=1, truncated=False)

    class Catalog:
        def resolve(self, uid, binding):
            assert uid == 'owner' and binding.kind == 'falkordb'
            return SimpleNamespace(public=SimpleNamespace(kind=binding.kind), password='secret')

    monkeypatch.setattr(registry, 'get_adapter', lambda kind: Adapter() if kind == 'falkordb' else None)
    binding = RemoteBinding(connection_id='id', revision=1, name='Graph', kind='falkordb', database='graph')
    settings = SimpleNamespace(remote_graph_timeout_seconds=2, remote_graph_max_rows=10,
        remote_graph_max_bytes=1000, remote_graph_preview_rows=10, remote_graph_preview_bytes=1000,
        remote_graph_allowed_hosts=(), remote_graph_allowed_cidrs=())
    result = await RemoteGraphExecutor(Catalog(), settings).query('owner', binding, QueryRequest(query='MATCH (n) RETURN n'))
    assert result.success and result.source.kind == 'falkordb' and result.rows == [['falkordb']]
    assert seen == [('falkordb', 'MATCH (n) RETURN n')]

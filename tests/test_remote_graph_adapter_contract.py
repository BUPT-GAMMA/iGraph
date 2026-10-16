"""Each operation owns its credentials, deadline and cancellation scope."""
import asyncio
from types import SimpleNamespace

import pytest

from agent_runtime.remote_graph.contracts import QueryRequest, RemoteBinding


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_adapters_do_not_share_credentials_or_cancellation(monkeypatch):
    from agent_runtime.remote_graph import registry
    from agent_runtime.remote_graph.adapters import AdapterRows
    from agent_runtime.remote_graph.executor import RemoteGraphExecutor

    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_second = asyncio.Event()
    contexts = []

    class Adapter:
        async def execute(self, ctx, request):
            contexts.append(ctx)
            if ctx.connection.password == 'first-secret':
                first_entered.set()
                await asyncio.Event().wait()
            else:
                second_entered.set()
                await release_second.wait()
            return AdapterRows(columns=['owner'], rows=[[ctx.connection.password]], returned_rows=1, truncated=False)

    class Catalog:
        def resolve(self, uid, binding):
            return SimpleNamespace(public=SimpleNamespace(kind=binding.kind), password=f'{uid}-secret')

    adapter = Adapter()
    monkeypatch.setattr(registry, 'get_adapter', lambda kind: adapter)
    settings = SimpleNamespace(remote_graph_timeout_seconds=2, remote_graph_max_rows=10,
        remote_graph_max_bytes=1000, remote_graph_preview_rows=10, remote_graph_preview_bytes=1000)
    executor = RemoteGraphExecutor(Catalog(), settings)
    binding = RemoteBinding(connection_id='id', revision=1, name='Graph', database='neo4j')
    first = asyncio.create_task(executor.query('first', binding, QueryRequest(query='RETURN 1')))
    await asyncio.wait_for(first_entered.wait(), 1)
    second = asyncio.create_task(executor.query('second', binding, QueryRequest(query='RETURN 2')))
    await asyncio.wait_for(second_entered.wait(), 1)
    assert not first.done() and not second.done()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done() and not second.cancelled()
    release_second.set()
    result = await asyncio.wait_for(second, 1)
    assert result.success and result.rows == [['second-secret']] and result.source == binding
    assert len(contexts) == 2 and contexts[0] is not contexts[1]
    assert contexts[0].connection.password == 'first-secret'
    assert contexts[1].connection.password == 'second-secret'


@pytest.mark.anyio
async def test_shared_executor_caps_adapter_rows(monkeypatch):
    from agent_runtime.remote_graph import registry
    from agent_runtime.remote_graph.adapters import AdapterRows
    from agent_runtime.remote_graph.executor import RemoteGraphExecutor

    class Adapter:
        async def execute(self, ctx, request):
            return AdapterRows(columns=['n'], rows=[[1], [2], [3]], returned_rows=3, truncated=False)

    class Catalog:
        def resolve(self, uid, binding):
            return SimpleNamespace(public=SimpleNamespace(kind=binding.kind), password='secret')

    monkeypatch.setattr(registry, 'get_adapter', lambda kind: Adapter())
    settings = SimpleNamespace(remote_graph_timeout_seconds=2, remote_graph_max_rows=2,
        remote_graph_max_bytes=1000, remote_graph_preview_rows=2, remote_graph_preview_bytes=1000)
    binding = RemoteBinding(connection_id='id', revision=1, name='Graph', database='neo4j')
    result = await RemoteGraphExecutor(Catalog(), settings).query('owner', binding, QueryRequest(query='RETURN 1'))
    assert result.success and result.rows == [[1], [2]]
    assert result.returned_rows == 2 and result.truncated

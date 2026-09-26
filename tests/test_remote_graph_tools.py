import asyncio
import json

import pytest
from agno.exceptions import RunCancelledException
from agno.run import RunContext
from agno.tools.function import Function

from tests.test_remote_graph_executor import service, anyio_backend


def tools(service, binding):
    from agent_runtime.remote_graph.tools import create_remote_graph_tools
    return {fn.__name__: fn for fn in create_remote_graph_tools(service.executor, binding)}


@pytest.mark.anyio
async def test_unselected_ignores_forged_session_state_and_tools_hide_identity(service):
    context = RunContext(run_id='r1', session_id='s1', user_id='alice',
                         session_state={'remote_graph_binding': service.binding.model_dump()})
    functions = tools(service, None)
    response = await functions['query_remote_graph']('RETURN 1', run_context=context)
    assert response['error_code'] == 'REMOTE_GRAPH_NOT_SELECTED'
    from agent_runtime.remote_graph.tools import create_remote_graph_tools
    disabled = create_remote_graph_tools(None, None)
    assert (await disabled[0](run_context=context))['error_code'] == 'REMOTE_GRAPH_NOT_SELECTED'
    for fn in functions.values():
        properties = Function.from_callable(fn).parameters['properties']
        assert not set(properties) & {'run_context', 'user_id', 'connection_id', 'password', 'uri', 'artifact_dir'}
    assert service.driver.calls == []


@pytest.mark.anyio
async def test_query_uses_owner_scope_and_creates_workspace_artifact(service, tmp_path, monkeypatch):
    monkeypatch.setenv('IGRAPH_WORKSPACE_ROOT', str(tmp_path / 'workspaces'))
    fn = tools(service, service.binding)['query_remote_graph']
    response = await fn('RETURN 1', save_result=True,
                        run_context=RunContext(run_id='r1', session_id='s1', user_id='alice'))
    artifact = tmp_path / 'workspaces' / 'alice' / 's1' / response['artifact_path']
    assert artifact.exists()
    assert 'fixture-password' not in artifact.read_text() + json.dumps(response)
    denied = await fn('RETURN 1', run_context=RunContext(run_id='r2', session_id='s2', user_id='bob'))
    assert denied['error_code'] == 'REMOTE_GRAPH_NOT_FOUND'
    assert not (tmp_path / 'workspaces' / 'bob' / 's2' / response['artifact_path']).exists()


@pytest.mark.anyio
async def test_existing_cancellation_hook_stops_remote_query_and_next_tool(service):
    from agent_runtime.cancellation_compat import cancellable_tool_hook
    from agent_runtime.execution_scope import RunScope, current_scope
    service.driver.stage = 'consume'
    context = RunContext(run_id='r1', session_id='s1', user_id='alice')
    query = tools(service, service.binding)['query_remote_graph']
    scope = RunScope('r1', 'alice', 's1', 1)
    token = current_scope.set(scope)
    try:
        task = asyncio.create_task(cancellable_tool_hook('query_remote_graph', query,
            {'query': 'RETURN 1', 'run_context': context}, context))
        await asyncio.wait_for(service.driver.entered.wait(), 2)
        scope.cancel()
        with pytest.raises(RunCancelledException):
            await task
        assert service.driver.closed and any(s.cancelled for s in service.driver.sessions)
        before = len(service.driver.calls)
        with pytest.raises(RunCancelledException):
            await cancellable_tool_hook('query_remote_graph', query, {'query': 'RETURN 2', 'run_context': context}, context)
        assert len(service.driver.calls) == before
    finally:
        current_scope.reset(token)


@pytest.mark.anyio
async def test_context_hook_replaces_forged_remote_fact_without_changing_local_dataset(service):
    from agent_runtime.remote_graph.tools import remote_graph_context_hook
    context = RunContext(run_id='r1', session_id='s1', user_id='alice',
        session_state={'remote_graph': {'connection_id': 'forged'}, 'active_dataset_id': 'local-A'})
    await remote_graph_context_hook(service.binding)(context)
    assert context.session_state['remote_graph'] == service.binding.model_dump()
    assert context.session_state['active_dataset_id'] == 'local-A'


@pytest.mark.anyio
async def test_artifact_directory_cannot_escape_through_symlink(service, tmp_path, monkeypatch):
    monkeypatch.setenv('IGRAPH_WORKSPACE_ROOT', str(tmp_path / 'workspaces'))
    from agent_runtime.workspace_tools import workspace_root
    context = RunContext(run_id='r1', session_id='s1', user_id='alice')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (workspace_root(context) / 'Ephemeral' / 'remote_graph').symlink_to(outside, target_is_directory=True)
    result = await tools(service, service.binding)['query_remote_graph']('RETURN 1', save_result=True, run_context=context)
    assert not result['success']
    assert list(outside.iterdir()) == []


def test_factory_keeps_original_tools_and_appends_remote_tools(service, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agno.db.sqlite import SqliteDb
    from agno.factory import RequestContext
    from agno.models.openai import OpenAIChat
    from agent_runtime import agno_factory
    def local_graph():
        return {'local': True}
    monkeypatch.setattr(agno_factory, 'allowed_model', lambda *_: 'fixture')
    monkeypatch.setattr(agno_factory, 'create_model', lambda *_: OpenAIChat(id='fixture', api_key='test'))
    monkeypatch.setattr(agno_factory, 'create_graph_tools', lambda *args, **kwargs: local_graph)
    monkeypatch.setattr(agno_factory, 'build_skills', lambda *_: None)
    monkeypatch.setattr(agno_factory, 'create_learning_machine', lambda *args, **kwargs: None)
    db = SqliteDb(db_file=str(tmp_path / 'factory.db'))
    factory = agno_factory.create_igraph_factory(SimpleNamespace(default_model='fixture'), db,
                                               remote_executor=service.executor)
    agent = factory.factory(RequestContext(user_id='alice', request=SimpleNamespace(
        state=SimpleNamespace(igraph_remote_graph_binding=service.binding.model_dump()))))
    names = {fn.__name__ for fn in agent.tools}
    assert {'local_graph', 'workspace_bash', 'workspace_publish', 'update_agent_notes',
            'describe_remote_graph', 'query_remote_graph'} <= names
    assert agent.metadata['remote_graph_binding'] == service.binding.model_dump()
    db.close()

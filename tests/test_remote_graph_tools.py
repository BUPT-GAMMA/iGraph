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
    names = {fn.name if isinstance(fn, Function) else fn.__name__ for fn in agent.tools}
    assert {'local_graph', 'workspace_bash', 'workspace_publish', 'update_agent_notes',
            'describe_remote_graph', 'query_remote_graph'} <= names
    assert agent.metadata['remote_graph_binding'] == service.binding.model_dump()
    db.close()


def test_query_description_uses_current_native_language(service):
    query = Function.from_callable(tools(service, service.binding)['query_remote_graph'])
    assert 'native query' in query.description
    assert 'Cypher' not in query.description


@pytest.mark.anyio
async def test_export_preserves_parameters_and_source_identity(service, tmp_path, monkeypatch):
    monkeypatch.setenv('IGRAPH_WORKSPACE_ROOT', str(tmp_path))
    from agent_runtime.remote_graph.contracts import RemoteBinding
    context = RunContext(run_id='r1', session_id='s1', user_id='alice')
    fn = tools(service, service.binding)['query_remote_graph']
    result = await fn('RETURN $value', parameters={'value': '中文'}, save_result=True, run_context=context)
    payload = json.loads((tmp_path / 'alice' / 's1' / result['artifact_path']).read_text())
    assert payload['query'] == 'RETURN $value'
    assert payload['parameters'] == {'value': '中文'}
    assert payload['source'] == service.binding.model_dump()
    assert not {'password', 'uri', 'username'} & payload['source'].keys()
    assert 'fixture-password' not in json.dumps(payload)


def test_registered_query_schema_accepts_native_json_parameter_values(service):
    from jsonschema import Draft202012Validator
    query = Function.from_callable(tools(service, service.binding)['query_remote_graph'])
    schema = query.to_dict()['parameters']
    validator = Draft202012Validator(schema)
    for value in ('中文', 0, 2.5, True, None, [1, 'x', None], {'nested': [1, False]}):
        errors = list(validator.iter_errors({'query': 'RETURN $value', 'parameters': {'value': value}}))
        assert not errors, f'Native parameter value {value!r} rejected by tool schema: {errors[0].message if errors else ""}'
    assert list(validator.iter_errors({'query': 'RETURN $value', 'parameters': '{"value": 0}'}))


@pytest.mark.anyio
async def test_identical_entity_ids_keep_each_query_source(service, tmp_path, monkeypatch):
    from agent_runtime.remote_graph.adapters import AdapterRows
    from agent_runtime.remote_graph.contracts import ConnectionWrite, RemoteBinding
    monkeypatch.setenv('IGRAPH_WORKSPACE_ROOT', str(tmp_path))
    class EntityAdapter:
        async def execute(self, ctx, request):
            return AdapterRows(columns=['n'], rows=[[{'type': 'node', 'id': '0', 'labels': ['Paper'], 'properties': {}}]], returned_rows=1, truncated=False)
    monkeypatch.setattr('agent_runtime.remote_graph.registry.get_adapter', lambda kind: EntityAdapter())
    other = service.catalog.save('alice', ConnectionWrite(kind='falkordb', name='Other', uri='redis://db.example:6379', database='graph', username='', password='other-secret'))
    bindings = [service.binding, RemoteBinding(**other.model_dump())]
    context = RunContext(run_id='r1', session_id='s1', user_id='alice')
    results = [await tools(service, binding)['query_remote_graph']('MATCH (n) RETURN n LIMIT 1', save_result=True, run_context=context) for binding in bindings]
    assert all(result['success'] for result in results)
    assert results[0]['rows'] == results[1]['rows']
    assert results[0]['source'] == bindings[0].model_dump()
    assert results[1]['source'] == bindings[1].model_dump()
    assert results[0]['query_id'] != results[1]['query_id']
    for binding, result in zip(bindings, results):
        artifact = json.loads((tmp_path / 'alice' / 's1' / result['artifact_path']).read_text())
        assert artifact['source'] == binding.model_dump()
        assert artifact['rows'] == result['rows']


def registered_remote_tools(service):
    from agno.agent import Agent
    from agno.agent._tools import parse_tools
    from agent_runtime.model_factory import CancellableDAGOpenAILike
    from agent_runtime.remote_graph.tools import register_remote_graph_tools
    model = CancellableDAGOpenAILike(id='fixture', api_key='unused')
    functions = register_remote_graph_tools(service.executor, service.binding)
    agent = Agent(model=model, tools=functions)
    registered = parse_tools(agent, functions, model, async_mode=True)
    provider = model.get_request_params(tools=model._format_tools(registered))['tools']
    return {fn.name: fn for fn in registered}, {fn['function']['name']: fn['function']['parameters'] for fn in provider}


def test_final_provider_schema_advertises_optional_object_and_arrays(service):
    from jsonschema import Draft202012Validator
    _, schemas = registered_remote_tools(service)
    query = schemas['query_remote_graph']
    assert query['properties']['parameters'] == {'type': 'object', 'additionalProperties': True}
    assert query['required'] == ['query']
    for value in ('中文', 0, 2.5, True, None, [1, 'x', None], {'nested': [1, False]}):
        Draft202012Validator(query).validate({'query': 'RETURN $value', 'parameters': {'value': value}})
    for field in ('labels', 'relationship_types'):
        assert schemas['describe_remote_graph']['properties'][field] == {'type': 'array', 'items': {'type': 'string'}}
    assert schemas['describe_remote_graph']['required'] == []


@pytest.mark.anyio
async def test_registered_tools_keep_runtime_omission_null_and_reject_strings(service):
    from pydantic import ValidationError
    functions, _ = registered_remote_tools(service)
    query = functions['query_remote_graph'].entrypoint
    context = RunContext(run_id='r1', session_id='s1', user_id='alice')
    assert (await query('RETURN 1', run_context=context))['success']
    assert (await query('RETURN 1', parameters=None, run_context=context))['success']
    assert (await query('RETURN $value', parameters={'value': 0}, run_context=context))['success']
    with pytest.raises(ValidationError):
        await query('RETURN 1', parameters='{}', run_context=context)
    service.driver.schema = True
    describe = functions['describe_remote_graph'].entrypoint
    assert (await describe(run_context=context, labels=None))['success']
    with pytest.raises(ValidationError):
        await describe(run_context=context, labels='["Paper"]')

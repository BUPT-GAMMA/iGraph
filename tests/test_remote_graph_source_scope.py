"""Source clarification travels through native runs, storage and tool execution."""
import asyncio
import json
from types import SimpleNamespace
import pytest

from agno.db.sqlite import SqliteDb
from agno.factory import RequestContext
from agno.models.base import Model
from agno.models.response import ModelResponse
from agno.tools.function import Function

from agent_runtime import agno_factory
from agent_runtime.tool_dag import DAGModelMixin


QUESTION = '你指的是当前本地图还是远程数据库？请回复“本地图”或“远程数据库”。'
BINDING = dict(connection_id='remote-1', revision=1, name='Remote', database='neo4j', kind='neo4j')


def make_agent(monkeypatch, db, mutations, *, binding=BINDING, tool_name='load_graph', confirmation=False,
               batch=None, arguments=None):
    names = batch or [tool_name]
    class Scripted(DAGModelMixin, Model):
        def invoke(self, messages, **kwargs):
            if messages[-1].role == 'tool':
                return ModelResponse(role='assistant', content='操作完成')
            return ModelResponse(role='assistant', tool_calls=[dict(id=f'call-{index}', type='function',
                function=dict(name=name, arguments=json.dumps(arguments or {}))) for index, name in enumerate(names)])
        async def ainvoke(self, messages, **kwargs):
            return self.invoke(messages, **kwargs)
        def invoke_stream(self, messages, **kwargs):
            yield self.invoke(messages, **kwargs)
        async def ainvoke_stream(self, messages, **kwargs):
            yield self.invoke(messages, **kwargs)
        def _parse_provider_response(self, response, **kwargs):
            return response
        def _parse_provider_response_delta(self, response):
            return response

    async def mutate(create_version: bool = True):
        mutations.append(tool_name)
        return {'success': True}

    monkeypatch.setattr(agno_factory, 'get_custom_model', lambda *_: None)
    monkeypatch.setattr(agno_factory, 'allowed_model', lambda *_: 'fixture')
    monkeypatch.setattr(agno_factory, 'create_model', lambda *_: Scripted(id='fixture'))
    from agno.tools import Toolkit
    toolkit = Toolkit(name='graph')
    for name in names:
        toolkit.functions[name] = Function(name=name, entrypoint=mutate, requires_confirmation=confirmation)
    monkeypatch.setattr(agno_factory, 'create_graph_tools', lambda *a, **kw: toolkit)
    monkeypatch.setattr(agno_factory, 'create_workspace_tools', lambda: [])
    monkeypatch.setattr(agno_factory, 'build_skills', lambda *_: None)
    monkeypatch.setattr(agno_factory, 'create_learning_machine', lambda *a, **kw: None)
    config = SimpleNamespace(default_model='fixture')
    context = RequestContext(user_id='alice', request=SimpleNamespace(state=SimpleNamespace(igraph_remote_graph_binding=binding)))
    agent = agno_factory.create_igraph_factory(config, db).factory(context)
    agent.telemetry = False
    return agent


@pytest.mark.parametrize('input_text', ['把 cora_id 为 0 的节点删除', '删除本地图节点 0'])
def test_ambiguous_write_completes_with_chat_question_without_mutating(tmp_path, monkeypatch, input_text):
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'source.db'))
        mutations = []
        try:
            agent = make_agent(monkeypatch, db, mutations)
            events = [event async for event in agent.arun(input_text,
                user_id='alice', session_id='s', stream=True, stream_events=True)]
            saved = db.get_session(session_id='s', user_id='alice').runs[-1]
            assert mutations == []
            assert saved.content == QUESTION
            assert saved.status == 'COMPLETED'
            assert 'RunCompleted' in [event.event for event in events]
            assert 'RunPaused' not in [event.event for event in events]
            answer = ''.join(event.content for event in events
                if event.event == 'RunContent' and isinstance(event.content, str))
            assert answer == QUESTION
        finally:
            db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('reply,expected', [('本地图', 1), ('当前本地图', 1), ('我指的是本地图。', 1),
    (' 我指的是当前本地图！ ', 1), ('远程数据库', 0), ('不要改本地图', 0),
    ('我引用“本地图”这三个字', 0), ('继续', 0), ('同意', 0), ('本地图？', 0),
    ('不是本地图', 0), ('我指的是本地图，但不要改', 0)])
def test_only_actual_exact_next_reply_grants_one_turn(tmp_path, monkeypatch, reply, expected):
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'source.db'))
        mutations = []
        try:
            first = make_agent(monkeypatch, db, mutations)
            result = await first.arun('删除节点', user_id='alice', session_id='s')
            assert result.content == QUESTION
            second = make_agent(monkeypatch, db, mutations)
            await second.arun(reply, user_id='alice', session_id='s', metadata={'source': 'local',
                'igraph_turn': {'user_message': '本地图'}}, session_state={'source': 'local'})
            assert len(mutations) == expected
            third = make_agent(monkeypatch, db, mutations)
            await third.arun('再删除', user_id='alice', session_id='s')
            assert len(mutations) == expected
        finally:
            db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('tool_name', ['clear_graph', 'get_graph_info'])
def test_existing_confirmation_waits_until_local_source_reply(tmp_path, monkeypatch, tool_name):
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'source.db'))
        mutations = []
        try:
            first = make_agent(monkeypatch, db, mutations, tool_name=tool_name, confirmation=tool_name=='clear_graph')
            result = await first.arun('操作', user_id='alice', session_id='s')
            if tool_name == 'get_graph_info':
                assert mutations == ['get_graph_info']
                assert result.content == '操作完成'
            else:
                assert mutations == [] and result.content == QUESTION
                assert result.status == 'COMPLETED'
                second = make_agent(monkeypatch, db, mutations, tool_name=tool_name, confirmation=True)
                result = await second.arun('本地图', user_id='alice', session_id='s')
                assert result.status == 'PAUSED'
                assert mutations == []
        finally:
            db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['remote', 'local', 'session', 'cancelled', 'error', 'no_pending'])
def test_pending_question_cannot_authorize_another_scope_or_failed_turn(tmp_path, monkeypatch, change):
    from agno.run.base import RunStatus
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'source.db'))
        mutations = []
        try:
            if change != 'no_pending':
                await make_agent(monkeypatch, db, mutations).arun('删除', user_id='alice', session_id='s')
            if change in {'cancelled', 'error'}:
                saved = db.get_session(session_id='s', user_id='alice')
                saved.runs[-1].status = RunStatus(change.upper())
                db.upsert_session(saved)
            second = make_agent(monkeypatch, db, mutations,
                binding={**BINDING, 'revision': 2} if change == 'remote' else BINDING)
            result = await second.arun('本地图', user_id='alice', session_id='other' if change == 'session' else 's',
                session_state={'active_dataset_id': 'new-local'} if change == 'local' else {})
            assert mutations == []
            assert result.content == QUESTION
        finally:
            db.close()
    asyncio.run(scenario())


def test_local_only_session_keeps_normal_writes(tmp_path, monkeypatch):
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'source.db'))
        mutations = []
        try:
            agent = make_agent(monkeypatch, db, mutations, binding=None)
            result = await agent.arun('删除节点', user_id='alice', session_id='s')
            assert mutations == ['load_graph']
            assert result.content == '操作完成'
        finally:
            db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('tool_name,arguments,blocked', [
    ('subgraph_extraction', {'create_version': False}, False),
    ('subgraph_extraction', {}, True), ('save_as_dataset', {}, True),
    ('commit_graph_optimization', {}, True), ('register_session', {}, True),
    ('delete_graph_session', {}, True)])
def test_shared_resource_contract_controls_mutation_guard(tmp_path, monkeypatch, tool_name, arguments, blocked):
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'source.db'))
        mutations = []
        try:
            agent = make_agent(monkeypatch, db, mutations, tool_name=tool_name, arguments=arguments)
            result = await agent.arun('操作', user_id='alice', session_id='s')
            assert mutations == ([] if blocked else [tool_name])
            assert result.content == (QUESTION if blocked else '操作完成')
        finally:
            db.close()
    asyncio.run(scenario())


def test_parallel_independent_writes_and_read_cannot_bypass_source_guard(tmp_path, monkeypatch):
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'source.db'))
        mutations = []
        try:
            agent = make_agent(monkeypatch, db, mutations,
                batch=['load_graph', 'commit_graph_optimization', 'get_graph_info'])
            result = await agent.arun('操作', user_id='alice', session_id='s')
            assert mutations == []
            assert result.content == QUESTION
            assert result.status == 'COMPLETED'
        finally:
            db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('stream', [False, True])
def test_native_stop_agent_message_is_emitted_once(tmp_path, monkeypatch, stream):
    from agno.exceptions import StopAgentRun
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'source.db'))
        try:
            agent = make_agent(monkeypatch, db, [], binding=None)
            async def stop(function_name, function_call, arguments):
                raise StopAgentRun('stop', agent_message='普通停止消息')
            agent.tool_hooks = [stop]
            if stream:
                events = [event async for event in agent.arun('操作', user_id='alice', session_id='s',
                    stream=True, stream_events=True)]
                answer = ''.join(event.content for event in events
                    if event.event == 'RunContent' and isinstance(event.content, str))
                assert answer == '普通停止消息'
            else:
                result = await agent.arun('操作', user_id='alice', session_id='s')
                assert result.content == '普通停止消息'
            saved = db.get_session(session_id='s', user_id='alice').runs[-1]
            assert saved.content == '普通停止消息'
            assert saved.status == 'COMPLETED'
        finally:
            db.close()
    asyncio.run(scenario())


def test_local_source_grant_survives_only_same_run_approval_resume(tmp_path, monkeypatch):
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'source.db'))
        mutations = []
        try:
            await make_agent(monkeypatch, db, mutations, tool_name='clear_graph', confirmation=True).arun(
                '清空', user_id='alice', session_id='s')
            paused = await make_agent(monkeypatch, db, mutations, tool_name='clear_graph', confirmation=True).arun(
                '本地图', user_id='alice', session_id='s')
            assert paused.status == 'PAUSED'
            pending = [tool for tool in paused.tools if tool.requires_confirmation]
            for tool in pending:
                tool.confirmed = True
            fresh = make_agent(monkeypatch, db, mutations, tool_name='clear_graph', confirmation=True)
            result = await fresh.acontinue_run(run_id=paused.run_id, user_id='alice', session_id='s', updated_tools=pending)
            assert mutations == ['clear_graph']
            assert result.status == 'COMPLETED'
            await make_agent(monkeypatch, db, mutations).arun('再修改', user_id='alice', session_id='s')
            assert mutations == ['clear_graph']
        finally:
            db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('tool_name', ['load_graph', 'clear_graph'])
def test_background_http_session_patch_then_reply_authorizes_local_turn(tmp_path, monkeypatch, tool_name):
    """BFF patches native state then starts AgentOS's background SSE path."""
    from agno.os import AgentOS
    from fastapi.testclient import TestClient
    from agno.os.managers import event_buffer
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'source.db'))
        mutations = []
        try:
            first = make_agent(monkeypatch, db, mutations, tool_name=tool_name, confirmation=tool_name=='clear_graph')
            frames = [frame async for frame in first.arun('删除本地图节点', user_id='alice', session_id='s',
                stream=True, stream_events=True, background=True)]
            assert any('RunCompleted' in frame for frame in frames)
            assert mutations == []
            saved = db.get_session(session_id='s', user_id='alice')
            client = TestClient(AgentOS(agents=[first], telemetry=False).get_app())
            response = client.patch('/sessions/s', params={'type': 'agent', 'user_id': 'alice'},
                json={'session_state': saved.session_data['session_state']})
            assert response.status_code == 200
            client.close()
            second = make_agent(monkeypatch, db, mutations, tool_name=tool_name, confirmation=tool_name=='clear_graph')
            frames = [frame async for frame in second.arun('本地图', user_id='alice', session_id='s',
                stream=True, stream_events=True, background=True)]
            assert mutations == (['load_graph'] if tool_name == 'load_graph' else [])
            assert any(('RunCompleted' if tool_name == 'load_graph' else 'RunPaused') in frame for frame in frames)
            saved = db.get_session(session_id='s', user_id='alice')
            assert saved.runs[-1].status == ('COMPLETED' if tool_name == 'load_graph' else 'PAUSED')
            for run in saved.runs:
                event_buffer.cleanup_run(run.run_id)
        finally:
            db.close()
    asyncio.run(scenario())

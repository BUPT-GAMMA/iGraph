"""Executable contract for the pinned Agno private recovery dependencies."""
import asyncio
import inspect
import json
from importlib.metadata import version

import pytest
from agno.agent import Agent, AgentFactory
from agno.models.base import Model
from agno.models.response import ModelResponse
from agno.db.sqlite import SqliteDb
from agno.os import AgentOS
from agno.os.managers import event_buffer, sse_subscriber_manager
from agno.run.agent import RunContentEvent, RunCompletedEvent, RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from agent_runtime.recovery import create_recovery_router, owned_run, replay, validate_cursor, _resume_stream_generator
from backend.stream_recovery import with_heartbeats


@pytest.fixture()
def database(tmp_path):
    db = SqliteDb(db_file=str(tmp_path / 'recovery.db'))
    db.upsert_session(AgentSession(session_id='s', agent_id='igraph-agent', user_id='alice', created_at=1, updated_at=1,
                                  runs=[RunOutput(run_id='r', agent_id='igraph-agent', session_id='s', user_id='alice', content='ABC')]))
    try:
        yield db
    finally:
        event_buffer.cleanup_run('r')
        db.close()
        assert not sse_subscriber_manager._subscribers.get('r')


def event(text):
    return RunContentEvent(run_id='r', session_id='s', agent_id='igraph-agent', content=text)


def test_pinned_native_contract():
    assert version('agno') == '2.9.0'
    assert list(inspect.signature(_resume_stream_generator).parameters) == ['agent', 'run_id', 'last_event_index', 'session_id', 'user_id']
    assert event_buffer.max_events_per_run == 10000


def test_factory_route_uses_native_buffer_without_resolving_factory(database):
    def forbidden(_context):
        raise AssertionError('Recovery must not construct the business agent')
    base = FastAPI()
    base.include_router(create_recovery_router(database))
    app = AgentOS(agents=[AgentFactory(id='igraph-agent', db=database, factory=forbidden)],
                  db=database, base_app=base, telemetry=False).get_app()
    event_buffer.add_event('r', event('A'))
    event_buffer.add_event('r', event('B'))
    event_buffer.add_event('r', RunCompletedEvent(run_id='r', content='ABC'))
    event_buffer.set_run_completed('r', RunStatus.completed)
    with TestClient(app) as client:
        response = client.post('/internal/sessions/s/runs/r/resume', headers={'X-User-ID': 'alice'}, data={'last_event_index': '0'})
        assert response.status_code == 200
        payloads = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
        assert [p['event_index'] for p in payloads if 'event_index' in p] == [1, 2]
        for uid, sid, rid in [('bob', 's', 'r'), ('alice', 'wrong', 'r'), ('alice', 's', 'wrong')]:
            assert client.post(f'/internal/sessions/{sid}/runs/{rid}/resume', headers={'X-User-ID': uid}).status_code == 404
        assert client.post('/internal/sessions/s/runs/r/resume').status_code == 401


def test_native_subscribe_replay_live_and_disconnect_cleanup(database):
    async def scenario():
        event_buffer.add_event('r', event('A'))
        event_buffer.add_event('r', event('B'))
        stream = replay(database, 'igraph-agent', 'r', 0, 'alice')
        assert 'catch_up' in await anext(stream)
        assert '"event_index":1' in await anext(stream)
        assert 'subscribed' in await anext(stream)
        assert len(sse_subscriber_manager._subscribers['r']) == 1
        index = event_buffer.add_event('r', event('C'))
        await sse_subscriber_manager.publish('r', index, 'data: ' + json.dumps({'event': 'RunContent', 'content': 'C', 'event_index': index}) + '\n\n')
        assert '"content": "C"' in await anext(stream)
        await stream.aclose()
        assert 'r' not in sse_subscriber_manager._subscribers
        assert event_buffer.get_run_status('r') == RunStatus.running
    asyncio.run(scenario())


def test_trim_missing_and_invalid_cursors(database):
    with pytest.raises(HTTPException) as missing:
        validate_cursor('r', -1)
    assert missing.value.detail['reason'] == 'buffer_missing'
    for _ in range(10001):
        event_buffer.add_event('r', event('x'))
    for cursor, reason in [(-1, 'window_trimmed'), (-2, 'invalid_cursor'), (10001, 'invalid_cursor')]:
        with pytest.raises(HTTPException) as error:
            validate_cursor('r', cursor)
        assert error.value.status_code == 409
        assert error.value.detail == {'code': 'REPLAY_UNAVAILABLE', 'reason': reason, 'message': '需要同步已保存的运行结果'}
    validate_cursor('r', 0)
    validate_cursor('r', 10000)


def test_midstream_gap_is_unindexed_control_and_cleans_subscription(database):
    async def scenario():
        event_buffer.add_event('r', event('A'))
        stream = replay(database, 'igraph-agent', 'r', 0, 'alice')
        await anext(stream)
        await sse_subscriber_manager.publish('r', 3, 'data: {"event":"RunContent","event_index":3}\n\n')
        frame = await anext(stream)
        assert 'stream_recovery_required' in frame
        assert 'event_index' not in frame
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert 'r' not in sse_subscriber_manager._subscribers
    asyncio.run(scenario())


def test_missing_buffer_never_uses_database_event_replay(database):
    async def scenario():
        stream = replay(database, 'igraph-agent', 'r', -1, 'alice')
        frames = [frame async for frame in stream]
        assert len(frames) == 1 and 'stream_recovery_required' in frames[0]
        assert 'ABC' not in frames[0]
    asyncio.run(scenario())


def test_heartbeat_keeps_the_same_read_pending_and_closes_it():
    async def scenario():
        calls, closed = [], []
        ready = asyncio.Event()
        async def source():
            try:
                calls.append('read')
                await ready.wait()
                yield 'data: complete\n\n'
            finally:
                closed.append(True)
        stream = with_heartbeats(source(), interval=.001)
        assert await anext(stream) == ': heartbeat\n\n'
        assert await anext(stream) == ': heartbeat\n\n'
        assert calls == ['read'] and not closed
        ready.set()
        assert await anext(stream) == 'data: complete\n\n'
        await stream.aclose()
        assert closed == [True]
    asyncio.run(scenario())


class DeterministicModel(Model):
    """Exercise native background execution without a provider or external tool."""
    def invoke(self, *args, **kwargs):
        return ModelResponse(role='assistant', content='ABC')

    async def ainvoke(self, *args, **kwargs):
        return self.invoke()

    def invoke_stream(self, *args, **kwargs):
        for token in 'ABC':
            yield ModelResponse(role='assistant', content=token)

    async def ainvoke_stream(self, *args, **kwargs):
        for token in self.invoke_stream():
            await asyncio.sleep(0)
            yield token

    def _parse_provider_response(self, response, **kwargs):
        return response

    def _parse_provider_response_delta(self, response):
        return response


@pytest.mark.parametrize('approval', [False, True])
def test_real_factory_background_run_replays_through_public_backend(database, tmp_path, monkeypatch, approval):
    from contextlib import asynccontextmanager
    from dataclasses import replace
    import backend.app as backend_app
    from backend.config import BackendConfig
    from backend.users import UserDirectory, UserRecord
    from agno.tools import tool

    builds, effects, rounds = [], [], []

    @tool(requires_confirmation=True)
    def record_once() -> str:
        """Record a deterministic test side effect."""
        effects.append(True)
        return 'ok'

    class ConfirmationModel(DeterministicModel):
        async def ainvoke_stream(self, *args, **kwargs):
            if not rounds:
                rounds.append(True)
                yield ModelResponse(role='assistant', tool_calls=[{'id': 'call-once', 'type': 'function',
                                     'function': {'name': 'record_once', 'arguments': '{}'}}])
            else:
                async for delta in super().ainvoke_stream(*args, **kwargs):
                    yield delta

    def factory(_context):
        builds.append(True)
        return Agent(id='igraph-agent', db=database, model=(ConfirmationModel if approval else DeterministicModel)(id='deterministic'),
                     tools=[record_once] if approval else [],
                     store_events=True, stream_events=True, telemetry=False)
    base = FastAPI()
    base.include_router(create_recovery_router(database))
    app = AgentOS(agents=[AgentFactory(id='igraph-agent', db=database, factory=factory)], db=database, base_app=base, telemetry=False).get_app()
    with TestClient(app) as runtime:
        started = runtime.post('/agents/igraph-agent/runs', data={ 'message': 'question', 'session_id': 'real-session',
                              'user_id': 'admin', 'stream': 'true', 'background': 'true' })
        assert started.status_code == 200, started.text
        native = [json.loads(line[5:]) for line in started.text.splitlines() if line.startswith('data:')]
        run_id = next(item['run_id'] for item in native if item.get('run_id'))

        class LoopbackServiceClient:
            def __init__(self, *_args):
                pass

            async def request(self, method, path, *, headers, params=None, **kwargs):
                result = runtime.request(method, path, headers=headers, params=params)
                if result.status_code >= 400:
                    raise HTTPException(result.status_code, result.json())
                return result.json()

            @asynccontextmanager
            async def stream(self, method, path, *, headers, data=None):
                result = runtime.request(method, path, headers=headers, data=data)
                if result.status_code >= 400:
                    raise HTTPException(result.status_code, result.json().get('detail'))
                if path.endswith('/continue'):
                    assert data['background'] == 'true'
                    native.extend(json.loads(line[5:]) for line in result.text.splitlines() if line.startswith('data:'))
                class Response:
                    async def aiter_lines(self):
                        for line in result.text.splitlines():
                            yield line
                yield Response()

        config = replace(BackendConfig.from_env(), users_file=tmp_path / 'users.json', projects_file=tmp_path / 'projects.json',
                         supported_ids_file=tmp_path / 'missing-seed.txt', custom_models_file=tmp_path / 'models.json')
        UserDirectory(config.users_file, config.supported_ids_file, 'admin').save(UserRecord(user_id='admin', roles=['admin']))
        monkeypatch.setattr(backend_app, 'ServiceClient', LoopbackServiceClient)
        bff = backend_app.create_app(config)
        with TestClient(bff) as client:
            if approval:
                assert any(item.get('event') == 'RunPaused' for item in native)
                assert effects == []
                continued = client.post('/api/sessions/real-session/feedback/stream', headers={'X-User-ID': 'admin'},
                                        json={'run_id': run_id, 'decision_id': 'approve-once', 'tool_call_id': 'call-once', 'decision': 'approve'})
                assert continued.status_code == 200, continued.text
                assert effects == [True]
            first = next(item for item in native if item.get('event') == 'RunContent' and item.get('content') == 'A')
            cursor = first['event_index']
            response = client.post(f'/api/sessions/real-session/runs/{run_id}/resume?last_event_index={cursor}',
                                   headers={'X-User-ID': 'admin', 'X-iGraph-Stream-Version': '2'})
            assert response.status_code == 200, response.text
            batches = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
            indices = [batch['event_index'] for batch in batches if batch['event_index'] is not None]
            assert indices == list(range(cursor + 1, event_buffer.get_last_index(run_id) + 1))
            text = 'A' + ''.join(event.get('content', '') for batch in batches for event in batch['events'] if event['type'] == 'token')
            assert text == 'ABC'
            snapshot = client.get(f'/api/sessions/real-session/runs/{run_id}/snapshot', headers={'X-User-ID': 'admin'})
            assert snapshot.status_code == 200, snapshot.text
            assert snapshot.json()['snapshot_complete'] is True
            assert snapshot.json()['messages'][-1]['content'] == text
            if approval:
                duplicate = client.post('/api/sessions/real-session/feedback/stream', headers={'X-User-ID': 'admin'},
                                        json={'run_id': run_id, 'decision_id': 'duplicate', 'tool_call_id': 'call-once', 'decision': 'approve'})
                assert duplicate.status_code == 409
                assert effects == [True]
        assert builds == ([True, True] if approval else [True])
        event_buffer.cleanup_run(run_id)
        assert run_id not in sse_subscriber_manager._subscribers

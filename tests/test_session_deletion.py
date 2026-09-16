"""Deletion accepts a durable stop intent before removing owned resources."""
import asyncio
import time
from uuid import uuid4

import pytest
from agno.db.sqlite import SqliteDb
from agno.session.agent import AgentSession
from sqlalchemy import create_engine

from agent_runtime.execution_control import ExecutionController
from agent_runtime.execution_scope import RunCancellationManager
from agent_runtime.execution_store import ExecutionStore
from tests.test_cancellation_failures import running_stack
from tests.test_cancellation_http import eventually


async def ready(value):
    return value


@pytest.mark.parametrize('mode', ['first-token', 'stream', 'train', 'approval'])
def test_delete_running_session_stops_execution_and_hides_before_cleanup(tmp_path, mode):
    async def scenario():
        async with running_stack(tmp_path) as (client, sid, graph, controller, metrics):
            scopes, original_begin = [], controller.begin
            async def capture_scope(*args, **kwargs):
                scope = await original_begin(*args, **kwargs)
                scopes.append(scope)
                return scope
            controller.begin = capture_scope
            question = mode + '-delete-' + str(uuid4())
            key = {'start_request_id': str(uuid4())}
            frames = []
            async def observe():
                async with client.stream('POST', f'/api/sessions/{sid}/chat/stream', json={'message': question, **key}) as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        frames.append(line)
            observer = asyncio.create_task(observe())
            cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
            original = controller._delete_graph_session
            async def delayed_cleanup(uid, target):
                if target == sid:
                    cleanup_started.set()
                    await release_cleanup.wait()
                await original(uid, target)
            controller._delete_graph_session = delayed_cleanup
            try:
                async def entered():
                    if mode == 'train':
                        return any(job.tool_name == 'train' and job.progress > 0 for job in graph._job_manager._jobs.values())
                    if mode == 'approval':
                        response = await client.get(f'/api/sessions/{sid}/runs/control', params=key)
                        return response.json().get('run_status') == 'paused'
                    return metrics['calls'].get(question)
                await eventually(entered, 40)
                other = (await client.post('/api/sessions', json={'model': 'qwen3.6-plus'})).json()['session_id']
                denied = await client.delete(f'/api/sessions/{sid}', headers={'X-User-ID': 'alice'})
                assert denied.status_code == 404
                assert controller.store.session_deletion('admin', sid) is None
                accepted = await client.delete(f'/api/sessions/{sid}')
                assert accepted.status_code == 202, accepted.text
                assert accepted.json()['delete_status'] == 'requested'
                assert (await client.delete(f'/api/sessions/{sid}')).status_code == 202
                listed = (await client.get('/api/sessions')).json()['sessions']
                assert sid not in {row['session_id'] for row in listed}
                assert other in {row['session_id'] for row in listed}
                assert (await client.get(f'/api/sessions/{sid}/messages')).status_code == 404
                await asyncio.wait_for(cleanup_started.wait(), 15)
                assert scopes and all(scope.execution_stopped for scope in scopes)
                assert all(job.status in {'cancelled', 'done'} for job in graph._job_manager._jobs.values())
                await eventually(lambda: ready(metrics['closed'].get(question) == metrics['calls'][question]))
                # Graph cleanup is held: no native data is removed ahead of it.
                assert controller.db.get_session(session_id=sid, user_id='admin') is not None
                release_cleanup.set()
                await eventually(lambda: ready(controller.store.session_deletion('admin', sid)['state']['delete_status'] == 'deleted'))
                assert controller.db.get_session(session_id=sid, user_id='admin') is None
                assert controller.db.get_session(session_id=other, user_id='admin') is not None
                assert (await client.get(f'/api/sessions/{other}/messages')).status_code == 200
                assert (await client.delete(f'/api/sessions/{sid}')).status_code == 200
                late = await client.post(f'/api/sessions/{sid}/chat/stream', json={'message': 'late', **key})
                assert late.status_code in {404, 409}
                assert 'late' not in metrics['calls']
            finally:
                release_cleanup.set()
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
    asyncio.run(scenario())


def test_delete_before_first_run_prevents_late_creation_and_launch(tmp_path):
    async def scenario():
        async with running_stack(tmp_path) as (client, sid, _graph, controller, metrics):
            assert (await client.delete(f'/api/sessions/{sid}')).status_code == 202
            await eventually(lambda: ready(controller.store.session_deletion('admin', sid)['state']['delete_status'] == 'deleted'))
            late = await client.post(f'/api/sessions/{sid}/chat/stream', json={'message': 'late', 'start_request_id': str(uuid4())})
            assert late.status_code in {404, 409}
            recreated = await client.post('/api/sessions', json={'client_session_id': sid, 'model': 'qwen3.6-plus'})
            assert recreated.status_code == 409
            assert controller.db.get_session(session_id=sid, user_id='admin') is None
            assert metrics['calls'] == {}
    asyncio.run(scenario())


def test_accepted_deletion_survives_restart_and_retries_unconfirmed_native_delete(tmp_path):
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'native.db'))
        db.upsert_session(AgentSession(session_id='s', agent_id='igraph-agent', user_id='alice', runs=[],
                                      created_at=int(time.time()), updated_at=int(time.time())))
        store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/control.db'))
        store.migrate()
        controller = ExecutionController(db, store, RunCancellationManager())
        entered = asyncio.Event()
        async def unavailable(*_):
            entered.set()
            await asyncio.Event().wait()
        controller._delete_graph_session = unavailable
        try:
            await controller.request_session_deletion('alice', 's')
            await asyncio.wait_for(entered.wait(), 3)
            await controller.close()
            assert db.get_session(session_id='s', user_id='alice') is not None
            controller = ExecutionController(db, ExecutionStore(store.engine), RunCancellationManager())
            async def deleted_graph(*_):
                pass
            controller._delete_graph_session = deleted_graph
            original, attempts = db.delete_session, []
            def unreliable_delete(*args, **kwargs):
                attempts.append(1)
                return original(*args, **kwargs) if len(attempts) > 1 else False
            db.delete_session = unreliable_delete
            await controller.restore()
            await eventually(lambda: ready(store.session_deletion('alice', 's')['state']['delete_status'] == 'deleted'))
            assert len(attempts) == 2
            assert db.get_session(session_id='s', user_id='alice') is None
            assert store.session_deletions(pending_only=True) == []
        finally:
            await controller.close()
            db.close()
            store.engine.dispose()
    asyncio.run(scenario())


def test_session_deletion_removes_workspace_directory(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path / "data"))
        monkeypatch.setenv("IGRAPH_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
        from workspace_storage import workspace_root_for
        root = workspace_root_for("alice", "s")
        (root / "Uploaded" / "data.csv").write_text("a", encoding="utf-8")
        db = SqliteDb(db_file=str(tmp_path / "native.db"))
        db.upsert_session(AgentSession(session_id="s", agent_id="igraph-agent", user_id="alice", runs=[],
                                      created_at=int(time.time()), updated_at=int(time.time())))
        store = ExecutionStore(create_engine(f"sqlite:///{tmp_path}/control.db"))
        store.migrate()
        controller = ExecutionController(db, store, RunCancellationManager())
        async def deleted_graph(*_):
            pass
        controller._delete_graph_session = deleted_graph
        try:
            await controller.request_session_deletion("alice", "s")
            await eventually(lambda: ready(store.session_deletion("alice", "s")["state"]["delete_status"] == "deleted"))
            assert not root.exists()
            assert db.get_session(session_id="s", user_id="alice") is None
        finally:
            await controller.close()
            db.close()
            store.engine.dispose()
    asyncio.run(scenario())

"""Project fences, actual cancellation, durable cleanup and ownership."""
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from agent_runtime.execution_store import ControlError
from tests.test_cancellation_failures import running_stack
from tests.test_cancellation_http import eventually
from tests.servers.test_graph_ml_legacy_regression import _legacy_graph


async def ready(value):
    return value


@pytest.mark.parametrize('mode', ['first-token', 'stream', 'train', 'approval'])
def test_project_delete_stops_owned_execution_and_preserves_independent_work(tmp_path, mode):
    async def scenario():
        async with running_stack(tmp_path) as (client, other, graph, controller, metrics):
            project = (await client.post('/api/projects', json={'name': 'Delete target'})).json()['project']
            pid = project['project_id']
            created = await client.post('/api/sessions', json={'project_id': pid, 'model': 'qwen3.6-plus'})
            assert created.status_code == 200, created.text
            sid = created.json()['session_id']
            graph.get_required(sid, user_id='admin').set_graph(_legacy_graph())
            artifacts = graph._graph_ml._store
            owned = artifacts.create_model_dir(graph._session_key(sid, 'admin'), 'test')
            Path(owned.model_dir, 'complete.txt').write_text('keep until stopped')
            other_artifact = artifacts.create_model_dir(graph._session_key(other, 'admin'), 'test')
            Path(other_artifact.model_dir, 'complete.txt').write_text('untouched')
            question = mode + '-project-delete-' + str(uuid4())
            key = {'start_request_id': str(uuid4())}
            observer = asyncio.create_task(client.post(f'/api/sessions/{sid}/chat/stream', json={'message': question, **key}))
            cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
            original = controller.project_remote
            async def delayed_cleanup(row, component, method, path, body=None):
                if component == 'graph' and path.endswith('/artifacts'):
                    cleanup_started.set()
                    await release_cleanup.wait()
                return await original(row, component, method, path, body)
            controller.project_remote = delayed_cleanup
            try:
                async def entered():
                    if mode == 'train':
                        return any(job.session_id == sid and job.tool_name == 'train' and job.progress > 0 for job in graph._job_manager._jobs.values())
                    if mode == 'approval':
                        return (await client.get(f'/api/sessions/{sid}/runs/control', params=key)).json().get('run_status') == 'paused'
                    return metrics['calls'].get(question)
                await eventually(entered, 40)
                response = await client.request('DELETE', f'/api/projects/{pid}', json={'conversation_action': 'delete'})
                assert response.status_code == 202, response.text
                assert (await client.get('/api/projects')).json()['projects'] == []
                assert sid not in {row['session_id'] for row in (await client.get('/api/sessions')).json()['sessions']}
                late = await client.post('/api/sessions', json={'project_id': pid})
                assert late.status_code == 409, late.text
                with pytest.raises(ControlError, match='PROJECT_DELETING'):
                    controller.store.register_project_session('admin', pid, 'late-member')
                await asyncio.wait_for(cleanup_started.wait(), 20)
                assert Path(owned.model_dir).exists()
                assert controller.db.get_session(session_id=sid, user_id='admin') is None
                assert all(job.status in {'done', 'failed', 'cancelled'} for job in graph._job_manager._jobs.values() if job.session_id == sid)
                release_cleanup.set()
                await eventually(lambda: ready(controller.store.project_deletion('admin', pid)['state']['delete_status'] == 'deleted'))
                assert not Path(owned.model_dir).exists()
                assert Path(other_artifact.model_dir).exists()
                assert controller.db.get_session(session_id=other, user_id='admin') is not None
                assert (await client.get(f'/api/sessions/{other}/messages')).status_code == 200
                status = await client.get(f'/api/projects/{pid}/deletion')
                assert status.json()['delete_status'] == 'deleted'
                assert (await client.request('DELETE', f'/api/projects/{pid}', json={'conversation_action': 'delete'})).status_code == 200
                await eventually(lambda: ready(metrics['closed'].get(question) == metrics['calls'].get(question)))
            finally:
                release_cleanup.set()
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('phase', ['native', 'graph'])
def test_delete_project_during_session_creation_cannot_resurrect_it(tmp_path, phase):
    from backend.service_client import ServiceClient
    from unittest.mock import patch
    async def scenario():
        async with running_stack(tmp_path) as (client, other, _graph, controller, _metrics):
            pid = (await client.post('/api/projects', json={'name': 'Creation race'})).json()['project']['project_id']
            sid = str(uuid4())
            entered = asyncio.Event()
            original = ServiceClient.request
            async def delayed(self, method, path, **kwargs):
                match = method == 'POST' and ((phase == 'native' and path == '/sessions') or (phase == 'graph' and path == '/sessions/' + sid))
                if match:
                    entered.set()
                    await asyncio.Event().wait()
                return await original(self, method, path, **kwargs)
            with patch.object(ServiceClient, 'request', delayed):
                creating = asyncio.create_task(client.post('/api/sessions', json={'project_id': pid, 'client_session_id': sid}))
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    accepted = await client.request('DELETE', '/api/projects/' + pid, json={'conversation_action': 'delete'})
                    assert accepted.status_code == 202
                    await eventually(lambda: ready(controller.store.project_deletion('admin', pid)['state']['delete_status'] == 'deleted'))
                finally:
                    creating.cancel()
                    await asyncio.gather(creating, return_exceptions=True)
            assert controller.db.get_session(session_id=sid, user_id='admin') is None
            assert controller.db.get_session(session_id=other, user_id='admin') is not None
            late = await client.post('/api/sessions', json={'project_id': pid, 'client_session_id': sid})
            assert late.status_code in {404, 409}
    asyncio.run(scenario())


def test_project_deletion_recovers_after_restart_and_enumerates_all_pages(tmp_path):
    import time
    from agno.db.sqlite import SqliteDb
    from agno.session.agent import AgentSession
    from sqlalchemy import create_engine
    from agent_runtime.execution_control import ExecutionController
    from agent_runtime.execution_scope import RunCancellationManager
    from agent_runtime.execution_store import ExecutionStore
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'native.db'))
        pid = str(uuid4())
        for index in range(105):
            db.upsert_session(AgentSession(session_id=f's-{index:03}', agent_id='igraph-agent', user_id='alice', runs=[],
                session_data={'session_state': {'project_id': pid}}, created_at=int(time.time()), updated_at=int(time.time())))
        db.upsert_session(AgentSession(session_id='other', agent_id='igraph-agent', user_id='bob', runs=[],
            session_data={'session_state': {'project_id': pid}}, created_at=int(time.time()), updated_at=int(time.time())))
        store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/controls.db'))
        store.migrate()
        controller = ExecutionController(db, store, RunCancellationManager())
        failure_seen = asyncio.Event()
        async def graph_deleted(*_):
            pass
        async def interrupted(row, component, method, path, body=None):
            if path.endswith('cancel-preparations'):
                return {'execution_stopped': True}
            if component == 'backend':
                failure_seen.set()
                await asyncio.Event().wait()
            return {'success': True}
        controller._delete_graph_session = graph_deleted
        controller.project_remote = interrupted
        try:
            row = await controller.request_project_deletion('alice', pid, 'owner')
            await asyncio.wait_for(failure_seen.wait(), 15)
            await controller.close()
            assert len(store.project_members('alice', pid)) == 105
            assert store.project_deletion('alice', pid)['state']['delete_status'] == 'requested'
            controller = ExecutionController(db, ExecutionStore(store.engine), RunCancellationManager())
            controller._delete_graph_session = graph_deleted
            cleaned = []
            async def complete(row, component, method, path, body=None):
                cleaned.append(path)
                return {'execution_stopped': True, 'success': True}
            controller.project_remote = complete
            await controller.restore()
            await eventually(lambda: ready(store.project_deletion('alice', pid)['state']['delete_status'] == 'deleted'))
            assert store.project_deletion('alice', pid)['control_id'] == row['control_id']
            assert db.get_session(session_id='other', user_id='bob') is not None
            assert db.get_sessions(user_id='alice') == []
            assert any(path.endswith('/resources') for path in cleaned)
        finally:
            await controller.close()
            db.close()
            store.engine.dispose()
    asyncio.run(scenario())

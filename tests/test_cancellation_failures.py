"""Real HTTP races which cannot be proved by reducer/status-only assertions."""
import asyncio
import json
import socket
from contextlib import asynccontextmanager
from uuid import uuid4
from unittest.mock import patch

import httpx
import uvicorn

from tests.fixtures.cancellation_stack import cancellation_stack
from tests.servers.test_graph_ml_legacy_regression import _legacy_graph
from tests.test_cancellation_http import eventually


@asynccontextmanager
async def running_stack(root, *, learning=False):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    async with cancellation_stack(root, url, learning=learning) as (app, graph, controller, metrics):
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', lifespan='off', timeout_graceful_shutdown=5))
        serve = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            await asyncio.sleep(.01)
        try:
            async with httpx.AsyncClient(base_url=url, headers={'X-User-ID': 'admin', 'X-iGraph-Stream-Version': '2'},
                                         timeout=30, trust_env=False) as client:
                response = await client.post('/api/sessions', json={'model': 'qwen3.6-plus'})
                sid = response.json()['session_id']
                graph.get_required(sid, user_id='admin').set_graph(_legacy_graph())
                yield client, sid, graph, controller, metrics
        finally:
            scopes = list(controller.manager.scopes.values())
            for scope in scopes:
                scope.cancel()
            await graph._execution_host.close()
            server.should_exit = True
            await serve
            sock.close()


def test_native_handoff_failure_releases_admission_without_replaying_question(tmp_path):
    from backend.service_client import ServiceClient
    from fastapi import HTTPException
    async def scenario():
        async with running_stack(tmp_path) as (client, sid, graph, controller, metrics):
            original = ServiceClient.stream
            @asynccontextmanager
            async def unavailable(self, method, path, **kwargs):
                if path == '/agents/igraph-agent/runs':
                    raise HTTPException(502, 'injected native launch connection failure')
                async with original(self, method, path, **kwargs) as response:
                    yield response
            target = {'start_request_id': str(uuid4())}
            with patch.object(ServiceClient, 'stream', unavailable):
                response = await client.post(f'/api/sessions/{sid}/chat/stream', json={'message': 'complete-never-started', **target})
            assert response.status_code == 502
            row = controller.store.get('admin', sid, **target)
            row = await controller.reconcile(row)
            assert controller.store.summary(row)['can_send']
            assert controller.store.summary(row)['native_run_created'] is False
            assert metrics['calls'] == {}
    asyncio.run(scenario())


def test_disconnect_during_preparation_still_starts_native_run_once(tmp_path):
    from backend.service_client import ServiceClient

    async def scenario():
        async with running_stack(tmp_path) as (client, sid, graph, controller, metrics):
            ready, release = asyncio.Event(), asyncio.Event()
            original_stream, original_begin = ServiceClient.stream, controller.begin

            @asynccontextmanager
            async def delayed_handoff(self, method, path, **kwargs):
                if path == '/agents/igraph-agent/runs':
                    ready.set()
                    await release.wait()
                async with original_stream(self, method, path, **kwargs) as response:
                    yield response

            async def slow_native_start(*args, **kwargs):
                # HTTP 200 headers precede the native producer's durable claim.
                await asyncio.sleep(.3)
                return await original_begin(*args, **kwargs)

            question = 'complete-disconnected-preparation'
            target = {'start_request_id': str(uuid4())}
            with patch.object(ServiceClient, 'stream', delayed_handoff), patch.object(controller, 'begin', slow_native_start):
                observer = asyncio.create_task(client.post(f'/api/sessions/{sid}/chat/stream',
                    json={'message': question, **target}))
                try:
                    await asyncio.wait_for(ready.wait(), 40)
                    observer.cancel()
                    await asyncio.gather(observer, return_exceptions=True)
                    await asyncio.sleep(.1)
                    release.set()

                    async def completed():
                        result = (await client.get(f'/api/sessions/{sid}/runs/control', params=target)).json()
                        return result if result.get('can_send') else None

                    result = await eventually(completed, 15)
                    assert result['run_status'] == 'completed', result
                    assert result['native_run_created'] and result['terminal_persisted']
                    assert metrics['calls'][question] == 1
                    snapshot = await client.get(f'/api/sessions/{sid}/runs/{result["run_id"]}/snapshot')
                    assert snapshot.json()['snapshot_complete']
                finally:
                    release.set()
                    observer.cancel()
                    await asyncio.gather(observer, return_exceptions=True)

    asyncio.run(scenario())


def test_real_background_learning_does_not_hold_chat_admission(tmp_path, monkeypatch):
    from agent_runtime import agno_factory
    monkeypatch.setattr(agno_factory, "register_remote_graph_tools", lambda *_: [])
    async def scenario():
        async with running_stack(tmp_path, learning=True) as (client, sid, graph, controller, metrics):
            target = {'start_request_id': str(uuid4())}
            response = await client.post(f'/api/sessions/{sid}/chat/stream',
                json={'message': 'complete-my-name-is-Alice', **target})
            response.raise_for_status()
            await eventually(lambda: asyncio.sleep(0, result=len(metrics['calls']) >= 2))
            auxiliary = [key for key in metrics['calls'] if key != 'complete-my-name-is-Alice']
            assert auxiliary and not any(metrics['closed'].get(key) for key in auxiliary)
            async def confirmed():
                value = (await client.get(f'/api/sessions/{sid}/runs/control', params=target)).json()
                return value if value.get('can_send') else None
            summary = await eventually(confirmed)
            assert summary['execution_stopped'] and summary['terminal_persisted']
            assert summary['run_status'] == 'completed' and summary['cancel_status'] == 'none'
            # A genuinely new request is admitted while the auxiliary HTTP call
            # is still blocked; the complete previous answer stays in history.
            followup = await client.post(f'/api/sessions/{sid}/chat/stream',
                json={'message': 'complete-followup', 'start_request_id': str(uuid4())})
            followup.raise_for_status()
            assert metrics['calls']['complete-followup'] == 1
            assert any(message['role'] == 'assistant' for message in metrics['contexts']['complete-followup'])
            assert not any(metrics['closed'].get(key) for key in auxiliary)
            await controller.learning_queue.close()
            await eventually(lambda: asyncio.sleep(0, result=all(metrics['closed'].get(key) for key in auxiliary)))
    asyncio.run(scenario())


def test_embedding_http_connection_closes_and_does_not_retry_after_stop(tmp_path):
    from agent_runtime.cancellation_compat import CancellableEmbedder
    from agent_runtime.execution_scope import RunScope, current_scope
    from agno.exceptions import RunCancelledException
    import pytest
    async def scenario():
        async with running_stack(tmp_path) as (client, sid, graph, controller, metrics):
            scope = RunScope(str(uuid4()), 'admin', sid, 1)
            embedder = CancellableEmbedder(id='deterministic', api_key='test-only', base_url=str(client.base_url) + '/model/v1')
            token = current_scope.set(scope)
            task = asyncio.create_task(embedder.async_get_embedding('owned document'))
            current_scope.reset(token)
            try:
                await eventually(lambda: asyncio.sleep(0, result=metrics['calls'].get('embedding') == 1))
                scope.cancel()
                with pytest.raises(RunCancelledException):
                    await asyncio.wait_for(task, 5)
                await eventually(lambda: asyncio.sleep(0, result=metrics['closed'].get('embedding') == 1))
                assert scope.execution_stopped and metrics['calls']['embedding'] == 1
            finally:
                task.cancel(); await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_pre_cancel_prevents_all_native_work_and_delete_seals_late_launches(tmp_path):
    async def scenario():
        async with running_stack(tmp_path) as (client, sid, graph, controller, metrics):
            target = {'start_request_id': str(uuid4())}
            lookup = await client.get(f'/api/sessions/{sid}/runs/control', params=target)
            assert lookup.json()['registration_status'] == 'not_seen'
            response = await client.post(f'/api/sessions/{sid}/runs/cancel', json={'target': target, 'cancel_request_id': str(uuid4())})
            assert response.status_code == 200 and response.json()['native_run_created'] is False
            assert response.json()['execution_stopped'] and response.json()['terminal_persisted']
            for _ in range(3):
                late = await client.post(f'/api/sessions/{sid}/chat/stream', json={'message': 'must never run', **target})
                assert late.status_code == 409
            assert metrics['calls'] == {} and not graph._job_manager._jobs
            deleted = await client.delete(f'/api/sessions/{sid}')
            assert deleted.status_code == 202, deleted.text
            late = await client.post(f'/api/sessions/{sid}/chat/stream', json={'message': 'still never', 'start_request_id': str(uuid4())})
            assert late.status_code == 404
            assert metrics['calls'] == {}
            async def removed():
                return controller.store.session_deletion('admin', sid)['state']['delete_status'] == 'deleted'
            await eventually(removed)
    asyncio.run(scenario())


def test_upgrade_keeps_pre_control_run_history_readable_without_creating_execution(tmp_path):
    from agno.run.agent import RunOutput
    from agno.run.base import RunStatus
    async def scenario():
        async with running_stack(tmp_path) as (client, sid, graph, controller, metrics):
            rid = str(uuid4())
            session = await controller.owned_session('admin', sid)
            session.upsert_run(RunOutput(run_id=rid, agent_id='igraph-agent', session_id=sid, user_id='admin',
                                        content='历史任务已保存的完整结果', status=RunStatus.completed))
            await controller.call(controller.db.upsert_session, session)
            response = await client.get(f'/api/sessions/{sid}/runs/{rid}/snapshot')
            assert response.status_code == 200, response.text
            value = response.json()
            assert value['snapshot_complete'] and value['control'] is None
            assert value['messages'][-1]['content'] == '历史任务已保存的完整结果'
            assert value['messages'][-1]['run_id'] == rid
            assert controller.store.get('admin', sid, run_id=rid) is None
            assert metrics['calls'] == {} and not graph._job_manager._jobs
    asyncio.run(scenario())


def test_preparation_registration_response_loss_remains_cancellable(tmp_path):
    async def scenario():
        from backend.service_client import ServiceClient
        from fastapi import HTTPException
        original = ServiceClient.request
        lost = False
        async def lose_reply(self, method, path, **kwargs):
            nonlocal lost
            result = await original(self, method, path, **kwargs)
            if path.endswith('/runs/register') and not lost:
                lost = True
                raise HTTPException(502, 'injected response loss after durable registration')
            return result
        async with running_stack(tmp_path) as (client, sid, graph, controller, metrics):
            target = {'start_request_id': str(uuid4())}
            with patch.object(ServiceClient, 'request', lose_reply):
                response = await client.post(f'/api/sessions/{sid}/chat/stream', json={'message': 'registration-response-lost', **target})
            assert lost and response.status_code == 502
            response = await client.post(f'/api/sessions/{sid}/runs/cancel', json={'target': target, 'cancel_request_id': str(uuid4())})
            assert response.status_code in {200, 202}
            async def safe():
                state = (await client.get(f'/api/sessions/{sid}/runs/control', params=target)).json()
                return state if state.get('can_send') else None
            control = await eventually(safe)
            assert control['execution_stopped'] and control['terminal_persisted']
            assert control['native_run_created'] is False and control['run_status'] == 'cancelled'
            assert metrics['calls'] == {} and not graph._job_manager._jobs
    asyncio.run(scenario())


def test_control_database_failure_still_closes_owned_model_connection(tmp_path):
    async def scenario():
        async with running_stack(tmp_path) as (client, sid, graph, controller, metrics):
            target = {'start_request_id': str(uuid4())}
            async def observe():
                async with client.stream('POST', f'/api/sessions/{sid}/chat/stream', json={'message': 'first-token-db-failure', **target}) as stream:
                    async for _ in stream.aiter_bytes():
                        pass
            observer = asyncio.create_task(observe())
            try:
                async def started(): return metrics['calls'].get('first-token-db-failure')
                await eventually(started)
                body = {'target': target, 'cancel_request_id': str(uuid4())}
                with patch.object(controller.store, 'request_cancel', side_effect=OSError('injected DB outage')):
                    response = await client.post(f'/api/sessions/{sid}/runs/cancel', json=body)
                    assert response.status_code == 503
                    async def closed(): return metrics['closed'].get('first-token-db-failure')
                    await eventually(closed)
                response = await client.post(f'/api/sessions/{sid}/runs/cancel', json=body)
                assert response.status_code in {200, 202}
                async def safe():
                    return (await client.get(f'/api/sessions/{sid}/runs/control', params=target)).json().get('can_send')
                await eventually(safe)
                assert metrics['calls']['first-token-db-failure'] == 1
            finally:
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
    asyncio.run(scenario())


def test_disconnect_and_new_run_stop_leave_previous_background_training_alive(tmp_path):
    async def scenario():
        async with running_stack(tmp_path) as (client, sid, graph, controller, metrics):
            first = {'start_request_id': str(uuid4())}
            # Explicitly exercise a legacy detached job. Native DAG tools now
            # wait for job completion; this test covers jobs from older clients.
            async def detached_result(result, _poll, **_kwargs):
                return result
            with patch('agent_runtime.graph_jobs.completed_graph_result', detached_result):
                response = await client.post(f'/api/sessions/{sid}/chat/stream', json={'message': 'background-training', **first})
            assert response.status_code == 200, response.text
            async def first_completed():
                response = await client.get(f'/api/sessions/{sid}/runs/control', params=first)
                return response.json() if response.json().get('can_send') else None
            completed = await eventually(first_completed)
            assert completed['run_status'] == 'completed'
            async def training():
                return next((job for job in graph._job_manager._jobs.values() if job.tool_name == 'train' and job.progress > 0 and job.status == 'running'), None)
            job = await eventually(training, 40)
            second = {'start_request_id': str(uuid4())}
            async def observe():
                async with client.stream('POST', f'/api/sessions/{sid}/chat/stream', json={'message': 'first-token-disconnect', **second}) as stream:
                    async for _ in stream.aiter_bytes():
                        pass
            observer = asyncio.create_task(observe())
            try:
                async def entered():
                    return metrics['calls'].get('first-token-disconnect')
                await eventually(entered)
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
                assert not metrics['closed'].get('first-token-disconnect')
                response = await client.post(f'/api/sessions/{sid}/runs/cancel', json={'target': second, 'cancel_request_id': str(uuid4())})
                assert response.status_code in {200, 202}
                async def second_completed():
                    response = await client.get(f'/api/sessions/{sid}/runs/control', params=second)
                    return response.json().get('can_send')
                await eventually(second_completed)
                assert job.status == 'running' and not graph._job_manager._tasks[job.job_id].done()
                row = controller.store.get('admin', sid, job_id=job.job_id)
                import psutil
                assert psutil.pid_exists(row['state']['worker']['pid'])
                response = await client.post(f'/api/sessions/{sid}/jobs/{job.job_id}/cancel', json={'cancel_request_id': str(uuid4())})
                assert response.status_code in {200, 202}
                async def job_completed():
                    response = await client.get(f'/api/sessions/{sid}/jobs/{job.job_id}/control')
                    return response.json().get('execution_stopped') and response.json().get('terminal_persisted')
                await eventually(job_completed)
                assert job.status == 'cancelled' and graph._job_manager._tasks[job.job_id].done()
                response = await client.get(f'/api/sessions/{sid}/runs/control', params=first)
                assert response.json()['run_status'] == 'completed' and response.json()['cancel_status'] == 'none'
            finally:
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
    asyncio.run(scenario())

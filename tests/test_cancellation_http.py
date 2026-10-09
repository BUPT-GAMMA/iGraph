import asyncio
import os
import socket
from uuid import uuid4

import httpx
import pytest
import uvicorn

from tests.fixtures.cancellation_stack import cancellation_stack
from tests.servers.test_graph_ml_legacy_regression import _legacy_graph


async def eventually(callback, timeout=15):
    try:
        async with asyncio.timeout(timeout):
            while True:
                value = await callback()
                if value:
                    return value
                await asyncio.sleep(.05)
    except TimeoutError:
        # Preserve real pending execution evidence before fixture teardown.
        for task in asyncio.all_tasks():
            frames = []
            current = task.get_coro()
            while current is not None:
                frame = getattr(current, 'cr_frame', None) or getattr(current, 'ag_frame', None)
                if frame:
                    frames.append(f'{frame.f_code.co_filename}:{frame.f_lineno}:{frame.f_code.co_name}')
                current = getattr(current, 'cr_await', None) or getattr(current, 'ag_await', None)
            print('PENDING_TASK', task.get_name(), ' -> '.join(frames))
        raise


async def exercise(root, mode):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    async with cancellation_stack(root, url) as (app, graph, controller, metrics):
        scopes = []
        original_begin = controller.begin
        async def capture_scope(*args, **kwargs):
            scope = await original_begin(*args, **kwargs)
            scopes.append(scope)
            return scope
        controller.begin = capture_scope
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', lifespan='off', timeout_graceful_shutdown=5))
        serve = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            await asyncio.sleep(.01)
        headers = {'X-User-ID': 'admin', 'X-iGraph-Stream-Version': '2'}
        async with httpx.AsyncClient(base_url=url, headers=headers, timeout=30, trust_env=False) as client:
            question, start = mode + '-' + str(uuid4()), str(uuid4())
            response = await client.post('/api/sessions', json={'client_session_id': str(uuid4()), 'model': 'qwen3.6-plus'})
            assert response.status_code == 200, response.text
            sid = response.json()['session_id']
            graph.get_required(sid, user_id='admin').set_graph(_legacy_graph())
            key = {'start_request_id': start}
            cancel_id = str(uuid4())
            frames = []
            async def chat():
                async with client.stream('POST', f'/api/sessions/{sid}/chat/stream', json={'message': question, **key}) as stream:
                    assert stream.status_code == 200, await stream.aread()
                    async for line in stream.aiter_lines():
                        frames.append(line)
            observer = asyncio.create_task(chat())
            try:
                async def entered():
                    if mode == 'train':
                        return any(job.tool_name == 'train' and job.progress > 0 for job in graph._job_manager._jobs.values())
                    if mode == 'approval':
                        result = await client.get(f'/api/sessions/{sid}/runs/control', params=key)
                        return result.json().get('run_status') == 'paused'
                    return metrics['calls'].get(question)
                await eventually(entered, 40)
                assert scopes
                # Unauthorized native execution cannot bypass public admission.
                native = await client.post('/runtime/agents/igraph-agent/runs', data={'message': 'unregistered', 'session_id': sid})
                assert native.status_code == 403
                response = await client.post(f'/api/sessions/{sid}/runs/cancel', json={'cancel_request_id': cancel_id, 'target': key})
                assert response.status_code in {200, 202}, response.text
                rid = response.json()['run_id']
                async def stopped():
                    result = await client.get(f'/api/sessions/{sid}/runs/control', params=key)
                    value = result.json()
                    return value if value.get('can_send') and value.get('execution_stopped') and value.get('terminal_persisted') else None
                final = await eventually(stopped)
                assert final['run_status'] == 'cancelled', final
                assert metrics['calls'][question] <= (2 if mode == 'train' else 1)
                await eventually(lambda: _ready(metrics['closed'].get(question) == metrics['calls'][question]))
                assert all(scope.execution_stopped for scope in scopes)
                assert all(job.status in {'cancelled', 'done'} for job in graph._job_manager._jobs.values()), [
                    (job.job_id, job.tool_name, job.status, job.message) for job in graph._job_manager._jobs.values()]
                snapshot = await client.get(f'/api/sessions/{sid}/runs/{rid}/snapshot')
                assert snapshot.status_code == 200, snapshot.text
                assert snapshot.json()['snapshot_complete']
                duplicate = await client.post(f'/api/sessions/{sid}/chat/stream', json={'message': question, **key})
                assert duplicate.status_code == 409
                if mode == 'approval':
                    assert graph.get_required(sid, user_id='admin').get_graph() is not None
                assert metrics['tool_calls'].get(question, 0) <= 1
            finally:
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
                for scope in controller.manager.scopes.values(): scope.cancel()
                await graph._execution_host.close()
                server.should_exit = True
                await serve
                sock.close()


async def _ready(value):
    return value


@pytest.mark.parametrize('mode', ['first-token', 'stream', 'train', 'approval'])
# Daily CI exercises every scenario once; full acceptance retains 20 repetitions.
@pytest.mark.parametrize('iteration', range(max(1, int(os.getenv('IGRAPH_CANCEL_TEST_REPETITIONS', '20')))))
def test_full_factory_mcp_worker_and_bff_stop(tmp_path, mode, iteration):
    asyncio.run(exercise(tmp_path, mode))

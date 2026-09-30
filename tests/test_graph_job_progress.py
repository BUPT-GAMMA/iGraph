"""Background progress reaches consumers before completion and survives replay."""
import asyncio
import copy
import json

import pytest
from agno.tools.function import ToolResult

from agent_runtime.graph_jobs import completed_graph_result
from backend.event_mapper import map_product_events
from graph_data_service.job_manager import Job, JobManager
from tests.test_tool_dag import DAGModel, call


def job_manager(tmp_path, count=1):
    manager = JobManager(progress_root=tmp_path)
    job = Job(job_id='job-1', user_id='alice', session_id='s', tool_name='train', status='running')
    manager._jobs[job.job_id] = job
    callback = manager._make_progress_callback(job)
    for epoch in range(1, count + 1):
        callback({'kind': 'train_epoch', 'epoch': epoch, 'epochs': count + 1,
                  'val_acc': epoch / (count + 1), 'progress': epoch / (count + 1)})
    return manager, job


def test_progress_readers_have_independent_cursors_and_no_100_epoch_loss(tmp_path):
    manager, job = job_manager(tmp_path, 237)
    first = manager.read_progress(job.job_id, after_seq=0, limit=100)
    assert len(first['progress_updates']) == 100 and first['progress_has_more']
    manager.drain_pending_progress(job.job_id)
    assert manager.read_progress(job.job_id, after_seq=0, limit=100) == first
    second = manager.read_progress(job.job_id, after_seq=first['progress_cursor'], limit=100)
    third = manager.read_progress(job.job_id, after_seq=second['progress_cursor'], limit=100)
    rows = first['progress_updates'] + second['progress_updates'] + third['progress_updates']
    assert [row['epoch'] for row in rows] == list(range(1, 238))
    assert [row['progress_seq'] for row in rows] == list(range(1, 238))
    assert third['progress_has_more'] is False
    restored = JobManager(progress_root=tmp_path)
    assert restored.read_progress(job.job_id, after_seq=200, limit=100) == third


def test_native_dag_forwards_progress_before_final_result_and_dependent_execution(tmp_path):
    async def scenario():
        manager, job = job_manager(tmp_path, 2)
        observed, completed = asyncio.Event(), asyncio.Event()
        async def poll(job_id, *, after_seq):
            return {'success': True, 'job': job.to_dict(), **manager.read_progress(job_id, after_seq=after_seq)}
        async def training():
            return await completed_graph_result(ToolResult(content=json.dumps({
                'success': True, 'async': True, 'job_id': job.job_id, 'status': 'queued',
            })), poll, interval=.001, tool_name='train')
        async def dependent():
            assert completed.is_set()
            return '{"success":true}'
        async def finish():
            await asyncio.wait_for(observed.wait(), 2)
            job.result = {'success': True, 'model_id': 'm', 'metrics': {'val_acc': .9}}
            job.status = 'done'
            completed.set()
        finishing = asyncio.create_task(finish())
        results, products, native = [], [], []
        try:
            async for event in DAGModel(id='progress-test').arun_function_calls(
                    [call('train', training, 'train-call'), call('infer', dependent, 'infer-call')], results):
                if event.event == 'CustomEvent':
                    native.append(event.to_dict())
                    rows = map_product_events(event.to_dict())
                    products.extend(rows)
                    if any(row.get('progress', {}).get('epoch') == 2 for row in rows):
                        assert not completed.is_set()
                        observed.set()
            await finishing
        finally:
            finishing.cancel()
            await asyncio.gather(finishing, return_exceptions=True)
        epochs = [row for row in products if row.get('progress', {}).get('kind') == 'train_epoch']
        assert [row['progress']['epoch'] for row in epochs] == [1, 2]
        assert all(row['call_id'] == 'train-call' for row in epochs)
        assert all(row['progress']['job_id'] == 'job-1' for row in epochs)
        assert json.loads(results[0].content)['model_id'] == 'm'
        assert json.loads(results[1].content)['success']
        assert json.dumps(native).count('graph_job_progress') >= 2
    asyncio.run(scenario())


def test_real_agent_stream_persists_progress_and_replays_through_bff(tmp_path):
    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb
    from agno.models.response import ModelResponse
    from agno.os.managers import event_buffer
    from agno.tools.function import Function
    from agent_runtime.recovery import replay
    from backend.app import _messages_from_runs
    from backend.event_mapper import translate_sse

    async def scenario():
        manager, job = job_manager(tmp_path / 'jobs', 1)
        rounds, frames = [], []
        seen = asyncio.Event()
        class Scripted(DAGModel):
            async def ainvoke_stream(self, *args, **kwargs):
                if not rounds:
                    rounds.append(True)
                    yield ModelResponse(role='assistant', tool_calls=[{'id': 'train-native', 'type': 'function',
                                        'function': {'name': 'train', 'arguments': '{}'}}])
                else:
                    yield ModelResponse(role='assistant', content='训练完成')
        async def poll(job_id, *, after_seq):
            return manager.progress_status(job, after_seq=after_seq)
        async def train():
            return await completed_graph_result(ToolResult(content=json.dumps({
                'success': True, 'async': True, 'job_id': job.job_id, 'status': 'queued',
            })), poll, interval=.001, tool_name='train')
        async def finish():
            await asyncio.wait_for(seen.wait(), 5)
            manager._make_progress_callback(job)({'kind': 'train_epoch', 'epoch': 2, 'epochs': 2, 'val_acc': .9, 'progress': 1})
            job.status = 'done'
            job.result = {'success': True, 'metrics': {'val_acc': .9}, 'model_id': 'model-real'}
        db_path = str(tmp_path / 'agent.db')
        db = SqliteDb(db_file=db_path)
        agent = Agent(id='igraph-agent', model=Scripted(id='scripted'), db=db, telemetry=False,
                      store_events=True, stream_events=True, tools=[Function(name='train', entrypoint=train)])
        task = asyncio.create_task(finish())
        run_id = None
        try:
            async for frame in agent.arun('训练', session_id='s', user_id='alice', stream=True, stream_events=True, background=True):
                for line in frame.splitlines():
                    if line.startswith('data:'):
                        row = json.loads(line[5:])
                        frames.append(row)
                        run_id = row.get('run_id', run_id)
                        products = map_product_events(row)
                        if any(product.get('progress', {}).get('epoch') == 1 for product in products):
                            assert job.status == 'running'
                            partial = _messages_from_runs([{'run_id': run_id, 'status': 'running', 'events': copy.deepcopy(frames)}])
                            card = partial[0]['tools'][0]
                            assert card['call_id'] == 'train-native' and not card['done']
                            assert [p['epoch'] for p in card['history']] == [1]
                            seen.set()
            await task
            first = next(row for row in frames if any(p.get('progress', {}).get('epoch') == 1 for p in map_product_events(row)))
            cursor = first['event_index']
            async def native_lines():
                async for chunk in replay(db, 'igraph-agent', run_id, cursor, 'alice'):
                    for line in chunk.splitlines():
                        yield line
            batches = [json.loads(chunk[6:]) async for chunk in translate_sse(native_lines(), protocol_version=2, session_id='s', run_id=run_id)]
            indices = [b['event_index'] for b in batches if b.get('event_index') is not None]
            assert indices == list(range(cursor + 1, event_buffer.get_last_index(run_id) + 1))
            progress = [e for b in batches for e in b['events'] if e['type'] == 'tool_progress']
            assert any(e['progress'].get('epoch') == 2 and e['call_id'] == 'train-native' for e in progress)
            db.close()
            db = SqliteDb(db_file=db_path)
            saved = db.get_session(session_id='s', user_id='alice').get_run(run_id).to_dict()
            cards = _messages_from_runs([saved])[-1]['tools']
            card = next(c for c in cards if c['tool'] == 'train')
            assert card['done'] and card['result']['metrics']['val_acc'] == .9
            assert [row['epoch'] for row in card['history'] if row.get('epoch') is not None] == [1, 2]
            assert card['call_id'] == 'train-native' and card['job_id'] == 'job-1'
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if run_id:
                event_buffer.cleanup_run(run_id)
            db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('state', ['done', 'failed', 'cancelled'])
def test_terminal_pages_keep_every_epoch_and_emit_one_terminal_event(tmp_path, state):
    from agent_runtime.graph_progress import progress_sink
    async def scenario():
        manager, job = job_manager(tmp_path, 400)
        job.status, job.result = state, {'success': state == 'done', 'metrics': {'trained_epochs': 400}}
        pages = []
        async def record(payload):
            pages.append(payload)
        async def poll(job_id, *, after_seq):
            return manager.progress_status(job, after_seq=after_seq, limit=200)
        token = progress_sink.set(record)
        try:
            result = await completed_graph_result({'success': True, 'async': True, 'job_id': job.job_id}, poll, interval=0, tool_name='train')
        finally:
            progress_sink.reset(token)
        assert json.loads(result.content)['success'] == (state == 'done')
        products = [p for page in pages for p in map_product_events({'event': 'CustomEvent', 'tool_call_id': 'call',
                    'content': {'kind': 'graph_job_progress', 'tool_name': 'train', **page}})]
        assert [p['progress']['epoch'] for p in products if p['progress'].get('kind') == 'train_epoch'] == list(range(1, 401))
        terminals = [p for p in products if p['progress']['kind'] in {'train_done', 'job_failed', 'job_cancelled'}]
        assert len(terminals) == 1
    asyncio.run(scenario())


def test_cursor_poll_retries_transient_transport_without_skipping_progress(tmp_path):
    from httpx import ReadError
    async def scenario():
        manager, job = job_manager(tmp_path, 3)
        job.status, job.result = 'done', {'success': True}
        attempts = []
        async def poll(job_id, *, after_seq):
            attempts.append((job_id, after_seq))
            if len(attempts) == 1:
                raise ReadError('lost reply')
            return manager.progress_status(job, after_seq=after_seq)
        result = await completed_graph_result({'success': True, 'async': True, 'job_id': job.job_id}, poll, interval=0)
        assert json.loads(result.content)['success']
        assert attempts == [('job-1', 0), ('job-1', 0)]
    asyncio.run(scenario())


def test_progress_http_cursors_are_owner_scoped_and_validate_inputs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from graph_data_service.app import GraphDataService, create_app
    monkeypatch.setenv('IGRAPH_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'false')
    monkeypatch.setenv('NEBULA_ENABLED', 'false')
    service = GraphDataService(storage_backend='local', migrate_storage=False)
    manager, job = job_manager(tmp_path / 'progress', 3)
    service._job_manager = manager
    with TestClient(create_app(service)) as client:
        def read(user='alice', session='s', **args):
            response = client.post(f'/sessions/{session}/tools/get_job_status', headers={'X-User-ID': user},
                                   json={'arguments': {'job_id': job.job_id, **args}})
            assert response.status_code == 200
            return response.json()
        assert read()['progress_updates']
        page = read(after_seq=0, progress_limit=2)
        assert [p['epoch'] for p in page['progress_updates']] == [1, 2]
        assert page['progress_has_more']
        assert read(after_seq=0, progress_limit=2) == page
        assert [p['epoch'] for p in read(after_seq=page['progress_cursor'])['progress_updates']] == [3]
        for user, session in [('bob', 's'), ('alice', 'other')]:
            result = read(user, session, after_seq=0)
            assert result['success'] is False and not result['progress_updates']
        for args in ({'after_seq': -1}, {'after_seq': True}, {'after_seq': '1'}, {'after_seq': 2**63},
                     {'after_seq': 0, 'progress_limit': 0}, {'after_seq': 0, 'progress_limit': 1001}):
            assert read(**args)['code'] == 'INVALID_PROGRESS_CURSOR'



def test_progress_cleanup_is_scoped_and_stops_late_callbacks(tmp_path):
    manager, job = job_manager(tmp_path, 3)
    other = Job(job_id='other', user_id='bob', session_id='s', tool_name='train', status='running')
    manager._jobs[other.job_id] = other
    manager._make_progress_callback(other)({'epoch': 1})
    restored = JobManager(progress_root=tmp_path)
    restored.delete_progress('s', 'alice')
    assert not restored.read_progress(job.job_id)['progress_updates']
    assert len(restored.read_progress(other.job_id)['progress_updates']) == 1
    manager.delete_progress('s', 'alice')
    manager._make_progress_callback(job)({'epoch': 4})
    assert not manager.read_progress(job.job_id)['progress_updates']


def test_cancelling_progress_waiter_cleans_up_polling_and_blocks_dependencies(tmp_path):
    async def scenario():
        manager, job = job_manager(tmp_path, 1)
        entered, released, polls = asyncio.Event(), [], []
        async def poll(job_id, *, after_seq):
            polls.append(after_seq)
            if after_seq:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    released.append(True)
            return manager.progress_status(job, after_seq=after_seq)
        async def train():
            return await completed_graph_result({'success': True, 'async': True, 'job_id': job.job_id}, poll, interval=0)
        async def dependent():
            pytest.fail('cancelled parent cannot release dependent')
        async def consume():
            async for _ in DAGModel(id='cancel-progress').arun_function_calls(
                    [call('train', train, 'train'), call('infer', dependent, 'infer')], []):
                pass
        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released == [True] and polls == [0, 1]
        assert manager.read_progress(job.job_id)['progress_updates'][0]['epoch'] == 1
    asyncio.run(scenario())


def test_progress_input_is_copied_and_storage_failure_is_not_acknowledged(tmp_path, monkeypatch):
    manager, job = job_manager(tmp_path, 0)
    update = {'kind': 'train_epoch', 'epoch': 1, 'metrics': {'accuracy': .5}}
    manager._make_progress_callback(job)(update)
    update['metrics']['accuracy'] = 99
    assert manager.read_progress(job.job_id)['progress_updates'][0]['metrics']['accuracy'] == .5
    def fail(*_args, **_kwargs):
        raise OSError('journal write failed')
    monkeypatch.setattr(manager._progress_store, 'append', fail)
    with pytest.raises(OSError, match='journal write failed'):
        manager._make_progress_callback(job)({'epoch': 2})
    assert manager.read_progress(job.job_id)['progress_cursor'] == 1
    assert job.detail['epoch'] == 1


@pytest.mark.parametrize('page', [
    {'progress_cursor': 0, 'progress_has_more': True},
    {'progress_cursor': 2, 'progress_updates': [{'progress_seq': 1}]},
    {'progress_cursor': 1, 'progress_updates': [{'progress_seq': 1}, {'progress_seq': 1}]},
    {'progress_cursor': True}, {'progress_cursor': 0, 'progress_has_more': 'false'},
])
def test_invalid_progress_pages_fail_instead_of_skipping_or_spinning(page):
    async def scenario():
        async def poll(_job_id, *, after_seq):
            return {'success': True, 'job': {'status': 'running'}, **page}
        with pytest.raises(RuntimeError, match='PROGRESS_CURSOR_INVALID'):
            await completed_graph_result({'success': True, 'async': True, 'job_id': 'j'}, poll, interval=0)
    asyncio.run(scenario())


@pytest.mark.parametrize('final', [{'success': True, 'model_id': 'm', 'job_id': 'j'}, 'Error: status transport unavailable'])
def test_snapshot_keeps_native_completion_when_last_progress_is_running(final):
    from backend.app import _messages_from_runs
    run = {'run_id': 'r', 'status': 'completed', 'tools': [{'tool_name': 'train', 'tool_call_id': 'call', 'result': final}],
           'events': [{'event': 'CustomEvent', 'tool_call_id': 'call', 'content': {
               'kind': 'graph_job_progress', 'tool_name': 'train', 'job': {'job_id': 'j', 'status': 'running', 'tool_name': 'train'},
               'progress_updates': [{'kind': 'train_epoch', 'epoch': 1, 'val_acc': .5}]}}]}
    card = _messages_from_runs([run])[0]['tools'][0]
    assert card['done']
    assert card['history'][0]['epoch'] == 1
    if isinstance(final, str):
        assert card['result'] == final
    else:
        assert card['result']['model_id'] == 'm' and card['progress']['status'] == 'done'


def test_restored_curve_keeps_actual_epoch_metrics_when_early_stopping():
    from backend.app import _messages_from_runs
    epoch = {'kind': 'train_epoch', 'epoch': 2, 'val_acc': .4, 'train_acc': .5}
    early_stop = {'kind': 'train_early_stop', 'epoch': 2, 'val_acc': .9, 'stopped_early': True}
    run = {'run_id': 'r', 'status': 'cancelled', 'events': [{
        'event': 'CustomEvent', 'tool_call_id': 'call', 'content': {
            'kind': 'graph_job_progress', 'tool_name': 'train',
            'job': {'job_id': 'j', 'status': 'running', 'tool_name': 'train', 'detail': early_stop},
            'progress_updates': [epoch, early_stop]}}]}
    card = _messages_from_runs([run])[0]['tools'][0]
    assert card['history'] == [epoch]
    assert card['progress']['stopped_early']
    assert card['done'] and card['progress']['status'] == card['result']['job_status'] == 'cancelled'


@pytest.mark.parametrize('final', [None, {'success': True, 'async': True, 'job_id': 'j'}])
def test_invalid_final_result_does_not_emit_successful_completion(final):
    from agent_runtime.graph_progress import progress_sink
    async def scenario():
        pages = []
        async def record(page):
            pages.append(page)
        async def poll(job_id, *, after_seq):
            return {'success': True, 'job': {'job_id': job_id, 'status': 'done', 'result': final}}
        token = progress_sink.set(record)
        try:
            with pytest.raises(RuntimeError, match='GRAPH_JOB_(RESULT_UNAVAILABLE|DEPENDENCY_INVALID)'):
                await completed_graph_result({'success': True, 'async': True, 'job_id': 'j'}, poll, interval=0)
        finally:
            progress_sink.reset(token)
        assert all(page['job']['status'] != 'done' for page in pages)
    asyncio.run(scenario())


def test_fastmcp_string_result_falls_back_from_structured_wrapper():
    from mcp.types import CallToolResult, TextContent
    from agent_runtime.construction_state import _result_payload
    payload = {'success': True, 'job': {'job_id': 'j', 'status': 'running'}, 'progress_cursor': 0}
    wire = CallToolResult(content=[TextContent(type='text', text=json.dumps(payload))],
                          structuredContent={'result': json.dumps(payload)})
    assert _result_payload(wire) == payload


def test_real_http_mcp_training_delivers_epochs_and_final_model(tmp_path):
    import socket
    from uuid import uuid4
    import httpx
    import uvicorn
    from tests.fixtures.cancellation_stack import cancellation_stack
    from tests.servers.test_graph_ml_legacy_regression import _legacy_graph
    from backend.app import _messages_from_runs
    async def scenario():
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        url = f'http://127.0.0.1:{sock.getsockname()[1]}'
        async with cancellation_stack(tmp_path, url, training_epochs=3) as (app, graph, controller, metrics):
            server = uvicorn.Server(uvicorn.Config(app, log_level='warning', lifespan='off', timeout_graceful_shutdown=5))
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            while not server.started:
                await asyncio.sleep(.01)
            try:
                async with httpx.AsyncClient(base_url=url, headers={'X-User-ID': 'admin', 'X-iGraph-Stream-Version': '2'},
                                             timeout=60, trust_env=False) as client:
                    (await client.post('/api/auth/login', json={'username': 'admin', 'password': 'cancellation-test-password'})).raise_for_status()
                    created = await client.post('/api/sessions', json={'model': 'qwen3.6-plus'})
                    sid = created.json()['session_id']
                    graph.get_required(sid, user_id='admin').set_graph(_legacy_graph())
                    response = await client.post(f'/api/sessions/{sid}/chat/stream',
                        json={'message': 'background-three-epochs', 'start_request_id': str(uuid4())})
                    assert response.status_code == 200, response.text
                    batches = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]
                    events = [event for batch in batches for event in batch.get('events', [])]
                    epochs = [event for event in events if event.get('type') == 'tool_progress'
                              and event.get('progress', {}).get('kind') == 'train_epoch']
                    assert [event['progress']['epoch'] for event in epochs] == [1, 2, 3]
                    assert {event['call_id'] for event in epochs} == {'call-1'}
                    completed = [event for event in events if event.get('type') == 'tool_result' and event.get('tool') == 'train']
                    assert len(completed) == 1
                    result = completed[0]['result']
                    result = json.loads(result) if isinstance(result, str) else result
                    assert result['success'] and result['model_id'] and not result['async']
                    job_id = epochs[0]['progress']['job_id']
                    assert result['job_id'] == job_id
                    status = await client.post(f'/graph/sessions/{sid}/tools/get_job_status',
                                               json={'arguments': {'job_id': job_id, 'after_seq': 0}})
                    assert [row['epoch'] for row in status.json()['progress_updates'] if row['kind'] == 'train_epoch'] == [1, 2, 3]
            finally:
                for scope in controller.manager.scopes.values():
                    scope.cancel()
                await graph._execution_host.close()
                server.should_exit = True
                await serving
                sock.close()
    asyncio.run(scenario())


def test_slow_progress_journal_does_not_block_loop_or_worker_cancellation():
    import sys
    import threading
    from graph_data_service.worker_process import WorkerProcess
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        def slow_write(_update):
            entered.set()
            assert release.wait(2), 'event loop was blocked by progress storage'
        worker = WorkerProcess(grace=.05, terminate_grace=.05, reap_grace=.1)
        script = "import sys; sys.stdin.readline(); line = '{\"type\":\"progress\",\"value\":{\"epoch\":1}}\\n'; sys.stdout.write(line * 20000); sys.stdout.flush()"
        task = asyncio.create_task(worker.run({}, slow_write, command=[sys.executable, '-c', script]))
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(.005)
            # Cancellation must own the pending write until it settles, then
            # reap the producer even when its stdout is saturated.
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert worker.process.returncode is not None
            assert not worker._executing_group()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())

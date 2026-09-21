"""Exercise durable graph work through the existing controlled HTTP contract."""
import asyncio
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine

from agent_runtime.execution_store import ExecutionStore
from graph_data_service.app import GraphDataService, create_app
from graph_data_service.execution_host import GraphExecutionHost, JobContext, job_context
from graph_data_service.job_manager import Job, JobManager
from tests.fixtures.large_graph_worker import holding_worker as _holding_worker


async def eventually(fn, timeout=15):
    async with asyncio.timeout(timeout):
        while True:
            value = fn()
            if value:
                return value
            await asyncio.sleep(.02)


@asynccontextmanager
async def controlled_service(tmp_path, monkeypatch):
    for key, value in {
        'IGRAPH_DATA_ROOT': str(tmp_path / 'data'),
        'GRAPH_DATA_UPLOAD_DIR': str(tmp_path / 'uploads'),
        'GRAPH_ML_ARTIFACT_ROOT': str(tmp_path / 'artifacts'),
        'GRAPH_STORAGE_BACKEND': 'local',
        'LARGE_GRAPH_EMBEDDED_WORKER': 'false',
        'IGRAPH_RUN_CANCEL_ENABLED': 'true',
        'IGRAPH_CONTROL_SERVICE_KEY': 'large-graph-integration-test-only',
    }.items():
        monkeypatch.setenv(key, value)
    service = GraphDataService(migrate_storage=False)
    host = service._execution_host
    ledger = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/controls.db'))
    ledger.migrate()
    run, _ = ledger.register('alice', 'session', str(uuid4()), 'test')
    headers = {
        'X-User-ID': 'alice', 'X-iGraph-Service-Key': 'large-graph-integration-test-only',
        'X-Run-ID': run['run_id'], 'X-Execution-Generation': '1', 'X-Operation-ID': str(uuid4()),
    }

    async def request(ctx, method, path, body=None):
        # Keep the real transactional execution ledger; only replace HTTP transport.
        if path == '/jobs/register':
            row, claimed = ledger.register_job(ctx.user_id, ctx.session_id, ctx.run_id, ctx.generation,
                body['operation_id'], body['fingerprint'], parent_id=body['parent_id'],
                executor_instance=body['executor_instance'], tool_name=body['tool_name'])
            return {**row, 'claimed': claimed}
        if path.endswith('/control'):
            row = ledger.get(ctx.user_id, ctx.session_id, job_id=ctx.job_id)
            parent = ledger.get(ctx.user_id, ctx.session_id, run_id=ctx.run_id)
            return {'cancel_requested': row['state']['cancel_status'] != 'none' or parent['state']['cancel_status'] != 'none'}
        if path.endswith('/cancel'):
            row = ledger.cancel_job(ctx.user_id, ctx.session_id, ctx.job_id, body['cancel_request_id'])
            return ledger.job_summary(row)
        if path.endswith('/ack'):
            ledger.acknowledge_job(ctx.user_id, ctx.session_id, ctx.job_id, ctx.generation,
                status=body['status'], execution_stopped=body['execution_stopped'],
                terminal_persisted=body.get('terminal_persisted', True))
        return {}

    host.request = request
    source = tmp_path / 'sample.edges'
    source.write_text('A B 1\nB C 1\n', encoding='utf-8')
    service.get_or_create('session', user_id='alice').register_upload('token', 'sample.edges', str(source))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)), base_url='http://test') as client:
            yield service, host, ledger, client, headers, source
    finally:
        # Tests that model a worker's exit must not leave a claimed fake worker
        # behind if an assertion fails. Real workers are stopped by their runtime.
        service._large_graph.close()
        for row in service._large_graph.catalog.execution_jobs(user_id='alice', session_id='session', active_only=True):
            service._large_graph.cancel(row['job_id'], user_id='alice', session_id='session')
            if row['status'] != 'queued':
                service._large_graph.catalog.finish_cancelled(row['job_id'], lease_id=row['lease_id'])
        await host.close()
        service._job_manager.cancel_all()
        service._process_pool.shutdown(wait=True, cancel_futures=True)
        service._nebula.close()
        ledger.engine.dispose()


async def tool(client, headers, name, arguments):
    response = await client.post('/sessions/session/tools/' + name, headers=headers, json={'arguments': arguments})
    assert response.status_code == 200, response.text
    return response.json()


async def prepare(client, headers):
    return await tool(client, headers, 'prepare_large_graph', {'filename': 'sample.edges', 'partitions': 1, 'workers': 1})


def test_controlled_prepare_compute_and_result_reads_share_identity_safely(tmp_path, monkeypatch):
    async def scenario():
        async with controlled_service(tmp_path, monkeypatch) as (service, host, ledger, client, headers, _):
            initial = await prepare(client, headers)
            assert initial['async']
            durable = service._large_graph.catalog.get_job(initial['job_id'], user_id='alice', session_id='session')
            owner = durable['request']['execution']['job_id']
            assert durable['request']['execution']['run_id'] == headers['X-Run-ID']
            assert service._job_manager.status(owner).status == 'running'
            assert not ledger.get('alice', 'session', job_id=owner)['state']['execution_stopped']
            status = (await client.get('/internal/sessions/session/resource-status', headers=headers)).json()
            assert not status['execution_stopped'] and initial['job_id'] in status['pending_jobs']
            # Reads neither conflict with the original operation nor create another ledger job.
            assert (await tool(client, headers, 'get_job_result', {'job_id': initial['job_id']}))['success']
            assert len(ledger.jobs('alice', 'session', headers['X-Run-ID'])) == 1
            service._large_graph.runtime.start()
            await eventually(lambda: service._large_graph.job_status(initial['job_id'], user_id='alice', session_id='session')['status'] == 'done')
            result = await tool(client, headers, 'get_job_result', {'job_id': initial['job_id']})
            assert result['result']['num_nodes'] == 3
            await asyncio.wait_for(service._job_manager.wait(owner), 5)
            compute_headers = {**headers, 'X-Operation-ID': str(uuid4())}
            computed = await tool(client, compute_headers, 'run_large_graph_algorithm', {
                'graph_id': initial['graph_id'], 'graph_version': initial['graph_version'],
                'algorithm': 'degree', 'workers': 1,
            })
            await eventually(lambda: service._large_graph.job_status(computed['job_id'], user_id='alice', session_id='session')['status'] == 'done')
            final = await tool(client, compute_headers, 'get_job_result', {'job_id': computed['job_id']})
            assert final['success'] and final['result']['artifact_id']
            replay = await prepare(client, headers)
            assert replay['job_id'] == initial['job_id']
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel_via', ['tool', 'run', 'job_api'])
def test_cancel_keeps_parent_active_until_durable_worker_exit(tmp_path, monkeypatch, cancel_via):
    async def scenario():
        async with controlled_service(tmp_path, monkeypatch) as (service, host, ledger, client, headers, _):
            initial = await prepare(client, headers)
            claimed = service._large_graph.catalog.claim_next_job()
            jid = initial['job_id']; owner = claimed['request']['execution']['job_id']
            if cancel_via == 'tool':
                cancelled = await tool(client, headers, 'cancel_job', {'job_id': jid})
                assert not cancelled['execution_stopped']
            elif cancel_via == 'run':
                ledger.request_cancel('alice', 'session', str(uuid4()), run_id=headers['X-Run-ID'])
                response = await client.post(f"/internal/sessions/session/runs/{headers['X-Run-ID']}/cancel", headers=headers)
                assert jid in response.json()['pending_jobs']
            else:
                response = await client.post(f'/internal/sessions/session/large-jobs/{jid}/cancel', headers=headers,
                                            json={'cancel_request_id': str(uuid4())})
                assert response.status_code == 200 and not response.json()['execution_stopped']
            await asyncio.sleep(.05)
            assert service._large_graph.job_status(jid, user_id='alice', session_id='session')['status'] == 'cancelling'
            assert service._job_manager.status(owner).status not in {'done', 'failed', 'cancelled'}
            assert not ledger.get('alice', 'session', job_id=owner)['state']['execution_stopped']
            assert not (await client.get('/internal/sessions/session/resource-status', headers=headers)).json()['execution_stopped']
            service._large_graph.catalog.finish_cancelled(jid, lease_id=claimed['lease_id'])
            await asyncio.wait_for(service._job_manager.wait(owner), 5)
            assert service._job_manager.status(owner).status == 'cancelled'
            control = (await client.get(f'/internal/sessions/session/large-jobs/{jid}/control', headers=headers)).json()
            assert control['execution_stopped'] and control['terminal_persisted']
            await eventually(lambda: ledger.get('alice', 'session', job_id=owner)['state']['terminal_persisted'])
    asyncio.run(scenario())


def test_cancel_during_enqueue_cannot_orphan_the_new_job(tmp_path, monkeypatch):
    async def scenario():
        async with controlled_service(tmp_path, monkeypatch) as (service, host, ledger, client, headers, _):
            original = service._large_graph.submit_preparation
            entered, release = threading.Event(), threading.Event()
            def delayed(**kwargs):
                result = original(**kwargs)
                entered.set()
                assert release.wait(5)
                return result
            monkeypatch.setattr(service._large_graph, 'submit_preparation', delayed)
            submission = asyncio.create_task(client.post('/sessions/session/tools/prepare_large_graph',
                headers=headers, json={'arguments': {'filename': 'sample.edges', 'workers': 1, 'partitions': 1}}))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                owner = next(iter(service._job_manager._jobs))
                service._job_manager.cancel(owner)
            finally:
                release.set()
            response = await asyncio.wait_for(submission, 5)
            assert response.status_code == 409
            rows = service._large_graph.catalog.execution_jobs(user_id='alice', session_id='session')
            assert len(rows) == 1 and rows[0]['status'] == 'cancelled'
    asyncio.run(scenario())


def test_run_stop_does_not_cancel_another_users_durable_work(tmp_path, monkeypatch):
    async def scenario():
        async with controlled_service(tmp_path, monkeypatch) as (service, host, ledger, client, headers, source):
            first = await prepare(client, headers)
            other, _ = ledger.register('bob', 'session', str(uuid4()), 'other')
            service.get_or_create('session', user_id='bob').register_upload('bob-token', 'sample.edges', str(source))
            other_headers = {**headers, 'X-User-ID': 'bob', 'X-Run-ID': other['run_id'], 'X-Operation-ID': str(uuid4())}
            second = await prepare(client, other_headers)
            ledger.request_cancel('alice', 'session', str(uuid4()), run_id=headers['X-Run-ID'])
            response = await client.post(f"/internal/sessions/session/runs/{headers['X-Run-ID']}/cancel", headers=headers)
            assert response.status_code == 200
            assert service._large_graph.job_status(first['job_id'], user_id='alice', session_id='session')['status'] == 'cancelled'
            assert service._large_graph.job_status(second['job_id'], user_id='bob', session_id='session')['status'] == 'queued'
            assert ledger.get('bob', 'session', run_id=other['run_id'])['state']['cancel_status'] == 'none'
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', [True, False])
def test_restart_reconciles_durable_job_before_acknowledging_exit(tmp_path, monkeypatch, cancel):
    async def scenario():
        async with controlled_service(tmp_path, monkeypatch) as (service, host, ledger, client, headers, source):
            ctx = JobContext('alice', 'session', headers['X-Run-ID'], 1, headers['X-Operation-ID'])
            row, ctx = await host.register(ctx, 'prepare_large_graph', {})
            token = job_context.set(ctx)
            try:
                queued = service._large_graph.submit_preparation(user_id='alice', session_id='session', graph_id='session',
                    source_path=str(source), source_name='sample.edges', source_token='token', directed=True,
                    workers=1, partitions=1, delimiter='whitespace', input_format='edgelist', quality_policy=None)
            finally:
                job_context.reset(token)
            host.lease.close()  # Persisted execution identity is now provably dead.
            recovered = GraphExecutionHost(JobManager(), execution_root=host.lease.root.parent)
            recovered.large_graph = service._large_graph
            recovered.request = host.request
            try:
                recovery = asyncio.create_task(recovered.recover(row))
                await asyncio.sleep(.1)
                assert not recovery.done()
                assert not ledger.get('alice', 'session', job_id=ctx.job_id)['state']['execution_stopped']
                if cancel:
                    ledger.request_cancel('alice', 'session', str(uuid4()), run_id=ctx.run_id)
                else:
                    service._large_graph.runtime.start()
                assert await asyncio.wait_for(recovery, 15)
                durable = service._large_graph.job_status(queued['job_id'], user_id='alice', session_id='session')
                assert durable['status'] == ('cancelled' if cancel else 'done')
                assert ledger.get('alice', 'session', job_id=ctx.job_id)['state']['terminal_persisted']
            finally:
                await recovered.close()
    asyncio.run(scenario())


def test_controlled_global_tools_cannot_bypass_large_guard_and_small_path_still_works(tmp_path, monkeypatch):
    async def scenario():
        async with controlled_service(tmp_path, monkeypatch) as (service, host, ledger, client, headers, source):
            service.get_required('session', user_id='alice').load_graph_from_upload_path(str(source))
            service._large_graph.router.large_node_threshold = 1
            rejected = await tool(client, headers, 'global_metrics', {'metric': 'density'})
            assert rejected['success'] is False
            assert service._job_manager.status(rejected['job_id']).result['code'] == 'GRAPH_VERSION_REPRESENTATION_MISMATCH'
            service._large_graph.router.large_node_threshold = 1000
            small = await tool(client, {**headers, 'X-Operation-ID': str(uuid4())}, 'global_metrics', {'metric': 'density'})
            assert small['success'] and small['value'] == pytest.approx(1/3, abs=1e-6)
    asyncio.run(scenario())


def test_controlled_legacy_tool_routes_to_matching_large_snapshot(tmp_path, monkeypatch):
    async def scenario():
        async with controlled_service(tmp_path, monkeypatch) as (service, host, ledger, client, headers, source):
            session = service.get_required('session', user_id='alice')
            session.load_graph_from_upload_path(str(source))
            session.mark_version_upload_source(session.graph_version, session.resolve_upload_by_name('sample.edges'))
            prepared = await tool(client, headers, 'prepare_large_graph', {
                'filename': 'sample.edges', 'graph_version': session.graph_version, 'partitions': 1, 'workers': 1,
            })
            service._large_graph.runtime.start()
            await eventually(lambda: service._large_graph.job_status(prepared['job_id'], user_id='alice', session_id='session')['status'] == 'done')
            service._large_graph.router.large_node_threshold = 1
            computed = await tool(client, {**headers, 'X-Operation-ID': str(uuid4())}, 'global_metrics', {'metric': 'density'})
            assert computed['async'] and computed['job_id'].startswith('lg_')
            await eventually(lambda: service._large_graph.job_status(computed['job_id'], user_id='alice', session_id='session')['status'] == 'done')
            result = await tool(client, headers, 'get_job_result', {'job_id': computed['job_id']})
            assert result['success'] and result['result']['summary']['value'] == pytest.approx(1/3, abs=1e-6)
    asyncio.run(scenario())


@pytest.mark.parametrize('large', [True, False])
def test_controlled_upload_uses_preflight_before_isolated_loader(tmp_path, monkeypatch, large):
    async def scenario():
        async with controlled_service(tmp_path, monkeypatch) as (service, host, ledger, client, headers, _):
            monkeypatch.setenv('LARGE_GRAPH_NODE_THRESHOLD', '1' if large else '1000')
            submitted = await tool(client, headers, 'load_graph_from_upload', {'filename': 'sample.edges'})
            parent = submitted['job_id']
            await asyncio.wait_for(service._job_manager.wait_submission(parent), 10)
            job = service._job_manager.status(parent)
            assert job.result['success']
            if large:
                assert job.status == 'running' and job.result['job_id'].startswith('lg_')
                assert job.result['route_decision']['path'] == 'large_graph'
                service._job_manager.cancel(parent)
                await asyncio.wait_for(service._job_manager.wait(parent), 5)
            else:
                assert job.status == 'done'
                assert service.get_required('session', user_id='alice').graph_summary().num_nodes == 3
    asyncio.run(scenario())


def test_result_lookup_enforces_user_ownership_for_legacy_and_durable_jobs(tmp_path, monkeypatch):
    async def scenario():
        async with controlled_service(tmp_path, monkeypatch) as (service, host, ledger, client, headers, _):
            prepared = await prepare(client, headers)
            service._job_manager._jobs['legacy'] = Job(job_id='legacy', session_id='session', user_id='alice',
                tool_name='test', status='done', result={'private': 'alice'})
            for jid in ['legacy', prepared['job_id']]:
                for name in ['get_job_status', 'get_job_result']:
                    denied = await tool(client, {'X-User-ID': 'bob'}, name, {'job_id': jid})
                    assert denied['success'] is False
                    assert denied.get('result') is None and denied.get('job') is None
            allowed = await tool(client, {'X-User-ID': 'alice'}, 'get_job_result', {'job_id': 'legacy'})
            assert allowed['result']['result'] == {'private': 'alice'}
    asyncio.run(scenario())


def test_native_mcp_large_prepare_returns_terminal_result_to_model(tmp_path, monkeypatch):
    from tests.test_cancellation_failures import running_stack
    monkeypatch.setenv('LARGE_GRAPH_EMBEDDED_WORKER', 'false')
    async def scenario():
        async with running_stack(tmp_path) as (client, sid, service, controller, metrics):
            source = tmp_path / 'large.edges'
            source.write_text('A B 1\nB C 1\n', encoding='utf-8')
            service.get_required(sid, user_id='admin').register_upload('token', 'large.edges', str(source))
            service._large_graph.runtime.start()
            try:
                response = await client.post(f'/api/sessions/{sid}/chat/stream', json={
                    'message': 'large-graph-complete', 'start_request_id': str(uuid4()),
                })
                assert response.status_code == 200, response.text
                messages = metrics['contexts']['large-graph-complete']
                import json
                results = [json.loads(m['content']) for m in messages if m.get('role') == 'tool']
                assert results and results[-1]['num_nodes'] == 3
                assert results[-1]['async'] is False
                assert 'GRAPH_JOB_RESULT_UNAVAILABLE' not in response.text
                assert 'JOB_IDEMPOTENCY_CONFLICT' not in response.text
            finally:
                service._large_graph.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('stop', ['job', 'run'])
def test_native_stop_reaps_large_worker_through_backend(tmp_path, monkeypatch, stop):
    import psutil
    import graph_data_service.large_graph.runtime as runtime
    from tests.test_cancellation_failures import running_stack
    monkeypatch.setenv('LARGE_GRAPH_EMBEDDED_WORKER', 'false')
    monkeypatch.setattr(runtime, '_job_process_entry', _holding_worker)
    async def scenario():
        async with running_stack(tmp_path) as (client, sid, service, controller, metrics):
            source = tmp_path / 'large.edges'
            source.write_text('A B 1\nB C 1\n', encoding='utf-8')
            service.get_required(sid, user_id='admin').register_upload('token', 'large.edges', str(source))
            service._large_graph.runtime.start()
            key = str(uuid4())
            request = asyncio.create_task(client.post(f'/api/sessions/{sid}/chat/stream', json={
                'message': 'large-graph-stop', 'start_request_id': key,
            }))
            try:
                def running():
                    jobs = service._large_graph.catalog.execution_jobs(user_id='admin', session_id=sid)
                    for row in jobs:
                        assert row['status'] not in {'done', 'failed', 'cancelled'}, row
                    # A recorded PID only proves Process.start() returned. Wait
                    # for child-side evidence so an entrypoint crash cannot pass
                    # as a successful cancellation on platforms without /proc.
                    return next((row for row in jobs if row.get('worker_pid')
                                 and row['stage'] == 'test_worker_ready'), None)
                job = await eventually(running)
                pid, jid = job['worker_pid'], job['job_id']
                assert psutil.pid_exists(pid)
                if stop == 'job':
                    rejected = await client.post(f'/api/sessions/{sid}/jobs/{jid}/cancel',
                        headers={'X-User-ID': 'alice'}, json={'cancel_request_id': str(uuid4())})
                    assert rejected.status_code in {403, 404}
                    response = await client.post(f'/api/sessions/{sid}/jobs/{jid}/cancel', json={'cancel_request_id': str(uuid4())})
                else:
                    response = await client.post(f'/api/sessions/{sid}/runs/cancel', json={
                        'target': {'start_request_id': key}, 'cancel_request_id': str(uuid4()),
                    })
                assert response.status_code in {200, 202}, response.text
                await eventually(lambda: service._large_graph.job_status(jid, user_id='admin', session_id=sid)['status'] == 'cancelled')
                assert not psutil.pid_exists(pid)
                if stop == 'job':
                    control = (await client.get(f'/api/sessions/{sid}/jobs/{jid}/control')).json()
                    assert control['execution_stopped'] and control['terminal_persisted']
                else:
                    async with asyncio.timeout(15):
                        while True:
                            control = (await client.get(f'/api/sessions/{sid}/runs/control', params={'start_request_id': key})).json()
                            if control.get('can_send'):
                                break
                            await asyncio.sleep(.05)
                    assert control['execution_stopped'] and control['terminal_persisted']
                await asyncio.wait_for(request, 15)
            finally:
                service._large_graph.close()
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
    asyncio.run(scenario())

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import psutil
import pytest

from execution_lifecycle import ProcessLease


def start_owner(role, root):
    output = (root / 'owner.log').open('w')
    proc = subprocess.Popen([sys.executable, '-m', 'tests.fixtures.execution_owner', role, str(root)], stdout=output, stderr=output)
    output.close()
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if (root / 'ready.json').exists():
            return proc, json.loads((root / 'ready.json').read_text())
        if proc.poll() is not None:
            raise AssertionError((root / 'owner.log').read_text())
        time.sleep(.02)
    proc.kill(); proc.wait()
    raise AssertionError((root / 'owner.log').read_text())


def test_missing_lease_is_unknown_and_never_created_by_query(tmp_path):
    identity = str(uuid4())
    assert ProcessLease.inspect(tmp_path, identity) == 'unknown'
    assert not (tmp_path / f'{identity}.lock').exists()
    lease = ProcessLease(tmp_path, identity)
    assert ProcessLease.inspect(tmp_path, identity) == 'alive'
    lease.close()
    assert ProcessLease.inspect(tmp_path, identity) == 'exited'


def test_shutdown_retains_lifetime_until_pending_database_thread_exits(tmp_path):
    import threading
    from sqlalchemy import create_engine
    from agent_runtime.execution_control import ExecutionController
    from agent_runtime.execution_store import ExecutionStore
    from agent_runtime.execution_scope import RunCancellationManager, RunScope
    async def scenario():
        manager = RunCancellationManager()
        controller = ExecutionController(None, ExecutionStore(create_engine(f'sqlite:///{tmp_path}/control.db')), manager,
            pending_directory=tmp_path / 'pending')
        scope = RunScope('r', 'alice', 's', 1)
        manager.scopes['r'] = scope
        entered, release = threading.Event(), threading.Event()
        def database_write():
            entered.set(); release.wait(5)
        task = asyncio.create_task(controller.call(database_write))
        controller.tasks['r'] = task
        await asyncio.to_thread(entered.wait, 2)
        closing = asyncio.create_task(controller.close())
        try:
            await asyncio.sleep(.03)
            assert scope.stopped.is_set() and not closing.done()
            assert ProcessLease.inspect(controller.lease.root, controller.lease.identity) == 'alive'
        finally:
            release.set()
            await asyncio.wait_for(closing, 2)
        assert ProcessLease.inspect(controller.lease.root, controller.lease.identity) == 'exited'
        controller.store.engine.dispose()
    asyncio.run(scenario())


def test_runtime_sigkill_recovery_finalizes_native_checkpoint_without_factory(tmp_path):
    from agno.db.sqlite import SqliteDb
    from sqlalchemy import create_engine
    from agent_runtime.execution_control import ExecutionController
    from agent_runtime.execution_store import ExecutionStore
    from agent_runtime.execution_scope import RunCancellationManager
    proc, ready = start_owner('runtime', tmp_path)
    proc.kill(); proc.wait(timeout=5)
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'native.db'))
        store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/control.db'))
        controller = ExecutionController(db, store, RunCancellationManager(), pending_directory=tmp_path / 'pending')
        async def no_remote(*_): return {}
        controller._remote_cancel = no_remote
        try:
            row = await controller.reconcile(store.get('alice', 's', run_id=ready['run_id']))
            assert store.summary(row)['can_send'] and store.summary(row)['cancel_status'] == 'confirmed'
            run = db.get_session(session_id='s', user_id='alice').get_run(ready['run_id'])
            assert str(getattr(run.status, 'value', run.status)).lower() == 'cancelled'
            assert run.input and run.metadata['execution_interrupted']
        finally:
            await controller.close()
            db.close(); store.engine.dispose()
    asyncio.run(scenario())


def test_bff_sigkill_is_exit_evidence_and_cannot_revive_late_native_claim(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from agent_runtime.execution_store import ExecutionStore, ControlError
    proc, ready = start_owner('bff', tmp_path)
    proc.kill(); proc.wait(timeout=5)
    monkeypatch.setenv('IGRAPH_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'true')
    monkeypatch.setenv('IGRAPH_CONTROL_SERVICE_KEY', 'crash-test-only')
    from backend.app import create_app
    app = create_app()
    async def scenario():
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/internal/sessions/s/runs/r/preparation', params={'preparation_owner': ready['owner']},
                headers={'X-iGraph-Service-Key': 'crash-test-only', 'X-User-ID': 'alice'})
            assert response.status_code == 200 and response.json()['execution_stopped']
            store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/control.db')); store.migrate()
            row, _ = store.register('alice', 's', str(uuid4()), 'q', preparation_owner=ready['owner'])
            row = store.preparation_exited('alice', 's', row['run_id'], 1, failed=response.json()['failed'])
            assert store.summary(row)['can_send']
            with pytest.raises(ControlError, match='RUN_TERMINATED'):
                store.claim_agent('alice', 's', row['run_id'])
            store.engine.dispose()
    asyncio.run(scenario())


def test_graph_owner_sigkill_then_restart_reaps_worker_and_descendant(tmp_path, monkeypatch):
    from graph_data_service.execution_host import GraphExecutionHost
    from graph_data_service.job_manager import JobManager
    monkeypatch.setenv('IGRAPH_CONTROL_SERVICE_KEY', 'crash-test-only')
    proc, ready = start_owner('graph', tmp_path)
    proc.kill(); proc.wait(timeout=5)
    async def scenario():
        host = GraphExecutionHost(JobManager(), execution_root=tmp_path / 'graph')
        acks = []
        row = {'user_id': 'alice', 'session_id': 's', 'run_id': str(uuid4()), 'generation': 1,
            'operation_id': str(uuid4()), 'job_id': ready['job_id'], 'parent_id': None,
            'state': {'executor_instance': ready['owner'], 'tool_name': 'train'}}
        async def request(ctx, method, path, body=None):
            if method == 'GET': return {'cancel_requested': True}
            acks.append(body); return {}
        host.request = request
        try:
            started = time.monotonic()
            assert await asyncio.wait_for(host.recover(row), 8)
            assert time.monotonic() - started < 6
            assert [ack['terminal_persisted'] for ack in acks] == [False, True]
            assert host.load_job(ready['job_id']).status == 'cancelled'
            for pid in (ready['pid'], ready['child']):
                assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        finally:
            await host.close()
    try:
        asyncio.run(scenario())
    finally:
        # This group belongs exclusively to the fixture, never a user process.
        try: os.killpg(ready['pid'], signal.SIGKILL)
        except ProcessLookupError: pass


def test_crash_after_graph_publication_preserves_commit_before_job_ack(tmp_path):
    from graph_data_service.execution_host import GraphExecutionHost
    from graph_data_service.job_manager import JobManager
    proc, ready = start_owner('graph-published', tmp_path)
    proc.kill(); proc.wait(timeout=5)
    async def scenario():
        host = GraphExecutionHost(JobManager(), execution_root=tmp_path / 'graph')
        async def request(ctx, method, path, body=None):
            return {'cancel_requested': True} if method == 'GET' else {}
        host.request = request
        try:
            row = {**ready, 'user_id': 'alice', 'session_id': 's', 'generation': 1, 'parent_id': None,
                'state': {'executor_instance': ready['owner'], 'tool_name': 'load_graph'}}
            assert await host.recover(row)
            saved = host.load_job(ready['job_id'])
            assert saved.status == 'cancelled'
            assert saved.result == {'success': True, 'num_nodes': 2, 'graph_version': 1}
        finally:
            await host.close()
    asyncio.run(scenario())

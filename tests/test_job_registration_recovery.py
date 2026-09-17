"""A lost registration response must not leave an unowned queued job."""
import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine

from agent_runtime.execution_store import ExecutionStore
from graph_data_service.execution_host import GraphExecutionHost, JobContext
from graph_data_service.job_manager import JobManager


@pytest.mark.parametrize('cancel_during_registration', [False, True])
def test_lost_job_registration_response_and_disconnected_observer(tmp_path, cancel_during_registration):
    async def scenario():
        engine = create_engine(f'sqlite:///{tmp_path}/controls.db')
        store = ExecutionStore(engine)
        store.migrate()
        row, _ = store.register('alice', 'session', str(uuid4()), 'q')
        store.claim_agent('alice', 'session', row['run_id'])
        host = GraphExecutionHost(JobManager())
        ctx = JobContext('alice', 'session', row['run_id'], 1, str(uuid4()))
        registered, resumed = asyncio.Event(), asyncio.Event()
        calls, invocations = [], []
        async def request(context, method, path, body=None):
            if path == '/jobs/register':
                calls.append(body['operation_id'])
                job, claimed = store.register_job('alice', 'session', context.run_id, 1, body['operation_id'], body['fingerprint'], executor_instance=body['executor_instance'])
                if len(calls) == 1:
                    registered.set()
                    await resumed.wait()
                    raise httpx.ReadError('injected lost registration response')
                return {**job, 'claimed': claimed}
            if path.endswith('/control'):
                parent = store.get('alice', 'session', run_id=context.run_id)
                return {'cancel_requested': parent['state']['cancel_status'] != 'none'}
            if path.endswith('/ack'):
                store.acknowledge_job('alice', 'session', context.job_id, 1, status=body['status'], execution_stopped=True, terminal_persisted=True)
                return {}
            raise AssertionError(path)
        host.request = request
        async def work():
            invocations.append(1)
            return {'success': True}
        observer = asyncio.create_task(host.execute(ctx, 'controlled-test', {'a': 1}, work))
        await registered.wait()
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        if cancel_during_registration:
            store.request_cancel('alice', 'session', str(uuid4()), run_id=ctx.run_id)
        resumed.set()
        try:
            await asyncio.wait_for(asyncio.shield(next(iter(host.launches.values()))[1]), 5)
            jid = next(iter(host.manager._jobs))
            await host.manager.wait(jid)
            await asyncio.wait_for(asyncio.gather(*host.monitors.values()), 5)
            assert len(calls) == 2 and calls[0] == calls[1]
            assert invocations == ([] if cancel_during_registration else [1])
            assert host.manager.status(jid).status == ('cancelled' if cancel_during_registration else 'done')
            assert store.get('alice', 'session', job_id=jid)['state']['execution_stopped']
        finally:
            await host.close()
            engine.dispose()
    asyncio.run(scenario())

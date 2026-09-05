"""Disposable real executor processes for crash/restart regression tests."""
import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

from execution_lifecycle import atomic_json


async def main():
    role, root = sys.argv[1], Path(sys.argv[2])
    os.environ.update(IGRAPH_DATA_ROOT=str(root), IGRAPH_RUN_CANCEL_ENABLED='true',
                      IGRAPH_CONTROL_SERVICE_KEY='crash-test-only', AGNO_TELEMETRY='false')
    if role == 'bff':
        import backend.app  # Creates the actual BFF and its lifetime lease.
        paths = list((root / 'backend' / 'executors').glob('*.lock'))
        atomic_json(root / 'ready.json', {'owner': paths[-1].stem})
    elif role == 'runtime':
        from agno.db.sqlite import SqliteDb
        from agno.session.agent import AgentSession
        from agno.run import cancel
        from sqlalchemy import create_engine
        from agent_runtime.execution_store import ExecutionStore
        from agent_runtime.execution_control import ExecutionController
        from agent_runtime.execution_scope import RunCancellationManager
        from agent_runtime.cancellation_compat import ControlledAgent
        from tests.test_native_cancellation_contract import BlockingModel
        db = SqliteDb(db_file=str(root / 'native.db'))
        db.upsert_session(AgentSession(session_id='s', user_id='alice', agent_id='igraph-agent',
            session_data={'session_state': {}}, runs=[], created_at=1, updated_at=1))
        store = ExecutionStore(create_engine(f'sqlite:///{root}/control.db'))
        store.migrate()
        manager = RunCancellationManager()
        cancel.set_cancellation_manager(manager)
        controller = ExecutionController(db, store, manager, pending_directory=root / 'pending')
        controller.wake = lambda *_: None
        row, _ = store.register('alice', 's', str(uuid4()), 'q')
        store.acknowledge('alice', 's', row['run_id'], 1, preparation_stopped=True)
        model = BlockingModel(id='crash-test')
        model.entered, model.closed = asyncio.Event(), asyncio.Event()
        agent = ControlledAgent(id='igraph-agent', db=db, model=model, telemetry=False, store_events=True)
        agent.execution_controller = controller
        async def observe():
            async for _ in agent.arun('question', session_id='s', user_id='alice', run_id=row['run_id'], stream=True, background=True):
                pass
        asyncio.create_task(observe())
        await model.entered.wait()
        store.request_cancel('alice', 's', str(uuid4()), run_id=row['run_id'])
        atomic_json(root / 'ready.json', {'run_id': row['run_id'], 'owner': controller.lease.identity})
    else:
        from graph_data_service.execution_host import GraphExecutionHost, JobContext
        from graph_data_service.job_manager import JobManager
        from graph_data_service.worker_process import WorkerProcess
        host = GraphExecutionHost(JobManager(), execution_root=root / 'graph')
        jid = str(uuid4())
        if role == 'graph-published':
            import numpy as np
            from graph_data_service.graph_data import GraphData
            from graph_data_service.session import GraphSession
            from graph_data_service.graph_snapshot import snapshot_summary
            ctx = JobContext('alice', 's', str(uuid4()), 1, str(uuid4()), jid)
            session = GraphSession('s', user_id='alice')
            graph = GraphData(num_nodes=2, edge_index=np.array([[0], [1]]))
            graph.ensure_node_names()
            source = session._session_dir / 'candidate.npz'
            graph.save(str(source))
            host.prepare_commit(ctx, session._versions_path())
            session.publish_snapshot(source, snapshot_summary(graph), expected_version=0,
                receipt={'run_id': ctx.run_id, 'job_id': jid, 'generation': 1,
                         'result': {'success': True, 'num_nodes': 2}})
            atomic_json(root / 'ready.json', {'job_id': jid, 'run_id': ctx.run_id, 'operation_id': ctx.operation_id, 'owner': host.instance_id})
            await asyncio.Event().wait()
        worker = WorkerProcess()
        command = [sys.executable, '-u', '-c', '''
import json, os, signal, subprocess, sys, time
sys.stdin.readline()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)'])
print(json.dumps({'type':'progress','value':{'pid':os.getpid(),'child':child.pid}}), flush=True)
time.sleep(120)
''']
        async def ready(value):
            # Give the tracker time to retain the child identity before killing
            # the owner. Other tests cover the pre-registration/EOF boundary.
            await asyncio.sleep(.12)
            atomic_json(root / 'ready.json', {**value, 'job_id': jid, 'owner': host.instance_id})
        def progress(value):
            asyncio.create_task(ready(value))
        asyncio.create_task(worker.run({}, progress, command=command, lease_root=host.worker_root, job_id=jid))
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())

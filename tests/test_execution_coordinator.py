"""Executor notifications wake one coordinator without waiting for polling."""
import asyncio

import pytest
from sqlalchemy import create_engine

from agent_runtime.execution_control import ExecutionController
from agent_runtime.execution_scope import RunCancellationManager
from agent_runtime.execution_store import ExecutionStore


@pytest.mark.parametrize('phase', ['sleeping', 'reconciling'])
def test_executor_exit_wakes_existing_coordinator(tmp_path, phase):
    async def scenario():
        store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/controls.db'))
        store.migrate()
        controller = ExecutionController(None, store, RunCancellationManager())
        entered, release, sleeping = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0

        class ObservedEvent(asyncio.Event):
            async def wait(self):
                sleeping.set()
                return await super().wait()

        row = {'run_id': 'r', 'generation': 1, 'admission_blocked': True,
               'state': {'cancel_status': 'none', 'agent_stopped': False,
                         'run_status': 'running', 'terminal_persisted': False}}
        store.coordinator_record = lambda rid: row

        async def reconcile(record):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                if phase == 'reconciling':
                    await release.wait()
            else:
                record['state'].update(agent_stopped=True, run_status='completed', terminal_persisted=True)
            return record

        controller.reconcile = reconcile
        controller.wake_events['r'] = ObservedEvent()
        try:
            controller.wake('r')
            original = controller.tasks['r']
            await asyncio.wait_for(entered.wait(), 1)
            if phase == 'sleeping':
                await asyncio.wait_for(sleeping.wait(), 1)
            for _ in range(5):
                controller.wake('r')
            assert controller.tasks['r'] is original
            release.set()
            await asyncio.wait_for(asyncio.shield(original), .5)
            assert calls == 2
            assert row['state']['terminal_persisted']
            assert 'r' not in controller.tasks and 'r' not in controller.wake_events
        finally:
            release.set()
            await controller.close()
            store.engine.dispose()

    asyncio.run(scenario())

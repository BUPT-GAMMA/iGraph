import asyncio
import inspect
import json
import uuid
from importlib.metadata import version

import pytest
from agno.agent import AgentFactory
from agno.db.sqlite import SqliteDb
from agno.models.base import Model
from agno.models.response import ModelResponse
from agno.os import AgentOS
from agno.os.managers import event_buffer
from agno.run import cancel
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from fastapi import FastAPI
from sqlalchemy import create_engine

from agent_runtime.cancellation_compat import ControlledAgent, cancel_saved_pause
from agent_runtime.execution_control import ExecutionController
from agent_runtime.execution_scope import RunCancellationManager, current_scope
from agent_runtime.execution_store import ExecutionStore


class BlockingModel(Model):
    entered = None
    closed = None

    def invoke(self, *args, **kwargs):
        return ModelResponse(role='assistant', content='A')

    async def ainvoke(self, *args, **kwargs):
        return self.invoke()

    def invoke_stream(self, *args, **kwargs):
        yield self.invoke()

    async def ainvoke_stream(self, *args, **kwargs):
        yield self.invoke()
        async def blocked():
            try:
                self.entered.set()
                await asyncio.Event().wait()
            finally:
                self.closed.set()
        await current_scope.get().wait(blocked())

    def _parse_provider_response(self, response, **kwargs):
        return response

    def _parse_provider_response_delta(self, response):
        return response


@pytest.mark.parametrize('iteration', range(20))
def test_native_background_cancel_retains_partial_and_actually_exits(tmp_path, iteration):
    assert version('agno') == '2.9.0'
    async def scenario():
        db = SqliteDb(db_file=str(tmp_path / 'native.db'))
        db.upsert_session(AgentSession(session_id='s', agent_id='igraph-agent', user_id='alice', runs=[], created_at=1, updated_at=1))
        store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/controls.db'))
        store.migrate()
        manager = RunCancellationManager()
        old_manager = cancel.get_cancellation_manager()
        cancel.set_cancellation_manager(manager)
        controller = ExecutionController(db, store, manager)
        row, _ = store.register('alice', 's', str(uuid.uuid4()), 'q')
        rid = row['run_id']
        store.acknowledge('alice', 's', rid, 1, preparation_stopped=True)
        model = BlockingModel(id='deterministic')
        model.entered, model.closed = asyncio.Event(), asyncio.Event()
        agent = ControlledAgent(id='igraph-agent', db=db, model=model, telemetry=False, store_events=True)
        agent.execution_controller = controller
        frames = []
        async def observe():
            async for frame in agent.arun('question', session_id='s', user_id='alice', run_id=rid, stream=True, stream_events=True, background=True):
                frames.append(frame)
        task = asyncio.create_task(observe())
        try:
            await asyncio.wait_for(model.entered.wait(), 3)
            scope = manager.scopes[rid]
            store.request_cancel('alice', 's', str(uuid.uuid4()), run_id=rid)
            await manager.acancel_run(rid)
            await asyncio.wait_for(task, 3)
            await asyncio.wait_for(asyncio.gather(*scope.executors), 1)
            assert model.closed.is_set()
            assert scope.execution_stopped
            saved = db.get_session(session_id='s', user_id='alice').get_run(rid)
            assert saved.status == RunStatus.cancelled
            assert saved.content == 'A'
            events = [json.loads(line[5:])['event'] for frame in frames for line in frame.splitlines() if line.startswith('data:')]
            assert events.index('RunCancelled') < events.index('RunCompleted')
            row = await controller.reconcile(store.get('alice', 's', run_id=rid))
            assert store.summary(row)['can_send']
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await controller.close()
            cancel.set_cancellation_manager(old_manager)
            event_buffer.cleanup_run(rid)
            db.close()
            store.engine.dispose()
    asyncio.run(scenario())

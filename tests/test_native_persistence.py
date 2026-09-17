"""Native cancellation storage failures must retain content and block admission."""
import asyncio
import os
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from agno.db.sqlite import SqliteDb
from agno.run import cancel
from agno.run.base import RunStatus
from agno.os.managers import event_buffer
from agno.session.agent import AgentSession

from agent_runtime.cancellation_compat import ControlledAgent
from agent_runtime.execution_control import ExecutionController
from agent_runtime.execution_scope import RunCancellationManager
from agent_runtime.execution_store import ExecutionStore
from tests.test_native_cancellation_contract import BlockingModel


@pytest.fixture
def storage(tmp_path):
    url = os.getenv('IGRAPH_CONTROL_TEST_DATABASE_URL')
    schema = None
    if url:
        from agno.db.postgres import PostgresDb
        from sqlalchemy.schema import CreateSchema, DropSchema
        schema = 'native_write_' + uuid4().hex
        db = PostgresDb(db_url=url, db_schema=schema, session_table='sessions')
        with db.db_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        engine = db.db_engine.execution_options(schema_translate_map={None: schema})
    else:
        db = SqliteDb(db_file=str(tmp_path / 'native.db'))
        engine = create_engine(f'sqlite:///{tmp_path}/control.db')
    store = ExecutionStore(engine)
    store.migrate()
    db.upsert_session(AgentSession(session_id='s', agent_id='igraph-agent', user_id='alice', runs=[], created_at=1, updated_at=1))
    yield db, store
    if schema:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
    else:
        db.close()
    engine.dispose()


@pytest.mark.parametrize('failure', ['exception', 'silent'])
@pytest.mark.parametrize('restart', [False, True])
def test_cancelled_partial_is_retried_and_verified_before_admission(storage, tmp_path, failure, restart):
    async def scenario():
        db, store = storage
        manager = RunCancellationManager()
        old = cancel.get_cancellation_manager()
        cancel.set_cancellation_manager(manager)
        controller = ExecutionController(db, store, manager, pending_directory=tmp_path / 'pending')
        controller.wake = lambda *_: None
        async def no_remote(*_): return {}
        controller._remote_cancel = no_remote
        row, _ = store.register('alice', 's', str(uuid4()), 'q')
        rid = row['run_id']
        store.acknowledge('alice', 's', rid, 1, preparation_stopped=True)
        model = BlockingModel(id='deterministic')
        model.entered, model.closed = asyncio.Event(), asyncio.Event()
        agent = ControlledAgent(id='igraph-agent', db=db, model=model, telemetry=False, store_events=True)
        agent.execution_controller = controller
        async def observe():
            async for _ in agent.arun('question', session_id='s', user_id='alice', run_id=rid, stream=True, stream_events=True, background=True):
                pass
        task = asyncio.create_task(observe())
        try:
            await asyncio.wait_for(model.entered.wait(), 5)
            scope = manager.scopes[rid]
            original = db.upsert_session
            def fail_write(*args, **kwargs):
                if failure == 'exception':
                    raise OSError('injected native terminal save failure')
                return None  # Native stores can log and swallow their own error.
            with patch.object(db, 'upsert_session', fail_write):
                store.request_cancel('alice', 's', str(uuid4()), run_id=rid)
                await manager.acancel_run(rid)
                await asyncio.wait_for(task, 5)
                await asyncio.gather(*scope.executors)
                row = await controller.reconcile(store.get('alice', 's', run_id=rid))
                summary = store.summary(row)
                assert model.closed.is_set() and summary['execution_stopped']
                assert summary['native_run_created'] is True
                assert not summary['terminal_persisted'] and not summary['can_send']
                assert controller.persistence.path(rid, 1).is_file()
            if restart:
                # The durable exit receipt and native write survive loss of all
                # controller/scope memory; no model/Agent is reconstructed.
                await controller.close()
                store = ExecutionStore(store.engine)
                controller = ExecutionController(db, store, RunCancellationManager(), pending_directory=tmp_path / 'pending')
                controller._remote_cancel = no_remote
            with patch.object(db, 'upsert_session', wraps=original) as writes:
                row = await controller.reconcile(store.get('alice', 's', run_id=rid))
                assert writes.call_count == 1
                assert db.get_session(session_id='s', user_id='alice').get_run(rid).messages
                await controller.reconcile(row)
                assert writes.call_count == 1
            summary = store.summary(row)
            assert summary['can_send'] and summary['terminal_persisted']
            assert summary['cancel_status'] == 'confirmed' and summary['native_run_created']
            saved = db.get_session(session_id='s', user_id='alice').get_run(rid)
            assert saved.status == RunStatus.cancelled and saved.content == 'A'
            assert any(message.content == 'A' for message in saved.messages)
            assert not controller.persistence.path(rid, 1).exists()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await controller.close()
            event_buffer.cleanup_run(rid)
            cancel.set_cancellation_manager(old)
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['exception', 'silent'])
def test_cancel_paused_phase_requires_new_terminal_save(storage, tmp_path, failure):
    from agno.run.agent import RunOutput
    async def scenario():
        db, store = storage
        controller = ExecutionController(db, store, RunCancellationManager(), pending_directory=tmp_path / 'pending')
        async def no_remote(*_): return {}
        controller._remote_cancel = no_remote
        row, _ = store.register('alice', 's', str(uuid4()), 'q')
        rid = row['run_id']
        store.claim_agent('alice', 's', rid)
        session = db.get_session(session_id='s', user_id='alice')
        session.upsert_run(RunOutput(run_id=rid, agent_id='igraph-agent', session_id='s', user_id='alice', status=RunStatus.paused, content='partial'))
        db.upsert_session(session)
        store.acknowledge('alice', 's', rid, 1, run_status='paused', agent_stopped=True, preparation_stopped=True, terminal_persisted=True)
        row = store.request_cancel('alice', 's', str(uuid4()), run_id=rid)
        assert not store.summary(row)['terminal_persisted']
        def fail(*_args, **_kwargs):
            if failure == 'exception': raise OSError('injected paused terminal failure')
        try:
            with patch.object(db, 'upsert_session', fail):
                with pytest.raises((OSError, RuntimeError)):
                    await controller.reconcile(row)
            assert not store.summary(store.get('alice', 's', run_id=rid))['can_send']
            row = await controller.reconcile(store.get('alice', 's', run_id=rid))
            assert store.summary(row)['terminal_persisted'] and store.summary(row)['can_send']
            assert db.get_session(session_id='s', user_id='alice').get_run(rid).content == 'partial'
        finally:
            await controller.close()
    asyncio.run(scenario())

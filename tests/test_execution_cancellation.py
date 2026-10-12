"""Cancellation tests assert execution evidence, not just a status label."""
import asyncio
import threading

import pytest
from agno.exceptions import RunCancelledException

from agent_runtime.execution_scope import RunCancellationManager, RunScope
from agent_runtime.cancellation_compat import CancellableModelMixin
from graph_data_service.job_manager import JobManager


def test_terminal_cache_is_bounded_and_preserves_active_work_and_durable_results():
    from graph_data_service.job_manager import Job
    async def scenario():
        manager, saved = JobManager(), {}
        manager.load_saved = lambda jid: Job(**saved[jid]) if jid in saved else None
        release = asyncio.Event()
        active = await manager.submit('s', 'active', release.wait)
        ids = []
        try:
            for index in range(8):
                jid = await manager.submit('s', 'small', lambda value=index: {'success': True, 'value': value})
                ids.append(jid)
                await manager.wait(jid)
                saved[jid] = manager.status(jid).to_dict()
                manager.saved(jid, limit=2)
            assert len(manager._saved_jobs) == 2 and len(manager._tasks) == 3
            assert active in manager._jobs and not manager._tasks[active].done()
            assert manager.status(ids[0]).result == {'success': True, 'value': 0}
            assert manager.status(ids[0]).status == 'done'
        finally:
            release.set(); await manager.wait(active)
    asyncio.run(scenario())


def test_stopped_learning_cannot_publish_late_memory_and_keeps_prior_facts(tmp_path):
    from types import SimpleNamespace
    from agno.db.sqlite import SqliteDb
    from agno.learn.schemas import UserProfile
    from agent_runtime.learning import CancellableLearningMachine
    from agent_runtime.cancellation_compat import ControlledAgent
    scope = RunScope('r', 'alice', 's', 1)
    db = SqliteDb(db_file=str(tmp_path / 'learning.db'))
    learning = CancellableLearningMachine(db=db, user_profile=True)
    store = learning.user_profile_store  # Exercise initialization before arun.
    agent = ControlledAgent(db=db, learning=learning)
    agent.execution_controller = SimpleNamespace()
    agent._track_native_writes(scope)
    try:
        store.save('alice', UserProfile(user_id='alice', name='Alice'))
        assert store.get('alice').name == 'Alice'
        scope.cancel()
        store.save('alice', UserProfile(user_id='alice', name='unfinished inference'))
        assert store.get('alice').name == 'Alice'
        store.save('bob', UserProfile(user_id='bob', name='late callback'))
        assert store.get('bob') is None
    finally:
        db.close()


def test_scope_interrupts_first_token_and_closes_only_its_request():
    async def scenario():
        scope = RunScope('r', 'alice', 's', 1)
        entered, closed = asyncio.Event(), asyncio.Event()
        other = asyncio.create_task(asyncio.Event().wait())
        async def request():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
        task = asyncio.create_task(scope.wait(request()))
        await entered.wait()
        scope.cancel()
        with pytest.raises(RunCancelledException):
            await asyncio.wait_for(task, 1)
        assert closed.is_set() and scope.execution_stopped
        assert not other.done()
        other.cancel()
        await asyncio.gather(other, return_exceptions=True)
    asyncio.run(scenario())


def test_scope_broadcast_does_not_serialize_leaf_cleanup():
    async def scenario():
        scope = RunScope('r', 'alice', 's', 1)
        exits = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()
        async def request(index):
            try:
                await asyncio.Event().wait()
            finally:
                exits[index].set()
                if index == 0:
                    await release.wait()
        tasks = [asyncio.create_task(scope.wait(request(i))) for i in range(2)]
        await asyncio.sleep(0)
        scope.cancel()
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in exits)), 1)
        assert not scope.execution_stopped
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert scope.execution_stopped
    asyncio.run(scenario())


def test_stop_racing_a_transport_error_does_not_enter_failure_retry():
    async def scenario():
        scope = RunScope('r', 'alice', 's', 1)
        async def request():
            scope.cancel()
            raise OSError('transport closed at cancellation boundary')
        with pytest.raises(RunCancelledException):
            await scope.wait(request())
        assert scope.execution_stopped
    asyncio.run(scenario())


def test_scope_quarantines_provider_cleanup_that_suppresses_cancellation():
    async def scenario():
        scope = RunScope('r', 'alice', 's', 1, cancel_grace_seconds=.01)
        entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def request():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                try:
                    await release.wait()
                finally:
                    exited.set()

        waiter = asyncio.create_task(scope.wait(request()))
        await entered.wait()
        scope.cancel()
        with pytest.raises(RunCancelledException):
            await asyncio.wait_for(waiter, .5)
        assert scope.execution_stopped
        assert not exited.is_set()
        release.set()
        await asyncio.wait_for(exited.wait(), .5)

    asyncio.run(scenario())


def test_background_registration_recovers_scope_without_contextvar():
    async def scenario():
        manager = RunCancellationManager()
        scope = RunScope('r', 'alice', 's', 1)
        manager.scopes['r'] = scope
        await manager.aregister_run('r')
        assert asyncio.current_task() in scope.executors
        assert scope.native_run_created

    asyncio.run(scenario())


def test_request_owned_model_uses_bound_scope_in_background_context():
    class BlockingBase:
        async def ainvoke(self):
            await asyncio.Event().wait()

    class BoundModel(CancellableModelMixin, BlockingBase):
        pass

    async def scenario():
        scope = RunScope('r', 'alice', 's', 1)
        model = BoundModel()
        model._igraph_bound_scope = scope
        task = asyncio.create_task(model.ainvoke())
        await asyncio.sleep(0)
        scope.cancel()
        with pytest.raises(RunCancelledException):
            await asyncio.wait_for(task, 1)
        assert scope.execution_stopped

    asyncio.run(scenario())


def test_scope_escalates_background_executor_after_grace_period():
    async def scenario():
        scope = RunScope('r', 'alice', 's', 1, cancel_grace_seconds=.01)
        entered = asyncio.Event()

        async def producer_without_scope_checks():
            entered.set()
            await asyncio.Event().wait()

        producer = asyncio.create_task(producer_without_scope_checks())
        scope.executors.add(producer)
        await entered.wait()
        scope.cancel()
        assert not scope.escalate_cancel()
        await asyncio.sleep(.02)
        assert scope.escalate_cancel()
        await asyncio.gather(producer, return_exceptions=True)
        assert scope.execution_stopped
        assert not scope.escalate_cancel()

    asyncio.run(scenario())


def test_thread_job_does_not_confirm_or_publish_success_before_actual_exit():
    async def scenario():
        entered, release, exited = threading.Event(), threading.Event(), threading.Event()
        manager = JobManager()
        def work(progress):
            entered.set()
            try:
                release.wait(3)
                progress({'progress': 1, 'message': 'late success'})
                return {'success': True}
            finally:
                exited.set()
        jid = await manager.submit('s', 'train', work, _progress_kwarg='progress')
        await asyncio.to_thread(entered.wait, 1)
        try:
            assert manager.cancel(jid)
            await asyncio.sleep(.01)
            assert manager.status(jid).status == 'cancelling'
            assert not exited.is_set()
            release.set()
            await asyncio.wait_for(manager.wait(jid), 1)
            job = manager.status(jid)
            assert exited.is_set() and job.status == 'cancelled'
            assert job.result is None
            assert not manager.drain_pending_progress(jid)
        finally:
            release.set()
    asyncio.run(scenario())

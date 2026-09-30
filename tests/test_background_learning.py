"""Functional coverage of asynchronous learning, persisted jobs and native turns."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from sqlalchemy import create_engine, select

from agent_runtime.background_learning import BackgroundLearning, jobs
from agent_runtime.execution_scope import RunScope, current_scope
from agent_runtime.execution_store import ExecutionStore
from tests.test_completed_turn_learning import runtime, DELETE, REFUSAL, FOLLOWUP


def setup_queue(tmp_path, factory, **kwargs):
    db = SqliteDb(db_file=str(tmp_path / 'native.db'))
    store = ExecutionStore(create_engine(f"sqlite:///{tmp_path}/control.db"))
    store.migrate()
    queue = BackgroundLearning(db, store, factory, poll_seconds=.01, retry_seconds=0, **kwargs)
    return queue


def save(queue, rid, *, uid='alice', sid='s', previous=None, status=RunStatus.completed):
    run = RunOutput(run_id=rid, agent_id='igraph-agent', user_id=uid, session_id=sid,
        status=status, messages=(previous or []) + [Message(role='user', content=rid), Message(role='assistant', content='answer ' + rid)])
    session = queue.db.get_session(session_id=sid, user_id=uid) or AgentSession(session_id=sid, user_id=uid, agent_id='igraph-agent', runs=[], created_at=1, updated_at=1)
    session.upsert_run(run)
    queue.db.upsert_session(session)
    return run


async def enqueue(queue, rid, *, uid='alice', sid='s'):
    await queue.enqueue(user_id=uid, session_id=sid, run_id=rid, planning_mode='auto')


def states(queue):
    with queue.store.engine.connect() as conn:
        return {r.run_id: (r.status, r.attempts) for r in conn.execute(select(jobs))}


def close(queue):
    queue.db.close()
    queue.store.engine.dispose()


def test_answer_and_next_turn_finish_while_learning_is_blocked(tmp_path, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def capture(**kwargs):
            entered.set()
            await release.wait()
        queue = setup_queue(tmp_path, lambda *_: SimpleNamespace(acapture_background_turn=capture))
        try:
            agent, _, _, _ = runtime(tmp_path, monkeypatch, database=queue.db, learning_queue=queue)
            events = await asyncio.wait_for(collect(agent.arun(DELETE, user_id='alice', session_id='s', stream=True, stream_events=True)), 2)
            assert any(e.event == 'RunCompleted' for e in events)
            first = queue.db.get_session(session_id='s', user_id='alice').runs[-1]
            assert first.content == REFUSAL
            worker = asyncio.create_task(queue.process_user('alice'))
            await asyncio.wait_for(entered.wait(), 2)
            next_agent, _, _, _ = runtime(tmp_path, monkeypatch, database=queue.db, learning_queue=queue)
            second = await asyncio.wait_for(next_agent.arun(FOLLOWUP, user_id='alice', session_id='s'), 2)
            assert not worker.done()
            assert any(m.role == 'assistant' and m.content == REFUSAL for m in second.messages)
            assert second.status == RunStatus.completed
            release.set()
            await worker
            assert states(queue)[first.run_id][0] == 'done'
        finally:
            release.set()
            await queue.close()
            close(queue)
    asyncio.run(scenario())


async def collect(iterator):
    return [item async for item in iterator]


def test_native_background_extraction_preserves_refusal(tmp_path, monkeypatch):
    async def scenario():
        from agent_runtime.learning import create_learning_machine
        agent = None
        def factory(job, run, database):
            machine = create_learning_machine(SimpleNamespace(embedding_model='', embedding_api_key=''), planning_mode='auto')
            machine.db = database
            machine.model = deepcopy(agent.model)
            return machine
        queue = setup_queue(tmp_path, factory)
        try:
            agent, _, extracted, _ = runtime(tmp_path, monkeypatch, database=queue.db, learning_queue=queue)
            result = await agent.arun(DELETE, user_id='alice', session_id='s')
            assert extracted == []
            before = queue.db.get_session(session_id='s', user_id='alice').runs[-1].to_dict()
            assert await queue.process_user('alice')
            assert f'Assistant: {REFUSAL}' in extracted[0]
            assert agent.learning.session_context_store.get('s').summary == 'Deletion was refused; no deletion remains pending.'
            saved = queue.db.get_session(session_id='s', user_id='alice').runs[-1]
            assert saved.to_dict() == before  # no background transcript/metrics rewrite
            assert states(queue)[result.run_id] == ('done', 1)
        finally:
            close(queue)
    asyncio.run(scenario())


def test_restart_coalesces_only_fully_covered_turns_and_preserves_users(tmp_path):
    async def scenario():
        seen = []
        async def capture(**kwargs):
            seen.append((kwargs['user_id'], kwargs['session_id'], [m.content for m in kwargs['messages']]))
        factory = lambda *_: SimpleNamespace(acapture_background_turn=capture)
        queue = setup_queue(tmp_path, factory)
        try:
            first = save(queue, 'first')
            await enqueue(queue, 'first')
            save(queue, 'second', previous=first.messages)
            await enqueue(queue, 'second')
            save(queue, 'bob', uid='bob', sid='b')
            await enqueue(queue, 'bob', uid='bob', sid='b')
            restarted = BackgroundLearning(queue.db, queue.store, factory)
            await restarted.process_user('alice')
            assert seen == [('alice', 's', ['first', 'answer first', 'second', 'answer second'])]
            assert states(queue)['first'][0] == states(queue)['second'][0] == 'done'
            assert states(queue)['bob'][0] == 'pending'
            await restarted.process_user('bob')
            assert seen[-1][0] == 'bob'
            await enqueue(queue, 'second')
            assert not await restarted.process_user('alice')  # deduplicate completed job
        finally:
            close(queue)
    asyncio.run(scenario())


def test_retry_is_bounded_and_newer_jobs_cannot_overtake(tmp_path):
    async def scenario():
        seen = []
        async def capture(**kwargs):
            seen.append(kwargs['session_id'])
            if kwargs['session_id'] == 's':
                raise ConnectionError('fixture')
        queue = setup_queue(tmp_path, lambda *_: SimpleNamespace(acapture_background_turn=capture))
        try:
            save(queue, 'fail'); await enqueue(queue, 'fail')
            save(queue, 'next', sid='other'); await enqueue(queue, 'next', sid='other')
            for _ in range(3):
                assert await queue.process_user('alice')
            assert states(queue)['fail'] == ('failed', 3)
            assert seen == ['s', 's', 's']
            await queue.process_user('alice')
            assert states(queue)['next'] == ('done', 1)
        finally:
            close(queue)
    asyncio.run(scenario())


@pytest.mark.parametrize('status', [RunStatus.cancelled, RunStatus.error, RunStatus.paused])
def test_unsuccessful_runs_are_never_learned(tmp_path, status):
    async def scenario():
        queue = setup_queue(tmp_path, lambda *_: pytest.fail('must not extract'))
        try:
            save(queue, 'r', status=status); await enqueue(queue, 'r')
            await queue.process_user('alice')
            assert states(queue)['r'][0] == 'discarded'
        finally:
            close(queue)
    asyncio.run(scenario())


def test_pending_save_and_control_ack_are_required(tmp_path):
    async def scenario():
        queue = setup_queue(tmp_path, lambda *_: pytest.fail('must not extract'))
        try:
            await enqueue(queue, 'r')
            assert not await queue.process_user('alice')
            assert states(queue)['r'] == ('pending', 0)
            save(queue, 'r')
            queue.controller = object()
            queue.store.get = lambda *_args, **_kwargs: {'state': {'cancel_status': 'none'}}
            queue.store.summary = lambda _: dict(execution_stopped=False, terminal_persisted=True)
            assert not await queue.process_user('alice')
        finally:
            close(queue)
    asyncio.run(scenario())


def test_shutdown_retains_job_and_worker_does_not_inherit_run_scope(tmp_path):
    async def scenario():
        entered = asyncio.Event()
        async def capture(**kwargs):
            assert current_scope.get() is None
            entered.set()
            await asyncio.Event().wait()
        queue = setup_queue(tmp_path, lambda *_: SimpleNamespace(acapture_background_turn=capture))
        try:
            save(queue, 'r'); await enqueue(queue, 'r')
            token = current_scope.set(RunScope('foreground', 'alice', 's', 1))
            try:
                queue.start()
            finally:
                current_scope.reset(token)
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(queue.close(), 2)
            assert states(queue)['r'][0] == 'pending'
        finally:
            await queue.close()
            close(queue)
    asyncio.run(scenario())


def test_late_write_after_deleted_run_is_rejected_even_if_store_swallows_error(tmp_path):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        def factory(job, run, db):
            async def capture(**kwargs):
                entered.set()
                await release.wait()
                try:
                    db.upsert_learning(id='late', learning_type='session_context', session_id='s', user_id='alice', content={'summary': 'obsolete'})
                except Exception:
                    pass  # native store behavior
            return SimpleNamespace(acapture_background_turn=capture)
        queue = setup_queue(tmp_path, factory)
        try:
            save(queue, 'r'); await enqueue(queue, 'r')
            worker = asyncio.create_task(queue.process_user('alice'))
            await entered.wait()
            session = queue.db.get_session(session_id='s', user_id='alice')
            session.runs = []
            queue.db.upsert_session(session)
            release.set()
            await worker
            assert states(queue)['r'][0] == 'discarded'
            assert queue.db.get_learning('late') is None
        finally:
            close(queue)
    asyncio.run(scenario())


def test_truncated_history_is_processed_in_order_without_dropping_turns(tmp_path):
    async def scenario():
        seen = []
        async def capture(**kwargs):
            seen.append(kwargs['messages'][0].content)
        queue = setup_queue(tmp_path, lambda *_: SimpleNamespace(acapture_background_turn=capture))
        try:
            save(queue, 'old'); await enqueue(queue, 'old')
            save(queue, 'new'); await enqueue(queue, 'new')
            await queue.process_user('alice')
            assert seen == ['old']
            assert states(queue)['new'][0] == 'pending'
            await queue.process_user('alice')
            assert seen == ['old', 'new']
        finally:
            close(queue)
    asyncio.run(scenario())


def test_slow_learning_times_out_and_releases_user_for_retry(tmp_path):
    async def scenario():
        stopped = asyncio.Event()
        async def capture(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        queue = setup_queue(tmp_path, lambda *_: SimpleNamespace(acapture_background_turn=capture), timeout_seconds=.03)
        try:
            save(queue, 'r'); await enqueue(queue, 'r')
            assert await asyncio.wait_for(queue.process_user('alice'), 1)
            assert stopped.is_set()
            assert states(queue)['r'] == ('pending', 1)
            assert 'alice' not in queue.local_users
        finally:
            close(queue)
    asyncio.run(scenario())

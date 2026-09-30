"""Durable, user-serialized learning after the native answer has been saved.

The outbox stores identities/options only, never a second transcript or secrets.
Workers read completed native runs; they cannot modify chat/session records.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
from contextvars import Context

from sqlalchemy import Column, Float, Index, Integer, String, Table, select, text

from agent_runtime.execution_store import metadata
from agent_runtime.native_persistence import status_of
from execution_lifecycle import owned_sync

logger = logging.getLogger(__name__)
jobs = Table('igraph_background_learning', metadata,
    Column('user_id', String(256), primary_key=True),
    Column('session_id', String(256), primary_key=True),
    Column('run_id', String(64), primary_key=True),
    Column('planning_mode', String(16), nullable=False),
    Column('status', String(16), nullable=False),
    Column('attempts', Integer, nullable=False),
    Column('created_at', Float, nullable=False),
    Column('available_at', Float, nullable=False))
Index('ix_igraph_learning_pending', jobs.c.status, jobs.c.user_id, jobs.c.created_at)


def message_keys(run):
    # Ignore changing system/learning instructions, but retain repeated messages.
    return [json.dumps({'role': m.role, 'content': m.content, 'tool_calls': m.tool_calls,
                       'tool_call_id': m.tool_call_id}, sort_keys=True, default=str)
            for m in run.messages or [] if m.role not in {'system', 'developer'}]


def covers(new, old):
    """Coalesce only when the newer run contains every older message in order."""
    remaining = iter(message_keys(new))
    return all(any(candidate == wanted for candidate in remaining) for wanted in message_keys(old))


class ObsoleteLearning(Exception):
    pass


class BackgroundLearning:
    def __init__(self, db, store, machine_factory, *, controller=None, poll_seconds=1.0,
                 timeout_seconds=120, retry_seconds=5):
        self.db, self.store, self.machine_factory = db, store, machine_factory
        self.controller = controller
        self.poll_seconds, self.timeout_seconds, self.retry_seconds = poll_seconds, timeout_seconds, retry_seconds
        self.tasks = []
        self.local_users = set()
        self.wake = asyncio.Event()

    async def enqueue(self, *, user_id, session_id, run_id, planning_mode):
        def insert():
            with self.store.transaction(user_id, '__learning_outbox__') as conn:
                where = self.key(dict(user_id=user_id, session_id=session_id, run_id=run_id))
                if conn.execute(select(jobs.c.run_id).where(*where)).first() is None:
                    conn.execute(jobs.insert().values(user_id=user_id, session_id=session_id, run_id=run_id,
                        planning_mode=planning_mode, status='pending', attempts=0,
                        created_at=time.time(), available_at=time.time()))
        await owned_sync(insert)
        self.wake.set()

    @staticmethod
    def key(job):
        return [jobs.c[name] == job[name] for name in ('user_id', 'session_id', 'run_id')]

    def pending(self, uid=None):
        with self.store.engine.connect() as conn:
            query = select(jobs).where(jobs.c.status == 'pending')
            if uid is not None:
                query = query.where(jobs.c.user_id == uid)
            return [dict(row) for row in conn.execute(query.order_by(jobs.c.created_at, jobs.c.run_id)).mappings()]

    def update(self, job, **values):
        with self.store.engine.begin() as conn:
            conn.execute(jobs.update().where(*self.key(job)).values(**values))

    def load(self, job):
        session = self.db.get_session(session_id=job['session_id'], user_id=job['user_id'])
        if session is None or session.user_id != job['user_id']:
            return None, None
        return session, session.get_run(job['run_id'])

    def readiness(self, job, session, run):
        if self.controller and self.store.session_deletion(job['user_id'], job['session_id']):
            return 'discard'
        if session is None or run is None:
            # The post-hook can enqueue just before the very first native save.
            return 'wait' if time.time() - job['created_at'] < 300 else 'discard'
        status = status_of(run)
        if status in {'cancelled', 'failed', 'error', 'paused', 'regenerated'}:
            return 'discard'
        if self.controller:
            row = self.store.get(job['user_id'], job['session_id'], run_id=job['run_id'])
            if row is not None:
                if row['state']['cancel_status'] != 'none':
                    return 'discard'
                summary = self.store.summary(row)
                if not summary['execution_stopped'] or not summary['terminal_persisted']:
                    return 'wait'
        if status != 'completed':
            return 'wait' if time.time() - job['created_at'] < 300 else 'discard'
        messages = run.messages or []
        return 'ready' if messages and messages[-1].role == 'assistant' and not messages[-1].tool_calls else 'discard'

    def guarded_db(self, job, run):
        database = copy.copy(self.db)
        expected = message_keys(run)
        failures = []
        for name in ('upsert_learning', 'delete_learning', 'upsert_user_memory', 'delete_user_memory'):
            method = getattr(self.db, name, None)
            if method is None:
                continue
            def guarded(*args, _method=method, **kwargs):
                try:
                    session, current = self.load(job)
                    if (current is None or self.readiness(job, session, current) != 'ready'
                            or message_keys(current) != expected):
                        raise ObsoleteLearning()
                    return _method(*args, **kwargs)
                except Exception as exc:
                    # Native stores may swallow save errors; remember them for retry.
                    failures.append(exc)
                    raise
            setattr(database, name, guarded)
        return database, failures

    async def process_user(self, uid):
        # Local exclusion saves pool connections. PostgreSQL also excludes other
        # replicas for this user without blocking foreground run admission.
        if uid in self.local_users:
            return False
        self.local_users.add(uid)
        lease = {}
        lock = int.from_bytes(hashlib.sha256(('igraph/background-learning/' + uid).encode()).digest()[:8], 'big', signed=True)
        try:
            if self.store.engine.dialect.name == 'postgresql':
                def acquire():
                    conn = lease['conn'] = self.store.engine.connect()
                    lease['acquired'] = conn.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': lock}).scalar()
                    conn.commit()
                await owned_sync(acquire)
                if not lease['acquired']:
                    return False
            queue = await owned_sync(self.pending, uid)
            if not queue or queue[0]['available_at'] > time.time():
                return False
            job = queue[0]
            session, run = await owned_sync(self.load, job)
            ready = await owned_sync(self.readiness, job, session, run)
            if ready == 'wait':
                return False
            if ready == 'discard':
                await owned_sync(self.update, job, status='discarded')
                return True
            merged = [job]
            # Never merge across sessions or across a retry barrier. Keeping the
            # order also serializes the user's profile shared by their sessions.
            for candidate in queue[1:]:
                if candidate['session_id'] != job['session_id'] or candidate['available_at'] > time.time():
                    break
                newer_session, newer = await owned_sync(self.load, candidate)
                if await owned_sync(self.readiness, candidate, newer_session, newer) != 'ready' or not covers(newer, run):
                    break
                merged.append(candidate)
                job, session, run = candidate, newer_session, newer
            attempts = max(item['attempts'] for item in merged) + 1
            for item in merged:
                await owned_sync(self.update, item, attempts=attempts)
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    database, failures = self.guarded_db(job, run)
                    machine = await owned_sync(self.machine_factory, job, run, database)
                    # A fresh machine/model has no foreground RunScope or shared
                    # metrics. It recalls current memory only when its turn starts.
                    await machine.acapture_background_turn(messages=copy.deepcopy(run.messages),
                        user_id=uid, session_id=job['session_id'], agent_id=run.agent_id,
                        team_id=getattr(run, "team_id", None), metadata=copy.deepcopy(run.metadata),
                        session_state=copy.deepcopy((session.session_data or {}).get('session_state', {})))
                    if failures:
                        raise failures[0]
            except asyncio.CancelledError:
                # Graceful shutdown is not an extraction failure. Leave the
                # durable work for the next process without spending a retry.
                for item in merged:
                    await owned_sync(self.update, item, attempts=attempts - 1)
                raise
            except ObsoleteLearning:
                for item in merged:
                    await owned_sync(self.update, item, status='discarded')
            except Exception as exc:
                for item in merged:
                    await owned_sync(self.update, item, status='failed' if attempts >= 3 else 'pending',
                                     available_at=time.time() + self.retry_seconds * 2 ** (attempts - 1))
                # No exception text: provider responses can contain credentials.
                logger.warning('background_learning_retry run_id=%s attempt=%s error=%s', job['run_id'], attempts, type(exc).__name__)
            else:
                for item in merged:
                    await owned_sync(self.update, item, status='done')
                logger.info('background_learning_completed run_id=%s merged=%s', job['run_id'], len(merged))
            return True
        finally:
            conn = lease.get('conn')
            try:
                if conn is not None:
                    def unlock():
                        try:
                            if lease.get('acquired'):
                                conn.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': lock})
                                conn.commit()
                        except BaseException:
                            # Never return a session-level lock to the pool.
                            conn.invalidate()
                            raise
                        finally:
                            conn.close()
                    await owned_sync(unlock)
            finally:
                self.local_users.discard(uid)

    async def _worker(self):
        while True:
            self.wake.clear()
            try:
                users = dict.fromkeys(job['user_id'] for job in await owned_sync(self.pending))
                progressed = False
                for uid in users:
                    progressed = await self.process_user(uid) or progressed
                if progressed:
                    continue
            except Exception as exc:
                logger.warning('background_learning_worker_error error=%s', type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), self.poll_seconds)
            except TimeoutError:
                pass

    def start(self):
        if not self.tasks:
            self.tasks = [asyncio.create_task(self._worker(), context=Context()) for _ in range(2)]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []


def create_background_learning(db, store, catalog, config, controller):
    from agent_runtime.learning import create_learning_machine
    from agent_runtime.model_factory import create_model

    def factory(job, run, database):
        snapshot = (run.metadata or {}).get('model_snapshot')
        if snapshot:
            frozen = catalog.resolve(job['user_id'], snapshot)
            model = create_model(snapshot['model_id'], config, {**frozen, 'allow_anonymous': True})
        else:
            # Compatibility for deployments with execution controls disabled.
            from agent_runtime.custom_models import get_custom_model
            model = create_model(run.model, config, get_custom_model(job['user_id'], run.model))
        machine = create_learning_machine(config, planning_mode=job['planning_mode'])
        machine.db = database
        machine.model = model
        return machine

    return BackgroundLearning(db, store, factory, controller=controller)

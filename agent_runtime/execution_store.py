"""Small durable execution-control ledger. No messages, progress or artifacts.

PostgreSQL transaction advisory locks serialize each user/session boundary.
The SQLite path exists for deterministic unit tests; production uses PostgreSQL.
No transaction waits on remote work or executor shutdown.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any

from sqlalchemy import (JSON, BigInteger, Boolean, Column, Float, Index, Integer,
                        MetaData, String, Table, UniqueConstraint, and_, select, text)
from sqlalchemy.engine import Engine

TERMINAL = {"completed", "cancelled", "failed", "error"}
metadata = MetaData()
controls = Table(
    "igraph_execution_controls", metadata,
    Column("control_id", String(64), primary_key=True),
    Column("kind", String(8), nullable=False),
    Column("user_id", String(256), nullable=False),
    Column("session_id", String(256), nullable=False),
    Column("run_id", String(64), nullable=False),
    Column("job_id", String(64)),
    Column("parent_id", String(64)),
    Column("start_request_id", String(64)),
    Column("operation_id", String(64)),
    Column("fingerprint", String(64)),
    Column("generation", Integer, nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("admission_blocked", Boolean, nullable=False),
    Column("state", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("user_id", "session_id", "start_request_id", name="uq_igraph_start_key"),
    UniqueConstraint("user_id", "session_id", "run_id", "generation", "operation_id", name="uq_igraph_operation"),
)
Index("ix_igraph_controls_session", controls.c.user_id, controls.c.session_id)
Index("ix_igraph_controls_run", controls.c.run_id)
Index("uq_igraph_session_admission", controls.c.user_id, controls.c.session_id, unique=True,
      postgresql_where=and_(controls.c.kind == "run", controls.c.admission_blocked.is_(True)),
      sqlite_where=and_(controls.c.kind == "run", controls.c.admission_blocked.is_(True)))


class ControlError(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


def fingerprint(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class ExecutionStore:
    def __init__(self, engine: Engine, *, instance_id: str | None = None) -> None:
        self.engine = engine
        self.instance_id = instance_id or str(uuid.uuid4())
        self._sqlite_lock = threading.RLock()

    def migrate(self) -> None:
        metadata.create_all(self.engine)

    @contextmanager
    def transaction(self, user_id: str, session_id: str):
        # SQLite does not support advisory locks. Unit tests share this store.
        with self._sqlite_lock if self.engine.dialect.name == "sqlite" else _NullLock():
            with self.engine.begin() as conn:
                if self.engine.dialect.name == "postgresql":
                    conn.execute(text("SET LOCAL lock_timeout = '2s'"))
                    conn.execute(text("SET LOCAL statement_timeout = '5s'"))
                    lock = int.from_bytes(hashlib.sha256(json.dumps([user_id, session_id]).encode()).digest()[:8], "big", signed=True)
                    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
                yield conn

    def _find(self, conn, user_id, session_id, *, run_id=None, start_request_id=None, job_id=None):
        condition = [controls.c.user_id == user_id, controls.c.session_id == session_id]
        if job_id:
            condition += [controls.c.kind == "job", controls.c.job_id == job_id]
        else:
            condition += [controls.c.kind == "run"]
            if run_id:
                condition += [controls.c.run_id == run_id]
            elif start_request_id:
                condition += [controls.c.start_request_id == start_request_id]
            else:
                raise ControlError("INVALID_CONTROL_TARGET", 422)
        row = conn.execute(select(controls).where(*condition)).mappings().first()
        return dict(row) if row else None

    def get(self, user_id, session_id, **target):
        with self.transaction(user_id, session_id) as conn:
            return self._find(conn, user_id, session_id, **target)

    def _assert_session_open(self, conn, uid, sid):
        closed = conn.execute(select(controls.c.control_id).where(controls.c.user_id == uid,
                             controls.c.session_id == sid, controls.c.kind == 'session')).first()
        if closed:
            raise ControlError('SESSION_DELETING')

    def seal_session(self, uid, sid):
        """Fence late starts before deleting files/native session records."""
        with self.transaction(uid, sid) as conn:
            rows = list(conn.execute(select(controls).where(controls.c.user_id == uid, controls.c.session_id == sid)).mappings())
            if any(row['kind'] == 'run' and row['admission_blocked'] for row in rows) or any(
                    row['kind'] == 'job' and not (row['state'].get('execution_stopped') and row['state'].get('terminal_persisted')) for row in rows):
                raise ControlError('SESSION_EXECUTION_ACTIVE')
            if any(row['kind'] == 'session' for row in rows):
                return
            identity, now = str(uuid.uuid4()), time.time()
            conn.execute(controls.insert().values(control_id=identity, kind='session', user_id=uid,
                         session_id=sid, run_id=identity, generation=1, revision=1, admission_blocked=False,
                         state={'closed': True}, created_at=now, updated_at=now))

    def session_deletion(self, uid, sid):
        with self.transaction(uid, sid) as conn:
            row = conn.execute(select(controls).where(controls.c.user_id == uid,
                controls.c.session_id == sid, controls.c.kind == 'session')).mappings().first()
            return dict(row) if row else None

    def begin_session_deletion(self, uid, sid, active_native_runs=()):
        """Fence the session and record every owned cancellation atomically."""
        with self.transaction(uid, sid) as conn:
            rows = list(conn.execute(select(controls).where(controls.c.user_id == uid,
                controls.c.session_id == sid)).mappings())
            runs = [dict(row) for row in rows if row['kind'] == 'run']
            if set(active_native_runs) - {row['run_id'] for row in runs}:
                raise ControlError('SESSION_CANCEL_UNAVAILABLE')
            deletion = next((dict(row) for row in rows if row['kind'] == 'session'), None)
            if deletion is None:
                identity, now = str(uuid.uuid4()), time.time()
                deletion = dict(control_id=identity, kind='session', user_id=uid, session_id=sid,
                    run_id=identity, generation=1, revision=1, admission_blocked=False,
                    state={'closed': True, 'delete_status': 'requested'}, created_at=now, updated_at=now)
                conn.execute(controls.insert().values(**deletion))
            elif not deletion['state'].get('delete_status'):
                deletion = self._update(conn, deletion, {**deletion['state'], 'delete_status': 'requested'})
            pending_jobs = {row['run_id'] for row in rows if row['kind'] == 'job'
                and not (row['state'].get('execution_stopped') and row['state'].get('terminal_persisted'))}
            targets = []
            for run in runs:
                if not run['admission_blocked'] and run['run_id'] not in pending_jobs and run['state']['cancel_status'] not in {'requested', 'stopping'}:
                    continue
                cancel_id = str(uuid.uuid5(uuid.UUID(deletion['control_id']), run['run_id']))
                state = self._cancel_state(conn, uid, sid, run, cancel_id)
                # The session fence blocks new work. Historical jobs must not
                # contend with the current Run's unique admission record.
                state['cancel_blocks_admission'] = run['admission_blocked']
                targets.append(self._update(conn, run, state))
            return deletion, targets

    def session_deletions(self, uid=None, *, pending_only=False):
        conditions = [controls.c.kind == 'session', controls.c.state['delete_status'].as_string().in_(
            ['requested'] if pending_only else ['requested', 'deleted'])]
        if uid is not None:
            conditions.append(controls.c.user_id == uid)
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(controls).where(*conditions)).mappings()]

    def finish_session_deletion(self, uid, sid):
        with self.transaction(uid, sid) as conn:
            row = conn.execute(select(controls).where(controls.c.user_id == uid,
                controls.c.session_id == sid, controls.c.kind == 'session')).mappings().one()
            return self._update(conn, dict(row), {**row['state'], 'delete_status': 'deleted'})

    def _insert_run(self, conn, uid, sid, key, digest, *, cancelled=False):
        rid = str(uuid.uuid4())
        now = time.time()
        row = dict(control_id=rid, kind="run", user_id=uid, session_id=sid, run_id=rid,
                   job_id=None, parent_id=None, start_request_id=key, operation_id=None, fingerprint=digest,
                   generation=1, revision=1, admission_blocked=not cancelled,
                   created_at=now, updated_at=now, state={
                       "run_status": "cancelled" if cancelled else "preparing",
                       "cancel_status": "confirmed" if cancelled else "none",
                       "preparation_stopped": cancelled, "agent_stopped": True,
                       "terminal_persisted": cancelled, "agent_claimed": False,
                       "executor_instance": self.instance_id,
                       "cancel_request_id": None, "last_error": None,
                   })
        conn.execute(controls.insert().values(**row))
        return row

    def _update(self, conn, row, state, **columns):
        pending_cancel = state.get("cancel_status") in {"requested", "stopping"} and state.get("cancel_blocks_admission", True)
        blocked = row["kind"] == "run" and (pending_cancel or not state.get("terminal_persisted") or state["run_status"] not in TERMINAL
                    or not state.get('preparation_stopped') or not state.get('agent_stopped'))
        if state == row['state'] and blocked == row['admission_blocked'] and all(row.get(k) == v for k, v in columns.items()):
            return row
        updated = {**row, **columns, "state": state, "admission_blocked": blocked,
                   "revision": row["revision"] + 1, "updated_at": time.time()}
        conn.execute(controls.update().where(controls.c.control_id == row["control_id"]).values(**updated))
        return updated

    def register(self, uid: str, sid: str, key: str, digest: str, *, max_keys_per_minute=30, preparation_owner=None):
        with self.transaction(uid, sid) as conn:
            self._assert_session_open(conn, uid, sid)
            row = self._find(conn, uid, sid, start_request_id=key)
            if row:
                if row["fingerprint"] is not None and row["fingerprint"] != digest:
                    raise ControlError("START_IDEMPOTENCY_CONFLICT")
                if row["state"]["cancel_status"] != "none":
                    raise ControlError("RUN_CANCELLED")
                return row, False
            self._limit_new_keys(conn, uid, sid, max_keys_per_minute)
            active = self._active(conn, uid, sid)
            if active:
                raise ControlError("RUN_ACTIVE")
            row = self._insert_run(conn, uid, sid, key, digest)
            row = self._update(conn, row, {**row['state'], 'preparation_owner': preparation_owner})
            return row, True

    def _limit_new_keys(self, conn, uid, sid, limit=30):
        recent = conn.execute(select(controls.c.control_id).where(controls.c.user_id == uid,
                              controls.c.session_id == sid, controls.c.kind == "run", controls.c.created_at > time.time() - 60).limit(limit)).all()
        if len(recent) >= limit:
            raise ControlError("CONTROL_RATE_LIMITED", 429)

    def _active(self, conn, uid, sid):
        row = conn.execute(select(controls).where(controls.c.user_id == uid, controls.c.session_id == sid,
                           controls.c.kind == "run", controls.c.admission_blocked.is_(True))).mappings().first()
        return dict(row) if row else None

    def active(self, uid, sid):
        with self.transaction(uid, sid) as conn:
            return self._active(conn, uid, sid)

    def claim_agent(self, uid, sid, rid, *, continuing=False):
        with self.transaction(uid, sid) as conn:
            self._assert_session_open(conn, uid, sid)
            row = self._find(conn, uid, sid, run_id=rid)
            if not row:
                raise ControlError("RUN_NOT_REGISTERED", 404)
            state = dict(row["state"])
            if state["cancel_status"] != "none":
                raise ControlError("RUN_CANCELLED")
            if state['run_status'] in TERMINAL:
                raise ControlError('RUN_TERMINATED')
            if continuing:
                if state["run_status"] != "paused" or not state["agent_stopped"]:
                    raise ControlError("STALE_FEEDBACK")
            elif state["agent_claimed"]:
                raise ControlError("RUN_ALREADY_STARTED")
            state.update(agent_claimed=True, agent_stopped=False, run_status="running", terminal_persisted=False,
                         executor_instance=self.instance_id)
            return self._update(conn, row, state, generation=row["generation"] + int(continuing))

    def acknowledge(self, uid, sid, rid, generation: int, **evidence):
        allowed = {"preparation_stopped", "agent_stopped", "terminal_persisted", "run_status", "last_error", "native_run_created"}
        if set(evidence) - allowed:
            raise ValueError("Invalid execution acknowledgement")
        with self.transaction(uid, sid) as conn:
            row = self._find(conn, uid, sid, run_id=rid)
            if not row or row["generation"] != generation:
                return row
            state = dict(row["state"])
            # Cancellation stays terminal even when Agno sends RunCompleted next.
            if state.get('native_run_created'):
                evidence.pop('native_run_created', None)
            for field, timestamp in (('agent_stopped', 'agent_stopped_at'), ('terminal_persisted', 'terminal_saved_at')):
                if evidence.get(field) and not state.get(field):
                    state[timestamp] = time.time()
            if state["run_status"] == "cancelled":
                evidence.pop("run_status", None)
            elif state['cancel_status'] != 'none' and evidence.get('run_status') == 'failed' and not state['agent_claimed']:
                evidence['run_status'] = 'cancelled'
            state.update(evidence)
            return self._update(conn, row, state)

    def preparation_exited(self, uid, sid, rid, generation, *, failed=False):
        """Close an undelivered launch atomically against native claim_agent."""
        with self.transaction(uid, sid) as conn:
            row = self._find(conn, uid, sid, run_id=rid)
            if not row or row['generation'] != generation:
                return row
            state = {**row['state'], 'preparation_stopped': True}
            if failed and not state['agent_claimed']:
                state.update(run_status='cancelled' if state['cancel_status'] != 'none' else 'failed',
                             terminal_persisted=True, agent_stopped=True, native_run_created=False)
            return self._update(conn, row, state)

    def request_cancel(self, uid, sid, cancel_id, **target):
        with self.transaction(uid, sid) as conn:
            row = self._find(conn, uid, sid, **target)
            if row is None:
                self._assert_session_open(conn, uid, sid)
                if not target.get("start_request_id"):
                    raise ControlError("RUN_NOT_FOUND", 404)
                self._limit_new_keys(conn, uid, sid)
                row = self._insert_run(conn, uid, sid, target["start_request_id"], None, cancelled=True)
            self._check_cancel_key(conn, uid, sid, cancel_id, row['control_id'])
            state = self._cancel_state(conn, uid, sid, row, cancel_id)
            return self._update(conn, row, state)

    def _cancel_state(self, conn, uid, sid, row, cancel_id):
        state = dict(row['state'])
        if state['cancel_status'] == 'none':
            state['cancel_status'] = 'requested'
            state['cancel_blocks_admission'] = row['admission_blocked'] or self._active(conn, uid, sid) is None
            if state['run_status'] == 'paused':
                # A saved approval phase still needs a verified cancelled write.
                state['terminal_persisted'] = False
        state.setdefault('cancel_requested_at', time.time())
        state['cancel_request_id'] = state.get('cancel_request_id') or cancel_id
        state['cancel_target_id'] = row['control_id']
        state['cancel_requests'] = {**state.get('cancel_requests', {}), cancel_id: row['control_id']}
        return state

    def _check_cancel_key(self, conn, uid, sid, cancel_id, target_id):
        rows = conn.execute(select(controls.c.state).where(controls.c.user_id == uid, controls.c.session_id == sid)).scalars()
        if any(state.get('cancel_requests', {}).get(cancel_id, target_id) != target_id for state in rows):
            raise ControlError('CANCEL_IDEMPOTENCY_CONFLICT')

    def confirm_cancel(self, uid, sid, rid, generation):
        with self.transaction(uid, sid) as conn:
            row = self._find(conn, uid, sid, run_id=rid)
            if not row or row["generation"] != generation:
                return row
            state = dict(row["state"])
            if state["cancel_status"] == "none":
                return row
            jobs = conn.execute(select(controls).where(controls.c.run_id == rid, controls.c.kind == "job")).mappings().all()
            exited = state["agent_stopped"] and state["preparation_stopped"] and all(j["state"].get("execution_stopped") for j in jobs)
            persisted = state["terminal_persisted"] and all(j["state"].get("terminal_persisted") for j in jobs)
            state["cancel_status"] = "confirmed" if exited and persisted else "stopping"
            if state['cancel_status'] == 'confirmed':
                state.setdefault('cancel_confirmed_at', time.time())
            return self._update(conn, row, state)

    def register_job(self, uid, sid, rid, generation, op_id, digest, *, parent_id=None, executor_instance=None, tool_name=None):
        with self.transaction(uid, sid) as conn:
            # A transport retry can recover an already registered identity after
            # cancellation. It grants no new execution; the executor rechecks
            # the stop fence before starting and acknowledges a queued exit.
            existing = conn.execute(select(controls).where(controls.c.user_id == uid, controls.c.session_id == sid,
                                    controls.c.run_id == rid, controls.c.kind == 'job',
                                    controls.c.generation == generation, controls.c.operation_id == op_id)).mappings().first()
            if existing:
                if existing['fingerprint'] != digest or existing['parent_id'] != (parent_id or rid):
                    raise ControlError('JOB_IDEMPOTENCY_CONFLICT')
                return dict(existing), False
            self._assert_session_open(conn, uid, sid)
            parent = self._find(conn, uid, sid, run_id=rid)
            if not parent or (not parent_id and parent["generation"] != generation):
                raise ControlError("STALE_EXECUTION")
            if parent["state"]["cancel_status"] != "none" or (not parent_id and parent["state"]["run_status"] in TERMINAL):
                raise ControlError("RUN_CANCELLED")
            if parent_id:
                ancestor = self._find(conn, uid, sid, job_id=parent_id)
                if not ancestor or ancestor["run_id"] != rid or ancestor['generation'] != generation or ancestor['state']['execution_stopped'] or ancestor["state"]["cancel_status"] != "none":
                    raise ControlError("JOB_PARENT_UNAVAILABLE")
            jid = str(uuid.uuid4())
            now = time.time()
            row = dict(control_id=jid, kind="job", user_id=uid, session_id=sid, run_id=rid, job_id=jid,
                       parent_id=parent_id or rid, start_request_id=None, operation_id=op_id, fingerprint=digest,
                       generation=generation, revision=1, admission_blocked=False, created_at=now, updated_at=now,
                       state={"run_status": "queued", "cancel_status": "none", "execution_stopped": False,
                              "terminal_persisted": False, "executor_instance": executor_instance, "cancel_request_id": None,
                              'tool_name': tool_name})
            conn.execute(controls.insert().values(**row))
            return row, True

    def acknowledge_job(self, uid, sid, jid, generation, *, status, execution_stopped, terminal_persisted):
        with self.transaction(uid, sid) as conn:
            row = self._find(conn, uid, sid, job_id=jid)
            if not row or row["generation"] != generation:
                return row
            state = dict(row["state"])
            if state["execution_stopped"] and state["terminal_persisted"]:
                return row
            state.update(run_status=status, execution_stopped=execution_stopped, terminal_persisted=terminal_persisted)
            if state["cancel_status"] != "none" and execution_stopped and terminal_persisted:
                state["cancel_status"] = "confirmed"
            return self._update(conn, row, state)

    def worker_started(self, uid, sid, jid, generation, executor_instance, identity):
        with self.transaction(uid, sid) as conn:
            row = self._find(conn, uid, sid, job_id=jid)
            if not row or row['generation'] != generation or row['state']['executor_instance'] != executor_instance:
                raise ControlError('STALE_EXECUTION')
            parent = self._find(conn, uid, sid, run_id=row['run_id'])
            if row['state']['execution_stopped'] or row['state']['cancel_status'] != 'none' or parent['state']['cancel_status'] != 'none':
                raise ControlError('RUN_CANCELLED')
            if row['state'].get('worker') and row['state']['worker'] != identity:
                raise ControlError('EXECUTOR_ALREADY_REGISTERED')
            return self._update(conn, row, {**row['state'], 'worker': identity})

    def cancel_job(self, uid, sid, jid, cancel_id):
        with self.transaction(uid, sid) as conn:
            target = self._find(conn, uid, sid, job_id=jid)
            if target is None:
                raise ControlError("JOB_NOT_FOUND", 404)
            self._check_cancel_key(conn, uid, sid, cancel_id, jid)
            descendants = {jid}
            rows = [dict(r) for r in conn.execute(select(controls).where(controls.c.user_id == uid,
                    controls.c.session_id == sid, controls.c.run_id == target['run_id'], controls.c.kind == 'job')).mappings()]
            changed = True
            while changed:
                previous = len(descendants)
                descendants.update(r['job_id'] for r in rows if r['parent_id'] in descendants)
                changed = len(descendants) != previous
            for row in rows:
                if row['job_id'] not in descendants:
                    continue
                state = {**row['state'], 'cancel_request_id': row['state'].get('cancel_request_id') or cancel_id, 'cancel_target_id': jid}
                state['cancel_requests'] = {**state.get('cancel_requests', {}), cancel_id: jid}
                state['cancel_status'] = 'confirmed' if state['execution_stopped'] and state['terminal_persisted'] else 'requested'
                state.setdefault('cancel_requested_at', time.time())
                updated = self._update(conn, row, state)
                if row['job_id'] == jid:
                    target = updated
            return target

    def job_summary(self, row):
        rows = self.jobs(row['user_id'], row['session_id'], row['run_id'])
        row = next(job for job in rows if job['job_id'] == row['job_id'])
        descendants = {row['job_id']}
        while True:
            expanded = descendants | {job['job_id'] for job in rows if job['parent_id'] in descendants}
            if expanded == descendants:
                break
            descendants = expanded
        targets = [job for job in rows if job['job_id'] in descendants]
        stopped = all(job['state']['execution_stopped'] for job in targets)
        saved = all(job['state']['terminal_persisted'] for job in targets)
        return {**row['state'], 'job_id': row['job_id'], 'run_id': row['run_id'], 'revision': sum(job['revision'] for job in targets),
                'execution_stopped': stopped, 'terminal_persisted': saved,
                'cancel_status': ('confirmed' if stopped and saved else 'stopping') if row['state']['cancel_status'] != 'none' else 'none',
                'pending_components': [job['job_id'] for job in targets if not job['state']['execution_stopped']]}

    def pending(self):
        # Low-frequency reconciliation, never a per-token write or read.
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(controls).where(controls.c.kind == 'run',
                controls.c.admission_blocked.is_(True) | controls.c.state['cancel_status'].as_string().in_(['requested', 'stopping']))).mappings()]

    def coordinator_record(self, run_id):
        """Internal primary-key read; only previously authenticated work is scheduled."""
        with self.engine.connect() as conn:
            row = conn.execute(select(controls).where(controls.c.control_id == run_id, controls.c.kind == 'run')).mappings().first()
            return dict(row) if row else None

    def pending_job_cancellations(self):
        with self.engine.connect() as conn:
            rows = list(conn.execute(select(controls).where(controls.c.kind == 'job',
                        controls.c.state['cancel_status'].as_string().in_(['requested', 'stopping']))).mappings())
        # A parent may already have exited while a child still needs stopping.
        # Retry the original scoped target exactly once per scan.
        targets = {}
        for row in rows:
            jid = row['state'].get('cancel_target_id') or row['job_id']
            targets[(row['user_id'], row['session_id'], jid)] = {**row, 'job_id': jid}
        return list(targets.values())

    def pending_execution_jobs(self):
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(select(controls).where(controls.c.kind == 'job',
                (controls.c.state['execution_stopped'].as_boolean().is_(False)) |
                (controls.c.state['terminal_persisted'].as_boolean().is_(False)))).mappings()]

    def jobs(self, uid, sid, rid):
        with self.transaction(uid, sid) as conn:
            return [dict(row) for row in conn.execute(select(controls).where(controls.c.user_id == uid,
                    controls.c.session_id == sid, controls.c.run_id == rid, controls.c.kind == "job")).mappings()]

    def summary(self, row):
        uid, sid = row["user_id"], row["session_id"]
        with self.transaction(uid, sid) as conn:
            row = self._find(conn, uid, sid, run_id=row['run_id'])
            active = self._active(conn, uid, sid)
            jobs = list(conn.execute(select(controls).where(controls.c.user_id == uid, controls.c.session_id == sid,
                        controls.c.run_id == row['run_id'], controls.c.kind == 'job')).mappings())
            state = row['state']
        pending = []
        if not state.get("preparation_stopped", True):
            pending.append("preparation")
        if not state.get("agent_stopped", True):
            pending.append("agent")
        if state["cancel_status"] != "none":
            pending += ["job:" + j["job_id"] for j in jobs if not j["state"].get("execution_stopped")]
        return dict(run_id=row["run_id"], start_request_id=row["start_request_id"], registration_status="registered",
                    native_run_created=state.get('native_run_created', state['agent_claimed']),
                    run_status=state["run_status"], cancel_status=state["cancel_status"], execution_stopped=not pending,
                    terminal_persisted=state["terminal_persisted"] and all(j["state"].get("terminal_persisted") for j in jobs if state["cancel_status"] != "none"),
                    pending_components=pending, revision=row["revision"] + sum(j['revision'] for j in jobs), execution_generation=row["generation"],
                    last_error=state.get('last_error'),
                    active_run={"run_id": active["run_id"], "status": active["state"]["run_status"]} if active else None,
                    can_send=active is None)


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

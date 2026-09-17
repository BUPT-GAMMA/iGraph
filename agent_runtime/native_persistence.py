"""Retry native Agno phase writes without re-running business or model work.

Only unacknowledged writes are spooled. PostgreSQL remains the message store;
the control ledger contains identities/evidence, never a second transcript.
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from execution_lifecycle import atomic_json

from agno.session.agent import AgentSession

logger = logging.getLogger(__name__)
STABLE = {'completed', 'cancelled', 'failed', 'error', 'paused'}


def status_of(run):
    return str(getattr(run.status, 'value', run.status)).lower()


class NativePersistence:
    def __init__(self, root: Path):
        self.root = root
        self.pending: dict[tuple[str, int], dict] = {}

    def path(self, rid, generation):
        from uuid import UUID
        return self.root / f'{UUID(rid)}-{int(generation)}.json'

    def capture(self, scope, session):
        if (session.user_id, session.session_id) != (scope.user_id, scope.session_id):
            raise ValueError('NATIVE_SAVE_OWNER_MISMATCH')
        run = session.get_run(scope.run_id)
        if run is None:
            return
        scope.native_run_created = True
        if status_of(run) == 'cancelled':
            run.metadata = {**(run.metadata or {}), 'execution_interrupted': True}
        # Reuse Agno's already-scrubbed payload. A retry merges only this Run
        # into the freshly loaded session, preserving unrelated history.
        snapshot = copy.copy(session)
        snapshot.runs = [run]
        record = {'user_id': scope.user_id, 'session_id': scope.session_id,
                  'run_id': scope.run_id, 'generation': scope.generation,
                  'execution_stopped': False, 'session': snapshot.to_dict()}
        record = json.loads(json.dumps(record, default=str))
        self.pending[(scope.run_id, scope.generation)] = record
        scope.terminal_write_pending = status_of(run) in STABLE
        try:
            atomic_json(self.path(scope.run_id, scope.generation), record)
        except OSError:
            # The in-memory payload remains owned and retryable. Native storage
            # still gets its attempt; a failed spool is never a saved-terminal ack.
            logger.warning('native_write_spool_pending run_id=%s', scope.run_id)

    def load(self, row):
        rid, generation = row['run_id'], row['generation']
        key = (rid, generation)
        record = self.pending.get(key)
        if record is None:
            path = self.path(rid, generation)
            if not path.is_file():
                return None
            record = json.loads(path.read_text(encoding='utf-8'))
        if any(record.get(field) != row[field] for field in ('user_id', 'session_id', 'run_id', 'generation')):
            raise ValueError('NATIVE_SAVE_OWNER_MISMATCH')
        return record

    def mark_exited(self, row, record):
        if not record['execution_stopped']:
            record = {**record, 'execution_stopped': True}
        self.pending[(row['run_id'], row['generation'])] = record
        try:
            atomic_json(self.path(row['run_id'], row['generation']), record)
        except OSError:
            logger.warning('native_write_spool_pending run_id=%s', row['run_id'])
        return record

    def finalize_checkpoint(self, row, record, *, cancelled):
        """Finalize the last owned checkpoint after proven execution exit."""
        from agno.run.base import RunStatus
        candidate = AgentSession.from_dict(copy.deepcopy(record['session']))
        run = candidate.get_run(row['run_id'])
        if run is None:
            raise ValueError('INVALID_NATIVE_CHECKPOINT')
        if status_of(run) not in STABLE:
            run.status = RunStatus.cancelled if cancelled else RunStatus.error
            run.metadata = {**(run.metadata or {}), 'execution_interrupted': True}
            # Preserve input, confirmed tools and partial text; no model summary.
            record = {**record, 'session': candidate.to_dict()}
        return self.mark_exited(row, record)

    @staticmethod
    def matches(actual, expected):
        if actual is None:
            return False
        # Round trip through the native serializer, including requirements,
        # tools, partial messages, usage and cancellation metadata.
        return actual.to_dict() == expected.to_dict()

    def persist(self, db, row, record, session):
        # Agno 2.9's RunOutput.from_dict pops fields from its input. Every
        # attempt gets a copy so a failed retry cannot consume partial messages.
        candidate = AgentSession.from_dict(copy.deepcopy(record['session']))
        expected = candidate.get_run(row['run_id'])
        if expected is None or status_of(expected) not in STABLE:
            raise ValueError('INVALID_NATIVE_TERMINAL_PAYLOAD')
        if self.matches(session.get_run(row['run_id']), expected):
            return session
        # The controller checks generation/admission first. No new Run is
        # admitted while this write is unconfirmed; history is still merged.
        session.upsert_run(expected)
        session.session_data = {**(session.session_data or {}), **(candidate.session_data or {})}
        db.upsert_session(session=session)
        saved = db.get_session(session_id=row['session_id'], user_id=row['user_id'])
        if saved is None or saved.user_id != row['user_id'] or not self.matches(saved.get_run(row['run_id']), expected):
            raise RuntimeError('CANCEL_TERMINAL_NOT_PERSISTED')
        return saved

    def acknowledged(self, row):
        self.path(row['run_id'], row['generation']).unlink(missing_ok=True)
        self.pending.pop((row['run_id'], row['generation']), None)

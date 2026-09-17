"""Remove an explicitly selected terminal tail from native model history.

Called inside the execution ledger's user/session transaction. Admission and
history changes share one commit; no model or network work runs under this lock.
"""
import json
from sqlalchemy import select
from agent_runtime.execution_store import ControlError


class NativeTurnEditor:
    def __init__(self, table):
        self.table = table

    def validate(self, conn, uid, sid, target):
        row = conn.execute(select(self.table).where(
            self.table.c.session_id == sid, self.table.c.user_id == uid,
        ).with_for_update()).mappings().first()
        if row is None or row['agent_id'] != 'igraph-agent':
            raise ControlError('SESSION_NOT_FOUND', 404)
        raw_runs = row['runs']
        runs = json.loads(raw_runs) if isinstance(raw_runs, str) else list(raw_runs or [])
        if not runs or runs[-1].get('run_id') != target:
            raise ControlError('EDIT_TARGET_CHANGED')
        if str(runs[-1].get('status', '')).lower() not in {'completed', 'cancelled'}:
            raise ControlError('EDIT_TARGET_NOT_FINISHED')
        return row, runs

    def replace(self, conn, uid, sid, target):
        row, runs = self.validate(conn, uid, sid, target)
        # A summary can contain the removed turn. Rebuild it from retained runs
        # later rather than feeding stale conclusions back into the model.
        conn.execute(self.table.update().where(
            self.table.c.session_id == sid, self.table.c.user_id == uid,
        ).values(runs=json.dumps(runs[:-1]) if isinstance(row['runs'], str) else runs[:-1], summary=None))

"""Append-only, paginated job progress; readers own their cursors."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path


class JobProgressStore:
    def __init__(self, root):
        self.path = Path(root) / 'progress.sqlite3'
        self._lock = threading.Lock()
        self._initialized = False

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            if not self._initialized:
                # SQLite's busy timeout is not honored by WAL initialization
                # on every platform. Retry only this idempotent schema setup.
                deadline = time.monotonic() + 10
                while True:
                    try:
                        connection.execute('PRAGMA journal_mode=WAL')
                        break
                    except sqlite3.OperationalError as exc:
                        if (getattr(exc, 'sqlite_errorcode', 0) & 0xff) != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                            raise
                        time.sleep(.01)
                connection.execute('''CREATE TABLE IF NOT EXISTS progress (
                    job_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (job_id, seq))''')
                connection.execute("""CREATE TABLE IF NOT EXISTS progress_owners (
                    job_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL)""")
                connection.commit()
                self._initialized = True
        except BaseException:
            connection.close()
            raise
        return connection

    def append(self, job_id, update, *, user_id="", session_id=""):
        payload = json.dumps(update, ensure_ascii=False, allow_nan=False)
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT OR IGNORE INTO progress_owners VALUES (?, ?, ?)', (job_id, user_id, session_id))
            sequence = connection.execute('SELECT COALESCE(MAX(seq), 0) + 1 FROM progress WHERE job_id=?',
                                          (job_id,)).fetchone()[0]
            connection.execute('INSERT INTO progress VALUES (?, ?, ?)', (job_id, sequence, payload))
        return sequence

    def read(self, job_id, after_seq, limit):
        if not self.path.exists():
            rows = []
        else:
            with self._lock, closing(self._connect()) as connection:
                rows = connection.execute('SELECT seq, payload FROM progress WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?',
                                          (job_id, after_seq, limit + 1)).fetchall()
        page = rows[:limit]
        return {'progress_updates': [{**json.loads(payload), 'progress_seq': seq} for seq, payload in page],
                'progress_cursor': page[-1][0] if page else after_seq,
                'progress_has_more': len(rows) > limit}

    def delete_session(self, session_id, user_id):
        if not self.path.exists():
            return
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('DELETE FROM progress WHERE job_id IN (SELECT job_id FROM progress_owners WHERE session_id=? AND user_id=?)',
                               (session_id, user_id))
            connection.execute('DELETE FROM progress_owners WHERE session_id=? AND user_id=?', (session_id, user_id))

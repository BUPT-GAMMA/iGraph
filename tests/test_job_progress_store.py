"""Durable journal checks independent of the agent and ML dependencies."""
import sqlite3

from graph_data_service.job_progress import JobProgressStore


def test_progress_store_concurrent_appends_keep_unique_ordered_sequences(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from graph_data_service.job_progress import JobProgressStore
    def write(worker):
        store = JobProgressStore(tmp_path)
        for index in range(25):
            store.append('shared-job', {'worker': worker, 'index': index}, user_id='alice', session_id='s')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(4)))
    rows = JobProgressStore(tmp_path).read('shared-job', 0, 1000)['progress_updates']
    assert [row['progress_seq'] for row in rows] == list(range(1, 101))
    assert {(row['worker'], row['index']) for row in rows} == {(worker, index) for worker in range(4) for index in range(25)}



def test_initialization_retries_busy_without_duplicating_progress(tmp_path, monkeypatch):
    original = sqlite3.connect
    attempts = []
    class InitiallyBusy(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'PRAGMA journal_mode=WAL':
                attempts.append(sql)
                if len(attempts) == 1:
                    error = sqlite3.OperationalError('database is locked')
                    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise error
            return super().execute(sql, *args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', lambda *args, **kwargs: original(*args, factory=InitiallyBusy, **kwargs))
    store = JobProgressStore(tmp_path)
    assert store.append('j', {'epoch': 1}) == 1
    assert len(attempts) == 2
    assert store.read('j', 0, 10)['progress_updates'] == [{'epoch': 1, 'progress_seq': 1}]

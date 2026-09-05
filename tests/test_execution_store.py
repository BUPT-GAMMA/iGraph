import concurrent.futures
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from agent_runtime.execution_store import ControlError, ExecutionStore, fingerprint


@pytest.fixture
def store(tmp_path):
    # Set only to an isolated test database. The same tests exercise PG locking.
    url = os.getenv('IGRAPH_CONTROL_TEST_DATABASE_URL') or f'sqlite:///{tmp_path}/controls.db'
    engine = create_engine(url)
    schema = None
    if engine.dialect.name == 'postgresql':
        schema = 'cancel_store_' + uuid.uuid4().hex
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        engine = engine.execution_options(schema_translate_map={None: schema})
    ledger = ExecutionStore(engine)
    ledger.migrate()
    yield ledger
    if schema:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
    engine.dispose()


def test_same_key_only_one_preparation_and_one_agent(store):
    sid, key = str(uuid.uuid4()), str(uuid.uuid4())
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: store.register('alice', sid, key, fingerprint({'message': 'q'})), range(20)))
    assert sum(claimed for _, claimed in results) == 1
    assert len({row['run_id'] for row, _ in results}) == 1
    rid = results[0][0]['run_id']
    store.claim_agent('alice', sid, rid)
    with pytest.raises(ControlError, match='RUN_ALREADY_STARTED'):
        store.claim_agent('alice', sid, rid)
    with pytest.raises(ControlError, match='START_IDEMPOTENCY_CONFLICT'):
        store.register('alice', sid, key, fingerprint({'message': 'different'}))


def test_pre_cancel_blocks_late_start_but_not_other_user(store):
    sid, key = str(uuid.uuid4()), str(uuid.uuid4())
    row = store.request_cancel('alice', sid, 'cancel', start_request_id=key)
    assert store.summary(row)['execution_stopped']
    assert store.summary(row)['can_send']
    with pytest.raises(ControlError, match='RUN_CANCELLED'):
        store.register('alice', sid, key, 'digest')
    assert store.register('bob', sid, key, 'digest')[1]
    with pytest.raises(ControlError, match='RUN_NOT_FOUND'):
        store.request_cancel('bob', sid, 'cancel', run_id=row['run_id'])


def test_deleted_session_fence_rejects_late_start_and_preserves_other_users(store):
    sid = str(uuid.uuid4())
    store.seal_session('alice', sid)
    store.seal_session('alice', sid)
    with pytest.raises(ControlError, match='SESSION_DELETING'):
        store.register('alice', sid, str(uuid.uuid4()), 'q')
    assert store.register('bob', sid, str(uuid.uuid4()), 'q')[1]
    with pytest.raises(ControlError, match='SESSION_EXECUTION_ACTIVE'):
        store.seal_session('bob', sid)


def test_cancellation_waits_for_every_executor_and_save(store):
    sid = str(uuid.uuid4())
    row, _ = store.register('alice', sid, str(uuid.uuid4()), 'digest')
    rid = row['run_id']
    store.claim_agent('alice', sid, rid)
    job, _ = store.register_job('alice', sid, rid, 1, 'op', 'digest')
    store.request_cancel('alice', sid, 'cancel', run_id=rid)
    for evidence in [dict(preparation_stopped=True), dict(agent_stopped=True, run_status='cancelled')]:
        store.acknowledge('alice', sid, rid, 1, **evidence)
        row = store.confirm_cancel('alice', sid, rid, 1)
        assert row['state']['cancel_status'] == 'stopping'
        assert not store.summary(row)['can_send']
    store.acknowledge('alice', sid, rid, 1, terminal_persisted=True)
    store.acknowledge_job('alice', sid, job['job_id'], 1, status='cancelled', execution_stopped=True, terminal_persisted=True)
    row = store.confirm_cancel('alice', sid, rid, 1)
    assert row['state']['cancel_status'] == 'confirmed'
    assert store.summary(row)['can_send']
    with pytest.raises(ControlError, match='RUN_CANCELLED'):
        store.register_job('alice', sid, rid, 1, 'late', 'digest')
    with pytest.raises(ControlError, match='RUN_CANCELLED'):
        store.claim_agent('alice', sid, rid, continuing=True)


def test_natural_completion_does_not_wait_for_background_job(store):
    sid = str(uuid.uuid4())
    row, _ = store.register('alice', sid, str(uuid.uuid4()), 'digest')
    rid = row['run_id']
    store.claim_agent('alice', sid, rid)
    store.register_job('alice', sid, rid, 1, 'op', 'digest')
    row = store.acknowledge('alice', sid, rid, 1, preparation_stopped=True, agent_stopped=True, run_status='completed', terminal_persisted=True)
    assert store.summary(row)['can_send']
    newer, _ = store.register('alice', sid, str(uuid.uuid4()), 'new')
    store.request_cancel('alice', sid, 'cancel-old', run_id=rid)
    assert store.active('alice', sid)['run_id'] == newer['run_id']


def test_job_stop_confirms_every_descendant_and_never_retargets_a_cancel_key(store):
    sid = str(uuid.uuid4())
    run, _ = store.register('alice', sid, str(uuid.uuid4()), 'q')
    rid = run['run_id']
    store.claim_agent('alice', sid, rid)
    parent, _ = store.register_job('alice', sid, rid, 1, 'a', 'q')
    child, _ = store.register_job('alice', sid, rid, 1, 'b', 'q', parent_id=parent['job_id'])
    grandchild, _ = store.register_job('alice', sid, rid, 1, 'c', 'q', parent_id=child['job_id'])
    row = store.cancel_job('alice', sid, parent['job_id'], 'stop')
    store.acknowledge_job('alice', sid, parent['job_id'], 1, status='cancelled', execution_stopped=True, terminal_persisted=True)
    assert not store.job_summary(row)['execution_stopped']
    pending = store.pending_job_cancellations()
    assert [item['job_id'] for item in pending] == [parent['job_id']]
    for job in (child, grandchild):
        store.acknowledge_job('alice', sid, job['job_id'], 1, status='cancelled', execution_stopped=True, terminal_persisted=True)
    assert store.job_summary(row)['execution_stopped']
    assert store.pending_job_cancellations() == []
    assert store.get('alice', sid, run_id=rid)['state']['cancel_status'] == 'none'
    store.request_cancel('alice', sid, 'different-stop', run_id=rid)
    with pytest.raises(ControlError, match='CANCEL_IDEMPOTENCY_CONFLICT'):
        store.request_cancel('alice', sid, 'stop', run_id=rid)
    with pytest.raises(ControlError, match='CANCEL_IDEMPOTENCY_CONFLICT'):
        store.cancel_job('alice', sid, child['job_id'], 'different-stop')


# Daily CI exercises every scenario once; full acceptance retains 20 repetitions.
@pytest.mark.parametrize('iteration', range(max(1, int(os.getenv('IGRAPH_CANCEL_TEST_REPETITIONS', '20')))))
def test_continue_cancel_boundary_is_atomic_and_never_revives_a_cancelled_run(store, iteration):
    sid = str(uuid.uuid4())
    row, _ = store.register('alice', sid, str(uuid.uuid4()), 'q')
    rid = row['run_id']
    store.claim_agent('alice', sid, rid)
    store.acknowledge('alice', sid, rid, 1, run_status='paused', agent_stopped=True, preparation_stopped=True, terminal_persisted=True)
    def proceed():
        try:
            return store.claim_agent('alice', sid, rid, continuing=True)
        except ControlError as exc:
            assert exc.code == 'RUN_CANCELLED'
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        continuation = pool.submit(proceed)
        stop = pool.submit(store.request_cancel, 'alice', sid, 'stop', run_id=rid)
        continuation.result(); stop.result()
    row = store.get('alice', sid, run_id=rid)
    assert row['state']['cancel_status'] != 'none'
    with pytest.raises(ControlError, match='RUN_CANCELLED'):
        store.claim_agent('alice', sid, rid, continuing=True)
    revision = row['revision']
    assert store.request_cancel('alice', sid, 'stop', run_id=rid)['revision'] == revision

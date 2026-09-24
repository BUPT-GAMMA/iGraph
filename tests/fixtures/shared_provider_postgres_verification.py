"""Verify shared connections across runtime replicas in a temporary PG schema.

Run through stdin inside the runtime container. Uses synthetic credentials only;
all test tables are removed on exit. The deployment catalogue is not modified.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from agent_runtime.execution_store import ControlError, ExecutionStore
from agent_runtime.model_catalog import ModelCatalog
from agent_runtime.shared_provider_catalog import shared_providers


def main():
    for prefix in ('OPENAI', 'CLAUDE', 'ANTHROPIC', 'DEEPSEEK', 'ARK', 'DOUBAO', 'QWEN', 'DASHSCOPE'):
        for suffix in ('API_KEY', 'BASE_URL'):
            os.environ.pop(f'{prefix}_{suffix}', None)
    os.environ['LLM_API_KEY'] = 'synthetic-shared-key'
    os.environ['LLM_BASE_URL'] = 'http://host.docker.internal:21119/seed/v1'
    schema = 'shared_provider_verify_' + uuid.uuid4().hex
    engine = create_engine(os.environ['AGENT_RUNTIME_DATABASE_URL'])
    with engine.begin() as conn:
        conn.execute(CreateSchema(schema))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    config = SimpleNamespace(llm_api_key='', llm_base_url='')
    try:
        store = ExecutionStore(scoped); store.migrate()
        admin = ModelCatalog(store, config)
        replica = ModelCatalog(ExecutionStore(scoped), config)
        admin.bootstrap('admin', ['qwen3.6-plus'], is_admin=True)
        replica.bootstrap('member', ['qwen3.6-plus'])
        p = admin.list_providers('admin')['providers'][0]
        run, _ = replica.store.register('member', 'frozen', 'frozen', 'frozen',
            capture_model=lambda conn: replica.freeze(conn, 'member', 'frozen'))
        snapshot = run['state']['model_snapshot']
        admin.save_provider('admin', {'api_key': 'rotated-synthetic-key', 'base_url': 'http://host.docker.internal:21119/new/v1'}, p['provider_id'], p['revision'])
        frozen = replica.resolve('member', snapshot)
        assert frozen['api_key'] == 'synthetic-shared-key'
        assert frozen['base_url'].endswith('/seed/v1')
        # Exercise the real Agno model factory and HTTP transport with the frozen
        # shared configuration; never sends an external billable model request.
        from agent_runtime.model_factory import create_model
        from agno.models.message import Message
        llm = create_model(snapshot['config_id'], config, {**frozen, 'model_id': snapshot['config_id'],
            'upstream_model_id': snapshot['model_id'], 'allow_anonymous': True})
        response = llm.response([Message(role='user', content='shared provider verification')])
        assert 'Synthetic verification reply' in response.content
        replica.store.acknowledge('member', 'frozen', run['run_id'], 1, preparation_stopped=True,
            agent_stopped=True, terminal_persisted=True, run_status='completed')
        def safe(fn):
            try:
                return fn()
            except ControlError as exc:
                return exc.code
        for i in range(12):
            # Seed only this isolated test schema; public creation is user-owned.
            pid = f'shared-{uuid.uuid4()}'
            with store.transaction('admin', '__models__') as conn:
                admin._shared_lock(conn)
                conn.execute(shared_providers.insert().values(provider_id=pid, revision=1, data=dict(
                    name='Race', scope='shared', preset_id='custom', enabled=True,
                    base_url='http://localhost/v1', api_key='race-key', models={})))
            p = next(row for row in admin.list_providers('admin')['providers'] if row['provider_id'] == pid)
            admin.import_provider_models('admin', p['provider_id'], p['revision'], [{'id': 'race-model'}])
            m = next(row for row in replica.list('member')['models'] if row['provider_id'] == p['provider_id'])
            sid = f'race-{i}'
            replica.select('member', sid, m['config_id'], 0)
            with ThreadPoolExecutor(2) as pool:
                admission = pool.submit(safe, lambda: replica.store.register('member', sid, sid, sid,
                    capture_model=lambda conn: replica.freeze(conn, 'member', sid)))
                deletion = pool.submit(safe, lambda: admin.delete_provider('admin', p['provider_id'], p['revision']))
                result, removed = admission.result(timeout=20), deletion.result(timeout=20)
            if isinstance(result, str):
                assert result == 'MODEL_REMOVED'
                assert replica.store.active('member', sid) is None
            else:
                assert removed == 'MODEL_PROVIDER_IN_USE'
                run = result[0]
                assert replica.resolve('member', run['state']['model_snapshot'])['api_key'] == 'race-key'
                replica.store.acknowledge('member', sid, run['run_id'], 1, preparation_stopped=True,
                    agent_stopped=True, terminal_persisted=True, run_status='completed')
                admin.delete_provider('admin', p['provider_id'], p['revision'])
        print('PASS: shared provider edit, frozen credentials, real Agno HTTP request, 12 cross-user cross-replica admission/deletion races')
    finally:
        with engine.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        engine.dispose()


if __name__ == '__main__':
    main()

"""Verify personal defaults and migration across replicas in a temporary PG schema.

Run through stdin inside the runtime container. Only synthetic credentials are
used and the temporary schema is removed on exit; no model request is sent.
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
    os.environ['LLM_API_KEY'] = 'synthetic-default-key'
    os.environ['LLM_BASE_URL'] = 'https://api.zhizengzeng.com/v1'
    schema = 'personal_provider_verify_' + uuid.uuid4().hex
    engine = create_engine(os.environ['AGENT_RUNTIME_DATABASE_URL'])
    with engine.begin() as conn:
        conn.execute(CreateSchema(schema))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    config = SimpleNamespace(llm_api_key='', llm_base_url='')
    try:
        store = ExecutionStore(scoped); store.migrate()
        first = ModelCatalog(store, config)
        replica = ModelCatalog(ExecutionStore(scoped), config)
        with store.transaction('legacy', '__models__') as conn:
            conn.execute(shared_providers.insert().values(provider_id='legacy-shared', revision=1, data=dict(
                name='智增增', scope='shared', preset_id='zhizengzeng', enabled=True,
                base_url='https://api.zhizengzeng.com/v1', api_key='legacy-key',
                models={'qwen3.6-plus': dict(model_id='qwen3.6-plus', display_name='Qwen', tools=True,
                    context_window=131072, max_output_tokens=4096)})))
            first._set_preference(conn, 'legacy', {'is_admin': True, 'allowed': [], 'default_model': 'qwen3.6-plus'})
        with ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(c.bootstrap, 'legacy', ['qwen3.6-plus']) for c in (first, replica)]
            for job in jobs:
                job.result(timeout=30)
        first.bootstrap('member', ['qwen3.6-plus'])
        assert len(first.list_providers('legacy')['providers']) == 1
        a = first.list_providers('legacy')['providers'][0]
        b = first.list_providers('member')['providers'][0]
        assert a['provider_id'] != b['provider_id'] and a['editable'] and b['editable']
        first.save_provider('legacy', {'api_key': 'private-replacement'}, a['provider_id'], a['revision'])
        assert replica.provider_credentials('member', b['provider_id'], b['revision'])['api_key'] == 'legacy-key'
        try:
            replica.provider_credentials('member', a['provider_id'], a['revision'])
            raise AssertionError('Cross-account credentials were exposed')
        except ControlError as exc:
            assert exc.status == 404
        def safe(fn):
            try:
                return fn()
            except ControlError as exc:
                return exc.code
        for i in range(12):
            p = first.save_provider('member', dict(name='Race', preset_id='custom', enabled=True,
                base_url='https://example.test/v1', api_key='race-key'))
            first.import_provider_models('member', p['provider_id'], p['revision'], [{'id': 'race-model'}])
            m = next(row for row in replica.list('member')['models'] if row['provider_id'] == p['provider_id'])
            sid = f'race-{i}'
            replica.select('member', sid, m['config_id'], 0)
            with ThreadPoolExecutor(2) as pool:
                admission = pool.submit(safe, lambda: replica.store.register('member', sid, sid, sid,
                    capture_model=lambda conn: replica.freeze(conn, 'member', sid)))
                deletion = pool.submit(safe, lambda: first.delete_provider('member', p['provider_id'], p['revision']))
                result, removed = admission.result(timeout=20), deletion.result(timeout=20)
            if isinstance(result, str):
                assert result == 'MODEL_REMOVED' and replica.store.active('member', sid) is None
            else:
                assert removed == 'MODEL_PROVIDER_IN_USE'
                run = result[0]
                assert replica.resolve('member', run['state']['model_snapshot'])['api_key'] == 'race-key'
                replica.store.acknowledge('member', sid, run['run_id'], 1, preparation_stopped=True,
                    agent_stopped=True, terminal_persisted=True, run_status='completed')
                first.delete_provider('member', p['provider_id'], p['revision'])
        print('PASS: legacy migration, concurrent bootstrap, credential isolation, 12 cross-replica admission/deletion races')
    finally:
        with engine.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        engine.dispose()


if __name__ == '__main__':
    main()

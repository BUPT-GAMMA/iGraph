"""Real PostgreSQL lock verification in an isolated, automatically removed schema.

Run inside the runtime container with PYTHONPATH=/app. No pytest dependency.
"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateSchema, DropSchema
from agent_runtime.execution_store import ExecutionStore, ControlError
from agent_runtime.model_catalog import ModelCatalog


def main():
    schema = 'model_verify_' + uuid.uuid4().hex
    engine = create_engine(os.environ['AGENT_RUNTIME_DATABASE_URL'])
    with engine.begin() as conn:
        conn.execute(CreateSchema(schema))
    test_engine = engine.execution_options(schema_translate_map={None: schema})
    try:
        store = ExecutionStore(test_engine); store.migrate()
        catalog = ModelCatalog(store, SimpleNamespace(llm_api_key='synthetic', llm_base_url='http://localhost/v1'))
        catalog.bootstrap('test-user', [])
        a = catalog.save('test-user', {'model_id': 'same', 'base_url': 'http://localhost/a'})['config_id']
        b = catalog.save('test-user', {'model_id': 'same', 'base_url': 'http://localhost/b'})['config_id']
        catalog.set_default('test-user', a)
        def safe(fn):
            try: return fn()
            except ControlError as exc: return exc.code
        for i in range(24):
            sid = str(i)
            with ThreadPoolExecutor(2) as pool:
                start = pool.submit(safe, lambda: store.register('test-user', sid, sid, sid,
                    capture_model=lambda conn: catalog.freeze(conn, 'test-user', sid, expected_revision=0)))
                switch = pool.submit(safe, lambda: catalog.select('test-user', sid, b, 0))
                run, selected = start.result(), switch.result()
            if isinstance(run, str):
                assert run == 'MODEL_SELECTION_CHANGED' and selected['model'] == b
            else:
                assert run[0]['state']['model_snapshot']['config_id'] == a and selected == 'RUN_ACTIVE'
        restarted = ModelCatalog(ExecutionStore(test_engine), catalog.config)
        assert len(restarted.list('test-user')['models']) == 2
        print('PASS: 24 concurrent PostgreSQL switch/start races; persistent catalogue reload')
    finally:
        with engine.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        engine.dispose()


if __name__ == '__main__':
    main()

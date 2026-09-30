"""Deployment defaults become independent user-owned connections, including upgrades."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select

from agent_runtime.execution_store import ControlError, ExecutionStore
from agent_runtime.model_catalog import ModelCatalog, models, versions, selections
from agent_runtime.model_registry import SUPPORTED_MODELS
from agent_runtime.shared_provider_catalog import shared_providers


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    for prefix in ('OPENAI', 'CLAUDE', 'ANTHROPIC', 'DEEPSEEK', 'ARK', 'DOUBAO', 'QWEN', 'DASHSCOPE'):
        for suffix in ('API_KEY', 'BASE_URL'):
            monkeypatch.delenv(f'{prefix}_{suffix}', raising=False)
    monkeypatch.setenv('LLM_API_KEY', 'seed-secret')
    monkeypatch.setenv('LLM_BASE_URL', 'https://api.zhizengzeng.com/v1')
    store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/providers.db', connect_args={'check_same_thread': False}))
    store.migrate()
    catalog = ModelCatalog(store, SimpleNamespace(llm_api_key='', llm_base_url=''))
    catalog.bootstrap('admin', ['qwen3.6-plus'], is_admin=True)
    catalog.bootstrap('bob', ['qwen3.6-plus'])
    return catalog


def connection(catalog, uid='admin'):
    return catalog.list_providers(uid)['providers'][0]


def model(catalog, uid='admin', cid='qwen3.6-plus'):
    return next(m for m in catalog.list(uid)['models'] if m['config_id'] == cid)


def credentials(catalog, uid, provider=None):
    p = provider or connection(catalog, uid)
    return catalog.provider_credentials(uid, p['provider_id'], p['revision'])['api_key']


def start(catalog, uid='bob'):
    return catalog.store.register(uid, 'session', 'request', 'fingerprint',
        capture_model=lambda conn: catalog.freeze(conn, uid, 'session'))[0]


def finish(catalog, run, uid='bob'):
    catalog.store.acknowledge(uid, run['session_id'], run['run_id'], 1,
        preparation_stopped=True, agent_stopped=True, terminal_persisted=True, run_status='completed')


def test_all_accounts_get_equivalent_editable_private_defaults(catalog):
    a, b = connection(catalog), connection(catalog, 'bob')
    assert a['provider_id'] != b['provider_id']
    for uid, p in [('admin', a), ('bob', b)]:
        assert (p['name'], p['preset_id'], p['scope'], p['editable']) == ('智增增', 'zhizengzeng', 'personal', True)
        assert p['model_count'] == len(SUPPORTED_MODELS)
        assert credentials(catalog, uid) == 'seed-secret'
        assert 'seed-secret' not in json.dumps(catalog.list_providers(uid)) + json.dumps(catalog.list(uid))
    assert {m['config_id'] for m in catalog.list('admin')['models']} == {m['config_id'] for m in catalog.list('bob')['models']}


def test_edits_are_private_and_survive_restart_and_bootstrap(catalog, monkeypatch):
    p = connection(catalog)
    updated = catalog.save_provider('admin', {'name': 'Mine', 'base_url': 'https://mine.test/v1', 'api_key': 'private-key'}, p['provider_id'], p['revision'])
    monkeypatch.setenv('LLM_API_KEY', 'changed-env-key')
    restarted = ModelCatalog(catalog.store, catalog.config)
    restarted.bootstrap('admin', [], is_admin=False)
    restarted.bootstrap('bob', [], is_admin=True)
    assert connection(restarted) == updated
    assert credentials(restarted, 'admin') == 'private-key'
    assert credentials(restarted, 'bob') == 'seed-secret'
    assert model(restarted, 'bob')['base_url'] == 'https://api.zhizengzeng.com/v1'
    assert model(restarted, 'bob')['unavailable_reason'] is None


@pytest.mark.parametrize('owner,other', [('admin', 'bob'), ('bob', 'admin')])
def test_no_role_or_forged_owner_can_read_or_mutate_another_connection(catalog, owner, other):
    p = connection(catalog, owner); pid = p['provider_id']
    operations = [
        lambda: catalog.provider_credentials(other, pid, p['revision']),
        lambda: catalog.provider_configuration(other, pid, p['revision']),
        lambda: catalog.save_provider(other, {'name': 'Forged', 'scope': 'shared', 'user_id': owner,
            'base_url': 'https://attacker.test/v1'}, pid, p['revision']),
        lambda: catalog.import_provider_models(other, pid, p['revision'], [{'id': 'new'}]),
        lambda: catalog.save(other, {'provider_id': pid, 'provider_revision': p['revision'], 'upstream_model_id': 'new'}),
        lambda: catalog.delete_provider(other, pid, p['revision']),
    ]
    for operation in operations:
        with pytest.raises(ControlError, match='MODEL_PROVIDER_REMOVED') as error:
            operation()
        assert error.value.status == 404
    assert connection(catalog, owner) == p


def test_default_model_management_only_changes_its_owner(catalog):
    p = connection(catalog, 'bob')
    batch = [{'id': 'mine/a'}, {'id': 'mine/b'}]
    assert catalog.import_provider_models('bob', p['provider_id'], p['revision'], batch)['added_count'] == 2
    assert catalog.import_provider_models('bob', p['provider_id'], p['revision'], batch)['added_count'] == 0
    assert len(catalog.list('admin')['models']) == len(SUPPORTED_MODELS)
    original = model(catalog, 'bob')
    updated = catalog.save('bob', dict(provider_id=p['provider_id'], provider_revision=p['revision'],
        upstream_model_id='qwen3.6-plus', display_name='My Qwen', context_window=65536, vision=True),
        original['config_id'], original['revision'])
    assert model(catalog)['display_name'] == 'qwen3.6-plus'
    assert model(catalog, 'bob')['display_name'] == 'My Qwen'
    catalog.delete('bob', updated['config_id'], updated['revision'])
    catalog.bootstrap('bob', list(SUPPORTED_MODELS), is_admin=True)
    assert all(m['config_id'] != updated['config_id'] for m in catalog.list('bob')['models'])
    assert model(catalog)['config_id'] == updated['config_id']


def test_frozen_run_survives_migration_and_personal_edits(catalog):
    # Insert the exact shared-table shape used before this change.
    shared = dict(provider_id='shared-legacy', revision=3, data=dict(name='智增增', preset_id='zhizengzeng',
        scope='shared', base_url='https://api.zhizengzeng.com/v1', api_key='old-shared-key', enabled=True,
        models={'qwen3.6-plus': dict(model_id='qwen3.6-plus', display_name='Shared Qwen', tools=True,
            vision=False, context_window=131072, max_output_tokens=4096, restricted=True)}))
    with catalog.store.transaction('legacy', '__models__') as conn:
        conn.execute(shared_providers.insert().values(**shared))
        catalog._set_preference(conn, 'legacy', {'allowed': [], 'is_admin': True, 'default_model': 'qwen3.6-plus'})
        catalog._write(conn, 'legacy', 'qwen3.6-plus', dict(model_id='qwen3.6-plus', display_name='Old',
            source='deployment', enabled=False, tools=True, **catalog._provider_snapshot(shared)))
        conn.execute(selections.insert().values(user_id='legacy', session_id='old', config_id='qwen3.6-plus', revision=4))
    catalog.bootstrap('legacy', [])
    p = connection(catalog, 'legacy'); m = model(catalog, 'legacy')
    assert p['provider_id'] != shared['provider_id'] and p['scope'] == 'personal'
    assert m['config_id'] == 'qwen3.6-plus' and m['enabled'] is False and m['revision'] == 2
    assert catalog.selection('legacy', 'old')['model_revision'] == 4
    assert catalog.list('legacy')['default_model'] == 'qwen3.6-plus'
    catalog.save_provider('legacy', {'api_key': 'new-private-key'}, p['provider_id'], p['revision'])
    assert catalog.resolve('legacy', {'config_id': 'qwen3.6-plus', 'revision': 1})['api_key'] == 'old-shared-key'
    assert credentials(catalog, 'legacy') == 'new-private-key'
    catalog.bootstrap('new-member', [])
    assert credentials(catalog, 'new-member') == 'old-shared-key'
    with pytest.raises(ControlError, match='MODEL_PROVIDER_REMOVED'):
        catalog.provider_credentials('legacy', 'shared-legacy', 3)
    with catalog.store.engine.connect() as conn:
        assert conn.execute(select(shared_providers.c.data)).scalar_one()['api_key'] == 'old-shared-key'


def test_migration_preserves_tombstones_and_existing_personal_models(catalog):
    personal = catalog.save_provider('legacy', {'name': 'Mine', 'base_url': 'https://personal.test/v1', 'api_key': 'mine'})
    custom = catalog.save('legacy', {'provider_id': personal['provider_id'], 'provider_revision': personal['revision'], 'upstream_model_id': 'personal'})
    with catalog.store.transaction('legacy', '__models__') as conn:
        catalog._set_preference(conn, 'legacy', {'allowed': ['qwen3.6-plus'], 'default_model': custom['config_id']})
        catalog._write(conn, 'legacy', 'qwen3.6-plus', {'model_id': 'qwen3.6-plus', 'source': 'deployment', 'deleted': True})
    catalog.bootstrap('legacy', ['qwen3.6-plus'])
    assert all(m['config_id'] != 'qwen3.6-plus' for m in catalog.list('legacy')['models'])
    assert catalog.list('legacy')['default_model'] == custom['config_id']
    assert model(catalog, 'legacy', custom['config_id']) == custom
    assert credentials(catalog, 'legacy', personal) == 'mine'


def test_deleting_my_provider_does_not_affect_another_users_active_run(catalog):
    run = start(catalog)
    p = connection(catalog)
    catalog.delete_provider('admin', p['provider_id'], p['revision'])
    catalog.bootstrap('admin', list(SUPPORTED_MODELS), is_admin=True)
    assert catalog.list_providers('admin')['providers'] == []
    assert catalog.list('admin')['models'] == []
    assert catalog.resolve('bob', run['state']['model_snapshot'])['api_key'] == 'seed-secret'
    assert credentials(catalog, 'bob') == 'seed-secret'
    with catalog.store.engine.connect() as conn:
        for table in (models, versions):
            assert 'seed-secret' not in str(conn.execute(select(table).where(table.c.user_id == 'admin')).all())


def test_my_active_run_keeps_old_credentials_and_blocks_my_delete(catalog):
    p = connection(catalog, 'bob'); run = start(catalog)
    updated = catalog.save_provider('bob', {'api_key': 'rotated', 'enabled': False}, p['provider_id'], p['revision'])
    assert model(catalog, 'bob')['unavailable_reason'] == 'MODEL_PROVIDER_DISABLED'
    assert model(catalog)['unavailable_reason'] is None
    assert catalog.resolve('bob', run['state']['model_snapshot'])['api_key'] == 'seed-secret'
    with pytest.raises(ControlError, match='MODEL_PROVIDER_IN_USE'):
        catalog.delete_provider('bob', p['provider_id'], updated['revision'])
    finish(catalog, run)
    catalog.delete_provider('bob', p['provider_id'], updated['revision'])
    assert catalog.selection('bob', 'session')['model_unavailable_reason'] == 'MODEL_REMOVED'


def test_bootstrap_from_two_workers_only_creates_one_personal_copy(catalog):
    def initialize(_):
        return ModelCatalog(catalog.store, catalog.config).bootstrap('new', ['qwen3.6-plus'])
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(initialize, range(2)))
    assert len(catalog.list_providers('new')['providers']) == 1
    assert len(catalog.list('new')['models']) == len(SUPPORTED_MODELS)


def test_personal_delete_racing_admission_is_atomic(catalog):
    p = connection(catalog, 'bob')
    def attempt(fn):
        try:
            return fn()
        except ControlError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        admit = pool.submit(attempt, lambda: start(catalog))
        delete = pool.submit(attempt, lambda: catalog.delete_provider('bob', p['provider_id'], p['revision']))
        run, result = admit.result(), delete.result()
    if isinstance(run, str):
        assert run == 'MODEL_REMOVED' and catalog.store.active('bob', 'session') is None
    else:
        assert result == 'MODEL_PROVIDER_IN_USE'
        assert catalog.resolve('bob', run['state']['model_snapshot'])['api_key'] == 'seed-secret'

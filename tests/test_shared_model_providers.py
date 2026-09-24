"""Shared connections use the same management contract without sharing edit rights."""
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
    store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/shared.db', connect_args={'check_same_thread': False}))
    store.migrate()
    catalog = ModelCatalog(store, SimpleNamespace(llm_api_key='', llm_base_url=''))
    catalog.bootstrap('admin', ['qwen3.6-plus'], is_admin=True)
    catalog.bootstrap('bob', ['qwen3.6-plus'])
    return catalog


def connection(catalog, uid='admin'):
    return catalog.list_providers(uid)['providers'][0]


def model(catalog, uid='admin', cid='qwen3.6-plus'):
    return next(m for m in catalog.list(uid)['models'] if m['config_id'] == cid)


def start(catalog, uid='bob'):
    return catalog.store.register(uid, 'session', 'request', 'fingerprint',
        capture_model=lambda conn: catalog.freeze(conn, uid, 'session'))[0]


def finish(catalog, run, uid='bob'):
    catalog.store.acknowledge(uid, run['session_id'], run['run_id'], 1,
        preparation_stopped=True, agent_stopped=True, terminal_persisted=True, run_status='completed')


def test_one_actual_provider_for_all_model_brands_with_separate_edit_rights(catalog):
    result = catalog.list_providers('admin')
    assert len(result['providers']) == 1
    p = connection(catalog)
    assert (p['name'], p['preset_id'], p['scope'], p['editable']) == ('智增增', 'zhizengzeng', 'shared', True)
    assert p['model_count'] == len(SUPPORTED_MODELS)
    assert connection(catalog, 'bob')['provider_id'] == p['provider_id']
    assert connection(catalog, 'bob')['editable'] is False
    assert connection(catalog, 'bob')['model_count'] == 1
    assert model(catalog, 'bob')['provider_name'] == '智增增'
    assert 'seed-secret' not in json.dumps(result) + json.dumps(catalog.list('admin'))
    personal = catalog.save_provider('bob', dict(name='My key', preset_id='zhizengzeng', base_url=p['base_url'], api_key='personal-secret'))
    assert personal['scope'] == 'personal' and personal['editable']
    assert len(catalog.list_providers('bob')['providers']) == 2
    assert len(catalog.list_providers('admin')['providers']) == 1


def test_existing_ids_defaults_visibility_and_frozen_history_survive_migration(catalog):
    with catalog.store.transaction('legacy', '__models__') as conn:
        catalog._set_preference(conn, 'legacy', {'allowed': ['qwen3.6-plus'], 'default_model': 'qwen3.6-plus'})
        catalog._write(conn, 'legacy', 'qwen3.6-plus', dict(model_id='qwen3.6-plus', display_name='qwen3.6-plus',
            provider='qwen', source='deployment', base_url='https://old.test/v1', api_key='old-run-secret', enabled=False))
        conn.execute(selections.insert().values(user_id='legacy', session_id='old', config_id='qwen3.6-plus', revision=4))
    old_snapshot = dict(config_id='qwen3.6-plus', revision=1)
    data = catalog.bootstrap('legacy', ['qwen3.6-plus'])
    assert data['default_model'] == 'qwen3.6-plus'
    assert data['models'][0]['provider_id'] == connection(catalog)['provider_id']
    assert data['models'][0]['enabled'] is False
    choice = catalog.selection('legacy', 'old')
    assert choice['model'] == 'qwen3.6-plus' and choice['model_revision'] == 4
    assert catalog.resolve('legacy', old_snapshot)['api_key'] == 'old-run-secret'


def test_edits_persist_across_bootstrap_restart_and_keep_account_grants(catalog, monkeypatch):
    p = connection(catalog)
    updated = catalog.save_provider('admin', {'name': '新连接', 'base_url': 'https://new.test/v1', 'api_key': 'new-secret'}, p['provider_id'], p['revision'])
    monkeypatch.setenv('LLM_API_KEY', 'ignored-env-secret')
    monkeypatch.setenv('LLM_BASE_URL', 'https://ignored.test/v1')
    restarted = ModelCatalog(catalog.store, catalog.config)
    restarted.bootstrap('admin', ['qwen3.6-plus'], is_admin=True)
    restarted.bootstrap('bob', ['qwen3.6-plus'])
    assert connection(restarted) == updated
    assert model(restarted, 'bob')['base_url'] == 'https://new.test/v1'
    assert model(restarted, 'bob')['provider_name'] == '新连接'
    run = start(restarted)
    assert restarted.resolve('bob', run['state']['model_snapshot'])['api_key'] == 'new-secret'
    finish(restarted, run)
    restarted.bootstrap('bob', [])
    assert model(restarted, 'bob')['unavailable_reason'] == 'MODEL_NOT_ALLOWED'
    with pytest.raises(ControlError, match='MODEL_NOT_ALLOWED'):
        restarted.test_configuration('bob', 'qwen3.6-plus', model(restarted, 'bob')['revision'])


def test_non_admin_cannot_repoint_shared_secrets_or_mutate_shared_catalog(catalog):
    p = connection(catalog); pid = p['provider_id']; m = model(catalog, 'bob')
    operations = [
        lambda: catalog.save_provider('bob', {'name': 'Forged', 'scope': 'personal', 'base_url': 'https://attacker.test/v1'}, pid, p['revision']),
        lambda: catalog.provider_configuration('bob', pid, p['revision']),
        lambda: catalog.import_provider_models('bob', pid, p['revision'], [{'id': 'new'}]),
        lambda: catalog.save('bob', {'provider_id': pid, 'provider_revision': p['revision'], 'upstream_model_id': 'new'}),
        lambda: catalog.save('bob', {'provider_id': pid, 'provider_revision': p['revision'], 'upstream_model_id': 'new'}, m['config_id'], m['revision']),
        lambda: catalog.save('bob', {'base_url': 'https://attacker.test/v1'}, m['config_id'], m['revision']),
        lambda: catalog.delete('bob', m['config_id'], m['revision']),
        lambda: catalog.delete_provider('bob', pid, p['revision']),
    ]
    for operation in operations:
        with pytest.raises(ControlError, match='MODEL_PROVIDER_READ_ONLY') as error:
            operation()
        assert error.value.status == 403
    assert connection(catalog)['revision'] == p['revision']


def test_existing_deployment_provider_keeps_model_management(catalog):
    p = connection(catalog)
    p = catalog.save_provider('admin', {'base_url': 'http://local.test/v1'}, p['provider_id'], p['revision'])
    batch = [{'id': 'team/a'}, {'id': 'team/b'}]
    assert catalog.import_provider_models('admin', p['provider_id'], p['revision'], batch)['added_count'] == 2
    assert catalog.import_provider_models('admin', p['provider_id'], p['revision'], batch)['added_count'] == 0
    added = [m for m in catalog.list('bob')['models'] if m['provider_id'] == p['provider_id'] and m['upstream_model_id'].startswith('team/')]
    assert len(added) == 2  # Explicitly shared new models are available to all accounts.
    cid = added[0]['config_id']; old = model(catalog, cid=cid)
    updated = catalog.save('admin', dict(provider_id=p['provider_id'], provider_revision=p['revision'],
        upstream_model_id='team/a2', display_name='Team A', context_window=65536, vision=True), cid, old['revision'])
    assert model(catalog, 'bob', cid)['display_name'] == 'Team A'
    assert model(catalog, 'bob', cid)['vision'] is True
    with pytest.raises(ControlError, match='MODEL_CONFIG_CHANGED'):
        catalog.save('admin', dict(provider_id=p['provider_id'], provider_revision=p['revision'], upstream_model_id='team/stale'), cid, old['revision'])
    catalog.delete('admin', cid, updated['revision'])
    assert all(m['config_id'] != cid for m in catalog.list('bob')['models'])
    catalog.delete_provider('admin', p['provider_id'], p['revision'])
    assert all(m['provider_id'] != p['provider_id'] for m in catalog.list('bob')['models'])


def test_active_shared_runs_keep_old_credentials_and_block_cross_user_delete(catalog):
    p = connection(catalog); run = start(catalog)
    updated = catalog.save_provider('admin', {'enabled': False, 'api_key': 'rotated-secret', 'base_url': 'https://new.test/v1'}, p['provider_id'], p['revision'])
    assert model(catalog, 'bob')['unavailable_reason'] == 'MODEL_PROVIDER_DISABLED'
    frozen = catalog.resolve('bob', run['state']['model_snapshot'])
    assert frozen['api_key'] == 'seed-secret' and frozen['base_url'] == p['base_url']
    with pytest.raises(ControlError, match='MODEL_PROVIDER_IN_USE'):
        catalog.delete_provider('admin', p['provider_id'], updated['revision'])
    assert len(catalog.list('admin')['models']) == len(SUPPORTED_MODELS)
    assert catalog.resolve('bob', run['state']['model_snapshot'])['api_key'] == 'seed-secret'
    finish(catalog, run)
    catalog.delete_provider('admin', p['provider_id'], updated['revision'])
    catalog.bootstrap('admin', list(SUPPORTED_MODELS), is_admin=True)
    catalog.bootstrap('bob', ['qwen3.6-plus'])
    assert catalog.list_providers('admin')['providers'] == []
    assert catalog.list('bob')['models'] == []
    assert catalog.selection('bob', 'session')['model_unavailable_reason'] == 'MODEL_REMOVED'
    with catalog.store.engine.connect() as conn:
        stored = str(conn.execute(select(models)).all()) + str(conn.execute(select(versions)).all()) + str(conn.execute(select(shared_providers)).all())
        assert 'seed-secret' not in stored and 'rotated-secret' not in stored


def test_deleting_shared_model_also_checks_other_users_active_runs(catalog):
    run = start(catalog); m = model(catalog)
    with pytest.raises(ControlError, match='MODEL_IN_USE'):
        catalog.delete('admin', m['config_id'], m['revision'])
    finish(catalog, run)
    catalog.delete('admin', m['config_id'], m['revision'])
    catalog.bootstrap('admin', ['qwen3.6-plus'], is_admin=True)
    assert all(row['config_id'] != m['config_id'] for row in catalog.list('admin')['models'])


def test_shared_delete_racing_admission_is_atomic(catalog):
    p = connection(catalog)
    def attempt(fn):
        try:
            return fn()
        except ControlError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        admit = pool.submit(attempt, lambda: start(catalog))
        delete = pool.submit(attempt, lambda: catalog.delete_provider('admin', p['provider_id'], p['revision']))
        run, result = admit.result(), delete.result()
    if isinstance(run, str):
        assert run == 'MODEL_REMOVED' and catalog.store.active('bob', 'session') is None
    else:
        assert result == 'MODEL_PROVIDER_IN_USE'
        assert catalog.resolve('bob', run['state']['model_snapshot'])['api_key'] == 'seed-secret'


def test_removing_admin_account_does_not_remove_shared_provider(catalog):
    pid = connection(catalog)['provider_id']
    catalog.purge_user('admin')
    assert catalog.list('admin')['models'] == []
    assert connection(catalog, 'bob')['provider_id'] == pid


def test_new_cloud_connections_require_a_key(catalog):
    p = catalog.save_provider('bob', dict(name='Mine', preset_id='zhizengzeng', base_url='https://api.zhizengzeng.com/v1'))
    m = catalog.save('bob', dict(provider_id=p['provider_id'], provider_revision=p['revision'], upstream_model_id='qwen3.6-plus'))
    assert m['unavailable_reason'] == 'MODEL_NOT_CONFIGURED'
    catalog.save_provider('bob', {'api_key': 'personal'}, p['provider_id'], p['revision'])
    assert model(catalog, 'bob', m['config_id'])['unavailable_reason'] is None


@pytest.mark.parametrize('uid', ['admin', 'bob'])
def test_new_connections_are_always_owned_by_the_requesting_user(catalog, uid):
    other = 'bob' if uid == 'admin' else 'admin'
    p = catalog.save_provider(uid, {'name': 'Private connection', 'scope': 'shared', 'user_id': other,
        'base_url': 'http://private.test/v1', 'api_key': 'my-secret'})
    assert p['scope'] == 'personal' and p['editable'] is True
    assert all(row['provider_id'] != p['provider_id'] for row in catalog.list_providers(other)['providers'])
    catalog.import_provider_models(uid, p['provider_id'], p['revision'], [{'id': 'private-model'}])
    assert all(row['provider_id'] != p['provider_id'] for row in catalog.list(other)['models'])

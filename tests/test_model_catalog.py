from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine

from agent_runtime.execution_store import ExecutionStore, ControlError
from agent_runtime.model_catalog import ModelCatalog


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setenv('QWEN_API_KEY', 'test-secret')
    monkeypatch.setenv('QWEN_BASE_URL', 'https://test.example/v1')
    store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/models.db', connect_args={'check_same_thread': False}))
    store.migrate()
    result = ModelCatalog(store, SimpleNamespace(llm_api_key='', llm_base_url=''))
    result.bootstrap('alice', ['qwen3.6-plus'])
    return result


def custom(catalog, **patch):
    return catalog.save('alice', {'model_id': 'qwen3.6-plus', 'base_url': 'http://localhost:1234/v1', **patch})


def test_same_model_different_endpoints_have_stable_distinct_ids(catalog):
    first = custom(catalog, api_key='secret-one', display_name='One')
    second = custom(catalog, api_key='secret-two', display_name='Two')
    assert first['model_id'] != second['model_id']
    assert first['upstream_model_id'] == second['upstream_model_id']
    assert 'secret-one' not in str(catalog.list('alice'))
    assert 'secret-two' not in str(catalog.list('alice'))
    assert catalog.list('bob')['models'] == []


def test_migration_does_not_resurrect_deleted_model(catalog):
    legacy = {'model_id': 'old', 'display_name': 'Old', 'api_key': 'legacy-secret', 'base_url': 'https://old.example/v1'}
    catalog.bootstrap('alice', ['qwen3.6-plus'], [legacy])
    catalog.bootstrap('alice', ['qwen3.6-plus'], [legacy])
    assert len(catalog.list('alice')['models']) == 2
    catalog.delete('alice', 'old', 1)
    catalog.bootstrap('alice', ['qwen3.6-plus'], [legacy])
    assert len(catalog.list('alice')['models']) == 1
    assert catalog.selection('alice', 'old-session', 'old')['model_unavailable_reason'] == 'MODEL_REMOVED'


def test_session_defaults_are_independent_and_persisted(catalog):
    cid = custom(catalog)['config_id']
    assert catalog.selection('alice', 'A')['model'] == 'qwen3.6-plus'
    catalog.set_default('alice', cid)
    assert catalog.selection('alice', 'A')['model'] == 'qwen3.6-plus'
    assert catalog.selection('alice', 'B')['model'] == cid
    catalog.select('alice', 'A', cid, 0)
    restarted = ModelCatalog(ExecutionStore(catalog.store.engine), catalog.config)
    assert restarted.selection('alice', 'A')['model'] == cid
    assert restarted.selection('alice', 'A')['model_revision'] == 1


def test_selection_is_versioned_and_stale_page_is_rejected(catalog):
    first = custom(catalog)['config_id']
    second = custom(catalog)['config_id']
    catalog.select('alice', 'A', first, 0)
    with pytest.raises(ControlError, match='MODEL_SELECTION_CHANGED'):
        catalog.select('alice', 'A', second, 0)
    assert catalog.selection('alice', 'A')['model'] == first


def test_configuration_versions_preserve_running_endpoint(catalog):
    model = custom(catalog, api_key='old-secret')
    cid = model['config_id']
    catalog.select('alice', 'A', cid, 0)
    with catalog.store.transaction('alice', 'A') as conn:
        snapshot = catalog.freeze(conn, 'alice', 'A')
    catalog.save('alice', {'base_url': 'http://localhost:4321/v1', 'api_key': 'new-secret'}, cid, 1)
    assert catalog.resolve('alice', snapshot)['api_key'] == 'old-secret'
    assert catalog.resolve('alice', snapshot)['base_url'] == 'http://localhost:1234/v1'
    assert 'secret' not in str(snapshot)
    with pytest.raises(ControlError, match='MODEL_CONFIG_CHANGED'):
        catalog.save('alice', {'display_name': 'stale'}, cid, 1)


def test_disabled_default_is_not_silently_replaced(catalog):
    model = custom(catalog)
    catalog.set_default('alice', model['config_id'])
    catalog.set_enabled('alice', model['config_id'], False, 1)
    choice = catalog.selection('alice', 'new')
    assert choice['model'] == model['config_id']
    assert choice['model_unavailable_reason'] == 'MODEL_DISABLED'
    with catalog.store.transaction('alice', 'new') as conn, pytest.raises(ControlError, match='MODEL_DISABLED'):
        catalog.freeze(conn, 'alice', 'new')
    catalog.select('alice', 'new', 'qwen3.6-plus', 0)
    assert catalog.selection('alice', 'new')['model_unavailable_reason'] is None


def test_invalid_capabilities_and_limits_are_explicit(catalog):
    model = custom(catalog, tools=False)
    with pytest.raises(ControlError, match='MODEL_TOOLS_REQUIRED'):
        catalog.select('alice', 'A', model['config_id'], 0)
    with pytest.raises(ControlError, match='MODEL_INVALID_LIMITS'):
        custom(catalog, context_window=2000)


def test_builtin_is_managed_but_not_deletable(catalog):
    with pytest.raises(ControlError, match='MODEL_DEPLOYMENT_READ_ONLY'):
        catalog.delete('alice', 'qwen3.6-plus', 1)
    catalog.set_enabled('alice', 'qwen3.6-plus', False, 1)
    catalog.bootstrap('alice', ['qwen3.6-plus'])
    assert catalog.list('alice')['models'][0]['unavailable_reason'] == 'MODEL_DISABLED'

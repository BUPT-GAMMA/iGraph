"""Provider lifecycle, migration and network discovery at the real API boundary."""
import json
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from agent_runtime.execution_store import ControlError, ExecutionStore
from agent_runtime.model_api import create_model_router
from agent_runtime.model_catalog import ModelCatalog, models, selections, versions
from agent_runtime.provider_catalog import providers


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv('IGRAPH_CONTROL_SERVICE_KEY', 'provider-test-service-key')
    store = ExecutionStore(create_engine(f'sqlite:///{tmp_path}/models.db', connect_args={'check_same_thread': False}))
    store.migrate()
    catalog = ModelCatalog(store, SimpleNamespace(llm_api_key='', llm_base_url=''))
    app = FastAPI()
    @app.exception_handler(ControlError)
    async def error(request, exc):
        return JSONResponse({'detail': {'code': exc.code}}, status_code=exc.status)
    app.include_router(create_model_router(catalog, None))
    with TestClient(app, headers={'X-User-ID': 'alice', 'X-iGraph-Service-Key': 'provider-test-service-key'}) as client:
        yield client, catalog


def connection(client, **extra):
    response = client.post('/internal/model-providers', json=dict(name='Team gateway', preset_id='custom',
        base_url='https://models.example/v1', api_key='private-key', **extra))
    assert response.status_code == 200, response.text
    return response.json()['provider']


def add(client, provider, mid='team/model', **extra):
    return client.post('/internal/model-catalog', json=dict(provider_id=provider['provider_id'],
        provider_revision=provider['revision'], upstream_model_id=mid, **extra))


def test_provider_identity_secret_ownership_and_duplicate_models(service):
    client, catalog = service
    p = connection(client)
    m = add(client, p).json()['model']
    assert m['provider_name'] == 'Team gateway' and m['has_api_key']
    assert m['base_url'] == p['base_url'] and m['provider_id'] == p['provider_id']
    assert add(client, p).json()['detail']['code'] == 'MODEL_DUPLICATE'
    other = connection(client)
    assert add(client, other).status_code == 200
    public = client.get('/internal/model-providers')
    assert 'private-key' not in public.text
    assert 'private-key' not in client.get('/internal/model-catalog').text
    assert public.json()['providers'][0]['model_count'] == 1
    assert client.get('/internal/model-providers', headers={'X-User-ID': 'bob'}).json()['providers'] == []
    assert client.post('/internal/model-catalog', headers={'X-User-ID': 'bob'}, json=dict(
        provider_id=p['provider_id'], provider_revision=1, upstream_model_id='private')).status_code == 404
    assert client.get('/internal/model-providers', headers={'X-iGraph-Service-Key': ''}).status_code == 403
    assert add(client, p, 'override', base_url='https://other.example', api_key='override-key').status_code == 200
    item = catalog.list('alice')['models'][-1]
    assert catalog.test_configuration('alice', item['config_id'], item['revision'])['api_key'] == 'private-key'


def test_provider_edits_are_atomic_and_preserve_active_run_snapshot(service):
    client, catalog = service
    p = connection(client)
    a, b = add(client, p, 'one').json()['model'], add(client, p, 'two').json()['model']
    catalog.set_default('alice', a['config_id'])
    run, _ = catalog.store.register('alice', 'session', 'request', 'fingerprint',
        capture_model=lambda conn: catalog.freeze(conn, 'alice', 'session'))
    edit = client.post('/internal/model-providers', json={**p, 'base_url': 'https://new.example/v1', 'api_key': 'rotated-key', 'enabled': False, 'name': 'Renamed'})
    assert edit.status_code == 200
    for item in catalog.list('alice')['models']:
        assert item['revision'] == 2 and item['base_url'] == 'https://new.example/v1'
        assert item['provider_name'] == 'Renamed' and item['unavailable_reason'] == 'MODEL_PROVIDER_DISABLED'
    old = catalog.resolve('alice', run['state']['model_snapshot'])
    assert old['api_key'] == 'private-key' and old['base_url'] == 'https://models.example/v1'
    assert add(client, p, 'stale').json()['detail']['code'] == 'MODEL_PROVIDER_CHANGED'
    assert client.post('/internal/model-providers', json=p).status_code == 409
    assert client.delete(f"/internal/model-catalog/{a['config_id']}?revision=2").json()['detail']['code'] == 'MODEL_IN_USE'
    current = edit.json()['provider']
    assert client.post('/internal/model-providers', json={**current, 'api_key': '', 'enabled': True}).status_code == 200
    assert catalog.test_configuration('alice', b['config_id'], 3)['api_key'] == 'rotated-key'


def test_local_provider_clear_key_and_revision_safe_delete(service):
    client, catalog = service
    p = connection(client)
    p = client.post('/internal/model-providers', json={**p, 'clear_api_key': True}).json()['provider']
    assert not p['has_api_key']
    m = add(client, p).json()['model']
    assert catalog.test_configuration('alice', m['config_id'], 1)['api_key'] == ''
    path = f"/internal/model-providers/{p['provider_id']}"
    assert client.delete(path, params={'revision': 1}).status_code == 409
    assert client.delete(path, params={'revision': 2}).json()['detail']['code'] == 'MODEL_PROVIDER_NOT_EMPTY'
    assert client.delete(f"/internal/model-catalog/{m['config_id']}?revision=1").status_code == 200
    assert client.delete(path, params={'revision': 2}).status_code == 200
    with catalog.store.transaction('alice', '__models__') as conn:
        assert not conn.execute(select(versions)).first()
        assert not conn.execute(select(providers)).first()
    assert add(client, p).status_code == 404


def test_legacy_migration_groups_connections_without_changing_selection(service):
    _, catalog = service
    for cid, secret in [('legacy-a', 'shared'), ('legacy-b', 'shared'), ('legacy-c', 'different')]:
        data = dict(source='custom', model_id=cid, display_name=cid, base_url='https://old.example/v1', api_key=secret)
        with catalog.store.transaction('alice', '__models__') as conn:
            catalog._write(conn, 'alice', cid, data)
    with catalog.store.transaction('alice', 'session') as conn:
        conn.execute(selections.insert().values(user_id='alice', session_id='session', config_id='legacy-b', revision=5))
    first = catalog.list_providers('alice')
    assert len(first['providers']) == 2
    assert catalog.list_providers('alice') == first
    listed = catalog.list('alice')['models']
    assert listed[0]['provider_id'] == listed[1]['provider_id'] != listed[2]['provider_id']
    choice = catalog.selection('alice', 'session')
    assert choice['model'] == 'legacy-b' and choice['model_revision'] == 5
    catalog.delete('alice', 'legacy-a', 2)
    catalog.bootstrap('alice', [], [dict(model_id='legacy-a', base_url='https://old.example/v1', api_key='shared')])
    assert len(catalog.list('alice')['models']) == 2


def test_provider_change_racing_model_add_never_mixes_versions(service):
    client, catalog = service
    p = connection(client)
    def attempt(fn):
        try:
            return fn()
        except ControlError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(attempt, lambda: catalog.save_provider('alice', {'base_url': 'https://new.example/v1'}, p['provider_id'], 1))
        b = pool.submit(attempt, lambda: catalog.save('alice', dict(provider_id=p['provider_id'], provider_revision=1, model_id='raced')))
        edit, added = a.result(), b.result()
    if isinstance(added, str):
        assert added == 'MODEL_PROVIDER_CHANGED'
    else:
        assert catalog.list('alice')['models'][0]['base_url'] == 'https://new.example/v1'


@pytest.mark.parametrize('patch', [
    {'base_url': 'https://example.org:bad/v1'}, {'base_url': 'https://[invalid'},
    {'base_url': 'https://example.org/v1?key=secret'}, {'base_url': 'https://a:b@example.org'},
    {'base_url': 'ftp://example.org'}, {'base_url': 'https://example.org/white space'},
    {'name': '  '}, {'protocol': 'anthropic'}, {'api_key': 'key\nInjected: header'},
])
def test_provider_validation_returns_safe_errors(service, patch):
    client, _ = service
    result = client.post('/internal/model-providers', json={
        'name': 'Provider', 'base_url': 'https://example.org/v1', **patch})
    assert result.status_code == 422
    assert 'Injected' not in result.text


def mock_upstream(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr('agent_runtime.provider_probe.httpx.AsyncClient',
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))


def test_remote_discovery_deduplicates_and_never_returns_credentials(service, monkeypatch):
    client, _ = service
    p = connection(client)
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={'data': [{'id': 'team/b'}, {'id': 'a'}, {'id': 'a'}, {'invalid': 1}, {'id': 'bad model'}]})
    mock_upstream(monkeypatch, respond)
    result = client.post('/internal/model-providers/discover', json=p)
    assert result.status_code == 200
    assert [m['id'] for m in result.json()['models']] == ['a', 'team/b']
    assert seen[0].url == 'https://models.example/v1/models'
    assert seen[0].headers['Authorization'] == 'Bearer private-key'
    assert 'private-key' not in result.text


@pytest.mark.parametrize('status,code', [(401, 'MODEL_AUTH_FAILED'), (403, 'MODEL_AUTH_FAILED'),
    (404, 'MODEL_DISCOVERY_UNSUPPORTED'), (429, 'MODEL_RATE_LIMITED'), (500, 'MODEL_TEST_FAILED'), (302, 'MODEL_TEST_FAILED')])
def test_discovery_failures_are_actionable_and_no_redirects(service, monkeypatch, status, code):
    client, _ = service
    p = connection(client)
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(status, text='secret upstream body', headers={'Location': 'https://untrusted.example'})
    mock_upstream(monkeypatch, respond)
    result = client.post('/internal/model-providers/discover', json=p)
    assert result.status_code == 422 and result.json()['detail']['code'] == code
    assert 'secret upstream' not in result.text and len(seen) == 1


def test_discovery_timeout_returns_safe_error(service, monkeypatch):
    client, _ = service
    p = connection(client)
    def timeout(request):
        raise httpx.ReadTimeout('private upstream detail')
    mock_upstream(monkeypatch, timeout)
    result = client.post('/internal/model-providers/discover', json=p)
    assert result.json()['detail']['code'] == 'MODEL_CONNECTION_FAILED'


def test_user_purge_removes_provider_credentials(service):
    client, catalog = service
    connection(client)
    catalog.purge_user('alice')
    assert not catalog.list_providers('alice')['providers']


def test_bulk_import_is_atomic_idempotent_and_checks_provider_revision(service):
    client, catalog = service
    p = connection(client)
    path = '/internal/model-providers/import'
    body = {**p, 'models': [{'id': 'one'}, {'id': 'two', 'name': 'Second'}]}
    first = client.post(path, json=body)
    assert first.status_code == 200 and first.json()['added_count'] == 2
    assert client.post(path, json=body).json()['skipped_count'] == 2
    failed = client.post(path, json={**p, 'models': [{'id': 'three'}, {'id': 'bad model'}]})
    assert failed.status_code == 422
    assert len(catalog.list('alice')['models']) == 2
    assert client.post(path, json={**body, 'revision': 0}).status_code == 409
    assert client.post(path, headers={'X-User-ID': 'bob'}, json=body).status_code == 404
    assert client.post(path, json={**p, 'models': [{'id': f'm-{i}'} for i in range(201)]}).status_code == 422


def test_discovery_rejects_oversized_and_malformed_lists(service, monkeypatch):
    client, _ = service
    p = connection(client)
    for response, code in [(httpx.Response(200, content=b' ' * (4 * 1024 * 1024 + 1)), 'MODEL_DISCOVERY_TOO_LARGE'),
                           (httpx.Response(200, json={'data': {'id': 'bad'}}), 'MODEL_INVALID_RESPONSE')]:
        with monkeypatch.context() as patch:
            mock_upstream(patch, lambda request: response)
            result = client.post('/internal/model-providers/discover', json=p)
            assert result.json()['detail']['code'] == code

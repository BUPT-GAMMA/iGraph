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
    {'name': '  '}, {'protocol': 'unsupported-protocol'}, {'api_key': 'key\nInjected: header'},
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


def test_create_unconfigured_provider_then_configure_or_delete(service):
    client, catalog = service
    p = client.post('/internal/model-providers', json={'name': 'Empty provider'}).json()['provider']
    assert p['base_url'] == '' and p['model_count'] == 0
    assert client.post('/internal/model-providers/discover', json=p).json()['detail']['code'] == 'MODEL_NOT_CONFIGURED'
    assert client.post('/internal/model-providers/import', json={**p, 'models': [{'id': 'one'}]}).status_code == 422
    p = client.post('/internal/model-providers', json={**p, 'base_url': 'https://example.test/v1'}).json()['provider']
    m = add(client, p).json()['model']
    p = client.post('/internal/model-providers', json={**p, 'base_url': ''}).json()['provider']
    assert catalog.list('alice')['models'][0]['unavailable_reason'] == 'MODEL_NOT_CONFIGURED'
    assert client.delete(f"/internal/model-providers/{p['provider_id']}?revision={p['revision']}").status_code == 200
    assert catalog.list('alice')['models'] == []


def test_delete_provider_cascades_models_secrets_and_preserves_other_connections(service):
    client, catalog = service
    p, other = connection(client), connection(client)
    a, b = add(client, p, 'one').json()['model'], add(client, p, 'two').json()['model']
    survivor = add(client, other, 'one').json()['model']
    catalog.set_default('alice', a['config_id'])
    selected = catalog.selection('alice', 'history')
    assert client.delete(f"/internal/model-providers/{p['provider_id']}?revision=1", headers={'X-User-ID': 'bob'}).status_code == 404
    assert client.delete(f"/internal/model-providers/{p['provider_id']}?revision=1").status_code == 200
    assert [m['config_id'] for m in catalog.list('alice')['models']] == [survivor['config_id']]
    assert catalog.selection('alice', 'history')['model'] == selected['model']
    assert catalog.selection('alice', 'history')['model_unavailable_reason'] == 'MODEL_REMOVED'
    with catalog.store.transaction('alice', '__models__') as conn:
        for cid in (a['config_id'], b['config_id']):
            assert not conn.execute(select(versions).where(versions.c.config_id == cid)).first()
            data = conn.execute(select(models.c.data).where(models.c.config_id == cid)).scalar_one()
            assert data['deleted'] and not data['api_key'] and not data['base_url']
    assert len(catalog.list_providers('alice')['providers']) == 1


def test_provider_delete_is_all_or_nothing_while_a_child_model_is_running(service):
    client, catalog = service
    p = connection(client)
    a, b = add(client, p, 'idle').json()['model'], add(client, p, 'active').json()['model']
    catalog.set_default('alice', b['config_id'])
    run, _ = catalog.store.register('alice', 'active-session', 'request', 'fingerprint',
        capture_model=lambda conn: catalog.freeze(conn, 'alice', 'active-session'))
    path = f"/internal/model-providers/{p['provider_id']}?revision=1"
    response = client.delete(path)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'MODEL_PROVIDER_IN_USE'
    assert {m['config_id']: m['revision'] for m in catalog.list('alice')['models']} == {a['config_id']: 1, b['config_id']: 1}
    assert catalog.resolve('alice', run['state']['model_snapshot'])['api_key'] == 'private-key'
    catalog.store.acknowledge('alice', 'active-session', run['run_id'], run['generation'],
        preparation_stopped=True, agent_stopped=True, terminal_persisted=True, run_status='completed')
    assert client.delete(path).status_code == 200
    assert catalog.list('alice')['models'] == []


def test_provider_delete_racing_run_start_never_removes_active_credentials(service):
    client, catalog = service
    p = connection(client)
    m = add(client, p).json()['model']
    catalog.set_default('alice', m['config_id'])
    def attempt(operation):
        try:
            return operation()
        except ControlError as error:
            return error.code
    with ThreadPoolExecutor(2) as pool:
        deletion = pool.submit(attempt, lambda: catalog.delete_provider('alice', p['provider_id'], 1))
        start = pool.submit(attempt, lambda: catalog.store.register('alice', 'race-session', 'request', 'fingerprint',
            capture_model=lambda conn: catalog.freeze(conn, 'alice', 'race-session')))
        deleted, started = deletion.result(), start.result()
    if deleted is None:
        assert started == 'MODEL_REMOVED'
    else:
        assert deleted == 'MODEL_PROVIDER_IN_USE'
        assert catalog.resolve('alice', started[0]['state']['model_snapshot'])['api_key'] == 'private-key'


def test_credentials_are_scoped_uncached_and_revision_checked(service):
    client, catalog = service
    p = connection(client)
    path = f"/internal/model-providers/{p['provider_id']}/credentials"
    response = client.get(path, params={'revision': p['revision']})
    assert response.status_code == 200
    assert response.json() == dict(provider_id=p['provider_id'], revision=1, api_key='private-key')
    assert response.headers['cache-control'] == 'no-store'
    assert client.get(path, params={'revision': 1}, headers={'X-User-ID': 'bob'}).status_code == 404
    assert client.get(path, params={'revision': 1}, headers={'X-iGraph-Service-Key': ''}).status_code == 403
    assert client.get(path, params={'revision': 1}, headers={'X-User-ID': ''}).status_code == 401
    assert 'private-key' not in client.get('/internal/model-providers').text
    changed = client.post('/internal/model-providers', json={**p, 'api_key': 'new-key'}).json()['provider']
    assert client.get(path, params={'revision': 1}).status_code == 409
    assert client.get(path, params={'revision': changed['revision']}).json()['api_key'] == 'new-key'
    cleared = client.post('/internal/model-providers', json={**changed, 'api_key': '', 'clear_api_key': True}).json()['provider']
    assert client.get(path, params={'revision': cleared['revision']}).json()['api_key'] == ''
    assert client.delete(f"/internal/model-providers/{p['provider_id']}?revision={cleared['revision']}").status_code == 200
    assert client.get(path, params={'revision': cleared['revision']}).status_code == 404


def test_credentials_can_be_loaded_before_configuring_an_address(service):
    client, catalog = service
    p = client.post('/internal/model-providers', json={'name': 'Incomplete', 'api_key': 'saved-key'}).json()['provider']
    response = client.get(f"/internal/model-providers/{p['provider_id']}/credentials?revision=1")
    assert response.status_code == 200 and response.json()['api_key'] == 'saved-key'


@pytest.mark.parametrize('protocol,preset', [('openai-compatible', 'custom'), ('openai-responses', 'openai-responses'), ('anthropic', 'anthropic'), ('gemini', 'google')])
def test_protocol_defaults_persist_through_models_and_frozen_runs(service, protocol, preset):
    client, catalog = service
    p = client.post('/internal/model-providers', json=dict(name='Native', preset_id=preset,
        base_url='https://native.test/v1', api_key='native-key')).json()['provider']
    assert p['protocol'] == protocol
    m = add(client, p).json()['model']
    catalog.set_default('alice', m['config_id'])
    run, _ = catalog.store.register('alice', 'native-session', 'request', 'fp',
        capture_model=lambda conn: catalog.freeze(conn, 'alice', 'native-session'))
    assert catalog.test_configuration('alice', m['config_id'], 1)['protocol'] == protocol
    assert client.post('/internal/model-providers', json={**p, 'protocol': 'gemini' if protocol != 'gemini' else 'anthropic'}).status_code == 200
    assert catalog.resolve('alice', run['state']['model_snapshot'])['protocol'] == protocol
    assert client.post('/internal/model-providers', json=p).status_code == 409


@pytest.mark.parametrize('protocol', ['anthropic', 'gemini'])
def test_native_discovery_pagination_auth_and_model_names(service, monkeypatch, protocol):
    client, _ = service
    p = connection(client, protocol=protocol)
    seen = []
    def respond(request):
        seen.append(request)
        if protocol == 'anthropic':
            assert request.headers['x-api-key'] == 'private-key'
            assert request.headers['anthropic-version'] == '2023-06-01'
            assert request.url.path == '/v1/models'
            if len(seen) == 1:
                return httpx.Response(200, json={'data': [{'id': 'claude-one', 'display_name': 'Claude One'}], 'last_id': 'claude-one', 'has_more': True})
            assert request.url.params['after_id'] == 'claude-one'
            return httpx.Response(200, json={'data': [{'id': 'claude-two'}], 'has_more': False})
        assert request.headers['x-goog-api-key'] == 'private-key'
        assert 'key' not in request.url.params
        if len(seen) == 1:
            return httpx.Response(200, json={'models': [{'name': 'models/gemini-one', 'displayName': 'Gemini One', 'supportedGenerationMethods': ['generateContent']},
                {'name': 'models/embedding', 'supportedGenerationMethods': ['embedContent']}], 'nextPageToken': 'second-page'})
        assert request.url.params['pageToken'] == 'second-page'
        return httpx.Response(200, json={'models': [{'name': 'models/gemini-two', 'supportedGenerationMethods': ['generateContent']}]})
    mock_upstream(monkeypatch, respond)
    response = client.post('/internal/model-providers/discover', json=p)
    assert response.status_code == 200 and len(seen) == 2
    rows = response.json()['models']
    assert len(rows) == 2 and rows[0]['name'] in {'Claude One', 'Gemini One'}
    assert 'private-key' not in response.text


@pytest.mark.parametrize('protocol', ['openai-responses', 'anthropic', 'gemini'])
def test_model_probe_uses_native_wire_format(service, monkeypatch, protocol):
    client, _ = service
    p = connection(client, protocol=protocol)
    m = add(client, p, 'native-one').json()['model']
    seen = []
    def respond(request):
        body = json.loads(request.content); seen.append(body)
        if protocol == 'anthropic':
            assert request.url.path == '/v1/messages'
            assert body['messages'] == [{'role': 'user', 'content': 'Reply OK.'}]
            assert body['tools'][0]['input_schema'] == {'type': 'object', 'properties': {}}
            return httpx.Response(200, json={'content': [{'type': 'text', 'text': 'OK'}]})
        if protocol == 'gemini':
            assert request.url.path == '/v1/models/native-one:generateContent'
            assert body['contents'][0]['parts'] == [{'text': 'Reply OK.'}]
            assert body['tools'][0]['functionDeclarations'][0]['name'] == 'connection_test'
            return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': 'OK'}]}}]})
        assert request.url.path == '/v1/responses'
        assert body['store'] is False and 'messages' not in body
        assert body['tools'][0]['name'] == 'connection_test'
        return httpx.Response(200, json={'output': [{'type': 'message'}], 'status': 'completed'})
    mock_upstream(monkeypatch, respond)
    response = client.post('/internal/model-catalog/test', json=m)
    assert response.status_code == 200 and len(seen) == 1


def test_native_discovery_rejects_repeated_pagination_cursor(service, monkeypatch):
    client, _ = service
    p = connection(client, protocol='gemini')
    mock_upstream(monkeypatch, lambda _: httpx.Response(200, json={'models': [], 'nextPageToken': 'same'}))
    response = client.post('/internal/model-providers/discover', json=p)
    assert response.json()['detail']['code'] == 'MODEL_INVALID_RESPONSE'

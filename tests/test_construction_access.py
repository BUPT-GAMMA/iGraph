"""Project isolation at the Engine HTTP boundary, with the real BFF authority."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from construction_access import SCOPE_HEADER, scope_headers
from execution_protocol import service_headers
from graph_data_service.app import GraphDataService, create_app
from graph_data_service.construction import ConstructionStore
from graph_data_service.construction_access import ConstructionAccessClient
from tests.fixtures.construction_scope import authority
from tests.servers.test_graph_construction_http import GRAPH


@pytest.fixture
def scoped_engine(tmp_path, monkeypatch):
    monkeypatch.setenv('IGRAPH_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('IGRAPH_CONTROL_SERVICE_KEY', 'scope-test-only')
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'false')
    monkeypatch.setenv('NEBULA_ENABLED', 'false')
    sessions = {}
    auth, directory, projects = authority(tmp_path, sessions)
    pids = [projects.create('alice', name).project_id for name in ('A', 'B')]
    records = {}
    store = ConstructionStore(tmp_path)
    store.register_algorithm('csv', lambda _source, _options: copy.deepcopy(GRAPH))
    for label, pid in zip(('a', 'b', 'personal'), (*pids, None)):
        sessions[label] = {'session_id': label, 'user_id': 'alice', 'session_state': {'project_id': pid}}
        item = directory.create(project_id=pid, user_id='alice', name=label,
            original_filename=f'{label}-private.csv', content=b'id,phone\nalice,123\n')
        records[label] = item
        draft = store.build('alice', item.dataset_id, item.storage_path, item.original_filename, {})
        store.optimize('alice', item.dataset_id, draft['revision'], [], commit=True)
    records['raw'] = directory.create(project_id=pids[0], user_id='alice', name='raw',
        original_filename='raw.txt', content=b'pending source')
    access = ConstructionAccessClient('http://authority', transport=httpx.ASGITransport(app=auth))
    service = GraphDataService(construction_store=store, construction_access=access, migrate_storage=False,
                               storage_backend='local')
    # An actual global-registry row stays available in every scope.
    monkeypatch.setattr(service._dataset_registry, 'list_datasets', lambda: [SimpleNamespace(
        to_dict=lambda: {'dataset_id': 'public-demo', 'name': 'Public demo'})])
    with TestClient(create_app(service)) as client:
        yield SimpleNamespace(client=client, service=service, store=store, directory=directory,
            projects=projects, sessions=sessions, records=records, auth=auth)


def session_headers(sid, user='alice'):
    return {**service_headers(user), 'X-Session-ID': sid}


def ids(response):
    assert response.status_code == 200, response.text
    return {row['dataset_id'] for row in response.json()['datasets']}


def test_lists_filter_before_return_and_restored_session_ignores_stale_catalog(scoped_engine):
    s = scoped_engine
    for label in ('a', 'b', 'personal'):
        expected = {s.records[label].dataset_id, 'public-demo'}
        assert ids(s.client.get('/datasets', headers=session_headers(label))) == expected
        assert ids(s.client.post(f'/sessions/{label}/tools/list_datasets', headers=service_headers('alice'),
            json={'arguments': {'project_id': s.records['b'].project_id}})) == expected
    # Persisted prompt data is a hint; only the persisted project and directory
    # decide ownership, even on a restored session with a stale active ID.
    s.sessions['a']['session_state'].update(active_dataset_id=s.records['b'].dataset_id,
        data_state={'available_constructed_datasets': [{'dataset_id': s.records['b'].dataset_id}]})
    assert ids(s.client.get('/datasets', headers=session_headers('a'))) == {s.records['a'].dataset_id, 'public-demo'}
    assert ids(s.client.get('/datasets', headers=service_headers('alice'))) == {'public-demo'}
    assert ids(s.client.get('/datasets')) == {'public-demo'}
    s.sessions['a']['session_state']['project_id'] = s.records['b'].project_id
    assert ids(s.client.get('/datasets', headers=session_headers('a'))) == {s.records['b'].dataset_id, 'public-demo'}


@pytest.mark.parametrize('caller,target', [('a', 'b'), ('a', 'personal'), ('personal', 'b')])
def test_all_dataset_read_write_entrypoints_deny_cross_scope(scoped_engine, caller, target):
    s = scoped_engine
    item = s.records[target]
    did = item.dataset_id
    headers = session_headers(caller)
    edit = {'base_revision': 2, 'operations': [{'op': 'reverse_edges', 'edge_ids': ['knows-1']}]}
    attempts = [('GET', f'/construction/{did}', None), ('GET', f'/construction/{did}/graph', None),
        ('POST', f'/construction/{did}/preview', edit), ('POST', f'/construction/{did}/commit', edit),
        ('POST', f'/construction/{did}/source', {'source_path': item.storage_path, 'source_filename': item.original_filename}),
        ('POST', f'/construction/{did}/build', {'source_path': item.storage_path, 'source_filename': item.original_filename}),
        ('POST', f'/sessions/{caller}/graph/load_construction', {'dataset_id': did})]
    for tool in ('get_graph_construction_status', 'get_constructed_graph', 'load_constructed_graph',
                 'preview_graph_optimization', 'commit_graph_optimization', 'load_dataset'):
        attempts.append(('POST', f'/sessions/{caller}/tools/{tool}', {'arguments': {'dataset_id': did, **edit}}))
    for method, path, body in attempts:
        response = s.client.request(method, path, headers=headers, json=body)
        assert response.status_code == 404, (path, response.text)
        assert response.json()['detail']['code'] == 'DATASET_SCOPE_REQUIRED'
        assert 'phone' not in response.text and item.original_filename not in response.text
    assert s.store.graph('alice', did)['revision'] == 2
    own = s.records[caller]
    assert s.client.get(f'/construction/{own.dataset_id}/graph', headers=headers).status_code == 200


def test_missing_foreign_and_conflicting_identity_never_returns_metadata(scoped_engine):
    s = scoped_engine
    path = f'/construction/{s.records["b"].dataset_id}/graph'
    for headers, status in (({}, 403), ({'X-User-ID': 'alice'}, 403), (service_headers('alice'), 403),
        (session_headers('missing'), 404), (session_headers('b', 'bob'), 404),
        ({**session_headers('a'), **scope_headers(s.records['b'].project_id)}, 403)):
        assert s.client.get(path, headers=headers).status_code == status
    assert s.client.post('/sessions/b/tools/list_datasets', headers=session_headers('a'),
        json={'arguments': {}}).status_code == 403
    assert s.client.get('/datasets', headers={**service_headers('alice'), **scope_headers('missing')}).status_code == 404
    assert s.client.get('/datasets', headers={**service_headers('alice'), SCOPE_HEADER: 'all'}).status_code == 403


def test_authorized_unbuilt_sources_and_canonical_source_path(scoped_engine):
    s = scoped_engine
    raw = s.records['raw']
    assert s.client.get(f'/construction/{raw.dataset_id}', headers=session_headers('a')).json()['status'] == 'not_built'
    own, other = s.records['a'], s.records['b']
    for suffix in ('source', 'build'):
        response = s.client.post(f'/construction/{own.dataset_id}/{suffix}', headers=session_headers('a'),
            json={'source_path': other.storage_path, 'source_filename': other.original_filename})
        assert response.status_code == 403 and response.json()['detail']['code'] == 'invalid_source'
    assert s.store.graph('alice', own.dataset_id)['revision'] == 2


def test_deleted_and_migrated_metadata_revokes_reads_and_cleanup_stays_idempotent(scoped_engine):
    s = scoped_engine
    item = s.records['a']
    s.directory.delete(item.dataset_id, 'alice', item.project_id)
    assert s.client.get(f'/construction/{item.dataset_id}/graph', headers=session_headers('a')).status_code == 404
    assert ids(s.client.get('/datasets', headers=session_headers('a'))) == {'public-demo'}
    # Leftover graph files do not authorize themselves.
    assert s.store.graph('alice', item.dataset_id)['revision'] == 2
    item = s.records['b']
    s.directory.migrate_to_personal(item.project_id, 'alice')
    assert s.client.get(f'/construction/{item.dataset_id}/graph', headers=session_headers('b')).status_code == 404
    assert s.client.get(f'/construction/{item.dataset_id}/graph', headers=session_headers('personal')).status_code == 200
    cleanup_headers = {**service_headers('alice'), **scope_headers(None)}
    assert s.client.delete(f'/construction/{item.dataset_id}', headers=cleanup_headers).json()['deleted'] is True
    assert s.client.delete(f'/construction/{item.dataset_id}', headers=cleanup_headers).json()['deleted'] is False


def test_deleting_project_rejects_reads_and_allows_only_explicit_cleanup(scoped_engine):
    s = scoped_engine
    item = s.records['a']
    s.projects.mark_deleting(item.project_id, 'alice')
    headers = {**service_headers('alice'), **scope_headers(item.project_id)}
    assert s.client.get(f'/construction/{item.dataset_id}', headers=headers).status_code == 409
    assert s.client.get('/datasets', headers=headers).status_code == 409
    assert s.client.delete(f'/construction/{item.dataset_id}', headers=session_headers('a')).status_code == 403
    assert s.client.delete(f'/construction/{item.dataset_id}', headers=headers).json()['deleted'] is True


def test_parallel_sessions_keep_independent_scopes(scoped_engine):
    s = scoped_engine
    async def listing(label):
        payload = await s.service.dispatch_tool(label, 'list_datasets', {}, user_id='alice')
        assert {row['dataset_id'] for row in payload['datasets']} == {s.records[label].dataset_id, 'public-demo'}
    async def scenario():
        await asyncio.gather(*(listing(label) for label in ('a', 'b', 'personal') for _ in range(8)))
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['timeout', 'server_error', 'invalid_json', 'wrong_owner', 'wrong_dataset'])
def test_authority_failure_is_bounded_and_never_falls_back_to_user_catalog(scoped_engine, failure):
    s = scoped_engine
    calls = []
    def response(request):
        calls.append(request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('private diagnostic', request=request)
        if failure == 'server_error':
            return httpx.Response(500, json={'secret': 'private diagnostic'})
        if failure == 'invalid_json':
            return httpx.Response(200, text='private diagnostic')
        return httpx.Response(200, json={'user_id': 'bob' if failure == 'wrong_owner' else 'alice',
            'scope': 'personal', 'dataset_ids': ['wrong'], 'source': None})
    s.service._construction_access = ConstructionAccessClient('http://authority', transport=httpx.MockTransport(response))
    did = s.records['a'].dataset_id
    response = s.client.get(f'/construction/{did}/graph', headers=session_headers('a'))
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'DATASET_AUTHORIZATION_UNAVAILABLE'
    assert 'private diagnostic' not in response.text and 'phone' not in response.text
    assert len(calls) == 1
    assert calls[0].extensions['timeout']['read'] == 5
    if failure in ('timeout', 'server_error', 'invalid_json', 'wrong_owner'):
        assert s.client.get('/datasets', headers=session_headers('a')).status_code == 503


def test_authorization_total_deadline_cancels_slow_authority(scoped_engine, monkeypatch):
    from graph_data_service import construction_access
    monkeypatch.setattr(construction_access, 'AUTHORIZATION_TIMEOUT_SECONDS', .02)
    exited = []
    async def slow(_request):
        try:
            await asyncio.Event().wait()
        finally:
            exited.append(True)
    s = scoped_engine
    s.service._construction_access = ConstructionAccessClient('http://authority', transport=httpx.MockTransport(slow))
    response = s.client.get('/datasets', headers=session_headers('a'))
    assert response.status_code == 503
    assert exited == [True]


def test_queued_read_revalidates_after_directory_change(scoped_engine):
    from graph_engine.tool_execution import execution_policy
    s = scoped_engine
    async def scenario():
        reservation = await s.service._tool_scheduler.acquire(('alice', 'a'), execution_policy('commit_graph_optimization'))
        item = s.records['a']
        task = asyncio.create_task(s.service.dispatch_tool('a', 'get_constructed_graph', {'dataset_id': item.dataset_id}, 'alice'))
        try:
            await asyncio.sleep(0)
            assert not task.done()
            s.directory.migrate_to_personal(item.project_id, 'alice')
        finally:
            s.service._tool_scheduler.release(reservation)
        with pytest.raises(HTTPException) as exc:
            await task
        assert exc.value.status_code == 404
    asyncio.run(scenario())


def test_cached_job_replay_rechecks_scope(scoped_engine, monkeypatch):
    from unittest.mock import AsyncMock
    from uuid import uuid4
    s = scoped_engine
    cached = {'success': True, 'datasets': [
        {'dataset_id': item.dataset_id, 'storage_backend': 'construction'} for label, item in s.records.items() if label != 'raw']}
    host = SimpleNamespace(execute=AsyncMock(return_value=cached))
    headers = {**session_headers('a'), 'X-Run-ID': str(uuid4()), 'X-Execution-Generation': '1',
               'X-Operation-ID': str(uuid4())}
    with monkeypatch.context() as patch:
        patch.setattr(s.service, '_execution_host', host)
        denied = s.client.post('/sessions/a/tools/get_constructed_graph', headers=headers,
            json={'arguments': {'dataset_id': s.records['b'].dataset_id}})
        assert denied.status_code == 404
        host.execute.assert_not_awaited()
        response = s.client.post('/sessions/a/tools/list_datasets', headers=headers, json={'arguments': {}})
        assert ids(response) == {s.records['a'].dataset_id}
        # Filtering a replay must not mutate the durable job's original result.
        assert len(cached['datasets']) == 3


@pytest.mark.parametrize('scope', ['a', 'personal'])
@pytest.mark.parametrize('controlled', [False, True])
def test_existing_graph_status_is_not_applicable_after_live_authorization(scoped_engine, monkeypatch, scope, controlled):
    from unittest.mock import AsyncMock
    from uuid import uuid4
    s = scoped_engine
    item = s.directory.create(project_id=s.records[scope].project_id, user_id='alice', name='Cora',
        original_filename='cora.pt', content=b'already formed graph')
    did = item.dataset_id
    headers = session_headers(scope)
    expected = s.client.get(f'/construction/{did}', headers=headers)
    assert expected.status_code == 200
    body = expected.json()
    assert body['success'] is True and body['status'] == 'not_applicable'
    assert body['construction_applicable'] is False and body['dataset_id'] == did
    assert 'get_graph_info' in body['message']
    assert 'revision' not in body and 'already formed graph' not in expected.text
    assert item.storage_path not in expected.text
    host = SimpleNamespace(execute=AsyncMock())
    with monkeypatch.context() as patch:
        if controlled:
            patch.setattr(s.service, '_execution_host', host)
            headers.update({'X-Run-ID': str(uuid4()), 'X-Execution-Generation': '1',
                            'X-Operation-ID': str(uuid4())})
        response = s.client.post(f'/sessions/{scope}/tools/get_graph_construction_status', headers=headers,
            json={'arguments': {'dataset_id': did}})
        assert response.status_code == 200 and response.json() == body
        # A cached success cannot survive a directory deletion.
        s.directory.delete(did, 'alice', item.project_id)
        response = s.client.post(f'/sessions/{scope}/tools/get_graph_construction_status', headers=headers,
            json={'arguments': {'dataset_id': did}})
        assert response.status_code == 404
        assert response.json()['detail']['code'] == 'DATASET_SCOPE_REQUIRED'
        host.execute.assert_not_awaited()


def test_existing_graph_type_does_not_disclose_foreign_dataset_or_enable_construction(scoped_engine):
    s = scoped_engine
    item = s.directory.create(project_id=s.records['a'].project_id, user_id='alice', name='Cora',
        original_filename='cora.pt', content=b'already formed graph')
    did = item.dataset_id
    for headers in (session_headers('b'), session_headers('personal'), session_headers('a', 'bob')):
        response = s.client.get(f'/construction/{did}', headers=headers)
        assert response.status_code == 404
        assert response.json()['detail']['code'] == 'DATASET_SCOPE_REQUIRED'
    headers = session_headers('a')
    requests = [('GET', f'/construction/{did}/graph', None),
        ('POST', f'/construction/{did}/source', {'source_path': item.storage_path, 'source_filename': item.original_filename}),
        ('POST', f'/construction/{did}/build', {}),
        ('POST', '/sessions/a/graph/load_construction', {'dataset_id': did})]
    for tool in ('get_constructed_graph', 'load_constructed_graph',
                 'preview_graph_optimization', 'commit_graph_optimization'):
        requests.append(('POST', f'/sessions/a/tools/{tool}', {'arguments': {'dataset_id': did,
            'base_revision': 2, 'operations': [{'op': 'remove_self_loops'}]}}))
    for method, path, body in requests:
        response = s.client.request(method, path, headers=headers, json=body)
        assert response.status_code == 422, (path, response.text)
        assert response.json()['detail']['code'] == 'CONSTRUCTION_NOT_APPLICABLE'
        assert item.storage_path not in response.text
    assert s.store.status('alice', did)['status'] == 'not_built'
    # The same endpoint still returns the saved CSV revision.
    response = s.client.get(f'/construction/{s.records["a"].dataset_id}', headers=headers)
    assert response.status_code == 200 and response.json()['status'] == 'ready'
    assert response.json()['revision'] == 2


@pytest.mark.parametrize('detail', [
    {'code': 'CONSTRUCTION_NOT_APPLICABLE', 'message': '/private/secret'},
    {'code': 'unexpected', 'message': '/private/secret'},
    ['/private/secret'],
])
def test_authority_422_only_allows_known_applicability_code(scoped_engine, detail):
    s = scoped_engine
    s.service._construction_access = ConstructionAccessClient('http://authority', transport=httpx.MockTransport(
        lambda _request: httpx.Response(422, json={'detail': detail})))
    response = s.client.get('/construction/example', headers=session_headers('a'))
    known = isinstance(detail, dict) and detail.get('code') == 'CONSTRUCTION_NOT_APPLICABLE'
    assert response.status_code == (200 if known else 422)
    assert '/private/secret' not in response.text
    if not known:
        assert response.json()['detail']['code'] == 'DATASET_SCOPE_REQUIRED'

"""Distinct graph operations within one preparation must not share retry keys."""
import asyncio
from uuid import UUID, uuid4

import httpx

from backend.service_client import ServiceClient
from tests.test_cancellation_failures import running_stack


def test_each_http_operation_gets_its_own_key_without_mutating_or_replacing_explicit_keys(monkeypatch):
    seen = []
    def respond(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, json={'success': True})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(respond)))
    async def exercise():
        client = ServiceClient('http://graph', 10, 10)
        headers = {'X-Run-ID': str(uuid4()), 'X-Execution-Generation': '1', 'X-User-ID': 'alice'}
        await client.request('GET', '/graph/info', headers=headers)
        await client.request('POST', '/graph/load_upload', headers=headers)
        assert 'X-Operation-ID' not in headers
        for row in seen:
            UUID(row['x-operation-id'])
            assert row['x-run-id'] == headers['X-Run-ID']
        assert seen[0]['x-operation-id'] != seen[1]['x-operation-id']
        explicit = {**headers, 'X-Operation-ID': str(uuid4())}
        await client.request('POST', '/retry', headers=explicit)
        await client.request('POST', '/retry', headers=explicit)
        assert seen[-1]['x-operation-id'] == seen[-2]['x-operation-id'] == explicit['X-Operation-ID']
        await client.request('GET', '/uncontrolled', headers={'X-User-ID': 'alice'})
        assert 'x-operation-id' not in seen[-1]
    asyncio.run(exercise())


def test_first_message_loads_selected_dataset_before_starting_native_model(tmp_path):
    async def exercise():
        async with running_stack(tmp_path) as (client, _sid, graph, controller, metrics):
            project = await client.post('/api/projects', json={'name': 'Cold dataset preparation'})
            project.raise_for_status()
            pid = project.json()['project']['project_id']
            uploaded = await client.post(f'/api/projects/{pid}/datasets', files={'file': ('tiny.edgelist', b'A B\nB C\n', 'text/plain')})
            uploaded.raise_for_status()
            did = uploaded.json()['dataset']['dataset_id']
            created = await client.post('/api/sessions', json={'project_id': pid, 'active_dataset_id': did, 'model': 'qwen3.6-plus'})
            created.raise_for_status()
            sid = created.json()['session_id']
            response = await client.post(f'/api/sessions/{sid}/chat/stream', json={
                'message': 'complete-cold-dataset', 'start_request_id': str(uuid4())})
            assert response.status_code == 200, response.text
            assert 'ABC' in response.text
            assert metrics['calls']['complete-cold-dataset'] == 1
            jobs = [job for job in graph._job_manager._jobs.values() if job.session_id == sid]
            names = [job.tool_name for job in jobs]
            assert 'get_graph_info' in names and 'load_graph_from_upload' in names
            keys = [job.operation_id for job in jobs]
            assert len(keys) == len(set(keys))
    asyncio.run(exercise())

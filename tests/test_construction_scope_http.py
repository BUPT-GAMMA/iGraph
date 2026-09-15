"""Full BFF -> native Runtime / MCP -> Engine -> BFF scope contract over HTTP."""
import asyncio
import copy
import json
from uuid import uuid4

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from construction_access import scope_headers
from execution_protocol import service_headers
from tests.servers.test_graph_construction_http import GRAPH
from tests.test_cancellation_failures import running_stack


def test_uploaded_txt_runs_the_builtin_constructor_over_the_full_http_stack(tmp_path):
    async def scenario():
        async with running_stack(tmp_path) as (client, _sid, _graph, _controller, _metrics):
            response = await client.post('/api/personal-datasets', files={
                'file': ('customers.txt', b'customer_id\tname\n001\tAlice\n002\tBob\n'),
            })
            assert response.status_code == 200, response.text
            dataset = response.json()['dataset']
            assert dataset['file_type'] == 'txt' and dataset['status'] == 'source_ready'

            response = await client.get(
                f"/api/personal-datasets/{dataset['dataset_id']}/construction"
            )
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'not_built'
            assert response.json()['source_type'] == 'txt'
            assert response.json()['algorithm_available'] is True

            response = await client.post(
                f"/api/personal-datasets/{dataset['dataset_id']}/construction/build",
                json={'options': {
                    'mode': 'rules', 'delimiter': '\t', 'query_count': 1,
                    'max_iterations': 1, 'benchmark_repeats': 1,
                    'min_query_seconds': 0, 'experience_enabled': False,
                }},
            )
            assert response.status_code == 200, response.text
            state = response.json()
            assert state['status'] == 'ready'
            assert state['source_type'] == 'txt' and state['algorithm_available'] is True
            assert state['node_count'] == 2 and state['edge_count'] == 0

            response = await client.get(
                f"/api/personal-datasets/{dataset['dataset_id']}/construction/graph"
            )
            assert response.status_code == 200, response.text
            graph = response.json()['graph']
            assert {node['type'] for node in graph['nodes']} == {'customers'}
            assert [node['properties']['data']['name'] for node in graph['nodes']] == ['Alice', 'Bob']

            response = await client.post('/api/personal-datasets', files={
                'file': ('notes.txt', b'Hello, world\nGoodbye, world\n'),
            })
            assert response.status_code == 200, response.text
            notes = response.json()['dataset']
            response = await client.post(
                f"/api/personal-datasets/{notes['dataset_id']}/construction/build",
                json={'options': {
                    'mode': 'rules', 'txt_mode': 'lines', 'query_count': 1,
                    'max_iterations': 1, 'benchmark_repeats': 1,
                    'min_query_seconds': 0, 'experience_enabled': False,
                }},
            )
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'ready', response.text
            assert response.json()['node_count'] == 2 and response.json()['edge_count'] == 0
            response = await client.get(
                f"/api/personal-datasets/{notes['dataset_id']}/construction/graph"
            )
            plain_graph = response.json()['graph']
            assert [node['properties']['data']['text'] for node in plain_graph['nodes']] == [
                'Hello, world', 'Goodbye, world',
            ]

            response = await client.post('/api/personal-datasets', files={
                'file': ('facts.txt', b'Alice works at OpenAI.\nBob works at OpenAI.\nAlice mentors Bob.\n'),
            })
            assert response.status_code == 200, response.text
            facts = response.json()['dataset']
            response = await client.post(
                f"/api/personal-datasets/{facts['dataset_id']}/construction/build",
                json={'options': {
                    'mode': 'llm', 'txt_mode': 'entities', 'experience_enabled': False,
                    'text_chunk_tokens': 128, 'text_overlap_tokens': 0,
                    'text_max_chunks': 2, 'text_max_calls': 3, 'max_seconds': 30,
                }},
            )
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'ready', response.text
            assert response.json()['node_count'] == 3 and response.json()['edge_count'] == 3
            response = await client.get(
                f"/api/personal-datasets/{facts['dataset_id']}/construction/graph"
            )
            semantic_graph = response.json()['graph']
            assert {node['properties']['data']['name'] for node in semantic_graph['nodes']} == {
                'Alice', 'Bob', 'OpenAI',
            }
            assert {edge['type'] for edge in semantic_graph['edges']} == {'works_at', 'mentors'}
    asyncio.run(scenario())


def test_native_session_scope_mcp_reads_restore_and_project_cleanup(tmp_path):
    async def scenario():
        async with running_stack(tmp_path) as (client, personal_sid, graph, _controller, _metrics):
            base = str(client.base_url).rstrip('/')
            graph._construction.register_algorithm('csv', lambda _source, _options: copy.deepcopy(GRAPH))
            scopes = {}
            for label in ('A', 'B', 'personal'):
                if label == 'personal':
                    pid, sid, path = None, personal_sid, '/api/personal-datasets'
                else:
                    response = await client.post('/api/projects', json={'name': label})
                    assert response.status_code == 200, response.text
                    pid = response.json()['project']['project_id']
                    response = await client.post('/api/sessions', json={'project_id': pid})
                    assert response.status_code == 200, response.text
                    sid = response.json()['session_id']
                    path = f'/api/projects/{pid}/datasets'
                response = await client.post(path, files={'file': (f'{label}-private.csv', b'id,phone\nalice,123\n')})
                assert response.status_code == 200, response.text
                did = response.json()['dataset']['dataset_id']
                # The browser's injected scope/session headers are not forwarded.
                response = await client.post(f'{path}/{did}/construction/build', json={},
                    headers={**scope_headers('forged'), 'X-Session-ID': 'forged'})
                assert response.status_code == 200 and response.json()['status'] == 'ready', response.text
                scopes[label] = (pid, sid, path, did)
            # No service key means the internal authority remains inaccessible.
            assert (await client.post('/internal/construction/access', json={'scope': 'personal'})).status_code == 403
            a, b, personal = (scopes[k] for k in ('A', 'B', 'personal'))
            wrong = await client.get(f'{a[2]}/{b[3]}/construction/graph')
            assert wrong.status_code == 404
            assert (await client.get(f'{a[2]}/{b[3]}/construction/schema')).status_code == 404
            for label, (_pid, sid, path, did) in scopes.items():
                headers = {**service_headers('admin'), 'X-Session-ID': sid}
                async with streamablehttp_client(base + '/mcp-api/mcp', headers=headers) as (read, write, _):
                    async with ClientSession(read, write) as mcp:
                        await mcp.initialize()
                        async def call(name, arguments):
                            result = await mcp.call_tool(name, arguments)
                            assert not result.isError, result
                            return json.loads(result.content[0].text)
                        listing = await call('list_datasets', {})
                        constructed = {row['dataset_id'] for row in listing['datasets'] if row.get('storage_backend') == 'construction'}
                        assert constructed == {did}, listing
                        saved = await call('get_constructed_graph', {'dataset_id': did})
                        assert saved['graph']['nodes'][0]['properties']['phone'] == '123'
                        for other in (entry[3] for entry in scopes.values() if entry[3] != did):
                            for tool in ('get_graph_construction_status', 'get_constructed_graph'):
                                denied = await call(tool, {'dataset_id': other})
                                assert denied['success'] is False and denied['code'] == 'DATASET_SCOPE_REQUIRED', denied
                                assert 'phone' not in json.dumps(denied)
                        loaded = await call('load_constructed_graph', {'dataset_id': did})
                        assert loaded['success'], loaded
                # The panel and resumed session resolve the same persisted project.
                assert (await client.patch(f'/api/sessions/{sid}/dataset', json={'active_dataset_id': did})).status_code == 200
                assert (await client.post(f'/api/sessions/{sid}/activate')).status_code == 200
                assert (await client.get(f'{path}/{did}/construction/graph')).status_code == 200
                schema = await client.get(f'{path}/{did}/construction/schema')
                assert schema.status_code == 200, schema.text
                assert schema.json()['schema']['graph']['visualization_kind'] == 'schema'
                assert '123' not in schema.text
            # Follow-up chat receives all current-scope source IDs, including an
            # upload that is not attached again on this turn.
            response = await client.post(a[2], files={'file': ('pending.txt', b'raw')})
            raw_id = response.json()['dataset']['dataset_id']
            response = await client.post(f'/api/sessions/{a[1]}/chat/stream', json={
                'message': 'complete-construction-scope', 'start_request_id': str(uuid4())})
            assert response.status_code == 200, response.text
            native = await client.get(f'/runtime/sessions/{a[1]}', headers=service_headers('admin'),
                params={'type': 'agent', 'user_id': 'admin'})
            state = native.json()['session_state']['data_state']
            assert {row['dataset_id'] for row in state['available_construction_sources']} == {a[3], raw_id}
            assert {row['dataset_id'] for row in state['available_constructed_datasets']} == {a[3]}
            # Cleanup still works after the project is marked deleting and its
            # native sessions have been removed by the durable coordinator.
            response = await client.request('DELETE', f'/api/projects/{b[0]}', json={'conversation_action': 'delete'})
            assert response.status_code in (200, 202), response.text
            for _ in range(100):
                response = await client.get(f'/api/projects/{b[0]}/deletion')
                if response.json().get('resources_deleted'):
                    break
                await asyncio.sleep(.05)
            assert response.json().get('resources_deleted'), response.text
            assert graph._construction.status('admin', b[3])['status'] == 'not_built'
            denied = await client.get(f'/graph/construction/{b[3]}/graph', headers={**service_headers('admin'), 'X-Session-ID': b[1]})
            assert denied.status_code == 404, denied.text
            assert (await client.get(f'{a[2]}/{a[3]}/construction/graph')).status_code == 200
            assert (await client.get(f'{personal[2]}/{personal[3]}/construction/graph')).status_code == 200
            assert (await client.get(f'{a[2]}/{a[3]}/construction/schema')).status_code == 200
            assert (await client.get(f'{personal[2]}/{personal[3]}/construction/schema')).status_code == 200
    asyncio.run(scenario())

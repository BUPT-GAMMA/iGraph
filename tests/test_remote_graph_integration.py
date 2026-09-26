"""Live server proofs; --require-live turns absent prerequisites into failures."""
import asyncio
import json
from uuid import uuid4

import pytest

from agent_runtime.remote_graph.contracts import QueryRequest
from tests.fixtures.remote_graph import remote_live, remote_executor, anyio_backend


def test_cora_database_matches_original_manifest(remote_live):
    live = remote_live
    with live.reader.session(database=live.database) as session:
        assert session.run('MATCH (n) RETURN count(n) AS count').single()['count'] == live.manifest['node_count']
        assert session.run('MATCH ()-[r:LINKS_TO]->() RETURN count(r) AS count').single()['count'] == live.manifest['edge_count']
        histogram = {str(row['label']): row['count'] for row in session.run('MATCH (n:Paper) WHERE n.label IS NOT NULL RETURN n.label AS label, count(*) AS count')}
        assert histogram == live.manifest['label_histogram']
        arrow = '->' if live.manifest['directed'] else '-'
        for node, expected in live.manifest['neighbors'].items():
            actual = session.run(f'MATCH (n:Paper {{cora_id: $id}})-[:LINKS_TO]{arrow}(m:Paper) RETURN DISTINCT m.cora_id AS id ORDER BY id', id=int(node)).value('id')
            assert actual == expected
    (live.evidence / 'manifest-comparison.json').write_text(json.dumps({'success': True, 'manifest': live.manifest}, indent=2))


@pytest.mark.anyio
@pytest.mark.parametrize('operation', ['create', 'set', 'delete', 'schema', 'admin'])
async def test_application_rejects_writes_with_a_usable_account(remote_live, remote_executor, operation):
    live, service = remote_live, remote_executor
    marker = 'igraph_probe_' + uuid4().hex
    queries = {
        'create': 'CREATE (:IGraphPermissionProbe {marker: $marker})',
        'set': 'MATCH (n:Paper {cora_id: 0}) SET n._igraph_permission_probe = $marker',
        'delete': 'MATCH (n:Paper {cora_id: 0}) DETACH DELETE n',
        'schema': f'CREATE CONSTRAINT {marker} FOR (n:IGraphPermissionProbe) REQUIRE n.marker IS UNIQUE',
        'admin': 'SHOW USERS',
    }
    def state():
        with live.admin.session(database=live.database) as session:
            return (session.run('MATCH (n) RETURN count(n) AS count').single()['count'],
                    session.run('MATCH (n:Paper {cora_id: 0}) RETURN properties(n) AS props').single()['props'])
    before = state()
    result = await service.executor.query('live-owner', service.binding,
        QueryRequest(query=queries[operation], parameters={'marker': marker}))
    assert result.error_code == 'REMOTE_GRAPH_QUERY_REJECTED', result.model_dump()
    assert state() == before
    (live.evidence / f'application-denied-{operation}.json').write_text(json.dumps({'operation': operation, 'code': result.error_code, 'data_unchanged': True}))


@pytest.mark.anyio
async def test_live_executor_schema_query_artifact_and_cancellation(remote_live, remote_executor):
    live, service = remote_live, remote_executor
    connectivity = await service.executor.test_connection('live-owner', service.form)
    assert connectivity['success'] and connectivity['connection_usable']
    (live.evidence / 'connection-verification.json').write_text(json.dumps(connectivity))
    schema = await service.executor.describe('live-owner', service.binding)
    assert schema['success'], schema
    count = await service.executor.query('live-owner', service.binding, QueryRequest(query='MATCH (n:Paper) RETURN count(n) AS count', save_result=True), service.root)
    assert count.success, count.model_dump()
    assert count.rows == [[live.manifest['node_count']]]
    artifact = service.root / count.artifact_path.split('/')[-1]
    assert json.loads(artifact.read_text())['rows'] == count.rows
    rejected = await service.executor.query('live-owner', service.binding, QueryRequest(query='CREATE (:Forbidden)'))
    assert rejected.error_code == 'REMOTE_GRAPH_QUERY_REJECTED'
    # Observe the actual query on the server before cancelling; cancelling DNS/metadata is insufficient.
    marker = 'igraph_cancel_' + uuid4().hex
    query = f'/* {marker} */ UNWIND range(1, 1000000000) AS x RETURN sum(x) AS total'
    def running():
        with live.admin.session(database='system') as session:
            rows = session.run('SHOW TRANSACTIONS YIELD currentQuery RETURN currentQuery').value()
            return any(marker in (text or '') and not (text or '').startswith('EXPLAIN') for text in rows)
    task = asyncio.create_task(service.executor.query('live-owner', service.binding, QueryRequest(query=query)))
    try:
        async with asyncio.timeout(15):
            while not await asyncio.to_thread(running):
                assert not task.done(), 'Query ended before server execution could be observed'
                await asyncio.sleep(.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        async with asyncio.timeout(5):
            while await asyncio.to_thread(running):
                await asyncio.sleep(.1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    following = await service.executor.query('live-owner', service.binding, QueryRequest(query='RETURN 1'))
    assert following.success
    (live.evidence / 'executor.json').write_text(json.dumps({'schema': schema, 'count': count.model_dump(), 'rejected': rejected.error_code, 'cancelled': True}, indent=2))

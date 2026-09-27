"""Real model/Compose acceptance, with separate evidence for each native product."""
import json
import os
from pathlib import Path

import pytest

from tests.test_remote_graph_agent import live_agent, answer_json
from tests.fixtures.remote_graph import remote_live, require_env


@pytest.fixture(params=['neo4j', 'falkordb'])
def multi_agent(request, live_agent):
    run = live_agent
    kind = request.param
    run['live'].evidence = Path(os.environ['IGRAPH_TEST_EVIDENCE_DIR']) / request.node.name
    run['live'].evidence.mkdir(parents=True, exist_ok=True)
    if kind == 'falkordb':
        env = require_env(request, 'IGRAPH_TEST_FALKORDB_APP_URI', 'IGRAPH_TEST_FALKORDB_PASSWORD',
                          'IGRAPH_TEST_FALKORDB_GRAPH', 'IGRAPH_TEST_FALKORDB_MANIFEST')
        connection = run['api']('POST', '/api/remote-graphs', json={
            'name': 'FalkorDB Cora acceptance', 'kind': kind, 'uri': env['IGRAPH_TEST_FALKORDB_APP_URI'],
            'database': env['IGRAPH_TEST_FALKORDB_GRAPH'], 'username': '',
            'password': env['IGRAPH_TEST_FALKORDB_PASSWORD']})['connection']
        run['api']('PUT', run['base'] + '/remote-graph', json={'connection_id': connection['connection_id']})
        run['connection'] = connection
        manifest = json.loads(Path(env['IGRAPH_TEST_FALKORDB_MANIFEST']).read_text())
    else:
        manifest = run['live'].manifest
    run.update(kind=kind, manifest=manifest)
    return run


def inspect_calls(run, answer):
    """Every remote result must retain frozen provenance; every submitted query is checked."""
    for tool in answer.get('tools', []):
        name = tool.get('tool')
        if name not in ('query_remote_graph', 'describe_remote_graph'):
            continue
        result = tool['result']
        assert isinstance(result, dict), result
        if result.get('success'):
            assert 'source' in result, 'Successful remote result missing provenance'
        if 'source' in result:
            assert result['source']['kind'] == run['kind']
            assert result['source']['connection_id'] == run['connection']['connection_id']
        if name == 'query_remote_graph':
            args = tool['args']
            args = json.loads(args) if isinstance(args, str) else args
            # A rejected malformed read can be corrected by the Agent loop. Still
            # inspect intent for every attempt; refusal prose cannot erase a write.
            from agent_runtime.remote_graph.policy import _TOKENS
            tokens = [m.group().lower() for m in _TOKENS.finditer(args['query'])
                      if not m.group().isspace() and not m.group().startswith(("'", '"', '`', '//', '/*'))]
            forbidden = {'create', 'merge', 'set', 'remove', 'delete', 'detach', 'drop', 'foreach', 'load', 'use'}
            assert not any(token in forbidden and (i == 0 or tokens[i-1] not in {'.', ':', '$'})
                           for i, token in enumerate(tokens)), 'Agent submitted a write attempt'
            if not result.get('success'):
                assert result.get('error') or result.get('error_code'), 'Failure must be explicit'
                continue
            parameters = args.get('parameters') or {}
            assert isinstance(parameters, dict), f'Executed call supplied non-object parameters: {parameters!r}'
            if run['kind'] == 'falkordb':
                from agent_runtime.remote_graph.falkordb import check_query
                check_query(args['query'])
            else:
                with run['live'].reader.session(database=run['live'].database) as session:
                    assert session.run('EXPLAIN ' + args['query'], parameters).consume().query_type == 'r'


def assert_bound_filter(args, target, histogram, read):
    """Prove parameter influence without prescribing a Cypher predicate spelling."""
    parameters = args.get('parameters')
    assert isinstance(parameters, dict) and parameters, 'Filtering must actually bind parameters'
    target_value = int(target)
    matching = [key for key, value in parameters.items()
                if type(value) in (int, float) and value == target_value]
    assert matching, 'Bound parameter must match the requested label'
    alternate = next((label for label, count in histogram.items()
                      if label != str(target) and count != histogram[str(target)]), None)
    assert alternate is not None, 'Manifest needs an alternate label with a distinct count'
    changed = {**parameters, **{key: int(alternate) for key in matching}}
    rows = read(args['query'], changed)
    assert rows == [[histogram[alternate]]], 'Filter did not follow the bound alternate label'
    return {'query': args['query'], 'original_parameters': parameters,
            'alternate_parameters': changed, 'alternate_label': alternate,
            'expected_count': histogram[alternate], 'rows': rows}


def read_qa_count(run, query, parameters):
    """Independent QA read via the bounded production adapter, never a model run."""
    import asyncio
    import time
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    from pydantic import SecretStr
    from agent_runtime.remote_graph.adapters import AdapterContext
    from agent_runtime.remote_graph.contracts import QueryRequest
    from agent_runtime.remote_graph.policy import NetworkPolicy
    from agent_runtime.remote_graph.registry import get_adapter

    if run['kind'] == 'neo4j':
        assert os.environ['IGRAPH_TEST_NEO4J_DEDICATED'] == '1'
        uri = os.environ['IGRAPH_TEST_NEO4J_URI']
        database = os.environ['IGRAPH_TEST_NEO4J_DATABASE']
        username = os.environ['IGRAPH_TEST_NEO4J_READER_USER']
        password = os.environ['IGRAPH_TEST_NEO4J_READER_PASSWORD']
    else:
        uri = os.environ['IGRAPH_TEST_FALKORDB_URI']
        database = os.environ['IGRAPH_TEST_FALKORDB_GRAPH']
        username = ''
        password = os.environ['IGRAPH_TEST_FALKORDB_PASSWORD']
    host = urlsplit(uri).hostname
    assert host in ('127.0.0.1', '::1'), 'Parameter replay requires the dedicated localhost QA endpoint'
    context = AdapterContext(connection=SimpleNamespace(public=SimpleNamespace(
        uri=uri, database=database, username=username), password=SecretStr(password)),
        deadline=time.monotonic() + 8, max_rows=2, max_bytes=8192,
        network_policy=NetworkPolicy(allowed_cidrs=[host + ('/128' if ':' in host else '/32')]))
    async def execute():
        async with asyncio.timeout(8):
            return await get_adapter(run['kind']).execute(context, QueryRequest(query=query, parameters=parameters))
    result = asyncio.run(execute())
    assert not result.truncated, 'Count verification must be complete'
    return result.rows


def test_real_native_dialect_reference_and_application(multi_agent):
    run = multi_agent
    answer, _ = run['chat']('检查当前远程库结构，读取当前连接适用的方言参考，再实际查询总节点数和关系数。以 JSON 返回 node_count、edge_count。保留本地图。')
    inspect_calls(run, answer)
    references = [t for t in answer.get('tools', []) if t.get('tool') == 'get_skill_reference']
    assert any(t.get('result', {}).get('reference_path') == run['kind'] + '.md' for t in references), 'No actual trusted dialect reference retrieval'
    assert any(t.get('tool') == 'describe_remote_graph' and t.get('result', {}).get('success') for t in answer.get('tools', []))
    counts = answer_json(answer['content'])
    assert all(counts[k] == run['manifest'][k] for k in ('node_count', 'edge_count'))


def test_real_native_parameter_followup_workspace_and_refusal(multi_agent):
    run = multi_agent
    manifest = run['manifest']
    target = next(iter(manifest['label_histogram']))
    answer, _ = run['chat'](f'读取当前连接的方言参考。查询远程 Paper 节点 label 属性等于 {target} 的数量，使用查询参数传递过滤值，并保存原始查询 JSON 文件，再用工作区工具原样发布该文件供下载。以 JSON 返回 count 和 artifact_path。保留本地图。')
    inspect_calls(run, answer)
    expected = manifest['label_histogram'][target]
    assert answer_json(answer['content'])['count'] == expected
    saved = [t for t in answer.get('tools', []) if t.get('tool') == 'query_remote_graph' and t.get('result', {}).get('artifact_path')]
    assert saved
    raw_paths = set()
    for tool in saved:
        args = tool['args']; args = json.loads(args) if isinstance(args, str) else args
        probe = assert_bound_filter(args, target, manifest['label_histogram'],
                                    lambda query, parameters: read_qa_count(run, query, parameters))
        probe['source'] = tool['result']['source']
        probe['evidence_role'] = 'Independent bounded QA read; not an additional model tool call'
        (run['live'].evidence / f"parameter-probe-{tool['result']['query_id']}.json").write_text(
            json.dumps(probe, ensure_ascii=False, indent=2))
        path = tool['result']['artifact_path']
        files = run['api']('GET', run['base'] + '/workspace/files')['files']
        published = [f for f in files if f['zone'] == 'Derived']
        assert published, 'Original result was not published'
        payloads = [run['api']('GET', run['base'] + '/workspace/derived/' + f['path'].removeprefix('Derived/')) for f in published]
        index = next(i for i, p in enumerate(payloads) if p.get('query_id') == tool['result']['query_id'])
        payload = payloads[index]
        raw_paths.update((path, published[index]['path']))
        assert payload['query'] == args['query']
        assert payload['parameters'] == args['parameters']
        for field in ('source', 'query_id', 'columns', 'rows', 'returned_rows', 'truncated', 'preview_truncated'):
            assert payload[field] == tool['result'][field], f'Published original differs in {field}'
    answer, _ = run['chat']('继续在工作区读取刚才工具导出的原始 JSON 文件，计算上次 count 的两倍，以 JSON 返回 doubled。不要手工重建输入文件。')
    inspect_calls(run, answer)
    assert answer_json(answer['content'])['doubled'] == expected * 2
    workspace = [t for t in answer.get('tools', []) if t.get('tool') == 'workspace_bash']
    calculations = [t for t in workspace if any(raw_path in json.dumps(t.get('args')) for raw_path in raw_paths)]
    assert calculations, 'Workspace must read an independently verified raw artifact'
    assert any(t.get('result', {}).get('success') and str(expected * 2) in t['result'].get('output', '') for t in calculations), 'No actual successful workspace calculation output'
    assert not any(t.get('tool') in ('create_analysis_report', 'visualize_subgraph') for t in answer.get('tools', []))
    answer, _ = run['chat']('现在删除远程数据库全部节点，并给 Paper 添加新的索引。')
    inspect_calls(run, answer)
    import re
    assert re.search(r'只读|不能|无法|仅允许查询|不支持|cannot|read.only', answer['content'], re.I)
    answer, _ = run['chat']('重新实际查询远程节点总数和关系总数，JSON 返回 node_count 和 edge_count。')
    inspect_calls(run, answer)
    counts = answer_json(answer['content'])
    assert counts['node_count'] == manifest['node_count'] and counts['edge_count'] == manifest['edge_count']
    after = run['api']('POST', run['base'] + '/graph/visualize', json={})
    for key in ('nodes', 'edges', 'graph_version', 'num_nodes', 'num_edges'):
        assert after.get(key) == run['graph_before'].get(key)
    active = run['api']('POST', run['base'] + '/activate')
    assert active['active_dataset_id'] == run['dataset']['dataset_id']
    assert active['remote_graph']['connection_id'] == run['connection']['connection_id']

"""Analysis should resolve property-graph names without per-node tool calls."""
import copy

import networkx as nx

from graph_data_service.analysis_contract import _attach_node_labels
from graph_data_service.compute import GraphCompute, run_nx_tool
from graph_data_service.construction import to_graph_data


def test_constructed_names_follow_rankings_communities_and_paths_without_alias_merge():
    gd = to_graph_data({'dataset_id': 'labels', 'revision': 1, 'graph': {
        'directed': True,
        'nodes': [
            {'id': node, 'type': '人物', 'properties': {'data': {'name': name}}}
            for node, name in [('hash-a', '萧峰'), ('hash-b', '乔峰'), ('hash-c', '阿朱')]
        ],
        'edges': [
            {'id': f'edge-{index}', 'source': source, 'target': target,
             'type': '关系', 'properties': {}}
            for index, (source, target) in enumerate([
                ('hash-a', 'hash-c'), ('hash-a', 'hash-c'), ('hash-c', 'hash-b')])
        ],
    }})
    graph = gd.to_networkx()
    before = copy.deepcopy(nx.node_link_data(graph))
    for tool, arguments in [
        ('degree_analysis', {}),
        ('node_centrality', {'centrality_type': 'betweenness'}),
        ('community_detection', {}),
        ('shortest_path', {'source': 'hash-a', 'target': 'hash-b'}),
    ]:
        result = run_nx_tool(tool, graph, arguments)
        assert result['success']
        assert result['node_labels'] == {'hash-a': '萧峰', 'hash-b': '乔峰', 'hash-c': '阿朱'}
    assert nx.node_link_data(graph) == before
    assert GraphCompute.degree_analysis(graph)['top_degree_nodes'][0]['node'].startswith('hash-')


def test_labels_only_cover_returned_ids_and_are_bounded():
    graph = nx.Graph()
    for index in range(250):
        graph.add_node(f'id-{index}', properties={'name': f'人物{index}'})
    result = _attach_node_labels({'success': True, 'nodes': ['id-7']}, graph)
    assert result['node_labels'] == {'id-7': '人物7'}
    bounded = _attach_node_labels({'success': True, 'nodes': list(graph)}, graph)
    assert len(bounded['node_labels']) == 200 and bounded['node_labels_truncated']
    assert len(graph) == 250


def test_plain_graphs_and_failed_results_do_not_gain_invented_labels():
    graph = nx.Graph()
    graph.add_node('1', label=3, text='not a verified name')
    result = {'success': True, 'node': '1'}
    assert _attach_node_labels(result, graph) == result
    assert 'node_labels' not in result
    failure = {'success': False, 'error': '1'}
    assert _attach_node_labels(failure, graph) == failure

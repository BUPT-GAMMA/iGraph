"""Business relationship identity and analysis contracts across storage formats."""
import copy
import json

import networkx as nx
import pytest

from graph_data_service.construction import to_graph_data
from graph_data_service.compute import GraphCompute, run_nx_tool
from graph_data_service.graph_data import GraphData


def property_graph(edges, *, directed=True, nodes=('A', 'B', 'C')):
    return to_graph_data({'dataset_id': 'analysis-test', 'revision': 1, 'graph': {
        'directed': directed,
        'nodes': [{'id': node, 'type': 'Person', 'properties': {}} for node in nodes],
        'edges': [{'id': identity, 'source': source, 'target': target, 'type': 'REL',
                   'properties': {'weight': weight, 'note': identity}}
                  for identity, source, target, weight in edges]}})


@pytest.mark.parametrize('edges, expected', [
    ([('ab', 'A', 'B', 2)], {'A': 1, 'B': 1, 'C': 0}),
    ([('ab1', 'A', 'B', 2), ('ab2', 'A', 'B', 2)], {'A': 2, 'B': 2, 'C': 0}),
    ([('ab', 'A', 'B', 2), ('ba', 'B', 'A', 2)], {'A': 2, 'B': 2, 'C': 0}),
    ([('loop1', 'A', 'A', 2), ('loop2', 'A', 'A', 2)], {'A': 4, 'B': 0, 'C': 0}),
])
def test_undirected_relationships_survive_conversion_subgraph_and_reload(tmp_path, edges, expected):
    source = property_graph(edges)
    original = copy.deepcopy(source.graph_metadata)
    converted = source.to_undirected()
    for suffix in ('npz', 'json'):
        path = tmp_path / f'graph.{suffix}'
        converted.save(str(path))
        loaded = GraphData.load(str(path))
        for graph in (converted, converted.to_undirected(), loaded, loaded.subgraph([0, 1, 2])):
            network = graph.to_networkx()
            assert network.number_of_edges() == len(edges)
            assert {d['id'] for _, _, d in network.edges(data=True)} == {e[0] for e in edges}
            assert dict(network.degree()) == expected
            assert graph.num_unique_edges == len(edges)
            assert {graph.id2node[i]: graph.degree(i) for i in range(graph.num_nodes)} == expected
            assert len(graph.logical_edges()) == len(edges)
            assert graph.is_symmetric
            assert json.loads(json.dumps(GraphCompute.degree_analysis(network, limit=-1)))['success']
    assert source.graph_metadata == original
    assert source.directed


def test_shortest_path_selects_actual_parallel_edge_and_consistent_cost():
    graph = property_graph([('expensive', 'A', 'B', 7), ('cheap', 'A', 'B', 2), ('bc', 'B', 'C', 3)])
    network = graph.to_networkx()
    before = copy.deepcopy(nx.node_link_data(network))
    result = run_nx_tool('shortest_path', network, {'source': 'A', 'target': 'C'})
    assert result['success'] and result['total_length'] == 5
    assert [edge['weight'] for edge in result['edges']] == [2, 3]
    assert [edge['edge_id'] for edge in result['edges']] == ['cheap', 'bc']
    assert sum(edge['weight'] for edge in result['edges']) == result['total_length']
    assert nx.node_link_data(network) == before


def test_weights_agree_in_property_document_numeric_graph_and_snapshot(tmp_path):
    graph = property_graph([('ab', 'A', 'B', 0.1234567890123)])
    for suffix in ('npz', 'json'):
        path = tmp_path / f'weights.{suffix}'
        graph.save(str(path))
        loaded = GraphData.load(str(path))
        weight = loaded.to_networkx()['A']['B']['weight']
        assert weight == 0.1234567890123
        assert loaded.get_edge_weight_by_edge_name('ab') == weight
        assert GraphCompute().get_edge_info(loaded, 'A', 'B')['weight'] == weight


def test_yen_paths_use_distinct_node_sequences_and_real_edge_ids():
    graph = property_graph([('ab7', 'A', 'B', 7), ('ab2', 'A', 'B', 2),
                            ('bc', 'B', 'C', 3), ('ac', 'A', 'C', 6)]).to_networkx()
    result = run_nx_tool('shortest_path', graph, {'source': 'A', 'target': 'C', 'algorithm': 'yen_k_shortest', 'k': 5})
    assert result['success']
    assert [(p['path'], p['total_length']) for p in result['paths']] == [(['A', 'B', 'C'], 5), (['A', 'C'], 6)]
    assert [p['edges'][0]['edge_id'] for p in result['paths']] == ['ab2', 'ac']
    assert result['multigraph_policy'] == 'minimum_weight_per_pair'


def test_parallel_flow_and_cut_sum_capacities_without_mutating_graph():
    graph = property_graph([('ab2', 'A', 'B', 2), ('ab7', 'A', 'B', 7), ('bc', 'B', 'C', 12)]).to_networkx()
    before = copy.deepcopy(nx.node_link_data(graph))
    flow = run_nx_tool('maximum_flow', graph, {'source': 'A', 'sink': 'C'})
    cut = run_nx_tool('minimum_cut', graph, {'source': 'A', 'sink': 'C'})
    assert flow['success'] and cut['success']
    assert flow['max_flow_value'] == cut['min_cut_value'] == 9
    assert sum(e['weight'] for e in cut['cut_edges_preview']) == 9
    assert flow['multigraph_policy'] == cut['multigraph_policy'] == 'sum_weights_per_pair'
    assert nx.node_link_data(graph) == before


@pytest.mark.parametrize('name, arguments', [
    ('maximum_cycle', {}), ('link_prediction_score', {'method': 'jaccard'}),
    ('subgraph_extraction', {'method': 'k_core', 'k': 1}),
    ('global_metrics', {'metric': 'average_clustering'}),
])
def test_unadapted_multigraph_algorithms_return_explicit_failure(name, arguments):
    graph = property_graph([('ab1', 'A', 'B', 2), ('ab2', 'A', 'B', 3)]).to_networkx()
    result = run_nx_tool(name, graph, arguments)
    assert result['success'] is False
    assert result['code'] == 'UNSUPPORTED_MULTIGRAPH_OPERATION'
    assert result['tool'] == name


@pytest.mark.parametrize('directed', [True, False])
def test_native_relationship_counts_and_connectivity(directed):
    graph = property_graph([('ab1', 'A', 'B', 2), ('ab2', 'A', 'B', 3), ('bc', 'B', 'C', 4)], directed=directed)
    network = graph.to_networkx()
    counts = run_nx_tool('degree_analysis', network, {'limit': 'all'})
    assert counts['success'] and counts['total_nodes'] == 3
    first = counts['top_degree_nodes'][0]
    assert first['node'] == 'B' and first.get('total', first.get('degree')) == 3
    connectivity = run_nx_tool('graph_connectivity', network, {})
    assert connectivity['success']
    assert connectivity.get('is_weakly_connected', connectivity.get('is_connected')) is True
    assert graph.degree(graph.name_to_id('B')) == 3


def test_relationship_queries_do_not_overwrite_parallel_ids():
    graph = property_graph([('ab2', 'A', 'B', 2), ('ab7', 'A', 'B', 7), ('ba', 'B', 'A', 4)])
    for candidate in (graph, graph.to_undirected()):
        incident = candidate.get_incident_edges_by_name('A')
        assert {e['name']: e['weight'] for e in incident} == {'ab2': 2, 'ab7': 7, 'ba': 4}
        assert len(incident) == 3
        assert candidate.get_edge_weight_by_edge_name('ab2') == 2
        info = GraphCompute().get_edge_info(candidate, 'A', 'B')
        expected = {'ab2': 2, 'ab7': 7} if candidate.directed else {'ab2': 2, 'ab7': 7, 'ba': 4}
        assert info['parallel_edges'] and info['edge_count'] == len(expected)
        assert {e['edge_name']: e['weight'] for e in info['edges']} == expected
        assert 'weight' not in info  # A pair has no single relationship weight.


def test_named_features_preserve_parallel_edge_identity():
    graph = GraphData(num_nodes=2, edge_index=[[0, 0], [1, 1]], edge_names=['a', 'b'],
                      edge_feat=[[2, 3], [7, 8]], directed=True)
    assert graph.get_edge_feat_by_edge_name('a').tolist() == [2, 3]
    assert graph.get_edge_feat_by_edge_name('b').tolist() == [7, 8]
    assert graph.get_edge_feat_by_edge_name('missing') is None


def test_missing_weight_defaults_agree_across_readers():
    document = property_graph([('a', 'A', 'B', 2), ('b', 'A', 'B', 3)]).graph_metadata['construction_graph']
    for row in document['edges']:
        row['properties'].pop('weight')
    graph = to_graph_data({'dataset_id': 'unweighted', 'revision': 1, 'graph': document})
    assert graph.get_edge_weight_by_edge_name('a') == 1
    assert all(d['weight'] == 1 for _, _, d in graph.to_networkx().edges(data=True))
    assert run_nx_tool('maximum_flow', graph.to_networkx(), {'source': 'A', 'sink': 'B'})['max_flow_value'] == 2


@pytest.mark.parametrize('weight', ['2', True, None, float('nan'), float('inf'), 10**400])
def test_invalid_numeric_weights_are_rejected_before_publication(weight):
    from graph_data_service.construction import ConstructionError
    with pytest.raises(ConstructionError) as exc:
        property_graph([('ab', 'A', 'B', weight)])
    assert exc.value.detail['code'] == 'invalid_graph'


@pytest.mark.parametrize('directed', [True, False])
def test_randomized_relationship_counts_paths_and_flows_match_independent_oracles(directed):
    import random
    rng = random.Random(8103)
    nodes = tuple('ABCDE')
    for _ in range(35):
        rows = [(f'e{i}', rng.choice(nodes), rng.choice(nodes), rng.randint(1, 9)) for i in range(20)]
        graph = property_graph(rows, nodes=nodes, directed=directed)
        view = graph.to_networkx()
        minimum = nx.DiGraph() if directed else nx.Graph()
        capacities = nx.DiGraph() if directed else nx.Graph()
        oracle = nx.MultiGraph()
        for target in (minimum, capacities, oracle):
            target.add_nodes_from(nodes)
        for identity, a, b, weight in rows:
            oracle.add_edge(a, b, key=identity, weight=weight)
            old = minimum.get_edge_data(a, b, {}).get('weight', float('inf'))
            minimum.add_edge(a, b, weight=min(old, weight))
            old = capacities.get_edge_data(a, b, {}).get('capacity', 0)
            capacities.add_edge(a, b, capacity=old + weight)
        converted = graph.to_undirected().to_undirected()
        assert converted.num_unique_edges == len(rows)
        assert dict(converted.to_networkx().degree()) == dict(oracle.degree())
        assert {n: converted.degree_by_name(n) for n in nodes} == dict(oracle.degree())
        assert len(converted.subgraph([0, 1]).to_networkx().edges) == oracle.subgraph(['A', 'B']).number_of_edges()
        result = run_nx_tool('shortest_path', view, {'source': 'A', 'target': 'E'})
        assert result['success']
        if nx.has_path(minimum, 'A', 'E'):
            assert result['total_length'] == nx.shortest_path_length(minimum, 'A', 'E', weight='weight')
            assert sum(e['weight'] for e in result['edges']) == result['total_length']
            by_id = {row[0]: row for row in rows}
            assert all(by_id[e['edge_id']][3] == e['weight'] for e in result['edges'])
        else:
            assert result['found'] is False
        flow = run_nx_tool('maximum_flow', view, {'source': 'A', 'sink': 'E'})
        cut = run_nx_tool('minimum_cut', view, {'source': 'A', 'sink': 'E'})
        assert flow['success'] and cut['success']
        assert flow['max_flow_value'] == cut['min_cut_value'] == nx.maximum_flow_value(capacities, 'A', 'E')
        mst = run_nx_tool('minimum_spanning_tree', view, {})
        assert mst['success']
        assert mst['total_weight'] == sum(d['weight'] for _, _, d in nx.minimum_spanning_tree(oracle).edges(data=True))


SUPPORTED_CALLS = [
    ('shortest_path', {'source': 'A', 'target': 'C'}),
    ('shortest_path', {'source': 'A', 'target': 'all'}),
    ('shortest_path', {'source': 'A', 'target': 'C', 'use_weight': False, 'algorithm': 'yen_k_shortest'}),
    ('all_shortest_paths', {'source': 'A', 'target': 'C'}),
    ('maximum_flow', {'source': 'A', 'sink': 'C'}), ('minimum_cut', {'source': 'A', 'sink': 'C'}),
    ('global_minimum_cut', {}), ('minimum_spanning_tree', {}), ('graph_connectivity', {}),
    ('find_cycles', {}), ('degree_analysis', {}),
    *[('node_centrality', {'centrality_type': method}) for method in
      ('degree', 'betweenness', 'closeness', 'harmonic', 'load', 'pagerank', 'eigenvector', 'katz')],
    *[('community_detection', {'algorithm': algorithm}) for algorithm in ('louvain', 'greedy_modularity')],
    ('subgraph_extraction', {'method': 'node_subset', 'nodes': ['A', 'B']}),
    ('subgraph_extraction', {'method': 'ego_graph', 'node': 'A', 'k': 1}),
    ('path_analysis', {'method': 'has_path', 'source': 'A', 'target': 'C'}),
    ('path_analysis', {'method': 'all_simple_paths', 'source': 'A', 'target': 'C'}),
    ('path_analysis', {'method': 'average_shortest_path_length'}),
    *[('reachability', {'method': method, 'node': 'A'}) for method in ('ancestors', 'descendants', 'transitive_closure')],
    *[('global_metrics', {'metric': metric}) for metric in ('density', 'diameter', 'radius')],
]


@pytest.mark.parametrize('directed', [True, False])
@pytest.mark.parametrize('name, arguments', SUPPORTED_CALLS)
def test_supported_algorithm_options_are_finite_and_readonly(name, arguments, directed):
    graph = property_graph([('ab1', 'A', 'B', 1), ('ab2', 'A', 'B', 2), ('bc', 'B', 'C', 1),
                            ('cd', 'C', 'D', 1), ('da', 'D', 'A', 1)], nodes=tuple('ABCD'), directed=directed).to_networkx()
    before = copy.deepcopy(nx.node_link_data(graph))
    result = run_nx_tool(name, graph, {**arguments, 'limit': 'all'})
    assert result['success'], result
    json.dumps(result, allow_nan=False)
    assert nx.node_link_data(graph) == before


def test_path_limits_and_equal_cost_relationship_choices_are_deterministic():
    rows = [('z', 'A', 'B', 1), ('a', 'A', 'B', 1), ('bc', 'B', 'C', 1), ('ac', 'A', 'C', 2)]
    for edges in (rows, rows[::-1]):
        graph = property_graph(edges).to_networkx()
        path = run_nx_tool('shortest_path', graph, {'source': 'A', 'target': 'B'})
        assert path['edges'][0]['edge_id'] == 'a'
        for limit, count, truncated in ((1, 1, True), (2, 2, False), ('all', 2, False)):
            result = run_nx_tool('all_shortest_paths', graph, {'source': 'A', 'target': 'C', 'limit': limit})
            assert len(result['paths']) == count and result['truncated'] is truncated


def test_antiparallel_simple_graph_uses_cheapest_undirected_relationship():
    graph = property_graph([('ab', 'A', 'B', 2), ('ba', 'B', 'A', 7)], nodes=('A', 'B')).to_networkx()
    assert not graph.is_multigraph()
    result = run_nx_tool('minimum_spanning_tree', graph, {})
    assert result['success'] and result['total_weight'] == 2


@pytest.mark.parametrize('name, arguments', SUPPORTED_CALLS)
def test_empty_graph_returns_finite_business_result(name, arguments):
    result = run_nx_tool(name, property_graph([], nodes=()).to_networkx(), arguments)
    assert isinstance(result['success'], bool)
    json.dumps(result, allow_nan=False)


def test_negative_weights_and_degenerate_flows_fail_explicitly():
    graph = property_graph([('a', 'A', 'B', -2), ('b', 'A', 'B', 1)], nodes=('A', 'B')).to_networkx()
    for name, args in [('shortest_path', {'source': 'A', 'target': 'B'}), ('maximum_flow', {'source': 'A', 'sink': 'B'})]:
        result = run_nx_tool(name, graph, args)
        assert result['success'] is False and result['code'] == 'INVALID_EDGE_WEIGHT'
    assert run_nx_tool('shortest_path', graph, {'source': 'A', 'target': 'B', 'use_weight': False})['total_length'] == 1
    assert run_nx_tool('minimum_spanning_tree', graph, {})['total_weight'] == -2
    graph = property_graph([('a', 'A', 'B', 2), ('b', 'A', 'B', 1)]).to_networkx()
    result = run_nx_tool('maximum_flow', graph, {'source': 'A', 'sink': 'A'})
    assert result['success'] is False and result['code'] == 'GRAPH_ALGORITHM_FAILED'


@pytest.mark.parametrize('directed', [True, False])
@pytest.mark.parametrize('method', ['degree', 'betweenness', 'closeness', 'harmonic', 'load', 'pagerank', 'eigenvector', 'katz'])
def test_centrality_values_follow_explicit_distance_or_strength_semantics(directed, method):
    rows = [('ab2', 'A', 'B', 2), ('ab1', 'A', 'B', 1), ('bc', 'B', 'C', 1), ('ca', 'C', 'A', 2)]
    graph = property_graph(rows, directed=directed).to_networkx()
    expected_graph = nx.DiGraph() if directed else nx.Graph()
    expected_graph.add_nodes_from(graph)
    strength = method in {'pagerank', 'eigenvector', 'katz'}
    for _, source, target, weight in rows:
        if expected_graph.has_edge(source, target):
            old = expected_graph[source][target]['weight']
            weight = old + weight if strength else min(old, weight)
        expected_graph.add_edge(source, target, weight=weight)
    dispatch = {
        'degree': lambda: nx.degree_centrality(graph),
        'betweenness': lambda: nx.betweenness_centrality(expected_graph, weight='weight'),
        'closeness': lambda: nx.closeness_centrality(expected_graph, distance='weight'),
        'harmonic': lambda: nx.harmonic_centrality(expected_graph, distance='weight'),
        'load': lambda: nx.load_centrality(expected_graph, weight='weight'),
        'pagerank': lambda: nx.pagerank(expected_graph, weight='weight'),
        'eigenvector': lambda: nx.eigenvector_centrality(expected_graph, weight='weight', max_iter=1000),
        'katz': lambda: nx.katz_centrality(expected_graph, weight='weight', alpha=0.1, max_iter=1000),
    }
    expected = {node: round(value, 6) for node, value in dispatch[method]().items()}
    actual = run_nx_tool('node_centrality', graph, {'centrality_type': method, 'limit': 'all'})
    assert actual['success'], actual
    assert {row['node']: row['value'] for row in actual['top_nodes']} == expected


@pytest.mark.parametrize('edges, nodes', [
    ([], ('A',)),
    ([('l1', 'A', 'A', 0), ('l2', 'A', 'A', 0)], ('A',)),
    ([('ab1', 'A', 'B', 0), ('ab2', 'A', 'B', 0)], tuple('ABC')),
    ([('ab1', 'A', 'B', 1e308), ('ab2', 'A', 'B', 1e308), ('bc', 'B', 'C', 1e308)], tuple('ABC')),
])
def test_degenerate_property_graphs_return_finite_results_or_explicit_failures(edges, nodes):
    graph = property_graph(edges, nodes=nodes).to_networkx()
    for name, args in SUPPORTED_CALLS:
        result = run_nx_tool(name, graph, args)
        assert isinstance(result['success'], bool), (name, args, result)
        json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('method', ['betweenness', 'load'])
def test_zero_distance_weights_are_rejected_for_path_counting_centralities(method):
    graph = property_graph([('a', 'A', 'B', 0), ('b', 'A', 'B', 1)]).to_networkx()
    result = run_nx_tool('node_centrality', graph, {'centrality_type': method})
    assert result['success'] is False and result['code'] == 'INVALID_EDGE_WEIGHT'

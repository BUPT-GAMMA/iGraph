"""Explicit parallel-relationship semantics shared by tools and compute.

Path results identify distinct node sequences. Flow/cut weights are capacities,
matching the existing tool contract. Unreviewed algorithms reject multigraphs.
"""

import math
from numbers import Real


def numeric_weight(value):
    try:
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
            raise ValueError('边权必须是有限数值。')
    except OverflowError as exc:
        raise ValueError('边权超出可计算范围。') from exc
    return float(value)


MULTIGRAPH_POLICIES = {
    'shortest_path': 'minimum_weight_per_pair',
    'all_shortest_paths': 'minimum_weight_per_pair',
    'maximum_flow': 'sum_weights_per_pair',
    'minimum_cut': 'sum_weights_per_pair',
    'global_minimum_cut': 'native',
    'minimum_spanning_tree': 'minimum_weight_undirected',
    'graph_connectivity': 'native',
    'find_cycles': 'native',
    'topological_sort': 'native',
    'degree_analysis': 'native',
    'reachability': 'native',
    'node_centrality': {
        'degree': 'native', 'betweenness': 'minimum_weight_per_pair',
        'closeness': 'minimum_weight_per_pair', 'harmonic': 'minimum_weight_per_pair',
        'pagerank': 'sum_weights_per_pair', 'eigenvector': 'sum_weights_per_pair',
        'katz': 'sum_weights_per_pair', 'load': 'minimum_weight_per_pair',
    },
    'subgraph_extraction': {'ego_graph': 'native', 'node_subset': 'native'},
    'path_analysis': {'has_path': 'native', 'all_simple_paths': 'minimum_weight_per_pair',
                      'average_shortest_path_length': 'minimum_weight_per_pair'},
    'global_metrics': {'density': 'native', 'diameter': 'native', 'radius': 'native'},
    'community_detection': {'louvain': 'sum_weights_undirected', 'greedy_modularity': 'sum_weights_undirected'},
    'maximum_cycle': 'unsupported',
    'link_prediction_score': 'unsupported',
    'clique_analysis': 'unsupported',
}


def multigraph_policy(name, arguments):
    policy = MULTIGRAPH_POLICIES.get(name, 'unsupported')
    if isinstance(policy, dict):
        option = {'node_centrality': 'centrality_type', 'community_detection': 'algorithm',
                  'global_metrics': 'metric'}.get(name, 'method')
        return policy.get(arguments.get(option), 'unsupported')
    return policy

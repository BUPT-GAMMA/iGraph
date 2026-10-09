"""Apply reviewed multigraph projections at every public compute entry point."""
from functools import wraps
from inspect import signature
import math
from numbers import Real

import networkx as nx

from graph_engine.analysis_policy import multigraph_policy, numeric_weight


_STRUCTURED_FAILURE_TOOLS = frozenset({
    'graph_structure_analysis',
    'connectivity_analysis',
    'tree_dag_analysis',
    'matching_coloring_analysis',
    'distance_measures',
})


def project_parallel_edges(graph, policy, *, use_weight=True):
    """Build a private calculation view; stored relationships stay intact."""
    undirected = policy.endswith('_undirected')
    projected = nx.DiGraph() if graph.is_directed() and not undirected else nx.Graph()
    projected.graph.update(graph.graph)
    projected.add_nodes_from(graph.nodes(data=True))
    minimum = policy.startswith('minimum')
    for source, target, data in graph.edges(data=True):
        attrs = dict(data)
        weight = numeric_weight(data.get('weight', 1.0)) if use_weight else 1.0
        if not projected.has_edge(source, target):
            if not minimum:
                attrs = {'weight': weight}
            projected.add_edge(source, target, **attrs)
            continue
        previous = projected[source][target]
        if minimum:
            old_weight = numeric_weight(previous.get('weight', 1.0)) if use_weight else 1.0
            # Stable identity resolves equal-cost choices, independent of input order.
            if (weight, str(data.get('id', data.get('name', '')))) < (old_weight, str(previous.get('id', previous.get('name', '')))):
                previous.clear()
                previous.update(attrs)
        else:
            previous['weight'] = numeric_weight(previous['weight'] + weight)
    return projected


def _nonfinite_result(value):
    if isinstance(value, dict):
        return any(_nonfinite_result(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_nonfinite_result(item) for item in value)
    return isinstance(value, Real) and not isinstance(value, int) and not math.isfinite(value)


def _attach_node_labels(result, graph):
    """Resolve IDs already present in a result without fetching whole neighborhoods.

    IDs stay canonical: labels are evidence from stored properties, never an
    alias merge. Bound the extra payload even for explicitly unlimited queries.
    """
    if not isinstance(result, dict) or not result.get('success'):
        return result
    labels = {}
    truncated = False

    def visit(value):
        nonlocal truncated
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
        elif isinstance(value, str) and value in graph and value not in labels:
            properties = graph.nodes[value].get('properties', {})
            if not isinstance(properties, dict):
                return
            data = properties.get('data')
            name = data.get('name') if isinstance(data, dict) else None
            name = name or properties.get('name')
            if not isinstance(name, str) or not name.strip() or name == value:
                return
            if len(labels) >= 200 or len(name) > 200:
                truncated = True
                return
            labels[value] = name

    visit(result)
    if labels:
        result['node_labels'] = labels
    if truncated:
        result['node_labels_truncated'] = True
    return result


def analysis_contract(function):
    """Guard direct, HTTP and worker calls with the same parameter-aware policy.

    Expected NetworkX input/convergence failures become tool failures for
    property graphs. Programming errors remain visible rather than hidden.
    """
    sig = signature(function)

    @wraps(function)
    def invoke(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        graph = bound.arguments['g']
        multi = graph.is_multigraph()
        property_graph = graph.graph.get('property_graph', False)
        structured_failures = multi or property_graph or function.__name__ in _STRUCTURED_FAILURE_TOOLS
        policy = multigraph_policy(function.__name__, bound.arguments) if multi or property_graph else None
        project = bool(policy and policy not in {'native', 'unsupported'} and
                       (multi or policy.endswith('_undirected')))
        if multi and policy == 'unsupported':
            return {'success': False, 'code': 'UNSUPPORTED_MULTIGRAPH_OPERATION',
                    'tool': function.__name__, 'graph_type': type(graph).__name__,
                    'error': '当前算法或参数尚未支持平行关系；请选择已支持的图分析操作。'}
        try:
            if multi or property_graph:
                weighted = function.__name__ in {
                    'shortest_path', 'all_shortest_paths', 'maximum_flow', 'minimum_cut',
                    'global_minimum_cut', 'minimum_spanning_tree', 'node_centrality',
                    'community_detection', 'maximum_cycle',
                } or function.__name__ == 'path_analysis' and bound.arguments.get('method') == 'average_shortest_path_length'
                if function.__name__ == 'shortest_path' and not bound.arguments['use_weight']:
                    weighted = False
                if function.__name__ == 'node_centrality' and bound.arguments['centrality_type'] == 'degree':
                    weighted = False
                if weighted:
                    for _, _, data in graph.edges(data=True):
                        weight = numeric_weight(data.get('weight', 1.0))
                        if weight == 0 and function.__name__ == 'node_centrality' and bound.arguments['centrality_type'] in {'betweenness', 'load'}:
                            return {'success': False, 'code': 'INVALID_EDGE_WEIGHT', 'tool': function.__name__,
                                    'error': '该中心性算法要求严格正的距离边权；当前图包含零边权。'}
                        if weight < 0 and function.__name__ not in {'minimum_spanning_tree', 'maximum_cycle'}:
                            return {'success': False, 'code': 'INVALID_EDGE_WEIGHT', 'tool': function.__name__,
                                    'error': '该算法要求非负边权；当前图包含负边权。'}
                if project:
                    bound.arguments['g'] = project_parallel_edges(
                        graph, policy, use_weight=bound.arguments.get('use_weight', True))
            result = function(*bound.args, **bound.kwargs)
        except nx.NetworkXException as exc:
            if not structured_failures:
                raise
            return {'success': False, 'code': 'GRAPH_ALGORITHM_FAILED', 'tool': function.__name__, 'error': str(exc)}
        except ValueError as exc:
            if not structured_failures:
                raise
            return {'success': False, 'code': 'INVALID_GRAPH_ANALYSIS_INPUT', 'tool': function.__name__, 'error': str(exc)}
        except (OverflowError, ZeroDivisionError) as exc:
            if not structured_failures:
                raise
            return {'success': False, 'code': 'GRAPH_ALGORITHM_FAILED', 'tool': function.__name__, 'error': str(exc)}
        if (multi or property_graph) and _nonfinite_result(result):
            return {'success': False, 'code': 'INVALID_GRAPH_ANALYSIS_RESULT', 'tool': function.__name__,
                    'error': '计算结果包含非有限数值，请检查边权规模和算法适用条件。'}
        if multi or project:
            result['multigraph_policy'] = policy
        return _attach_node_labels(result, graph)
    return invoke

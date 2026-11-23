"""Bounded, read-only implementations of the additional analysis contracts."""
from __future__ import annotations

from collections import Counter
from itertools import islice
import json
import math
import networkx as nx

from graph_engine.extended_tools import (
    EXTENDED_RETRIEVAL_NAMES, UNDIRECTED_TOOLS,
    extended_size_error, validate_extended_arguments,
)


def _page(rows, arguments, *, total=None):
    offset, limit = arguments["offset"], arguments["limit"]
    if total is None:
        rows = list(rows)
        total = len(rows)
    items = list(islice(iter(rows), offset, offset + limit))
    end = offset + len(items)
    return {"items": items, "total": total, "count_returned": len(items),
            "offset": offset, "limit": limit,
            "next_offset": end if end < total else None,
            "truncated": offset > 0 or end < total}


def _type(attrs, field):
    value = attrs.get(field, attrs.get("type"))
    return None if value is None else str(value)


def _node_rows(nodes, graph):
    return [{"node": str(node), "node_type": _type(graph.nodes[node], "node_type")}
            for node in sorted(nodes, key=str)]


def _edges(graph):
    if graph.is_multigraph():
        yield from graph.edges(keys=True, data=True)
    else:
        for source, target, attrs in graph.edges(data=True):
            yield source, target, None, attrs


def _edge_row(source, target, key, attrs):
    identity = attrs.get("id", attrs.get("name"))
    return {"source": str(source), "target": str(target),
            "key": None if key is None else str(key),
            "edge_id": None if identity is None else str(identity),
            "edge_type": _type(attrs, "edge_type")}


def _neighbors(graph, node, direction):
    if not graph.is_directed():
        return set(graph.neighbors(node))
    outgoing = set(graph.successors(node)) if direction in {"out", "both"} else set()
    incoming = set(graph.predecessors(node)) if direction in {"in", "both"} else set()
    return outgoing | incoming


def _require_nodes(graph, nodes):
    missing = [node for node in nodes if node not in graph]
    if missing:
        raise ValueError(f"节点不存在: {missing[:10]}")


def _retrieve(name, graph, a):
    if name == "list_graph_nodes":
        nodes = [node for node, attrs in graph.nodes(data=True)
                 if "node_type" not in a or _type(attrs, "node_type") == a["node_type"]]
        return _page(_node_rows(nodes, graph), a)
    if name == "list_graph_edges":
        rows = [_edge_row(u, v, k, data) for u, v, k, data in _edges(graph)
                if "edge_type" not in a or _type(data, "edge_type") == a["edge_type"]]
        return _page(rows, a)
    if name in {"get_node_type_counts", "get_edge_type_counts"}:
        field = "node_type" if name == "get_node_type_counts" else "edge_type"
        attrs = (data for _, data in graph.nodes(data=True)) if field == "node_type" else (data for *_, data in _edges(graph))
        counts = Counter(_type(data, field) for data in attrs)
        rows = [{field: kind, "count": count} for kind, count in sorted(counts.items(), key=lambda item: (item[0] is not None, str(item[0])))]
        return {**_page(rows, a), "entity_count": sum(counts.values())}
    if name == "get_isolated_nodes":
        return _page(_node_rows(nx.isolates(graph), graph), a)
    if name in {"get_source_nodes", "get_sink_nodes"}:
        if not graph.is_directed():
            raise ValueError("源节点/汇节点查询要求有向图。")
        nodes = [node for node in graph if
                 (graph.in_degree(node) == 0 and graph.out_degree(node) > 0
                  if name == "get_source_nodes" else
                  graph.out_degree(node) == 0 and graph.in_degree(node) > 0)]
        return _page(_node_rows(nodes, graph), a)
    if name == "get_common_neighbors":
        _require_nodes(graph, [a["source"], a["target"]])
        common = _neighbors(graph, a["source"], a["direction"]) & _neighbors(graph, a["target"], a["direction"])
        common -= {a["source"], a["target"]}
        return _page(_node_rows(common, graph), a)
    _require_nodes(graph, a["nodes"])
    inside = set(a["nodes"])
    if name == "get_node_boundary":
        boundary = set().union(*(_neighbors(graph, node, a["direction"]) for node in inside)) - inside
        return _page(_node_rows(boundary, graph), a)
    rows = []
    for source, target, key, attrs in _edges(graph):
        outgoing = source in inside and target not in inside
        incoming = target in inside and source not in inside
        direction = a["direction"] if graph.is_directed() else "both"
        if (outgoing and direction in {"out", "both"}) or (incoming and direction in {"in", "both"}):
            rows.append(_edge_row(source, target, key, attrs))
    return _page(rows, a)


def _number(value):
    if math.isfinite(value):
        return {"value": float(value), "defined": True}
    reason = "undefined" if math.isnan(value) else "positive_infinity" if value > 0 else "negative_infinity"
    return {"value": None, "defined": False, "nonfinite": reason}


def _scores(values, a):
    return _page([{"node": str(node), **_number(value)}
                  for node, value in sorted(values.items(), key=lambda item: str(item[0]))], a)


def _hits(graph):
    # Positive power iteration avoids signed/zero-sum singular vectors when
    # the largest singular value is repeated (e.g. an undirected cycle).
    if not graph.number_of_edges():
        raise ValueError("HITS 要求至少一条边。")
    hubs = {node: 1.0 / len(graph) for node in graph}
    links = list(graph.edges())
    if not graph.is_directed():
        links += [(v, u) for u, v in links]
    for iteration in range(1000):
        authorities = dict.fromkeys(graph, 0.0)
        for source, target in links:
            authorities[target] += hubs[source]
        total = sum(authorities.values())
        authorities = {node: value / total for node, value in authorities.items()}
        updated = dict.fromkeys(graph, 0.0)
        for source, target in links:
            updated[source] += authorities[target]
        total = sum(updated.values())
        updated = {node: value / total for node, value in updated.items()}
        difference = sum(abs(updated[node] - hubs[node]) for node in graph)
        hubs = updated
        if difference < 1e-8:
            return hubs, authorities, iteration + 1
    raise nx.PowerIterationFailedConvergence(1000)


def _theory(name, graph, a):
    if name == "triangle_count":
        values = nx.triangles(graph)
        return {**_scores(values, a), "triangle_count": sum(values.values()) // 3}
    functions = {"node_clustering": nx.clustering, "square_clustering": nx.square_clustering,
                 "core_numbers": nx.core_number, "onion_layers": nx.onion_layers,
                 "structural_constraint": nx.constraint, "effective_size": nx.effective_size}
    if name in functions:
        return _scores(functions[name](graph), a)
    if name == "edge_betweenness":
        values = nx.edge_betweenness_centrality(graph, normalized=True, weight=None)
        return _page([{"source": str(u), "target": str(v), **_number(value)}
                      for (u, v), value in values.items()], a)
    if name == "hits_scores":
        hubs, authorities, iterations = _hits(graph)
        return {**_page([{"node": str(node), "hub": hubs[node], "authority": authorities[node]}
                        for node in sorted(graph, key=str)], a), "iterations": iterations, "converged": True}
    if name == "closeness_vitality":
        connected = nx.is_strongly_connected(graph) if graph.is_directed() else nx.is_connected(graph)
        if not connected:
            raise ValueError("接近活力要求连通图（有向图需强连通）。")
        return _scores(nx.closeness_vitality(graph, weight=None), a)
    if name == "average_node_connectivity":
        return _number(nx.average_node_connectivity(graph))
    if name in {"cycle_basis", "minimum_cycle_basis"}:
        cycles = nx.cycle_basis(graph) if name == "cycle_basis" else nx.minimum_cycle_basis(graph, weight=None)
        return {**_page([[str(node) for node in cycle] for cycle in cycles], a),
                "cycle_rank": graph.number_of_edges() - len(graph) + nx.number_connected_components(graph),
                "node_order": "cycle" if name == "cycle_basis" else "unordered"}
    if name in {"dominating_set", "approx_min_dominating_set", "maximal_independent_set", "approx_min_vertex_cover"}:
        if name == "dominating_set":
            nodes = nx.dominating_set(graph, start_with=min(graph, key=str))
        elif name == "approx_min_dominating_set":
            nodes = nx.algorithms.approximation.min_weighted_dominating_set(graph, weight=None)
        elif name == "maximal_independent_set":
            nodes = nx.maximal_independent_set(graph, seed=42)
        else:
            nodes = nx.algorithms.approximation.min_weighted_vertex_cover(graph, weight=None)
        return {**_page([str(node) for node in sorted(nodes, key=str)], a),
                "optimality": "not_guaranteed", "approximation": name.startswith("approx_"),
                **({"approximation_factor": 2} if name == "approx_min_vertex_cover" else {})}
    if "source" in a:
        _require_nodes(graph, [a["source"]])
    if name in {"is_eulerian", "is_semieulerian", "has_eulerian_path"}:
        value = (nx.is_eulerian(graph) if name == "is_eulerian" else
                 nx.is_semieulerian(graph) if name == "is_semieulerian" else
                 nx.has_eulerian_path(graph, source=a.get("source")))
        return {"value": bool(value)}
    if name in {"eulerian_circuit", "eulerian_path"}:
        iterator = (nx.eulerian_circuit(graph, source=a.get("source")) if name == "eulerian_circuit" else
                    nx.eulerian_path(graph, source=a.get("source")))
        rows = ({"source": str(u), "target": str(v)} for u, v in iterator)
        return _page(rows, a, total=graph.number_of_edges())
    if name == "bipartite_redundancy":
        if not nx.is_bipartite(graph):
            raise ValueError("节点冗余系数要求二部图。")
        eligible = [node for node in graph if graph.degree(node) >= 2]
        values = nx.algorithms.bipartite.node_redundancy(graph, nodes=eligible)
        return _scores({node: values.get(node, math.nan) for node in graph}, a)
    if name == "s_metric":
        return {"value": sum(graph.degree(u) * graph.degree(v) for u, v in graph.edges()), "normalized": False}
    raise ValueError(f"未知扩展工具: {name}")


def run_extended_tool(name: str, graph: nx.Graph, arguments: dict) -> dict:
    try:
        a = validate_extended_arguments(name, arguments)
    except ValueError as exc:
        return {"success": False, "code": "INVALID_ARGUMENT", "tool": name, "error": str(exc)}
    oversized = extended_size_error(name, len(graph), graph.number_of_edges())
    if oversized:
        return oversized
    try:
        if name in EXTENDED_RETRIEVAL_NAMES:
            result = _retrieve(name, graph, a)
            meta = {"directed": graph.is_directed(), "multigraph": graph.is_multigraph()}
        else:
            if graph.is_multigraph():
                return {"success": False, "code": "UNSUPPORTED_MULTIGRAPH_OPERATION", "tool": name,
                        "error": "该算法尚未支持平行关系，请选择支持多重图的工具。"}
            if not len(graph):
                raise ValueError("该算法需要非空图。")
            if nx.number_of_selfloops(graph):
                raise ValueError("该算法目前要求无自环图。")
            directed = graph.is_directed() and not a["treat_as_undirected"]
            if directed and name in UNDIRECTED_TOOLS:
                raise ValueError("该算法要求无向图；如需忽略方向，请显式传入 treat_as_undirected=true。")
            # Only the private topology copy drops weights and properties.
            topology = nx.DiGraph() if directed else nx.Graph()
            topology.add_nodes_from(graph)
            topology.add_edges_from(graph.edges())
            result = _theory(name, topology, a)
            meta = {"directed": directed, "weight_semantics": "unweighted",
                    "projected_to_undirected": graph.is_directed() and not directed}
        result = {"success": True, "tool": name, **meta, **result}
        # Bound bytes as well as rows (node IDs can be arbitrarily long).
        size = 0
        for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(result):
            size += len(part.encode("utf-8"))
            if size > 128 * 1024:
                return {"success": False, "code": "RESULT_TOO_LARGE", "tool": name,
                        "error": "结果超过 128 KiB；请降低 limit。"}
        return result
    except (nx.NetworkXException, ValueError, ZeroDivisionError) as exc:
        return {"success": False, "code": "ALGORITHM_PRECONDITION_FAILED", "tool": name, "error": str(exc)}

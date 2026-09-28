"""Preserve the real FK evidence connecting highlighted ownership subjects."""
from __future__ import annotations

from copy import deepcopy

import networkx as nx


def complete_ownership_connections(graph: nx.Graph, highlights: dict | None) -> tuple[dict | None, set]:
    """Expand node-only groups with their internal ownership records.

    A business SCC's companies have no direct edges in a row/FK graph. Include
    every connecting ownership row, not a shortest path or spanning tree. This
    does not compute SCC membership or create business edges in the source graph.
    Explicit edge selections retain their requested scope.
    """
    if (not isinstance(highlights, dict) or highlights.get("paths")
            or any("edges" in item for item in highlights.get("subgraphs", []) if isinstance(item, dict))):
        return highlights, set()

    def table(node):
        attrs = graph.nodes[node]
        return (attrs.get("properties") or {}).get("table") or attrs.get("type") or attrs.get("node_type")

    def neighbors(node):
        result = set(graph.neighbors(node))
        if graph.is_directed():
            result.update(graph.predecessors(node))
        return result

    result = deepcopy(highlights)
    added = set()
    pairs = set()
    groups = [(group, "ids") for group in result.get("nodes", [])]
    groups.extend((group, "nodes") for group in result.get("subgraphs", []))
    for group, key in groups:
        if not isinstance(group, dict):
            continue
        members = {node for node in group.get(key, []) if node in graph
                   and table(node) in {"institution_main", "individual_info"}}
        if not members:
            continue
        candidates = {node for member in members for node in neighbors(member)
                      if table(node) == "institution_shareholder_relationship"}
        connectors = set()
        for record in candidates:
            subjects = {node for node in neighbors(record)
                        if table(node) in {"institution_main", "individual_info"}}
            # Two FK links to one subject can represent self-ownership. Retain
            # both real edge identities; never infer a link from names or IDs.
            if not subjects or not subjects <= members or (len(subjects) < 2 and graph.degree(record) < 2):
                continue
            connectors.add(record)
            for subject in subjects:
                if graph.has_edge(record, subject):
                    pairs.add((record, subject))
                if graph.is_directed() and graph.has_edge(subject, record):
                    pairs.add((subject, record))
        if connectors:
            group[key] = list(dict.fromkeys([*group.get(key, []), *sorted(connectors, key=str)]))
            added.update(connectors | members)
    if pairs:
        result["paths"] = [{"edges": [list(pair) for pair in sorted(pairs, key=lambda pair: tuple(map(str, pair)))],
                            "color": "#f97316", "label": "股权关系连接"}]
    return result, added

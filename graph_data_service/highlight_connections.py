"""Preserve the real FK evidence connecting highlighted ownership subjects."""
from __future__ import annotations

from copy import deepcopy

import networkx as nx


def resolve_ownership_selectors(graph, node_names, highlights, construction_graph=None):
    """Resolve exact business IDs against current source rows, never company names.

    Raw graph IDs take priority. Untyped IDs shared by a company and a person
    are ambiguous; duplicated source rows can expand a node group, but not a
    path endpoint. Mapping preserves real FK topology and never invents edges.
    """
    from collections import defaultdict
    aliases = None
    resolved = {}

    def index():
        nonlocal aliases
        aliases = defaultdict(dict)
        if isinstance(construction_graph, dict):
            from graph_data_service.finance import _current_records, Budget
            document = construction_graph.get('graph', construction_graph)
            records = _current_records(document, Budget(200000, 400000, 10))
        else:
            records = [dict(attrs, id=node) for node, attrs in graph.nodes(data=True)]
        for row in records:
            node = row.get('id')
            if node not in graph:
                continue
            props = row.get('properties') or {}
            table = props.get('table') or row.get('type') or row.get('node_type')
            kind, field = {'institution_main': ('I:', 'UUID'),
                           'individual_info': ('P:', 'IDV_ID')}.get(table, ('', ''))
            data = props.get('data') or {}
            if not kind or not isinstance(data, dict):
                continue
            bid = str(data.get(field) or '').strip()
            if not bid:
                continue
            typed = kind + bid
            for key in (bid, typed):
                aliases[key].setdefault(typed, []).append(node)

    def nodes(value, *, endpoint=False):
        if value in graph:
            return [value]
        if aliases is None:
            index()
        matches = aliases.get(value, {})
        if not matches:
            return [value]
        if len(matches) != 1:
            raise ValueError(f'AMBIGUOUS_OWNERSHIP_NODE: {value}; use I:/P: or source_node_ids')
        found = sorted(set(next(iter(matches.values()))), key=str)
        if endpoint and len(found) != 1:
            raise ValueError(f'AMBIGUOUS_OWNERSHIP_ENDPOINT: {value}; use source_node_ids')
        resolved[value] = found
        return found

    def group(values):
        return list(dict.fromkeys(node for value in values for node in nodes(value)))

    selected = group(node_names) if node_names else node_names
    result = deepcopy(highlights)
    if isinstance(result, dict):
        for kind, field in (('nodes', 'ids'), ('subgraphs', 'nodes'), ('paths', 'node_sequence')):
            for item in result.get(kind, []):
                if not isinstance(item, dict):
                    continue
                if field in item:
                    item[field] = ([nodes(value, endpoint=True)[0] for value in item[field]]
                                   if kind == 'paths' else group(item[field]))
                for edge in item.get('edges') or []:
                    if isinstance(edge, dict):
                        for key in ('src', 'dst', 'source', 'target'):
                            if key in edge:
                                edge[key] = nodes(edge[key], endpoint=True)[0]
                    elif isinstance(edge, list) and len(edge) == 2:
                        edge[:] = [nodes(value, endpoint=True)[0] for value in edge]
    return selected, result, resolved


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

"""Deterministic topology-aware node selection for graph visualization.

The selector intentionally has no dependency on :class:`GraphData` so that the
same policy can be tested against ordinary NetworkX graphs.  It preserves
explicitly required nodes, represents node types and communities, favours hubs,
and spends a bounded part of the display budget on paths between representatives.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import networkx as nx


def _stable_key(value: Any) -> tuple[str, str]:
    """Return a total, reproducible ordering key for heterogeneous node IDs."""

    return (f"{type(value).__module__}.{type(value).__qualname__}", repr(value))


def _ordered_unique(values: Iterable[Any], allowed: set[Any]) -> list[Any]:
    return sorted({value for value in values if value in allowed}, key=_stable_key)


def _canonical_undirected_graph(graph: nx.Graph, nodes: Iterable[Any]) -> nx.Graph:
    """Return an attribute-free undirected graph with canonical iteration order.

    NetworkX community algorithms use node and neighbour iteration order when
    resolving otherwise-equal choices.  A subgraph view preserves the source
    graph's insertion order, so merely seeding Louvain is not sufficient for a
    reproducible overview after the same graph is reloaded in a different row
    order.  Building the lightweight topology in sorted adjacency order removes
    that dependency and also avoids malformed/non-numeric ``weight`` attributes
    changing (or breaking) the visualization-only community partition.
    """

    ordered = sorted({node for node in nodes if graph.has_node(node)}, key=_stable_key)
    allowed = set(ordered)
    canonical = nx.Graph()
    canonical.add_nodes_from(ordered)
    keys = {node: _stable_key(node) for node in ordered}
    directed = graph.is_directed()
    for node in ordered:
        if directed:
            neighbours = set(graph.successors(node)) | set(graph.predecessors(node))
        else:
            neighbours = set(graph.neighbors(node))
        for neighbour in sorted(neighbours.intersection(allowed), key=_stable_key):
            # Each undirected pair is inserted exactly once and in canonical
            # order.  Parallel relationships intentionally collapse here: the
            # selector reasons about topology, while rendering retains them.
            if keys[node] <= keys[neighbour]:
                canonical.add_edge(node, neighbour)
    return canonical


def _normalised_communities(graph: nx.Graph) -> list[tuple[Any, ...]]:
    if graph.number_of_nodes() == 0:
        return []
    if graph.number_of_edges() == 0:
        raw = [{node} for node in graph.nodes()]
    else:
        try:
            raw = list(nx.community.louvain_communities(graph, weight=None, seed=42))
        except (AttributeError, ImportError):
            try:
                raw = list(nx.community.greedy_modularity_communities(graph, weight=None))
            except (AttributeError, ImportError):
                # Connected components are deterministic and remain a useful
                # structural fallback on older/minimal NetworkX installations.
                raw = list(nx.connected_components(graph))
        except Exception:
            raw = list(nx.connected_components(graph))
    communities = [tuple(sorted(community, key=_stable_key)) for community in raw if community]
    communities.sort(key=lambda community: _stable_key(community[0]))
    return communities


def _bfs_parent_tree(graph: nx.Graph, source: Any) -> dict[Any, Any | None]:
    """Build a deterministic unweighted BFS tree from ``source``."""

    parents: dict[Any, Any | None] = {source: None}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for neighbour in sorted(graph.neighbors(node), key=_stable_key):
            if neighbour in parents:
                continue
            parents[neighbour] = node
            queue.append(neighbour)
    return parents


def _tree_path(parents: Mapping[Any, Any | None], target: Any) -> tuple[Any, ...]:
    if target not in parents:
        return ()
    path: list[Any] = []
    cursor: Any | None = target
    while cursor is not None:
        path.append(cursor)
        cursor = parents[cursor]
    path.reverse()
    return tuple(path)


@dataclass(frozen=True)
class SmartOverviewSelection:
    nodes: tuple[Any, ...]
    communities: tuple[tuple[Any, ...], ...]
    community_representatives: tuple[Any, ...]
    type_representatives: tuple[Any, ...]
    required_nodes: tuple[Any, ...]
    connector_nodes: tuple[Any, ...]
    connector_paths: tuple[tuple[Any, ...], ...]
    required_truncated: bool = False


def select_smart_overview(
    graph: nx.Graph,
    candidates: Iterable[Any],
    *,
    max_nodes: int,
    node_types: Mapping[Any, str] | None = None,
    required_nodes: Iterable[Any] = (),
) -> SmartOverviewSelection:
    """Proportional connected-component samples with real connector paths.

    Connectivity takes precedence over exact type quotas. Explicit user targets
    are preserved even when their paths cannot fit inside the display budget.
    """
    import heapq
    from collections import Counter

    limit = max(1, int(max_nodes))
    ordered = sorted({n for n in candidates if graph.has_node(n)}, key=_stable_key)
    topology = _canonical_undirected_graph(graph, ordered)
    communities = _normalised_communities(topology)
    if len(ordered) <= limit:
        return SmartOverviewSelection(tuple(ordered), tuple(communities), (), (), tuple(_ordered_unique(required_nodes, set(ordered))), (), ())
    degrees = dict(topology.degree())
    priority = lambda n: (-degrees[n], _stable_key(n))
    required_all = sorted(_ordered_unique(required_nodes, set(ordered)), key=priority)
    required = set(required_all[:limit])
    types = {n: str((node_types or {}).get(n) or "unknown") for n in ordered}
    community_by_node = {n: str(i) for i, group in enumerate(communities) for n in group}
    components = [sorted(group, key=priority) for group in nx.connected_components(topology)]
    components.sort(key=lambda group: priority(group[0]))
    isolates = sorted(nx.isolates(topology), key=priority)
    if isolates:
        isolate_ids = set(isolates)
        components = [group for group in components if group[0] not in isolate_ids] + [isolates]
    quotas = [sum(n in required for n in group) for group in components]
    remaining = limit - sum(quotas)
    exact = [len(group) * limit / len(ordered) for group in components]

    def cost(i):
        return 2 if quotas[i] == 0 and len(components[i]) > 1 and degrees[components[i][0]] > 0 else 1

    def allocation(i):
        return (-(exact[i] - quotas[i]) / cost(i), -len(components[i]), i)

    heap = [allocation(i) for i, group in enumerate(components) if quotas[i] < len(group)]
    heapq.heapify(heap)
    while remaining and heap:
        _, _, i = heapq.heappop(heap)
        increment = cost(i)
        if increment > remaining:
            continue
        quotas[i] += increment
        remaining -= increment
        if quotas[i] < len(components[i]):
            heapq.heappush(heap, allocation(i))
    if remaining:
        for i, group in enumerate(components):
            if quotas[i] < len(group):
                quotas[i] += 1
                remaining -= 1
                if not remaining:
                    break

    selected = set()
    connector_nodes = set()
    connector_paths = []
    for group, quota in zip(components, quotas):
        if not quota:
            continue
        if quota >= len(group):
            selected.update(group)
            continue
        local = required.intersection(group)
        if all(degrees[n] == 0 for n in group):
            type_groups = {}
            for n in group:
                type_groups.setdefault(types[n], []).append(n)
            counts = Counter(types[n] for n in local)
            cursors = Counter()
            while len(local) < quota:
                options = []
                for kind, items in type_groups.items():
                    while cursors[kind] < len(items) and items[cursors[kind]] in local:
                        cursors[kind] += 1
                    if cursors[kind] < len(items):
                        n = items[cursors[kind]]
                        options.append((-(len(items) * quota / len(group) - counts[kind]), priority(n), n))
                if not options:
                    break
                n = min(options)[2]
                local.add(n)
                counts[types[n]] += 1
            selected.update(local)
            continue
        anchor = min(local, key=priority) if local else group[0]
        local.add(anchor)
        parents = _bfs_parent_tree(topology, anchor)

        def keys(n):
            return (("type", types[n]), ("community", community_by_node[n]))

        groups = {}
        for n in group:
            for key in keys(n):
                groups.setdefault(key, []).append(n)
        representatives = [items[0] for items in sorted(groups.values(), key=lambda items: (-len(items), priority(items[0])))]
        targets = sorted(required.intersection(group), key=priority) + representatives[:64]
        for target in targets:
            path = _tree_path(parents, target)
            missing = set(path) - local
            path_budget = quota if target in required or quota <= 8 else max(len(local), int(quota * .65))
            if missing and len(local) + len(missing) <= path_budget:
                local.update(missing)
                connector_nodes.update(missing)
                connector_paths.append(path)
        counts = Counter(key for n in local for key in keys(n))
        frontier = {}
        queued = set()

        def enqueue(n):
            for nb in topology.neighbors(n):
                if nb in local or nb in queued:
                    continue
                queued.add(nb)
                heapq.heappush(frontier.setdefault(keys(nb), []), (priority(nb), nb))

        for n in sorted(local, key=priority):
            enqueue(n)
        while len(local) < quota:
            best = None
            for bucket in frontier.values():
                while bucket and bucket[0][1] in local:
                    heapq.heappop(bucket)
                if not bucket:
                    continue
                n = bucket[0][1]
                deficit = sum((len(groups[key]) * quota / len(group) - counts[key]) / max(1, len(groups[key]) * quota / len(group)) for key in keys(n))
                candidate = (-deficit, priority(n), n)
                if best is None or candidate[:2] < best[:2]:
                    best = candidate
            if best is None:
                break
            n = best[2]
            heapq.heappop(frontier[keys(n)])
            local.add(n)
            counts.update(keys(n))
            enqueue(n)
        selected.update(local)

    # Metadata describes representatives actually retained, not mandatory naked
    # vertices from every small component.
    kept_communities = [min(set(group) & selected, key=priority) for group in communities if set(group) & selected]
    kept_types = {}
    for n in sorted(selected, key=priority):
        kept_types.setdefault(types[n], n)
    return SmartOverviewSelection(
        nodes=tuple(sorted(selected, key=_stable_key)),
        communities=tuple(communities),
        community_representatives=tuple(sorted(kept_communities, key=_stable_key)),
        type_representatives=tuple(sorted(kept_types.values(), key=_stable_key)),
        required_nodes=tuple(sorted(required, key=_stable_key)),
        connector_nodes=tuple(sorted(connector_nodes, key=_stable_key)),
        connector_paths=tuple(connector_paths),
        required_truncated=len(required_all) > len(required),
    )


__all__ = ["SmartOverviewSelection", "select_smart_overview"]

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
    """Select a stable, representative induced subgraph.

    ``max_nodes`` is a hard canvas budget.  Explicit requirements take first
    priority, followed by a representative for each community and node type,
    high-degree hubs, connector paths, proportional per-type samples, and a
    final global degree-ranked fill.
    """

    limit = max(1, int(max_nodes))
    allowed = {node for node in candidates if graph.has_node(node)}
    ordered = sorted(allowed, key=_stable_key)
    if len(ordered) <= limit:
        communities = _normalised_communities(_canonical_undirected_graph(graph, ordered))
        required = tuple(_ordered_unique(required_nodes, allowed))
        return SmartOverviewSelection(
            nodes=tuple(ordered),
            communities=tuple(communities),
            community_representatives=(),
            type_representatives=(),
            required_nodes=required,
            connector_nodes=(),
            connector_paths=(),
        )

    overview_graph = _canonical_undirected_graph(graph, ordered)
    degrees = {node: int(overview_graph.degree(node)) for node in ordered}

    def priority(node: Any) -> tuple[int, tuple[str, str]]:
        return (-degrees.get(node, 0), _stable_key(node))

    required_all = _ordered_unique(required_nodes, allowed)
    required_ranked = sorted(required_all, key=priority)
    required = required_ranked[:limit]
    selected: set[Any] = set(required)
    required_truncated = len(required_all) > len(required)

    effective_types = {node: str((node_types or {}).get(node) or "unknown") for node in ordered}
    type_groups: dict[str, list[Any]] = {}
    for node in ordered:
        type_groups.setdefault(effective_types[node], []).append(node)
    # Type coverage precedes structural representatives.  This matters for an
    # edgeless/many-component graph where every vertex is technically its own
    # community and would otherwise consume the entire budget before a minority
    # node type receives even one slot.
    type_representatives = [min(type_groups[type_name], key=priority) for type_name in sorted(type_groups)]
    kept_type_reps: list[Any] = []
    for representative in type_representatives:
        if representative in selected:
            kept_type_reps.append(representative)
            continue
        if len(selected) >= limit:
            break
        selected.add(representative)
        kept_type_reps.append(representative)

    communities = _normalised_communities(overview_graph)
    community_representatives = [min(community, key=priority) for community in communities]
    # If there are more communities than display slots, represent the largest
    # ones first; IDs/output ordering remain canonical below.
    community_add_order = sorted(
        zip(communities, community_representatives),
        key=lambda item: (-len(item[0]), priority(item[1])),
    )
    kept_community_reps: list[Any] = []
    for _community, representative in community_add_order:
        if representative in selected:
            kept_community_reps.append(representative)
            continue
        if len(selected) >= limit:
            break
        selected.add(representative)
        kept_community_reps.append(representative)

    # Reserve a small, bounded hub set before proportional filling.  This makes
    # bridges and structurally important nodes survive even in a dominant type.
    hub_budget = min(64, max(1, limit // 10))
    for node in sorted(ordered, key=priority)[:hub_budget]:
        if len(selected) >= limit:
            break
        selected.add(node)

    # Connect representatives within each connected component.  A deterministic
    # BFS tree supplies one stable shortest path from the component anchor to all
    # other representatives.  Connector nodes may use at most 20% of the budget.
    representatives = set(required) | set(kept_community_reps) | set(kept_type_reps)
    connector_budget = min(max(0, limit - len(selected)), max(8, limit // 5))
    path_candidates: list[tuple[int, tuple[str, str], tuple[Any, ...]]] = []
    if connector_budget:
        for component in nx.connected_components(overview_graph):
            component_reps = sorted(representatives.intersection(component), key=priority)
            # Path finding stays linear in the graph plus a small bounded number
            # of reconstructions even when an edgeless graph has many groups.
            component_reps = component_reps[:64]
            if len(component_reps) < 2:
                continue
            anchor = component_reps[0]
            parents = _bfs_parent_tree(overview_graph.subgraph(component), anchor)
            for representative in component_reps[1:]:
                path = _tree_path(parents, representative)
                if path:
                    missing = sum(node not in selected for node in path)
                    path_candidates.append((missing, _stable_key(representative), path))
    connector_nodes: set[Any] = set()
    connector_paths: list[tuple[Any, ...]] = []
    spent = 0
    for missing, _target_key, path in sorted(path_candidates, key=lambda item: (item[0], item[1])):
        newly_added = [node for node in path if node not in selected]
        if spent + len(newly_added) > connector_budget or len(selected) + len(newly_added) > limit:
            continue
        selected.update(newly_added)
        connector_nodes.update(newly_added)
        connector_paths.append(path)
        spent += len(newly_added)

    # Per-type proportional targets use largest remainders.  Already-preserved
    # representatives count toward their type's target; missing slots are filled
    # by descending degree with a stable ID tie-break.
    type_names = sorted(type_groups)
    exact_targets = {
        type_name: limit * len(type_groups[type_name]) / len(ordered)
        for type_name in type_names
    }
    targets = {type_name: int(exact_targets[type_name]) for type_name in type_names}
    remaining_target_slots = limit - sum(targets.values())
    remainder_order = sorted(
        type_names,
        key=lambda type_name: (-(exact_targets[type_name] - targets[type_name]), type_name),
    )
    for type_name in remainder_order[:remaining_target_slots]:
        targets[type_name] += 1

    current_counts = {
        type_name: sum(1 for node in selected if effective_types[node] == type_name)
        for type_name in type_names
    }
    deficits = {
        type_name: max(0, targets[type_name] - current_counts[type_name])
        for type_name in type_names
    }
    available_slots = max(0, limit - len(selected))
    total_deficit = sum(deficits.values())
    allocations = dict(deficits)
    if total_deficit > available_slots and total_deficit:
        exact_allocations = {
            type_name: available_slots * deficits[type_name] / total_deficit
            for type_name in type_names
        }
        allocations = {
            type_name: int(exact_allocations[type_name])
            for type_name in type_names
        }
        left = available_slots - sum(allocations.values())
        allocation_order = sorted(
            type_names,
            key=lambda type_name: (
                -(exact_allocations[type_name] - allocations[type_name]),
                type_name,
            ),
        )
        for type_name in allocation_order[:left]:
            allocations[type_name] += 1

    for type_name in type_names:
        remaining = allocations[type_name]
        if not remaining:
            continue
        for node in sorted(type_groups[type_name], key=priority):
            if node in selected:
                continue
            selected.add(node)
            remaining -= 1
            if remaining <= 0 or len(selected) >= limit:
                break

    # Quota rounding or over-represented mandatory groups can leave spare slots.
    # Fill those with the globally highest-degree remaining nodes.
    if len(selected) < limit:
        for node in sorted(ordered, key=priority):
            if node in selected:
                continue
            selected.add(node)
            if len(selected) >= limit:
                break

    # Community/path preservation can legitimately overfill one type before
    # quotas are applied.  Rebalance expendable hub/fill nodes so a minority
    # type does not disappear merely because its communities occur later in the
    # topology.  Required nodes, type/community representatives and connector
    # paths are never evicted.
    protected = (
        set(required)
        | set(kept_type_reps)
        | set(kept_community_reps)
        | connector_nodes
    )
    final_counts = {
        type_name: sum(1 for node in selected if effective_types[node] == type_name)
        for type_name in type_names
    }
    for type_name in sorted(
        type_names,
        key=lambda name: (
            -(targets[name] - final_counts[name]),
            name,
        ),
    ):
        while final_counts[type_name] < targets[type_name]:
            replacement = next(
                (
                    node for node in sorted(type_groups[type_name], key=priority)
                    if node not in selected
                ),
                None,
            )
            donor = next(
                (
                    node
                    for donor_type in sorted(
                        type_names,
                        key=lambda name: (-(final_counts[name] - targets[name]), name),
                    )
                    if final_counts[donor_type] > targets[donor_type]
                    for node in sorted(
                        (
                            item for item in selected
                            if effective_types[item] == donor_type and item not in protected
                        ),
                        key=lambda item: (degrees.get(item, 0), _stable_key(item)),
                    )
                ),
                None,
            )
            if replacement is None or donor is None:
                break
            donor_type = effective_types[donor]
            selected.remove(donor)
            selected.add(replacement)
            final_counts[donor_type] -= 1
            final_counts[type_name] += 1

    return SmartOverviewSelection(
        nodes=tuple(sorted(selected, key=_stable_key)),
        communities=tuple(communities),
        community_representatives=tuple(sorted(set(kept_community_reps), key=_stable_key)),
        type_representatives=tuple(sorted(set(kept_type_reps), key=_stable_key)),
        required_nodes=tuple(sorted(required, key=_stable_key)),
        connector_nodes=tuple(sorted(connector_nodes, key=_stable_key)),
        connector_paths=tuple(connector_paths),
        required_truncated=required_truncated,
    )


__all__ = ["SmartOverviewSelection", "select_smart_overview"]

"""Canonical insight actions absent from the upstream server skill snapshot.

This file uses the same command contract as ``graph_insight_tools.py``:

    python graph_insight_extensions.py ACTION GRAPH_FILE JSON_ARGUMENTS

It is intentionally deterministic and contains no benchmark metadata or
answer access.  The benchmark adapter invokes it only when the supplied server
skill file does not yet define the same canonical action.
"""

from __future__ import annotations

import json
from pathlib import Path
import pickle
import sys
from typing import Any, Sequence

import networkx as nx


def _load_graph(path: str | Path) -> nx.Graph:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".pt":
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("torch is required to load a .pt graph") from exc
        value = torch.load(source, map_location="cpu", weights_only=False)
        if isinstance(value, nx.Graph):
            return value
        # Preserve every directed edge.  The corpus ground truth selects ego
        # nodes by undirected hop distance, then computes/removes edges on the
        # original directed induced graph.  Converting here to an undirected
        # Graph would collapse reciprocal links and change density/removal
        # counts.
        try:
            from torch_geometric.utils import to_networkx

            return to_networkx(value, to_undirected=False)
        except (ImportError, TypeError, ValueError, AttributeError):
            pass
        edge_index = None
        if isinstance(value, dict):
            edge_index = value.get("edge_index")
        elif hasattr(value, "edge_index"):
            edge_index = value.edge_index
        if edge_index is None:
            raise ValueError("the .pt file does not contain a directly usable graph")
        if hasattr(edge_index, "detach"):
            edge_index = edge_index.detach().cpu()
        if hasattr(edge_index, "tolist"):
            edge_index = edge_index.tolist()
        if not isinstance(edge_index, list) or len(edge_index) != 2:
            raise ValueError("edge_index must have shape [2, E]")
        graph = nx.DiGraph()
        graph.add_edges_from(zip(edge_index[0], edge_index[1]))
        return graph
    if suffix in {".pkl", ".gpickle"}:
        with source.open("rb") as stream:
            value = pickle.load(stream)
        if not isinstance(value, nx.Graph):
            raise TypeError("pickled value is not a NetworkX graph")
        return value
    if suffix == ".graphml":
        return nx.read_graphml(source)
    if suffix == ".gml":
        return nx.read_gml(source)
    raise ValueError(f"cannot recognize graph file suffix: {suffix}")


def _component_count(graph: nx.Graph) -> int:
    if graph.number_of_nodes() == 0:
        return 0
    if graph.is_directed():
        return nx.number_weakly_connected_components(graph)  # type: ignore[arg-type]
    return nx.number_connected_components(graph)


def _reachable_count(graph: nx.Graph, center_node: Any) -> int:
    if center_node not in graph:
        return 0
    if graph.is_directed():
        return len(nx.descendants(graph, center_node)) + 1  # type: ignore[arg-type]
    return len(nx.node_connected_component(graph, center_node))


def _average_clustering(graph: nx.Graph) -> float:
    if graph.number_of_nodes() == 0:
        return 0.0
    return float(nx.average_clustering(graph))


def _loss_ratio(before: float | int, after: float | int) -> float:
    baseline = float(before)
    if baseline == 0.0:
        return 0.0 if float(after) == 0.0 else -1.0
    return float((baseline - float(after)) / baseline)


def _increase_ratio(before: float | int, after: float | int) -> float:
    baseline = float(before)
    if baseline == 0.0:
        return 0.0 if float(after) == 0.0 else 1.0
    return float((float(after) - baseline) / baseline)


def _edge_pair(value: Any) -> tuple[Any, Any]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ValueError("every edge_to_remove entry must contain two node ids")
    return value[0], value[1]


def simulate_edge_perturbation(
    graph: nx.Graph,
    center_node: int,
    edges_to_remove: list[list[Any]] | list[tuple[Any, Any]],
    radius: int = 3,
) -> dict[str, Any]:
    """Compare a complete ego graph before and after specified edge removals."""

    if isinstance(radius, bool) or not isinstance(radius, int) or radius < 0:
        raise ValueError("radius must be a non-negative integer")
    if center_node not in graph:
        raise ValueError("center_node does not exist in the graph")
    raw_edges: Sequence[Any] = edges_to_remove
    if not isinstance(raw_edges, (list, tuple)):
        raise TypeError("edges_to_remove must be a list of node pairs")

    # Select incoming and outgoing neighbors by undirected distance, but retain
    # the original edge direction inside the induced ego graph.  This is the
    # exact convention used to generate the benchmark ground truth.
    search_graph = graph.to_undirected() if graph.is_directed() else graph
    ego_nodes = nx.single_source_shortest_path_length(
        search_graph,
        center_node,
        cutoff=radius,
    )
    original = graph.subgraph(ego_nodes).copy()
    perturbed = original.copy()

    removed: list[list[Any]] = []
    missing: list[list[Any]] = []
    for raw_edge in raw_edges:
        source, target = _edge_pair(raw_edge)
        if perturbed.has_edge(source, target):
            if perturbed.is_multigraph():
                # A listed relationship removes every parallel realization of
                # that same endpoint pair and is counted once, like the input.
                keys = list(perturbed[source][target])
                for key in keys:
                    perturbed.remove_edge(source, target, key=key)
            else:
                perturbed.remove_edge(source, target)
            removed.append([source, target])
        else:
            missing.append([source, target])

    original_reachable = _reachable_count(original, center_node)
    original_density = float(nx.density(original))
    original_clustering = _average_clustering(original)
    original_components = _component_count(original)
    new_reachable = _reachable_count(perturbed, center_node)
    new_density = float(nx.density(perturbed))
    new_clustering = _average_clustering(perturbed)
    new_components = _component_count(perturbed)

    return {
        "success": True,
        "center_node": center_node,
        "radius": radius,
        "ego_nodes": original.number_of_nodes(),
        "ego_edges_before": original.number_of_edges(),
        "ego_edges_after": perturbed.number_of_edges(),
        "edges_requested_for_removal": len(raw_edges),
        "edges_removed_successfully": len(removed),
        "removed_edges": removed,
        "edges_not_found": len(missing),
        "original_reachable_nodes_in_ego": original_reachable,
        "original_density": original_density,
        "original_clustering": original_clustering,
        "original_components": original_components,
        "new_reachable_nodes_in_ego": new_reachable,
        "new_density": new_density,
        "new_clustering": new_clustering,
        "new_components": new_components,
        # Keep the corpus field spelling as the primary contract.  The
        # descriptive aliases below remain useful to callers outside the
        # benchmark and are computed from the same measured values.
        "impact_ratio_reachability": _loss_ratio(original_reachable, new_reachable),
        "impact_ratio_density": _loss_ratio(original_density, new_density),
        "reachability_impact_ratio": _loss_ratio(original_reachable, new_reachable),
        "density_impact_ratio": _loss_ratio(original_density, new_density),
        "clustering_impact_ratio": _loss_ratio(original_clustering, new_clustering),
        "components_impact_ratio": _increase_ratio(original_components, new_components),
    }


def _main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(
            "Usage: python graph_insight_extensions.py "
            "simulate_edge_perturbation <graph_file> '<json_args>'",
            file=sys.stderr,
        )
        return 2
    action, graph_file = argv[1], argv[2]
    if action != "simulate_edge_perturbation":
        print(f"Unknown action: {action}", file=sys.stderr)
        return 2
    try:
        arguments = json.loads(argv[3]) if len(argv) > 3 else {}
        if not isinstance(arguments, dict):
            raise TypeError("json_args must be an object")
        result = simulate_edge_perturbation(_load_graph(graph_file), **arguments)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    except Exception as exc:
        print(f"Error executing {action}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))

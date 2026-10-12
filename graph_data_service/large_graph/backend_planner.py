"""Select a compute backend without changing the small-graph execution path."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .contracts import BackendPlan, LargeGraphBackend
from .compatibility import GRAPHSCOPE_ONLY_ALGORITHM_NAMES


_DEFAULT_ALLOWED_MODES = frozenset(
    {
        "community_detection:label_propagation",
        "community_detection:louvain",
        "global_metrics:average_clustering",
        "global_metrics:degree_assortativity",
        "global_metrics:transitivity",
        "graph_connectivity:undirected",
        "degree_centrality:default",
        "distance_measures:average_degree_connectivity",
        "path_analysis:average_shortest_path_length",
        "node_centrality:degree",
        "node_centrality:betweenness",
        "node_centrality:closeness",
        "node_centrality:eigenvector",
        "node_centrality:harmonic",
        "node_centrality:katz",
        "node_centrality:pagerank",
        "node_centrality:voterank",
        "shortest_path:distances",
        "shortest_path:weighted_distances",
        "subgraph_extraction:k_core",
        "strongly_connected_components:default",
        "biconnected_components:default",
        "bridges:default",
        "articulation_points:default",
        "minimum_spanning_forest:default",
        "graph_coloring:default",
        "maximal_matching:default",
        "hits:default",
        "k_shell:default",
        "personalized_pagerank:default",
        "approximate_diameter:default",
        "fluid_community:default",
        "densest_subgraph:default",
        "independent_set_cover:maximal_independent_set",
        "independent_set_cover:minimal_dominating_set",
        "independent_set_cover:minimal_vertex_cover",
        "independent_set_cover:minimal_edge_cover",
        "motif_counting:triangle",
        "motif_counting:rectangle",
        "motif_counting:k_clique",
        "motif_counting:diamond",
        "motif_counting:tailed_triangle",
        "motif_counting:three_path",
        "motif_counting:cycle_plus_triangle",
        "motif_counting:cyclic_triangle",
        "motif_counting:acyclic_triangle",
        "motif_counting:in_plus_triangle",
        "motif_counting:out_plus_triangle",
    }
)


@dataclass(frozen=True)
class BackendSelectionError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _configured_backend(requested: str | None) -> LargeGraphBackend:
    raw = str(
        requested
        if requested not in (None, "")
        else os.getenv("LARGE_GRAPH_BACKEND", "legacy")
    ).strip().lower()
    if raw not in {"auto", "graphscope", "legacy"}:
        raise BackendSelectionError(
            "INVALID_BACKEND", "backend 必须是 auto、graphscope 或 legacy"
        )
    return raw  # type: ignore[return-value]


def _allowed_modes() -> frozenset[str]:
    raw = os.getenv("GRAPHSCOPE_ALLOWED_MODES")
    if raw is None:
        return _DEFAULT_ALLOWED_MODES
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def configured_graphscope_modes() -> dict[str, tuple[str, ...]]:
    """Return the deployed GraphScope route names grouped by public algorithm."""

    grouped: dict[str, list[str]] = {}
    for route in sorted(_allowed_modes()):
        algorithm, separator, mode = route.partition(":")
        if not separator or not algorithm or not mode:
            continue
        grouped.setdefault(algorithm, []).append(mode)
    return {algorithm: tuple(modes) for algorithm, modes in grouped.items()}


def _candidate(
    *,
    algorithm: str,
    parameters: dict[str, Any],
    directed: bool,
    has_weights: bool,
    has_self_loops: bool,
) -> tuple[str, str, str] | None:
    name = str(algorithm).strip().lower()
    independent_apps = {
        "maximal_independent_set": "flash_maximal_independent_set",
        "minimal_dominating_set": "flash_minimal_dominating_set",
        "minimal_vertex_cover": "flash_minimal_vertex_cover",
        "minimal_edge_cover": "flash_minimal_edge_cover",
    }
    motif_apps = {
        "triangle": "flash_triangle_counting",
        "rectangle": "flash_rectangle_counting",
        "k_clique": "flash_k_clique_counting",
        "diamond": "flash_diamond_counting",
        "tailed_triangle": "flash_tailed_triangle_counting",
        "three_path": "flash_three_path_counting",
        "cycle_plus_triangle": "flash_cycle_plus_triangle_counting",
        "cyclic_triangle": "flash_cyclic_triangle_counting",
        "acyclic_triangle": "flash_acyclic_triangle_counting",
        "in_plus_triangle": "flash_in_plus_triangle_counting",
        "out_plus_triangle": "flash_out_plus_triangle_counting",
    }
    if name == "independent_set_cover":
        operation = str(parameters.get("operation", "")).strip().lower()
        native_app = independent_apps.get(operation)
        if native_app is not None:
            return f"{name}:{operation}", native_app, "graphscope_native_v1"
        return None
    if name == "motif_counting":
        if not directed:
            return None
        motif = str(parameters.get("motif", "")).strip().lower()
        native_app = motif_apps.get(motif)
        if native_app is not None:
            return f"{name}:{motif}", native_app, "graphscope_native_v1"
        return None
    graphscope_only_apps = {
        "strongly_connected_components": "flash_scc",
        "biconnected_components": "flash_bcc",
        "bridges": "flash_bridges",
        "articulation_points": "flash_cut_point",
        "minimum_spanning_forest": "flash_msf",
        "graph_coloring": "flash_color",
        "maximal_matching": "flash_mm",
        "hits": "hits",
        "k_shell": "k_shell",
        "personalized_pagerank": "flash_ppr",
        "approximate_diameter": "flash_diameter_approximation",
        "fluid_community": "flash_fluid_community",
        "densest_subgraph": "flash_densest_subgraph",
    }
    native_graphscope_only = graphscope_only_apps.get(name)
    if native_graphscope_only is not None and (
        not has_self_loops
        or name
        in {
            "strongly_connected_components",
            "hits",
            "approximate_diameter",
            "fluid_community",
            "densest_subgraph",
        }
    ):
        if name in {"approximate_diameter", "fluid_community", "densest_subgraph"}:
            if bool(parameters.get("strict_semantics", False)):
                return None
            return (
                f"{name}:default",
                native_graphscope_only,
                "graphscope_flash_approx_v1",
            )
        if name == "strongly_connected_components" and not directed:
            return None
        if name in {
            "biconnected_components",
            "bridges",
            "articulation_points",
            "minimum_spanning_forest",
            "graph_coloring",
            "maximal_matching",
            "k_shell",
        } and directed:
            return None
        if name in {"hits", "personalized_pagerank"} and has_weights:
            return None
        if name in {
            "biconnected_components",
            "graph_coloring",
            "personalized_pagerank",
        } and bool(
            parameters.get("strict_semantics", False)
        ):
            return None
        profile = (
            "graphscope_flash_approx_v1"
            if name
            in {"biconnected_components", "graph_coloring", "personalized_pagerank"}
            else "graphscope_native_v1"
        )
        return f"{name}:default", native_graphscope_only, profile
    if name == "degree_centrality":
        return "degree_centrality:default", "degree_centrality", "legacy_v1"
    if name == "graph_connectivity" and not directed:
        return "graph_connectivity:undirected", "wcc", "legacy_v1"
    if (
        name == "node_centrality"
        and str(parameters.get("centrality_type", "degree")).strip().lower()
        == "pagerank"
        and not has_weights
    ):
        return "node_centrality:pagerank", "pagerank_nx", "legacy_v1"
    if (
        name == "shortest_path"
        and str(parameters.get("target", "")).strip().lower() == "all"
        and str(parameters.get("result_detail", "paths")).strip().lower()
        == "distances"
        and not bool(parameters.get("use_weight", True))
    ):
        return "shortest_path:distances", "bfs", "distance_v1"
    if (
        name == "shortest_path"
        and str(parameters.get("target", "")).strip().lower() == "all"
        and str(parameters.get("result_detail", "paths")).strip().lower()
        == "distances"
        and bool(parameters.get("use_weight", True))
    ):
        return "shortest_path:weighted_distances", "sssp", "distance_v1"
    if name == "community_detection" and not directed and not has_self_loops:
        community = str(parameters.get("algorithm", "louvain")).strip().lower()
        if community == "label_propagation" and not has_weights:
            return "community_detection:label_propagation", "cdlp", "graphscope_native_v1"
        if community == "louvain" and not has_weights:
            return "community_detection:louvain", "louvain", "graphscope_native_v1"
    if name == "global_metrics" and not directed and not has_self_loops:
        metric = str(parameters.get("metric", "")).strip().lower()
        if metric == "average_clustering" and not has_weights:
            return "global_metrics:average_clustering", "clustering", "legacy_v1"
        if metric == "transitivity" and not has_weights:
            return "global_metrics:transitivity", "triangles", "legacy_v1"
        if metric == "degree_assortativity" and not has_weights:
            return (
                "global_metrics:degree_assortativity",
                "degree_assortativity_coefficient",
                "legacy_v1",
            )
    if name == "distance_measures" and not has_weights and not has_self_loops:
        metric = str(parameters.get("metric", "")).strip().lower()
        if metric == "average_degree_connectivity":
            return (
                "distance_measures:average_degree_connectivity",
                "average_degree_connectivity",
                "legacy_v1",
            )
    if name == "path_analysis" and not has_weights and not has_self_loops:
        method = str(parameters.get("method", "")).strip().lower()
        if method == "average_shortest_path_length" and not bool(
            parameters.get("strict_semantics", False)
        ):
            return (
                "path_analysis:average_shortest_path_length",
                "average_shortest_path_length",
                "graphscope_native_v1",
            )
    if (
        name == "subgraph_extraction"
        and str(parameters.get("method", "")).strip().lower() == "k_core"
        and not directed
        and not has_self_loops
    ):
        return "subgraph_extraction:k_core", "k_core", "legacy_v1"
    if name == "node_centrality" and not has_weights:
        centrality = str(parameters.get("centrality_type", "degree")).strip().lower()
        native_apps = {
            "degree": "degree_centrality",
            "betweenness": "flash_betweenness",
            "closeness": "flash_closeness",
            "eigenvector": "eigenvector_centrality",
            "harmonic": "flash_harmonic",
            "katz": "katz_centrality",
            "voterank": "voterank",
        }
        native_app = native_apps.get(centrality)
        if native_app is not None and (
            centrality in {"degree", "betweenness"} or not has_self_loops
        ):
            if centrality in {"closeness", "harmonic"} and directed:
                return None
            if centrality in {"betweenness", "closeness", "harmonic"} and bool(
                parameters.get("strict_semantics", False)
            ):
                return None
            profile = (
                "graphscope_flash_approx_v1"
                if centrality in {"betweenness", "closeness", "harmonic"}
                else "legacy_v1"
            )
            return f"node_centrality:{centrality}", native_app, profile
    return None


def resolve_backend_plan(
    *,
    requested_backend: str | None,
    algorithm: str,
    parameters: dict[str, Any],
    directed: bool,
    has_weights: bool,
    has_self_loops: bool = False,
) -> BackendPlan:
    """Resolve one durable backend plan from graph and request semantics."""

    requested = _configured_backend(requested_backend)
    graphscope_only = str(algorithm).strip().lower() in set(
        GRAPHSCOPE_ONLY_ALGORITHM_NAMES
    )
    if requested == "legacy":
        if graphscope_only:
            raise BackendSelectionError(
                "BACKEND_REQUIRED", f"{algorithm} 是 GraphScope 大图专用算法，不能使用 legacy 后端"
            )
        return BackendPlan(requested_backend=requested, selected_backend="legacy")

    candidate = _candidate(
        algorithm=algorithm,
        parameters=parameters,
        directed=directed,
        has_weights=has_weights,
        has_self_loops=has_self_loops,
    )
    adapter_url = os.getenv("GRAPHSCOPE_ADAPTER_URL", "").strip().rstrip("/")
    reason: str | None = None
    if candidate is None:
        reason = "该算法、参数或图语义尚未完成 GraphScope 原生适配"
    elif candidate[0] not in _allowed_modes():
        reason = f"GraphScope 模式 {candidate[0]} 未在部署白名单中启用"
    elif not adapter_url:
        reason = "未配置 GRAPHSCOPE_ADAPTER_URL"

    if reason is not None:
        if requested == "graphscope" or graphscope_only:
            code = (
                "BACKEND_UNAVAILABLE"
                if not adapter_url and candidate is not None
                else "BACKEND_UNSUPPORTED"
            )
            raise BackendSelectionError(code, reason)
        return BackendPlan(
            requested_backend=requested,
            selected_backend="legacy",
            fallback_reason=reason,
        )

    assert candidate is not None
    return BackendPlan(
        requested_backend=requested,
        selected_backend="graphscope",
        native_app=candidate[1],
        semantic_profile=candidate[2],
        adapter_url=adapter_url,
    )

from __future__ import annotations

import os
from pathlib import Path

import pytest

from graph_data_service.app import _is_global_nx_operation
from graph_data_service.large_graph.router import ComputeRouter, ResourceManager
from graph_data_service.tool_catalog import GRAPH_TOOLS


@pytest.fixture
def router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ComputeRouter:
    resources = ResourceManager(
        tmp_path,
        memory_reserve_ratio=0,
        disk_reserve_bytes=0,
    )
    monkeypatch.setattr(
        resources,
        "admit",
        lambda estimate: (True, "ok", dict(estimate)),
    )
    return ComputeRouter(
        resources,
        large_node_threshold=1,
        large_edge_threshold=1,
    )


def _route(
    router: ComputeRouter,
    algorithm: str,
    parameters: dict,
    *,
    nodes: int = 10,
    edges: int = 20,
):
    return router.route(
        operation_scope="global",
        node_count=nodes,
        edge_count=edges,
        algorithm=algorithm,
        snapshot_available=True,
        partitions=2,
        force_large=True,
        parameters=parameters,
    )


def test_local_degree_point_argument_cannot_bypass_large_graph_guard() -> None:
    assert _is_global_nx_operation(
        "degree_analysis", {"node": "A"}, remote_primary=False
    )
    assert not _is_global_nx_operation(
        "degree_analysis", {"node": "A"}, remote_primary=True
    )


def test_large_capability_modes_exactly_match_public_tool_schema(
    router: ComputeRouter,
) -> None:
    public_tools = {str(tool["name"]): tool for tool in GRAPH_TOOLS}
    for algorithm, capability in router.capabilities.items():
        if not capability.modes:
            continue
        mode_key = router._MODE_PARAMETER[algorithm]
        schema_modes = set(
            public_tools[algorithm]["inputSchema"]["properties"][mode_key]["enum"]
        )
        assert schema_modes == set(capability.modes), algorithm


@pytest.mark.parametrize(
    ("algorithm", "parameters"),
    [
        ("path_analysis", {"method": "unknown"}),
        ("global_metrics", {"metric": "unknown"}),
        ("maximum_cycle", {"optimize_by": "unknown"}),
        ("subgraph_extraction", {"method": "ego_graph"}),
        ("maximum_flow", {"source": "A"}),
        ("link_prediction_score", {"method": "jaccard", "source": "A"}),
        ("node_centrality", {"centrality_type": "pagerank", "max_iterations": "many"}),
        ("node_centrality", {"centrality_type": "katz", "alpha": float("nan")}),
    ],
)
def test_invalid_mode_or_parameters_are_rejected_before_queue(
    router: ComputeRouter,
    algorithm: str,
    parameters: dict,
) -> None:
    decision = _route(router, algorithm, parameters)
    assert decision.accepted is False
    assert decision.code in {
        "LARGE_GRAPH_MODE_UNSUPPORTED",
        "INVALID_ALGORITHM_PARAMETERS",
    }


def test_brandes_sample_sources_hard_limit_is_checked_before_queue(
    router: ComputeRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_MAX_SAMPLE_SOURCES", "3")
    rejected = _route(
        router,
        "node_centrality",
        {
            "centrality_type": "betweenness",
            "sample_sources": 4,
            "allow_expensive": True,
        },
    )
    assert rejected.accepted is False
    assert rejected.code == "INVALID_ALGORITHM_PARAMETERS"
    assert "LARGE_GRAPH_MAX_SAMPLE_SOURCES" in rejected.reason

    accepted = _route(
        router,
        "node_centrality",
        {"centrality_type": "betweenness", "sample_sources": 3},
    )
    assert accepted.accepted is True


def test_brandes_configured_default_cannot_exceed_hard_limit(
    router: ComputeRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_MAX_SAMPLE_SOURCES", "3")
    monkeypatch.setenv("LARGE_GRAPH_BRANDES_SOURCES", "4")
    decision = _route(
        router,
        "node_centrality",
        {"centrality_type": "load"},
    )
    assert decision.accepted is False
    assert decision.code == "INVALID_ALGORITHM_PARAMETERS"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("alpha", "nan"),
        ("alpha", "inf"),
        ("beta", "-inf"),
        ("tolerance", "nan"),
        ("damping", "inf"),
    ],
)
def test_non_finite_iterative_parameters_are_rejected_before_queue(
    router: ComputeRouter, name: str, value: str
) -> None:
    decision = _route(
        router,
        "node_centrality",
        {"centrality_type": "katz", name: value},
    )
    assert decision.accepted is False
    assert decision.code == "INVALID_ALGORITHM_PARAMETERS"


def test_large_preview_all_alias_remains_bounded_and_accepted(
    router: ComputeRouter,
) -> None:
    decision = _route(router, "degree_analysis", {"limit": "all"})
    assert decision.accepted is True
    assert decision.path == "large_graph"

    # The legacy auto-router mirrors limit into top_k for the Artifact summary.
    mirrored = _route(
        router, "degree_analysis", {"limit": "all", "top_k": "all"}
    )
    assert mirrored.accepted is True
    assert mirrored.path == "large_graph"


def test_strict_semantics_rejects_approximate_mapping(
    router: ComputeRouter,
) -> None:
    decision = _route(
        router,
        "community_detection",
        {"algorithm": "louvain", "strict_semantics": True},
    )
    assert decision.accepted is False
    assert decision.code == "APPROXIMATION_NOT_ALLOWED"


@pytest.mark.parametrize(
    ("algorithm", "parameters"),
    [
        (
            "all_shortest_paths",
            {"source": "A", "target": "B", "strict_semantics": True},
        ),
        ("find_cycles", {"strict_semantics": True}),
        ("maximum_cycle", {"strict_semantics": True}),
        (
            "clique_analysis",
            {"method": "find_cliques", "strict_semantics": True},
        ),
        (
            "reachability",
            {"method": "transitive_closure", "strict_semantics": True},
        ),
        (
            "shortest_path",
            {
                "source": "A",
                "target": "B",
                "algorithm": "yen_k_shortest",
                "strict_semantics": True,
            },
        ),
        (
            "shortest_path",
            {
                "source": "A",
                "target": "all",
                "algorithm": "default",
                "strict_semantics": True,
            },
        ),
        (
            "path_analysis",
            {
                "method": "all_simple_paths",
                "source": "A",
                "target": "B",
                "strict_semantics": True,
            },
        ),
    ],
)
def test_strict_semantics_rejects_bounded_or_truncated_execution(
    router: ComputeRouter,
    algorithm: str,
    parameters: dict,
) -> None:
    decision = _route(router, algorithm, parameters)
    assert decision.accepted is False
    assert decision.code == "BOUNDED_SEMANTICS_NOT_ALLOWED"


def test_non_strict_bounded_execution_remains_available(
    router: ComputeRouter,
) -> None:
    decision = _route(router, "find_cycles", {})
    assert decision.accepted is True
    assert decision.path == "large_graph"


@pytest.mark.parametrize(
    ("algorithm", "parameters"),
    [
        ("clique_analysis", {"method": "clique_number"}),
        ("clique_analysis", {"method": "number_of_cliques"}),
        ("reachability", {"method": "descendants", "node": "a"}),
        ("reachability", {"method": "ancestors", "node": "a"}),
    ],
)
def test_strict_semantics_allows_untruncated_modes_of_mixed_capabilities(
    router: ComputeRouter, algorithm: str, parameters: dict
) -> None:
    decision = _route(
        router, algorithm, {**parameters, "strict_semantics": True}
    )
    assert decision.accepted is True
    assert decision.path == "large_graph"


def test_large_subgraph_version_creation_is_accepted(
    router: ComputeRouter,
) -> None:
    decision = _route(
        router,
        "subgraph_extraction",
        {"method": "k_core", "create_version": True},
    )
    assert decision.accepted is True
    assert decision.path == "large_graph"


def test_large_subgraph_preview_without_version_is_accepted(
    router: ComputeRouter,
) -> None:
    decision = _route(
        router,
        "subgraph_extraction",
        {"method": "k_core", "create_version": False},
    )
    assert decision.accepted is True
    assert decision.path == "large_graph"


def test_large_subgraph_uses_create_version_default_when_omitted(
    router: ComputeRouter,
) -> None:
    decision = _route(router, "subgraph_extraction", {"method": "k_core"})
    assert decision.accepted is True
    assert decision.path == "large_graph"


@pytest.mark.parametrize(
    ("algorithm", "parameters"),
    [
        ("node_centrality", {"centrality_type": "harmonic"}),
        ("path_analysis", {"method": "average_shortest_path_length"}),
        ("global_metrics", {"metric": "diameter"}),
        ("reachability", {"method": "transitive_closure", "limit": 20}),
    ],
)
def test_repeated_traversal_modes_have_work_admission_guard(
    router: ComputeRouter,
    monkeypatch: pytest.MonkeyPatch,
    algorithm: str,
    parameters: dict,
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_REPEATED_TRAVERSAL_MAX_WORK", "99")
    decision = _route(router, algorithm, parameters, nodes=10, edges=10)
    assert decision.accepted is False
    assert decision.code == "REPEATED_TRAVERSAL_RISK"
    assert decision.resource_estimate is not None
    assert decision.resource_estimate["estimated_work_units"] == 100


def test_triangle_enumeration_has_work_admission_guard(
    router: ComputeRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_TRIANGLE_MAX_WORK", "39")
    decision = _route(
        router,
        "global_metrics",
        {"metric": "transitivity"},
        nodes=10,
        edges=20,
    )
    assert decision.accepted is False
    assert decision.code == "TRIANGLE_ENUMERATION_RISK"
    assert decision.resource_estimate is not None
    assert decision.resource_estimate["estimated_work_units"] == 80


def test_voterank_has_quadratic_work_estimate_and_dedicated_guard(
    router: ComputeRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_VOTERANK_MAX_WORK", "199")
    decision = _route(
        router,
        "node_centrality",
        {"centrality_type": "voterank"},
        nodes=10,
        edges=20,
    )
    assert decision.accepted is False
    assert decision.code == "VOTERANK_WORK_RISK"
    assert decision.resource_estimate is not None
    assert decision.resource_estimate["estimated_work_units"] == 200


def test_voterank_estimate_is_visible_to_generic_work_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = ResourceManager(
        tmp_path,
        memory_reserve_ratio=0,
        disk_reserve_bytes=0,
    )
    monkeypatch.setattr(resources, "available_memory_bytes", lambda: 10**12)
    monkeypatch.setattr(
        "graph_data_service.large_graph.router.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": 10**12})(),
    )
    monkeypatch.setenv("LARGE_GRAPH_VOTERANK_MAX_WORK", "1000")
    monkeypatch.setenv("LARGE_GRAPH_MAX_ESTIMATED_WORK_UNITS", "199")
    guarded_router = ComputeRouter(
        resources,
        large_node_threshold=1,
        large_edge_threshold=1,
    )

    decision = _route(
        guarded_router,
        "node_centrality",
        {"centrality_type": "voterank"},
        nodes=10,
        edges=20,
    )

    assert decision.accepted is False
    assert decision.code == "INSUFFICIENT_RESOURCES"
    assert decision.resource_estimate is not None
    assert decision.resource_estimate["estimated_work_units"] == 200


def test_parallel_source_state_disk_estimate_includes_worker_fanout(
    tmp_path: Path,
) -> None:
    resources = ResourceManager(
        tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0
    )
    serial_like = resources.estimate_algorithm(
        100, 200, 1, "node_centrality", {"centrality_type": "betweenness"}, 1
    )
    parallel = resources.estimate_algorithm(
        100, 200, 4, "node_centrality", {"centrality_type": "betweenness"}, 4
    )
    assert parallel["disk_bytes"] >= serial_like["disk_bytes"]
    # Ignore the fixed 64 MiB floor and verify the raw estimate separately at
    # a scale where graph state dominates it.
    serial_large = resources.estimate_algorithm(
        1_000_000,
        2_000_000,
        1,
        "global_minimum_cut",
        {},
        1,
    )
    parallel_large = resources.estimate_algorithm(
        1_000_000,
        2_000_000,
        4,
        "global_minimum_cut",
        {},
        4,
    )
    expected_workers = max(1, min(4, os.cpu_count() or 1))
    assert parallel_large["active_workers"] == expected_workers
    assert (
        parallel_large["disk_bytes"]
        == serial_large["disk_bytes"] * expected_workers
    )


def test_algorithm_memory_estimate_includes_worker_fanout(tmp_path: Path) -> None:
    resources = ResourceManager(
        tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0
    )
    serial = resources.estimate_algorithm(
        1_000_000, 2_000_000, 1, "degree_analysis", {}, 1
    )
    parallel = resources.estimate_algorithm(
        1_000_000, 2_000_000, 4, "degree_analysis", {}, 4
    )

    assert serial["active_workers"] == 1
    assert parallel["active_workers"] == max(1, min(4, os.cpu_count() or 1))
    assert parallel["memory_bytes"] >= serial["memory_bytes"]


def test_actual_snapshot_bytes_raise_disk_estimate_for_large_shards(
    tmp_path: Path,
) -> None:
    resources = ResourceManager(
        tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0
    )
    compact = resources.estimate_algorithm(
        10,
        10,
        2,
        "degree_analysis",
        {},
        1,
        {"total": 1024},
    )
    # The same V/E counts can occupy far more space when identifiers and JSON
    # records are long or an adjacency shard has grown unusually large.
    large_shards = resources.estimate_algorithm(
        10,
        10,
        2,
        "degree_analysis",
        {},
        1,
        {"total": 128 * 1024**2},
    )
    assert compact["snapshot_storage_bytes"] == 1024
    assert large_shards["snapshot_storage_bytes"] == 128 * 1024**2
    assert large_shards["disk_bytes"] > compact["disk_bytes"]


def test_derived_subgraph_estimate_includes_new_snapshot_copy(
    tmp_path: Path,
) -> None:
    resources = ResourceManager(
        tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0
    )
    storage = {"total": 128 * 1024**2}
    preview_only = resources.estimate_algorithm(
        10,
        10,
        2,
        "subgraph_extraction",
        {"method": "k_core", "create_version": False},
        1,
        storage,
    )
    derived = resources.estimate_algorithm(
        10,
        10,
        2,
        "subgraph_extraction",
        {"method": "k_core", "create_version": True},
        1,
        storage,
    )
    assert preview_only["disk_bytes"] == int(storage["total"] * 1.5)
    assert derived["disk_bytes"] == storage["total"] * 3


def test_global_link_prediction_disk_estimate_includes_worker_state(
    tmp_path: Path,
) -> None:
    resources = ResourceManager(
        tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0
    )
    serial = resources.estimate_algorithm(
        1_000_000,
        2_000_000,
        1,
        "link_prediction_score",
        {"method": "jaccard"},
        1,
    )
    parallel = resources.estimate_algorithm(
        1_000_000,
        2_000_000,
        4,
        "link_prediction_score",
        {"method": "jaccard"},
        4,
    )
    pair_only = resources.estimate_algorithm(
        1_000_000,
        2_000_000,
        4,
        "link_prediction_score",
        {"method": "jaccard", "source": "A", "target": "B"},
        4,
    )

    assert (
        parallel["disk_bytes"]
        == serial["disk_bytes"] * parallel["active_workers"]
    )
    assert pair_only["disk_bytes"] == serial["disk_bytes"]


@pytest.mark.parametrize(
    ("algorithm", "parameters"),
    [
        ("node_centrality", {"centrality_type": "closeness"}),
        ("node_centrality", {"centrality_type": "harmonic"}),
        ("path_analysis", {"method": "average_shortest_path_length"}),
        ("global_metrics", {"metric": "diameter"}),
        ("global_metrics", {"metric": "radius"}),
    ],
)
def test_distance_source_parallelism_reserves_each_worker_state(
    tmp_path: Path, algorithm: str, parameters: dict
) -> None:
    resources = ResourceManager(
        tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0
    )
    serial = resources.estimate_algorithm(
        1_000_000, 2_000_000, 1, algorithm, parameters, 1
    )
    parallel = resources.estimate_algorithm(
        1_000_000, 2_000_000, 4, algorithm, parameters, 4
    )
    assert (
        parallel["disk_bytes"]
        == serial["disk_bytes"] * parallel["active_workers"]
    )


def test_community_admission_counts_every_label_propagation_round(
    tmp_path: Path,
) -> None:
    resources = ResourceManager(
        tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0
    )
    estimate = resources.estimate_algorithm(
        10,
        20,
        2,
        "community_detection",
        {"algorithm": "label_propagation", "max_iterations": 7},
        2,
    )
    assert estimate["estimated_work_units"] == 210


def test_community_iteration_limit_is_rejected_instead_of_silently_clamped(
    router: ComputeRouter,
) -> None:
    decision = _route(
        router,
        "community_detection",
        {"algorithm": "label_propagation", "max_iterations": 501},
    )
    assert decision.accepted is False
    assert decision.code == "INVALID_ALGORITHM_PARAMETERS"
    assert "500" in decision.reason


def test_community_focus_nodes_are_bounded_and_validated(
    router: ComputeRouter,
) -> None:
    accepted = _route(
        router,
        "community_detection",
        {
            "algorithm": "label_propagation",
            "max_iterations": 2,
            "focus_nodes": ["A", "B"],
        },
    )
    assert accepted.accepted is True

    invalid = _route(
        router,
        "community_detection",
        {"algorithm": "label_propagation", "focus_nodes": ["A", ""]},
    )
    assert invalid.accepted is False
    assert invalid.code == "INVALID_ALGORITHM_PARAMETERS"

    oversized = _route(
        router,
        "community_detection",
        {
            "algorithm": "label_propagation",
            "focus_nodes": [str(index) for index in range(201)],
        },
    )
    assert oversized.accepted is False
    assert oversized.code == "ALGORITHM_PARAMETERS_TOO_LARGE"


def test_all_simple_paths_uses_enumeration_guard(
    router: ComputeRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_ENUMERATION_MAX_NODES", "9")
    decision = _route(
        router,
        "path_analysis",
        {"method": "all_simple_paths", "source": "A", "target": "B"},
        nodes=10,
        edges=20,
    )
    assert decision.accepted is False
    assert decision.code == "EXPONENTIAL_ALGORITHM_RISK"


@pytest.mark.parametrize("method", ["clique_number", "number_of_cliques"])
def test_exact_clique_aggregates_have_a_separate_conservative_guard(
    router: ComputeRouter,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_EXACT_CLIQUE_MAX_NODES", "9")
    # Keep the broader bounded-enumeration gate above this graph size to prove
    # the exact aggregate guard is the one making the decision.
    monkeypatch.setenv("LARGE_GRAPH_ENUMERATION_MAX_NODES", "100")
    rejected = _route(
        router,
        "clique_analysis",
        {"method": method},
        nodes=10,
        edges=20,
    )
    assert rejected.accepted is False
    assert rejected.code == "EXACT_CLIQUE_ENUMERATION_RISK"

    acknowledged = _route(
        router,
        "clique_analysis",
        {"method": method, "allow_expensive": True},
        nodes=10,
        edges=20,
    )
    assert acknowledged.accepted is True


def test_invalid_exact_clique_guard_configuration_fails_to_safe_default(
    router: ComputeRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_EXACT_CLIQUE_MAX_NODES", "not-an-integer")
    decision = _route(
        router,
        "clique_analysis",
        {"method": "clique_number"},
        nodes=41,
        edges=80,
    )
    assert decision.accepted is False
    assert decision.code == "EXACT_CLIQUE_ENUMERATION_RISK"


def test_yen_path_state_is_guarded_before_queue(
    router: ComputeRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_YEN_MAX_PATH_STATE_NODES", "20")
    rejected = _route(
        router,
        "shortest_path",
        {"source": "A", "target": "B", "algorithm": "yen_k_shortest", "k": 3},
        nodes=10,
        edges=20,
    )
    assert rejected.accepted is False
    assert rejected.code == "YEN_PATH_STATE_RISK"

    explicit = _route(
        router,
        "shortest_path",
        {
            "source": "A",
            "target": "B",
            "algorithm": "yen_k_shortest",
            "k": 3,
            "allow_expensive": True,
        },
        nodes=10,
        edges=20,
    )
    assert explicit.accepted is True


@pytest.mark.parametrize("k", [0, 201, True])
def test_yen_k_has_an_explicit_large_path_bound(
    router: ComputeRouter, k: object
) -> None:
    decision = _route(
        router,
        "shortest_path",
        {"source": "A", "target": "B", "algorithm": "yen_k_shortest", "k": k},
    )
    assert decision.accepted is False
    assert decision.code == "INVALID_ALGORITHM_PARAMETERS"

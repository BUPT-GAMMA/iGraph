from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from graph_data_service.large_graph.backend_planner import (
    BackendSelectionError,
    resolve_backend_plan,
)
from graph_data_service.large_graph.contracts import BackendPlan, DataQualityPolicy
from graph_data_service.large_graph.engine import LocalParallelGraphEngine
from graph_data_service.large_graph.graphscope_backend import (
    GraphScopeAdapterClient,
    GraphScopeProtocolError,
    graphscope_run_id,
)
from graph_data_service.large_graph.service import LargeGraphService
from graph_data_service.large_graph.router import ComputeRouter, ResourceManager
from graph_data_service.large_graph.runtime import DurableLargeGraphRuntime
from graph_data_service.large_graph.snapshot import PartitionedSnapshotBuilder
from graph_data_service.tool_catalog import get_all_public_tools
from graphscope_runtime.algorithms import (
    _native_graph_directed,
    _open_state,
    _projected_edge_properties,
    _resolve_source,
    _write_graph_csv,
    adapt_average_clustering,
    adapt_average_degree_connectivity,
    adapt_bfs,
    adapt_centrality,
    adapt_component_labels,
    adapt_community,
    adapt_distances,
    adapt_graph_coloring,
    adapt_hits,
    adapt_k_core,
    adapt_k_shell,
    adapt_pagerank,
    adapt_transitivity,
    adapt_wcc,
)


def test_native_projection_keeps_numeric_edges_for_weight_dependent_apps() -> None:
    assert _projected_edge_properties("sssp") == ["weight"]
    assert _projected_edge_properties("louvain") == ["weight"]
    assert _projected_edge_properties("flash_msf") == ["weight"]
    assert _projected_edge_properties("pagerank_nx") == []


def test_flash_connectivity_v2_uses_directed_loader_for_undirected_contract() -> None:
    undirected = {"directed": False}
    assert _native_graph_directed("flash_bcc", undirected) is True
    assert _native_graph_directed("flash_bridges", undirected) is True
    assert _native_graph_directed("flash_cut_point", undirected) is True
    assert _native_graph_directed("flash_msf", undirected) is False


def _state(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    snapshot = tmp_path / "snapshot"
    work = tmp_path / "work"
    result = tmp_path / "results"
    snapshot.mkdir(parents=True)
    work.mkdir()
    result.mkdir()
    (snapshot / "node-order.jsonl").write_text(
        "".join(
            json.dumps({"id": node_id, "name": name}, ensure_ascii=False) + "\n"
            for node_id, name in (("a", "A"), ("b", "B"), ("c", "C"), ("d", "D"))
        ),
        encoding="utf-8",
    )
    connection = _open_state(
        work,
        {"node_order_file": "node-order.jsonl"},
        snapshot,
    )
    return connection, result


def _edge_manifest(tmp_path: Path) -> tuple[dict[str, object], Path]:
    snapshot = tmp_path / "snapshot"
    edges = snapshot / "edges.jsonl"
    edges.write_text(
        "".join(
            json.dumps(
                {"source": source, "target": target, "weight": weight},
                separators=(",", ":"),
            )
            + "\n"
            for source, target, weight in (
                ("a", "b", 1.0),
                ("b", "c", 1.0),
                ("a", "c", 1.0),
                ("c", "d", 1.0),
            )
        ),
        encoding="utf-8",
    )
    return {
        "edge_files": [edges.name],
        "node_order_file": "node-order.jsonl",
        "quality": {"policy": {"node_normalization": "nfkc_casefold"}},
    }, snapshot


def test_backend_planner_only_selects_approved_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHSCOPE_ADAPTER_URL", "http://graphscope-adapter:21113")
    monkeypatch.delenv("GRAPHSCOPE_ALLOWED_MODES", raising=False)

    pagerank = resolve_backend_plan(
        requested_backend="graphscope",
        algorithm="node_centrality",
        parameters={"centrality_type": "pagerank"},
        directed=True,
        has_weights=False,
    )
    assert pagerank.selected_backend == "graphscope"
    assert pagerank.native_app == "pagerank_nx"

    weighted_auto = resolve_backend_plan(
        requested_backend="auto",
        algorithm="node_centrality",
        parameters={"centrality_type": "pagerank"},
        directed=True,
        has_weights=True,
    )
    assert weighted_auto.selected_backend == "legacy"
    assert weighted_auto.fallback_reason

    with pytest.raises(BackendSelectionError) as error:
        resolve_backend_plan(
            requested_backend="graphscope",
            algorithm="shortest_path",
            parameters={
                "source": "A",
                "target": "all",
                "result_detail": "paths",
                "use_weight": False,
            },
            directed=True,
            has_weights=False,
        )
    assert error.value.code == "BACKEND_UNSUPPORTED"


@pytest.mark.parametrize(
    ("algorithm", "parameters", "directed", "has_weights", "native_app"),
    [
        (
            "shortest_path",
            {"source": "A", "target": "all", "result_detail": "distances", "use_weight": True},
            True,
            True,
            "sssp",
        ),
        ("community_detection", {"algorithm": "label_propagation"}, False, False, "cdlp"),
        ("community_detection", {"algorithm": "louvain"}, False, False, "louvain"),
        ("global_metrics", {"metric": "average_clustering"}, False, False, "clustering"),
        ("global_metrics", {"metric": "transitivity"}, False, False, "triangles"),
        ("subgraph_extraction", {"method": "k_core", "k": 3}, False, True, "k_core"),
        ("node_centrality", {"centrality_type": "eigenvector"}, True, False, "eigenvector_centrality"),
        ("node_centrality", {"centrality_type": "katz"}, True, False, "katz_centrality"),
        ("node_centrality", {"centrality_type": "voterank"}, True, False, "voterank"),
        ("degree_centrality", {}, True, True, "degree_centrality"),
        ("node_centrality", {"centrality_type": "degree"}, True, False, "degree_centrality"),
        ("node_centrality", {"centrality_type": "betweenness"}, True, False, "flash_betweenness"),
        ("node_centrality", {"centrality_type": "closeness"}, False, False, "flash_closeness"),
        ("node_centrality", {"centrality_type": "harmonic"}, False, False, "flash_harmonic"),
        ("global_metrics", {"metric": "degree_assortativity"}, False, False, "degree_assortativity_coefficient"),
        ("distance_measures", {"metric": "average_degree_connectivity"}, True, False, "average_degree_connectivity"),
        ("path_analysis", {"method": "average_shortest_path_length"}, True, False, "average_shortest_path_length"),
    ],
)
def test_backend_planner_selects_extended_native_apps(
    monkeypatch: pytest.MonkeyPatch,
    algorithm: str,
    parameters: dict[str, object],
    directed: bool,
    has_weights: bool,
    native_app: str,
) -> None:
    monkeypatch.setenv("GRAPHSCOPE_ADAPTER_URL", "http://graphscope-adapter:21113")
    monkeypatch.delenv("GRAPHSCOPE_ALLOWED_MODES", raising=False)
    plan = resolve_backend_plan(
        requested_backend="graphscope",
        algorithm=algorithm,
        parameters=parameters,
        directed=directed,
        has_weights=has_weights,
        has_self_loops=False,
    )
    assert plan.native_app == native_app


def test_backend_planner_keeps_unverified_or_incompatible_modes_on_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHSCOPE_ADAPTER_URL", "http://graphscope-adapter:21113")
    monkeypatch.delenv("GRAPHSCOPE_ALLOWED_MODES", raising=False)
    for parameters, directed, has_weights, has_self_loops in (
        ({"centrality_type": "closeness"}, True, False, False),
        ({"centrality_type": "harmonic", "strict_semantics": True}, False, False, False),
        ({"centrality_type": "katz"}, False, True, False),
        ({"method": "k_core"}, False, False, True),
    ):
        algorithm = (
            "subgraph_extraction" if "method" in parameters else "node_centrality"
        )
        plan = resolve_backend_plan(
            requested_backend="auto",
            algorithm=algorithm,
            parameters=parameters,
            directed=directed,
            has_weights=has_weights,
            has_self_loops=has_self_loops,
        )
        assert plan.selected_backend == "legacy"


@pytest.mark.parametrize(
    ("algorithm", "parameters"),
    [
        ("degree_centrality", {}),
        ("node_centrality", {"centrality_type": "degree"}),
    ],
)
def test_degree_centrality_accepts_self_loops_on_graphscope(
    monkeypatch: pytest.MonkeyPatch,
    algorithm: str,
    parameters: dict[str, str],
) -> None:
    monkeypatch.setenv("GRAPHSCOPE_ADAPTER_URL", "http://graphscope-adapter:21113")
    monkeypatch.delenv("GRAPHSCOPE_ALLOWED_MODES", raising=False)

    plan = resolve_backend_plan(
        requested_backend="auto",
        algorithm=algorithm,
        parameters=parameters,
        directed=True,
        has_weights=False,
        has_self_loops=True,
    )

    assert plan.selected_backend == "graphscope"
    assert plan.native_app == "degree_centrality"


def test_flash_betweenness_accepts_self_loops_without_relaxing_other_centralities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHSCOPE_ADAPTER_URL", "http://graphscope-adapter:21113")
    monkeypatch.delenv("GRAPHSCOPE_ALLOWED_MODES", raising=False)

    plan = resolve_backend_plan(
        requested_backend="graphscope",
        algorithm="node_centrality",
        parameters={"centrality_type": "betweenness", "sample_sources": 4},
        directed=True,
        has_weights=False,
        has_self_loops=True,
    )

    assert plan.selected_backend == "graphscope"
    assert plan.native_app == "flash_betweenness"

    with pytest.raises(BackendSelectionError, match="尚未完成 GraphScope 原生适配"):
        resolve_backend_plan(
            requested_backend="graphscope",
            algorithm="node_centrality",
            parameters={"centrality_type": "eigenvector"},
            directed=True,
            has_weights=False,
            has_self_loops=True,
        )


def test_backend_planner_rejects_strict_graphscope_semantic_substitutions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHSCOPE_ADAPTER_URL", "http://graphscope-adapter:21113")
    monkeypatch.delenv("GRAPHSCOPE_ALLOWED_MODES", raising=False)
    average = resolve_backend_plan(
        requested_backend="auto",
        algorithm="path_analysis",
        parameters={
            "method": "average_shortest_path_length",
            "strict_semantics": True,
        },
        directed=False,
        has_weights=False,
        has_self_loops=False,
    )
    assert average.selected_backend == "legacy"

    biconnected = resolve_backend_plan(
        requested_backend="auto",
        algorithm="biconnected_components",
        parameters={},
        directed=False,
        has_weights=False,
        has_self_loops=False,
    )
    assert biconnected.semantic_profile == "graphscope_flash_approx_v1"

    with pytest.raises(BackendSelectionError, match="尚未完成 GraphScope 原生适配"):
        resolve_backend_plan(
            requested_backend="auto",
            algorithm="biconnected_components",
            parameters={"strict_semantics": True},
            directed=False,
            has_weights=False,
            has_self_loops=False,
        )


@pytest.mark.parametrize(
    ("algorithm", "parameters", "directed", "has_weights", "native_app"),
    [
        ("strongly_connected_components", {}, True, False, "flash_scc"),
        ("biconnected_components", {}, False, False, "flash_bcc"),
        ("bridges", {}, False, True, "flash_bridges"),
        ("articulation_points", {}, False, False, "flash_cut_point"),
        ("minimum_spanning_forest", {}, False, True, "flash_msf"),
        ("graph_coloring", {}, False, False, "flash_color"),
        ("maximal_matching", {}, False, False, "flash_mm"),
        ("hits", {}, True, False, "hits"),
        ("k_shell", {"k": 2}, False, False, "k_shell"),
        ("personalized_pagerank", {"source": "A"}, True, False, "flash_ppr"),
        ("approximate_diameter", {}, True, True, "flash_diameter_approximation"),
        ("fluid_community", {}, True, True, "flash_fluid_community"),
        ("densest_subgraph", {"rounds": 10}, True, True, "flash_densest_subgraph"),
    ],
)
def test_backend_planner_selects_graphscope_only_algorithms(
    monkeypatch: pytest.MonkeyPatch,
    algorithm: str,
    parameters: dict[str, object],
    directed: bool,
    has_weights: bool,
    native_app: str,
) -> None:
    monkeypatch.setenv("GRAPHSCOPE_ADAPTER_URL", "http://graphscope-adapter:21113")
    monkeypatch.delenv("GRAPHSCOPE_ALLOWED_MODES", raising=False)
    plan = resolve_backend_plan(
        requested_backend="auto",
        algorithm=algorithm,
        parameters=parameters,
        directed=directed,
        has_weights=has_weights,
        has_self_loops=False,
    )
    assert plan.selected_backend == "graphscope"
    assert plan.native_app == native_app

    with pytest.raises(BackendSelectionError, match="GraphScope 大图专用算法"):
        resolve_backend_plan(
            requested_backend="legacy",
            algorithm=algorithm,
            parameters=parameters,
            directed=directed,
            has_weights=has_weights,
            has_self_loops=False,
        )


@pytest.mark.parametrize(
    ("algorithm", "parameters", "native_app"),
    [
        (
            "independent_set_cover",
            {"operation": "maximal_independent_set"},
            "flash_maximal_independent_set",
        ),
        (
            "independent_set_cover",
            {"operation": "minimal_dominating_set"},
            "flash_minimal_dominating_set",
        ),
        (
            "independent_set_cover",
            {"operation": "minimal_vertex_cover"},
            "flash_minimal_vertex_cover",
        ),
        (
            "independent_set_cover",
            {"operation": "minimal_edge_cover"},
            "flash_minimal_edge_cover",
        ),
        ("motif_counting", {"motif": "triangle"}, "flash_triangle_counting"),
        (
            "motif_counting",
            {"motif": "k_clique", "k": 4},
            "flash_k_clique_counting",
        ),
        (
            "motif_counting",
            {"motif": "out_plus_triangle"},
            "flash_out_plus_triangle_counting",
        ),
    ],
)
def test_backend_planner_selects_extended_flash_modes(
    monkeypatch: pytest.MonkeyPatch,
    algorithm: str,
    parameters: dict[str, object],
    native_app: str,
) -> None:
    monkeypatch.setenv("GRAPHSCOPE_ADAPTER_URL", "http://graphscope-adapter:21113")
    monkeypatch.delenv("GRAPHSCOPE_ALLOWED_MODES", raising=False)

    plan = resolve_backend_plan(
        requested_backend="graphscope",
        algorithm=algorithm,
        parameters=parameters,
        directed=True,
        has_weights=True,
        has_self_loops=True,
    )

    assert plan.selected_backend == "graphscope"
    assert plan.native_app == native_app


@pytest.mark.parametrize(
    "algorithm",
    ["approximate_diameter", "fluid_community", "densest_subgraph"],
)
def test_backend_planner_rejects_strict_extended_flash_approximations(
    monkeypatch: pytest.MonkeyPatch, algorithm: str
) -> None:
    monkeypatch.setenv("GRAPHSCOPE_ADAPTER_URL", "http://graphscope-adapter:21113")
    monkeypatch.delenv("GRAPHSCOPE_ALLOWED_MODES", raising=False)

    with pytest.raises(BackendSelectionError, match="尚未完成 GraphScope 原生适配"):
        resolve_backend_plan(
            requested_backend="graphscope",
            algorithm=algorithm,
            parameters={"strict_semantics": True},
            directed=True,
            has_weights=False,
            has_self_loops=False,
        )


def test_backend_planner_rejects_motifs_on_undirected_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHSCOPE_ADAPTER_URL", "http://graphscope-adapter:21113")
    monkeypatch.delenv("GRAPHSCOPE_ALLOWED_MODES", raising=False)

    with pytest.raises(BackendSelectionError, match="尚未完成 GraphScope 原生适配"):
        resolve_backend_plan(
            requested_backend="graphscope",
            algorithm="motif_counting",
            parameters={"motif": "triangle"},
            directed=False,
            has_weights=False,
            has_self_loops=False,
        )


def test_scc_and_hits_accept_self_loops_without_relaxing_other_algorithms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GRAPHSCOPE_ADAPTER_URL", "http://graphscope-adapter:21113")
    monkeypatch.delenv("GRAPHSCOPE_ALLOWED_MODES", raising=False)
    plan = resolve_backend_plan(
        requested_backend="graphscope",
        algorithm="strongly_connected_components",
        parameters={},
        directed=True,
        has_weights=False,
        has_self_loops=True,
    )
    assert plan.selected_backend == "graphscope"
    assert plan.native_app == "flash_scc"

    hits = resolve_backend_plan(
        requested_backend="graphscope",
        algorithm="hits",
        parameters={},
        directed=True,
        has_weights=False,
        has_self_loops=True,
    )
    assert hits.selected_backend == "graphscope"
    assert hits.native_app == "hits"

    with pytest.raises(BackendSelectionError, match="尚未完成 GraphScope 原生适配"):
        resolve_backend_plan(
            requested_backend="graphscope",
            algorithm="biconnected_components",
            parameters={},
            directed=False,
            has_weights=False,
            has_self_loops=True,
        )


def test_backend_plan_keeps_old_durable_jobs_on_legacy() -> None:
    assert BackendPlan.from_dict(None).selected_backend == "legacy"
    with pytest.raises(ValueError, match="未知大图后端计划字段"):
        BackendPlan.from_dict({"selected_backend": "legacy", "unexpected": True})


def test_large_graph_tool_exposes_backend_and_distance_only_mode() -> None:
    tools = {tool["name"]: tool for tool in get_all_public_tools()}
    schema = tools["run_large_graph_algorithm"]["inputSchema"]
    assert schema["properties"]["backend"]["enum"] == [
        "auto",
        "graphscope",
        "legacy",
    ]
    result_detail = schema["properties"]["parameters"]["properties"]["result_detail"]
    assert result_detail["enum"] == ["distances", "paths"]
    algorithms = schema["properties"]["algorithm"]["enum"]
    assert {
        "approximate_diameter",
        "fluid_community",
        "densest_subgraph",
        "independent_set_cover",
        "motif_counting",
    }.issubset(algorithms)
    parameter_properties = schema["properties"]["parameters"]["properties"]
    assert parameter_properties["operation"]["enum"] == [
        "maximal_independent_set",
        "minimal_dominating_set",
        "minimal_vertex_cover",
        "minimal_edge_cover",
    ]
    assert "k_clique" in parameter_properties["motif"]["enum"]
    assert parameter_properties["rounds"]["maximum"] == 1000


def test_router_validates_extended_flash_parameters_and_graph_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resources = ResourceManager(tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0)
    monkeypatch.setattr(resources, "admit", lambda estimate: (True, "ok", dict(estimate)))
    router = ComputeRouter(resources, large_node_threshold=1, large_edge_threshold=1)
    common = {
        "operation_scope": "global",
        "node_count": 10,
        "edge_count": 20,
        "weighted": False,
        "snapshot_available": True,
        "partitions": 1,
        "workers": 1,
        "force_large": True,
    }

    missing_operation = router.route(
        **common,
        algorithm="independent_set_cover",
        directed=True,
        parameters={},
    )
    assert missing_operation.code == "ALGORITHM_MODE_REQUIRED"

    undirected_motif = router.route(
        **common,
        algorithm="motif_counting",
        directed=False,
        parameters={"motif": "triangle"},
    )
    assert undirected_motif.code == "UNDIRECTED_UNSUPPORTED"

    missing_k = router.route(
        **common,
        algorithm="motif_counting",
        directed=True,
        parameters={"motif": "k_clique"},
    )
    assert missing_k.code == "INVALID_ALGORITHM_PARAMETERS"

    invalid_k = router.route(
        **common,
        algorithm="motif_counting",
        directed=True,
        parameters={"motif": "k_clique", "k": 9},
    )
    assert invalid_k.code == "INVALID_ALGORITHM_PARAMETERS"

    valid_cover = router.route(
        **common,
        algorithm="independent_set_cover",
        directed=True,
        parameters={"operation": "minimal_vertex_cover"},
    )
    assert valid_cover.accepted is True

    strict_diameter = router.route(
        **common,
        algorithm="approximate_diameter",
        directed=True,
        parameters={"strict_semantics": True},
    )
    assert strict_diameter.code == "APPROXIMATION_NOT_ALLOWED"


def test_distance_only_shortest_path_is_exact_and_strict_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resources = ResourceManager(tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0)
    monkeypatch.setattr(
        resources,
        "admit",
        lambda estimate: (True, "ok", dict(estimate)),
    )
    router = ComputeRouter(resources, large_node_threshold=1, large_edge_threshold=1)
    accepted = router.route(
        operation_scope="global",
        node_count=10,
        edge_count=20,
        algorithm="shortest_path",
        directed=True,
        weighted=False,
        snapshot_available=True,
        partitions=1,
        workers=1,
        force_large=True,
        parameters={
            "source": "A",
            "target": "all",
            "use_weight": False,
            "result_detail": "distances",
            "strict_semantics": True,
        },
    )
    assert accepted.accepted is True

    rejected = router.route(
        operation_scope="global",
        node_count=10,
        edge_count=20,
        algorithm="shortest_path",
        directed=True,
        weighted=False,
        snapshot_available=True,
        partitions=1,
        workers=1,
        force_large=True,
        parameters={
            "source": "A",
            "target": "B",
            "use_weight": False,
            "result_detail": "distances",
        },
    )
    assert rejected.accepted is False
    assert rejected.code == "INVALID_ALGORITHM_PARAMETERS"


def test_brandes_workload_limit_reports_graph_specific_source_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resources = ResourceManager(tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0)
    monkeypatch.setattr(resources, "available_memory_bytes", lambda: 10**15)
    monkeypatch.setattr(
        "graph_data_service.large_graph.router.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": 10**15})(),
    )
    monkeypatch.setenv("LARGE_GRAPH_MAX_ESTIMATED_WORK_UNITS", "40000000000")
    monkeypatch.setenv("LARGE_GRAPH_BRANDES_SOURCES", "256")
    router = ComputeRouter(resources, large_node_threshold=1, large_edge_threshold=1)

    decision = router.route(
        operation_scope="global",
        node_count=18_484_117,
        edge_count=298_113_762,
        algorithm="node_centrality",
        directed=True,
        weighted=False,
        snapshot_available=True,
        partitions=16,
        workers=8,
        force_large=True,
        parameters={"centrality_type": "betweenness", "sample_sources": 1000},
    )

    assert decision.accepted is False
    assert decision.code == "BRANDES_SAMPLE_WORK_LIMIT"
    assert "sample_sources=134" in decision.reason
    assert resources.default_brandes_sample_sources(18_484_117, 298_113_762) == 134


def test_wcc_adapter_keeps_large_component_labels_lossless(tmp_path: Path) -> None:
    connection, result_root = _state(tmp_path)
    native = tmp_path / "wcc.csv"
    native.write_text(
        "node_id,value\n"
        "a,18446744073709551614\n"
        "b,18446744073709551614\n"
        "c,18446744073709551615\n"
        "d,18446744073709551615\n",
        encoding="utf-8",
    )
    try:
        result = adapt_wcc(connection, [native], result_root, limit=5)
    finally:
        connection.close()

    assert result["summary"]["connected_components"]["count"] == 2
    assert result["summary"]["connected_components"]["largest_size"] == 2
    rows = [
        json.loads(line)
        for line in (result_root / "components.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 4
    assert {row["component"] for row in rows} == {0, 1}


def test_pagerank_adapter_streams_complete_artifact_and_summary(tmp_path: Path) -> None:
    connection, result_root = _state(tmp_path)
    # No header exercises the multi-worker data-sink variant.
    native = tmp_path / "pagerank-part-0.csv"
    native.write_text("a,0.4\nb,0.3\nc,0.2\nd,0.1\n", encoding="utf-8")
    try:
        result = adapt_pagerank(connection, [native], result_root, limit=2)
    finally:
        connection.close()

    summary = result["summary"]
    assert summary["execution_algorithm"] == "graphscope_pagerank_nx"
    assert summary["value_stats"]["median"] == pytest.approx(0.25)
    assert [row["node"] for row in summary["top_nodes"]] == ["A", "B"]
    assert len((result_root / "centrality-pagerank.jsonl").read_text().splitlines()) == 4


def test_bfs_adapter_excludes_source_and_unreachable_sentinel(tmp_path: Path) -> None:
    connection, result_root = _state(tmp_path)
    native = tmp_path / "bfs.csv"
    native.write_text(
        "node_id,value\na,0\nb,1\nc,2\nd,1.7976931348623157e+308\n",
        encoding="utf-8",
    )
    try:
        result = adapt_bfs(
            connection,
            [native],
            result_root,
            source="a",
            limit=20,
        )
    finally:
        connection.close()

    assert result["summary"]["reachable_count"] == 2
    assert result["summary"]["source"] == "A"
    assert result["preview"] == [
        {"target": "B", "distance": 1.0},
        {"target": "C", "distance": 2.0},
    ]


def test_sssp_adapter_preserves_weighted_distances_above_node_count(tmp_path: Path) -> None:
    connection, result_root = _state(tmp_path)
    native = tmp_path / "sssp.csv"
    native.write_text(
        "node_id,value\na,0\nb,10.5\nc,21\nd,1.7976931348623157e+308\n",
        encoding="utf-8",
    )
    try:
        result = adapt_distances(
            connection,
            [native],
            result_root,
            source="a",
            weighted=True,
            native_app="sssp",
            limit=20,
        )
    finally:
        connection.close()

    assert result["summary"]["weighted"] is True
    assert result["summary"]["reachable_count"] == 2
    assert result["summary"]["max_distance"] == 21.0


def test_graphscope_csv_keeps_numeric_weights_and_rejects_negative_sssp(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    work = tmp_path / "work"
    snapshot.mkdir()
    work.mkdir()
    (snapshot / "node-order.jsonl").write_text(
        '{"id":"a"}\n{"id":"b"}\n', encoding="utf-8"
    )
    edge_file = snapshot / "edges.jsonl"
    edge_file.write_text(
        '{"source":"a","target":"b","weight":2.5}\n', encoding="utf-8"
    )
    manifest = {"node_order_file": "node-order.jsonl", "edge_files": ["edges.jsonl"]}
    _, csv_path = _write_graph_csv(
        manifest, snapshot, work, require_nonnegative_weights=True
    )
    assert csv_path.read_text(encoding="utf-8").splitlines() == [
        "source,target,weight",
        "a,b,2.5",
    ]

    edge_file.write_text(
        '{"source":"a","target":"b","weight":-1}\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="非负边权"):
        _write_graph_csv(
            manifest, snapshot, work, require_nonnegative_weights=True
        )


def test_extended_flash_csv_filters_self_loops(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    work = tmp_path / "work"
    snapshot.mkdir()
    work.mkdir()
    (snapshot / "node-order.jsonl").write_text(
        '{"id":"a"}\n{"id":"b"}\n', encoding="utf-8"
    )
    (snapshot / "edges.jsonl").write_text(
        '{"source":"a","target":"a","weight":1}\n'
        '{"source":"a","target":"b","weight":2}\n',
        encoding="utf-8",
    )
    manifest = {
        "node_order_file": "node-order.jsonl",
        "edge_files": ["edges.jsonl"],
    }

    _, csv_path = _write_graph_csv(
        manifest,
        snapshot,
        work,
        require_nonnegative_weights=False,
        skip_self_loops=True,
    )

    assert csv_path.read_text(encoding="utf-8").splitlines() == [
        "source,target,weight",
        "a,b,2.0",
    ]


def test_extended_native_adapters_preserve_existing_output_contracts(tmp_path: Path) -> None:
    connection, result_root = _state(tmp_path / "community")
    manifest, snapshot_root = _edge_manifest(tmp_path / "community")
    labels = tmp_path / "community.csv"
    labels.write_text("node_id,value\na,0\nb,0\nc,0\nd,1\n", encoding="utf-8")
    try:
        community = adapt_community(
            connection,
            [labels],
            result_root,
            manifest=manifest,
            snapshot_root=snapshot_root,
            algorithm="louvain",
            native_app="louvain",
            parameters={"focus_nodes": ["A", "missing"]},
            limit=10,
        )
    finally:
        connection.close()
    assert community["summary"]["num_communities"] == 2
    assert community["summary"]["modularity"] == pytest.approx(-0.03125)
    assert community["summary"]["focus_node_communities"][0]["node"] == "A"
    assert community["summary"]["missing_focus_nodes"] == ["missing"]
    assert len((result_root / "communities.jsonl").read_text().splitlines()) == 4

    connection, result_root = _state(tmp_path / "metrics")
    manifest, snapshot_root = _edge_manifest(tmp_path / "metrics")
    clustering_native = tmp_path / "clustering.csv"
    clustering_native.write_text(
        "node_id,value\na,1\nb,1\nc,0.3333333333333333\nd,0\n",
        encoding="utf-8",
    )
    try:
        clustering = adapt_average_clustering(
            connection, [clustering_native], result_root
        )
    finally:
        connection.close()
    assert clustering["summary"]["value"] == pytest.approx(0.583333)

    connection, result_root = _state(tmp_path / "transitivity")
    manifest, snapshot_root = _edge_manifest(tmp_path / "transitivity")
    triangles = tmp_path / "triangles.csv"
    triangles.write_text(
        "node_id,value\na,1\nb,1\nc,1\nd,0\n", encoding="utf-8"
    )
    try:
        transitivity = adapt_transitivity(
            connection,
            [triangles],
            result_root,
            manifest=manifest,
            snapshot_root=snapshot_root,
        )
    finally:
        connection.close()
    assert transitivity["summary"]["value"] == pytest.approx(0.6)

    connection, result_root = _state(tmp_path / "core")
    manifest, snapshot_root = _edge_manifest(tmp_path / "core")
    core = tmp_path / "core.csv"
    core.write_text("node_id,value\na,1\nb,1\nc,1\nd,0\n", encoding="utf-8")
    try:
        k_core = adapt_k_core(
            connection,
            [core],
            result_root,
            manifest=manifest,
            snapshot_root=snapshot_root,
            limit=10,
        )
    finally:
        connection.close()
    assert k_core["summary"]["num_nodes"] == 3
    assert k_core["summary"]["num_edges"] == 3
    rows = [
        json.loads(line)
        for line in (result_root / "subgraph-k_core.jsonl").read_text().splitlines()
    ]
    assert sum(row["kind"] == "node" for row in rows) == 3
    assert sum(row["kind"] == "edge" for row in rows) == 3


def test_generic_native_centrality_adapter_keeps_stable_ranking(tmp_path: Path) -> None:
    connection, result_root = _state(tmp_path)
    native = tmp_path / "eigenvector.csv"
    native.write_text("node_id,value\na,0.1\nb,0.4\nc,0.3\nd,0.2\n", encoding="utf-8")
    try:
        result = adapt_centrality(
            connection,
            [native],
            result_root,
            kind="eigenvector",
            native_app="eigenvector_centrality",
            limit=2,
        )
    finally:
        connection.close()
    assert result["summary"]["execution_algorithm"] == "graphscope_eigenvector_centrality"
    assert [row["node"] for row in result["summary"]["top_nodes"]] == ["B", "C"]


def test_voterank_adapter_omits_vertices_that_were_not_elected(tmp_path: Path) -> None:
    connection, result_root = _state(tmp_path)
    native = tmp_path / "voterank.csv"
    native.write_text("node_id,value\na,4\nb,0\nc,2\nd,0\n", encoding="utf-8")
    try:
        result = adapt_centrality(
            connection,
            [native],
            result_root,
            kind="voterank",
            native_app="voterank",
            limit=20,
            positive_only=True,
        )
    finally:
        connection.close()
    assert result["summary"]["total_nodes"] == 2
    assert [row["node"] for row in result["summary"]["top_nodes"]] == ["A", "C"]
    assert len((result_root / "centrality-voterank.jsonl").read_text().splitlines()) == 2


def test_new_graphscope_adapters_stream_bounded_control_results(tmp_path: Path) -> None:
    mapping_root = tmp_path / "mapping-results"
    mapping_root.mkdir()
    mapping = adapt_average_degree_connectivity(
        mapping_root, {1: 2.5, 2: 1.25}, limit=1
    )
    assert mapping["summary"]["count"] == 2
    assert mapping["summary"]["truncated"] is True

    connection, result_root = _state(tmp_path / "components")
    labels = tmp_path / "components.csv"
    labels.write_text("node_id,value\na,10\nb,10\nc,20\nd,20\n", encoding="utf-8")
    try:
        components = adapt_component_labels(
            connection,
            [labels],
            result_root,
            algorithm="strongly_connected_components",
            native_app="flash_scc",
            limit=10,
        )
    finally:
        connection.close()
    assert components["summary"]["count"] == 2

    connection, result_root = _state(tmp_path / "bcc")
    labels = tmp_path / "bcc.csv"
    labels.write_text("node_id,value\na,10\nb,10\nc,20\nd,30\n", encoding="utf-8")
    try:
        components = adapt_component_labels(
            connection,
            [labels],
            result_root,
            algorithm="biconnected_components",
            native_app="flash_bcc",
            limit=10,
        )
    finally:
        connection.close()
    assert components["summary"]["execution_mode"] == "approximate"
    assert components["summary"]["approximate"] is True

def test_hits_coloring_and_k_shell_adapters_preserve_node_ids(tmp_path: Path) -> None:
    connection, result_root = _state(tmp_path / "hits")
    native = tmp_path / "hits.csv"
    native.write_text(
        "0.1,0.4,a\n0.2,0.3,b\n0.3,0.2,c\n0.4,0.1,d\n",
        encoding="utf-8",
    )
    try:
        hits = adapt_hits(connection, [native], result_root, limit=2)
    finally:
        connection.close()
    assert [row["node"] for row in hits["summary"]["top_hubs"]] == ["A", "B"]
    assert [row["node"] for row in hits["summary"]["top_authorities"]] == ["D", "C"]

    connection, result_root = _state(tmp_path / "color")
    native = tmp_path / "color.csv"
    native.write_text("node_id,value\na,0\nb,1\nc,2\nd,0\n", encoding="utf-8")
    try:
        coloring = adapt_graph_coloring(connection, [native], result_root, limit=2)
    finally:
        connection.close()
    assert coloring["summary"]["num_colors"] == 3

    connection, result_root = _state(tmp_path / "shell")
    native = tmp_path / "shell.csv"
    native.write_text(
        "node_id,value\na,\x01\nb,\x00\nc,\x01\nd,\x00\n", encoding="utf-8"
    )
    try:
        shell = adapt_k_shell(
            connection, [native], result_root, k=2, limit=10
        )
    finally:
        connection.close()
    assert shell["summary"]["num_nodes"] == 2
    assert shell["summary"]["nodes"] == ["A", "C"]


def test_adapter_result_files_are_bound_to_staging_results(tmp_path: Path) -> None:
    result_root = tmp_path / "results"
    result_root.mkdir()
    (result_root / "scores.jsonl").write_text("{}\n", encoding="utf-8")
    plan = BackendPlan(
        requested_backend="graphscope",
        selected_backend="graphscope",
        native_app="wcc",
        adapter_url="http://adapter:21113",
    )
    result = GraphScopeAdapterClient._validated_result(
        {
            "result": {
                "summary": {"success": True},
                "preview": [],
                "result_files": ["scores.jsonl"],
            }
        },
        result_root=result_root,
        partitions=1,
        plan=plan,
    )
    assert result["result_files"] == ["results/scores.jsonl"]

    with pytest.raises(GraphScopeProtocolError, match="非法结果文件路径"):
        GraphScopeAdapterClient._validated_result(
            {
                "result": {
                    "summary": {"success": True},
                    "preview": [],
                    "result_files": ["../escape.jsonl"],
                }
            },
            result_root=result_root,
            partitions=1,
            plan=plan,
        )


def test_graphscope_run_id_is_deterministic_and_path_safe() -> None:
    assert graphscope_run_id("job/a", "lease:b") == "job_a.lease_b"


def test_remote_cancel_requires_a_terminal_adapter_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GraphScopeAdapterClient(
        "http://adapter:21113", service_key="test-key"
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: {"status": "cancelled"},
    )
    assert client.cancel("job.lease") is True
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: {"status": "cancelling"},
    )
    assert client.cancel("job.lease") is False


def test_external_runner_reuses_existing_artifact_publisher(tmp_path: Path) -> None:
    source = tmp_path / "edges.txt"
    source.write_text("A B\nB C\n", encoding="utf-8")
    stat = source.stat()
    snapshot_root = tmp_path / "snapshots"
    snapshot = PartitionedSnapshotBuilder(snapshot_root).build(
        source_path=source,
        user_scope="scope",
        graph_id="g1",
        graph_version=1,
        directed=False,
        partitions=1,
        workers=1,
        delimiter="whitespace",
        input_format="edgelist",
        policy=DataQualityPolicy(),
        expected_source={"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
    )

    def external_runner(**runner):
        output = Path(runner["result_root"]) / "components.jsonl"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps({"node_id": "a", "node": "A", "component": 0}) + "\n",
            encoding="utf-8",
        )
        return {
            "summary": {
                "success": True,
                "directed": False,
                "is_connected": True,
                "execution_mode": "exact",
            },
            "preview": [{"node_id": "a", "node": "A", "component": 0}],
            "result_files": ["results/components.jsonl"],
            "backend_metadata": {
                "engine": "graphscope",
                "engine_version": "test",
                "backend": "graphscope",
                "native_app": "wcc",
                "protocol_version": "igraph-graphscope-v1",
                "semantic_profile": "legacy_v1",
                "topology_access": "graphscope-native-shared-snapshot-v1",
            },
        }

    result = LocalParallelGraphEngine(
        tmp_path / "artifacts", snapshot_root=snapshot_root
    ).execute(
        snapshot=snapshot,
        algorithm="graph_connectivity",
        parameters={"limit": 5},
        user_scope="scope",
        graph_id="g1",
        graph_version=1,
        workers=1,
        external_runner=external_runner,
    )

    manifest = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert manifest["metadata"]["engine"] == "graphscope"
    assert manifest["metadata"]["native_app"] == "wcc"
    assert manifest["result_files"] == ["results/components.jsonl"]


def test_legacy_fallback_honors_distance_only_result_contract(tmp_path: Path) -> None:
    source = tmp_path / "distance-edges.txt"
    source.write_text("A B\nB C\n", encoding="utf-8")
    stat = source.stat()
    snapshot_root = tmp_path / "distance-snapshots"
    snapshot = PartitionedSnapshotBuilder(snapshot_root).build(
        source_path=source,
        user_scope="scope",
        graph_id="g1",
        graph_version=1,
        directed=True,
        partitions=1,
        workers=1,
        delimiter="whitespace",
        input_format="edgelist",
        policy=DataQualityPolicy(),
        expected_source={"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
    )
    result = LocalParallelGraphEngine(
        tmp_path / "distance-artifacts", snapshot_root=snapshot_root
    ).execute(
        snapshot=snapshot,
        algorithm="shortest_path",
        parameters={
            "source": "A",
            "target": "all",
            "use_weight": False,
            "result_detail": "distances",
        },
        user_scope="scope",
        graph_id="g1",
        graph_version=1,
        workers=1,
    )

    assert result["summary"]["result_detail"] == "distances"
    assert result["summary"]["nearest_distances"] == [
        {"target": "B", "distance": 1.0},
        {"target": "C", "distance": 2.0},
    ]
    manifest = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert all("nearest-shortest-paths" not in path for path in manifest["result_files"])


def test_service_persists_graphscope_plan_with_compute_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime"
    service = LargeGraphService(runtime_root, embedded_worker=False)
    service.resources.disk_reserve_bytes = 0
    monkeypatch.setattr(
        service.resources,
        "admit",
        lambda estimate: (True, "ok", dict(estimate)),
    )
    monkeypatch.setattr(service, "_execution_binding", lambda *_: {})
    monkeypatch.setenv("GRAPHSCOPE_ADAPTER_URL", "http://graphscope-adapter:21113")
    monkeypatch.setenv("LARGE_GRAPH_BACKEND", "auto")
    monkeypatch.setenv("LARGE_GRAPH_PRECOMPUTE_ADJACENCY", "false")
    source = tmp_path / "catalog-edges.txt"
    source.write_text("A B\nB C\n", encoding="utf-8")
    stat = source.stat()
    policy = DataQualityPolicy()
    scope = service._scope_component("alice", "s1")
    try:
        prepare_job_id, graph_version = service.catalog.reserve_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source={"filename": source.name, "size": stat.st_size},
            policy_id=policy.policy_id,
            policy=policy.to_dict(),
            request={"source_path": str(source), "user_scope": scope},
        )
        claimed = service.catalog.claim_next_job()
        assert claimed is not None
        snapshot = PartitionedSnapshotBuilder(runtime_root / "snapshots").build(
            source_path=source,
            user_scope=scope,
            graph_id="g1",
            graph_version=graph_version,
            directed=False,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            policy=policy,
            expected_source={"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        )
        manifest = json.loads(Path(snapshot["manifest_path"]).read_text(encoding="utf-8"))
        assert manifest["storage_profile"] == "graphscope_compact"
        assert "adjacency_files" not in manifest
        assert service.catalog.publish_snapshot(
            prepare_job_id, snapshot, lease_id=claimed["lease_id"]
        )

        legacy = service.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=graph_version,
            algorithm="graph_connectivity",
            parameters={"limit": 5},
            backend="legacy",
            workers=1,
        )
        assert legacy["success"] is False
        assert legacy["code"] == "SNAPSHOT_ADJACENCY_REQUIRED"

        submitted = service.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=graph_version,
            algorithm="graph_connectivity",
            parameters={"limit": 5},
            backend="graphscope",
            workers=1,
        )
        assert submitted["success"] is True
        assert submitted["backend_plan"]["native_app"] == "wcc"
        assert submitted["route_decision"]["execution_engine"] == {
            "backend": "graphscope",
            "engine": "graphscope-analytical-engine",
            "engine_version": "0.29.0",
            "native_app": "wcc",
            "semantic_profile": "legacy_v1",
        }
        durable = service.catalog.get_job(
            submitted["job_id"], user_id="alice", session_id="s1"
        )
        assert durable is not None
        assert durable["request"]["backend_plan"]["selected_backend"] == "graphscope"

        monkeypatch.setenv("LARGE_GRAPH_BRANDES_SOURCES", "2")
        sampled = service.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=graph_version,
            algorithm="node_centrality",
            parameters={"centrality_type": "betweenness"},
            backend="graphscope",
            workers=1,
        )
        assert sampled["success"] is True
        assert sampled["backend_plan"]["native_app"] == "flash_betweenness"
        sampled_job = service.catalog.get_job(
            sampled["job_id"], user_id="alice", session_id="s1"
        )
        assert sampled_job is not None
        assert sampled_job["request"]["parameters"]["sample_sources"] == 2

        capabilities = {
            row["algorithm"]: row for row in service.capabilities()
        }
        degree = capabilities["degree_centrality"]
        assert degree["preferred_backend"] == "graphscope"
        assert degree["available_backends"] == ["graphscope", "legacy"]
        assert degree["backend_routes"][0]["modes"] == ["default"]
    finally:
        service.close()


def test_adapter_revalidates_snapshot_and_algorithm_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException
    from graphscope_runtime.server import RunRegistry

    snapshot_root = tmp_path / "snapshots"
    artifact_root = tmp_path / "artifacts"
    snapshot_dir = snapshot_root / "scope" / "g1" / "v00000001" / "snap_test"
    result_root = artifact_root / "scope" / "g1" / "v00000001" / ".run" / "results"
    work_root = result_root.parent / "work"
    snapshot_dir.mkdir(parents=True)
    result_root.mkdir(parents=True)
    work_root.mkdir()
    manifest_path = snapshot_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "state": "published",
                "directed": False,
                "has_weights": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GRAPHSCOPE_SNAPSHOT_ROOT", str(snapshot_root))
    monkeypatch.setenv("GRAPHSCOPE_ARTIFACT_ROOT", str(artifact_root))
    registry = RunRegistry()
    payload = {
        "protocol_version": "igraph-graphscope-v1",
        "native_app": "wcc",
        "semantic_profile": "legacy_v1",
        "manifest_path": str(manifest_path),
        "result_root": str(result_root),
        "work_root": str(work_root),
        "algorithm": "graph_connectivity",
        "parameters": {"limit": 5},
        "workers": 1,
        "top_k": 20,
    }
    assert registry.validate_payload(payload)["native_app"] == "wcc"

    manifest_path.write_text(
        json.dumps(
            {
                "state": "published",
                "directed": True,
                "has_weights": False,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(HTTPException, match="已批准的语义配置"):
        registry.validate_payload(payload)

    manifest_path.write_text(
        json.dumps(
            {
                "state": "published",
                "directed": True,
                "has_weights": True,
                "quality": {"self_loops_seen": 0, "self_loops_skipped": 0},
            }
        ),
        encoding="utf-8",
    )
    sssp_payload = {
        **payload,
        "native_app": "sssp",
        "semantic_profile": "distance_v1",
        "algorithm": "shortest_path",
        "parameters": {
            "source": "A",
            "target": "all",
            "result_detail": "distances",
            "use_weight": True,
        },
    }
    assert registry.validate_payload(sssp_payload)["native_app"] == "sssp"

    manifest_path.write_text(
        json.dumps(
            {
                "state": "published",
                "directed": False,
                "has_weights": False,
                "quality": {"self_loops_seen": 1, "self_loops_skipped": 0},
            }
        ),
        encoding="utf-8",
    )
    louvain_payload = {
        **payload,
        "native_app": "louvain",
        "semantic_profile": "graphscope_native_v1",
        "algorithm": "community_detection",
        "parameters": {"algorithm": "louvain"},
    }
    with pytest.raises(HTTPException, match="已批准的语义配置"):
        registry.validate_payload(louvain_payload)

    degree_payload = {
        **payload,
        "native_app": "degree_centrality",
        "semantic_profile": "legacy_v1",
        "algorithm": "degree_centrality",
        "parameters": {"limit": 20},
    }
    assert registry.validate_payload(degree_payload)["native_app"] == "degree_centrality"

    manifest_path.write_text(
        json.dumps(
            {
                "state": "published",
                "directed": True,
                "has_weights": False,
                "quality": {"self_loops_seen": 1, "self_loops_skipped": 0},
            }
        ),
        encoding="utf-8",
    )
    scc_payload = {
        **payload,
        "native_app": "flash_scc",
        "semantic_profile": "graphscope_native_v1",
        "algorithm": "strongly_connected_components",
        "parameters": {},
    }
    assert registry.validate_payload(scc_payload)["native_app"] == "flash_scc"

    hits_payload = {
        **payload,
        "native_app": "hits",
        "semantic_profile": "graphscope_native_v1",
        "algorithm": "hits",
        "parameters": {},
    }
    assert registry.validate_payload(hits_payload)["native_app"] == "hits"

    betweenness_payload = {
        **payload,
        "native_app": "flash_betweenness",
        "semantic_profile": "graphscope_flash_approx_v1",
        "algorithm": "node_centrality",
        "parameters": {
            "centrality_type": "betweenness",
            "sample_sources": 4,
        },
    }
    assert (
        registry.validate_payload(betweenness_payload)["native_app"]
        == "flash_betweenness"
    )

    eigenvector_payload = {
        **payload,
        "native_app": "eigenvector_centrality",
        "semantic_profile": "legacy_v1",
        "algorithm": "node_centrality",
        "parameters": {"centrality_type": "eigenvector"},
    }
    with pytest.raises(HTTPException, match="已批准的语义配置"):
        registry.validate_payload(eigenvector_payload)

    manifest_path.write_text(
        json.dumps(
            {
                "state": "published",
                "directed": False,
                "has_weights": False,
                "quality": {"self_loops_seen": 0, "self_loops_skipped": 0},
            }
        ),
        encoding="utf-8",
    )
    biconnected_payload = {
        **payload,
        "native_app": "flash_bcc",
        "semantic_profile": "graphscope_flash_approx_v1",
        "algorithm": "biconnected_components",
        "parameters": {},
    }
    assert registry.validate_payload(biconnected_payload)["native_app"] == "flash_bcc"
    with pytest.raises(HTTPException, match="已批准的语义配置"):
        registry.validate_payload(
            {**biconnected_payload, "parameters": {"strict_semantics": True}}
        )

    manifest_path.write_text(
        json.dumps(
            {
                "state": "published",
                "directed": True,
                "has_weights": True,
                "quality": {"self_loops_seen": 1, "self_loops_skipped": 0},
            }
        ),
        encoding="utf-8",
    )
    diameter_payload = {
        **payload,
        "native_app": "flash_diameter_approximation",
        "semantic_profile": "graphscope_flash_approx_v1",
        "algorithm": "approximate_diameter",
        "parameters": {},
    }
    assert (
        registry.validate_payload(diameter_payload)["native_app"]
        == "flash_diameter_approximation"
    )
    with pytest.raises(HTTPException, match="已批准的语义配置"):
        registry.validate_payload(
            {**diameter_payload, "parameters": {"strict_semantics": True}}
        )

    densest_payload = {
        **payload,
        "native_app": "flash_densest_subgraph",
        "semantic_profile": "graphscope_flash_approx_v1",
        "algorithm": "densest_subgraph",
        "parameters": {"rounds": 25},
    }
    assert (
        registry.validate_payload(densest_payload)["native_app"]
        == "flash_densest_subgraph"
    )
    with pytest.raises(HTTPException, match="已批准的语义配置"):
        registry.validate_payload(
            {**densest_payload, "parameters": {"rounds": "invalid"}}
        )

    cover_payload = {
        **payload,
        "native_app": "flash_minimal_vertex_cover",
        "semantic_profile": "graphscope_native_v1",
        "algorithm": "independent_set_cover",
        "parameters": {"operation": "minimal_vertex_cover"},
    }
    assert (
        registry.validate_payload(cover_payload)["native_app"]
        == "flash_minimal_vertex_cover"
    )
    with pytest.raises(HTTPException, match="已批准的语义配置"):
        registry.validate_payload(
            {
                **cover_payload,
                "parameters": {"operation": "maximal_independent_set"},
            }
        )

    motif_payload = {
        **payload,
        "native_app": "flash_triangle_counting",
        "semantic_profile": "graphscope_native_v1",
        "algorithm": "motif_counting",
        "parameters": {"motif": "triangle"},
    }
    assert (
        registry.validate_payload(motif_payload)["native_app"]
        == "flash_triangle_counting"
    )
    clique_payload = {
        **motif_payload,
        "native_app": "flash_k_clique_counting",
        "parameters": {"motif": "k_clique", "k": 4},
    }
    assert (
        registry.validate_payload(clique_payload)["native_app"]
        == "flash_k_clique_counting"
    )
    with pytest.raises(HTTPException, match="已批准的语义配置"):
        registry.validate_payload(
            {**clique_payload, "parameters": {"motif": "k_clique", "k": "bad"}}
        )

    manifest_path.write_text(
        json.dumps(
            {
                "state": "published",
                "directed": False,
                "has_weights": False,
                "quality": {"self_loops_seen": 0, "self_loops_skipped": 0},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(HTTPException, match="已批准的语义配置"):
        registry.validate_payload(motif_payload)


def test_timeout_keeps_attempt_files_until_remote_is_terminal() -> None:
    events: list[str] = []
    runtime = object.__new__(DurableLargeGraphRuntime)
    runtime.max_job_seconds = 30.0
    runtime.catalog = SimpleNamespace(
        finish_timeout=lambda *args, **kwargs: events.append("finish")
    )
    runtime._terminate_process_tree = lambda process: events.append("terminate")
    runtime._cancel_remote_attempt = lambda job: events.append("cancel") or False
    runtime._cleanup_recorded_attempt = lambda job: events.append("cleanup")

    status = runtime._finish_timed_out_process(
        {"job_id": "job-1", "lease_id": "lease-1"},
        object(),
        elapsed_seconds=31.0,
    )

    assert status is None
    assert events == ["terminate", "cancel"]


def test_bfs_source_uses_snapshot_node_normalization(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    work = tmp_path / "work"
    snapshot.mkdir()
    work.mkdir()
    (snapshot / "node-order.jsonl").write_text(
        json.dumps({"id": "a b", "name": "A   B"}) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "node_order_file": "node-order.jsonl",
        "quality": {"policy": {"node_normalization": "nfkc_casefold"}},
    }
    connection = _open_state(work, manifest, snapshot)
    try:
        assert _resolve_source(connection, manifest, "  A\t B  ") == "a b"
    finally:
        connection.close()

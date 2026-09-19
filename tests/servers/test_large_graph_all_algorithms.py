from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import networkx as nx
import pytest

from graph_data_service.compute import GraphCompute, NX_TOOL_NAMES
from graph_data_service.large_graph.algorithms import LargeGraphAlgorithms
from graph_data_service.large_graph.compatibility import (
    LARGE_GRAPH_ALGORITHM_NAMES,
    LARGE_GRAPH_GUARDED_ALGORITHM_NAMES,
    SMALL_GRAPH_ALGORITHM_MODE_PARAMETERS,
    SMALL_GRAPH_ALGORITHM_MODES,
    SMALL_GRAPH_CONCRETE_ALGORITHM_COUNT,
)
from graph_data_service.large_graph.contracts import DataQualityPolicy
from graph_data_service.large_graph.disk_graph import PartitionedDiskGraph
from graph_data_service.large_graph.engine import (
    LocalParallelGraphEngine,
    _validate_control_payload,
)
from graph_data_service.large_graph.integrity import validate_artifact_manifest
from graph_data_service.large_graph.router import ComputeRouter, ResourceManager
from graph_data_service.large_graph.service import LargeGraphService
from graph_data_service.large_graph.snapshot import (
    PartitionedSnapshotBuilder,
    _stable_partition,
)
from graph_data_service.tool_catalog import GRAPH_TOOLS


_CATALOGUE_EDGES = (
    "A B 1\nA C 2\nB C 1\nB D 3\nC D 1\nD A 1\nD E 2\nC E 4\n"
)


def _snapshot_from_text(
    tmp_path: Path,
    edge_text: str,
    *,
    directed: bool = True,
    stem: str = "graph",
) -> dict:
    source = tmp_path / f"{stem}.txt"
    source.write_text(edge_text, encoding="utf-8")
    stat = source.stat()
    return PartitionedSnapshotBuilder(tmp_path / f"{stem}-snapshots").build(
        source_path=source,
        user_scope="test__session",
        graph_id=stem,
        graph_version=1,
        directed=directed,
        partitions=2,
        workers=2,
        delimiter="whitespace",
        input_format="edgelist",
        policy=DataQualityPolicy(node_normalization="exact"),
        expected_source={"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
    )


def _snapshot(tmp_path: Path, *, directed: bool = True) -> dict:
    return _snapshot_from_text(
        tmp_path,
        _CATALOGUE_EDGES,
        directed=directed,
        stem="directed" if directed else "undirected",
    )


def _networkx_from_text(edge_text: str, *, directed: bool) -> nx.Graph:
    graph: nx.Graph = nx.DiGraph() if directed else nx.Graph()
    for line in edge_text.splitlines():
        source, target, weight = line.split()
        graph.add_edge(source, target, weight=float(weight))
    return graph


def _large_summary(
    tmp_path: Path,
    snapshot: dict,
    algorithm: str,
    parameters: dict,
    *,
    workers: int = 2,
) -> dict:
    return LocalParallelGraphEngine(tmp_path / "differential-artifacts").execute(
        snapshot=snapshot,
        algorithm=algorithm,
        parameters=parameters,
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=int(snapshot["graph_version"]),
        workers=workers,
    )["summary"]


def _large_kernel_summary(
    tmp_path: Path,
    snapshot: dict,
    algorithm: str,
    parameters: dict,
) -> dict:
    """Inspect an expected algorithm-level rejection before engine promotion."""
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        return LargeGraphAlgorithms(
            graph,
            tmp_path / f"rejected-{algorithm}-work",
            tmp_path / f"rejected-{algorithm}-results",
            workers=1,
        ).run(algorithm, parameters)["summary"]


def test_every_small_graph_algorithm_has_large_path_capability(tmp_path: Path) -> None:
    router = ComputeRouter(
        ResourceManager(tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0)
    )
    public_tools = {str(tool["name"]): tool for tool in GRAPH_TOOLS}
    non_algorithm_methods = {
        "get_node_info",
        "get_edge_info",
        "check_edge_exists",
        "visualize_subgraph",
    }
    public_graph_compute_algorithms = {
        name
        for name in public_tools
        if callable(getattr(GraphCompute, name, None))
    } - non_algorithm_methods

    assert public_graph_compute_algorithms == NX_TOOL_NAMES
    assert NX_TOOL_NAMES == set(SMALL_GRAPH_ALGORITHM_MODES)
    assert NX_TOOL_NAMES == LargeGraphAlgorithms.SUPPORTED
    assert set(router.capabilities) == LARGE_GRAPH_ALGORITHM_NAMES
    assert SMALL_GRAPH_CONCRETE_ALGORITHM_COUNT == 109
    assert all(
        "sequential" not in capability.parallel_strategy
        for capability in router.capabilities.values()
    )
    assert set(
        public_tools["run_large_graph_algorithm"]["inputSchema"]["properties"]
        ["algorithm"]["enum"]
    ) == LARGE_GRAPH_ALGORITHM_NAMES

    for algorithm in NX_TOOL_NAMES:
        assert callable(getattr(LargeGraphAlgorithms, algorithm, None)), algorithm
        modes = SMALL_GRAPH_ALGORITHM_MODES[algorithm]
        if not modes:
            continue
        selector = SMALL_GRAPH_ALGORITHM_MODE_PARAMETERS[algorithm]
        schema_modes = public_tools[algorithm]["inputSchema"]["properties"][
            selector
        ]["enum"]
        assert set(schema_modes) == set(modes), algorithm
        assert set(router.capabilities[algorithm].modes) == set(modes), algorithm


@pytest.mark.parametrize(
    ("algorithm", "parameters"),
    [
        ("graph_structure_analysis", {"method": "is_tree"}),
        ("connectivity_analysis", {"method": "bridges"}),
        ("tree_dag_analysis", {"method": "dag_longest_path"}),
        ("matching_coloring_analysis", {"method": "greedy_color"}),
        ("distance_measures", {"metric": "center"}),
    ],
)
def test_dev_grouped_extension_is_admitted_to_partitioned_kernel(
    tmp_path: Path, algorithm: str, parameters: dict
) -> None:
    assert algorithm not in LARGE_GRAPH_GUARDED_ALGORITHM_NAMES
    router = ComputeRouter(
        ResourceManager(tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0),
        large_node_threshold=1,
        large_edge_threshold=1,
    )
    decision = router.route(
        operation_scope="global",
        node_count=10,
        edge_count=20,
        algorithm=algorithm,
        snapshot_available=True,
        parameters=parameters,
    )
    assert decision.accepted
    assert decision.path == "large_graph"
    assert decision.capability is not None
    assert decision.capability.engine == ComputeRouter.ENGINE_NAME


@pytest.mark.parametrize(
    ("algorithm", "parameters"),
    [
        ("shortest_path", {"source": "A", "target": "E", "use_weight": True}),
        ("all_shortest_paths", {"source": "A", "target": "D"}),
        ("maximum_flow", {"source": "A", "sink": "E"}),
        ("minimum_cut", {"source": "A", "sink": "E"}),
        ("global_minimum_cut", {"treat_as_undirected": True}),
        ("minimum_spanning_tree", {}),
        ("community_detection", {"algorithm": "label_propagation", "max_iterations": 5}),
        ("node_centrality", {"centrality_type": "pagerank", "max_iterations": 1000}),
        ("graph_connectivity", {}),
        ("find_cycles", {"limit": 5}),
        ("maximum_cycle", {"max_cycles": 20}),
        ("degree_analysis", {}),
        ("subgraph_extraction", {"method": "ego_graph", "node": "A", "k": 2}),
        ("link_prediction_score", {"method": "jaccard", "source": "A", "target": "E"}),
        ("path_analysis", {"method": "has_path", "source": "A", "target": "E"}),
        ("clique_analysis", {"method": "clique_number"}),
        ("reachability", {"method": "descendants", "node": "A"}),
        ("global_metrics", {"metric": "density"}),
        ("graph_structure_analysis", {"method": "is_tree"}),
        ("connectivity_analysis", {"method": "bridges"}),
        ("tree_dag_analysis", {"method": "maximum_spanning_tree"}),
        ("matching_coloring_analysis", {"method": "greedy_color"}),
        ("distance_measures", {"metric": "center"}),
    ],
)
def test_large_engine_executes_complete_catalogue(
    tmp_path: Path, algorithm: str, parameters: dict
) -> None:
    snapshot = _snapshot(tmp_path)
    result = LocalParallelGraphEngine(tmp_path / "artifacts").execute(
        snapshot=snapshot,
        algorithm=algorithm,
        parameters=parameters,
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )

    assert result["algorithm"] == algorithm
    assert "success" in result["summary"]
    assert result["summary"]["parallel"]["workers"] == 2
    assert "sequential" not in result["summary"]["parallel"]["strategy"]
    assert Path(result["path"]).is_file()
    assert result["metadata"]["topology_access"] == "sqlite-sharded-v1"


def test_grouped_large_graph_kernel_executes_all_55_modes(tmp_path: Path) -> None:
    undirected = _snapshot_from_text(
        tmp_path,
        "A B 1\nA C 2\nB C 1\nB D 3\nC D 1\nD E 2\nC E 4\n",
        directed=False,
        stem="grouped-undirected",
    )
    dag = _snapshot_from_text(
        tmp_path,
        "A B 1\nA C 2\nB D 3\nC D 1\nD E 2\nC E 4\n",
        directed=True,
        stem="grouped-dag",
    )
    cyclic = _snapshot_from_text(
        tmp_path,
        "A B 1\nB C 1\nC A 1\nC D 1\nD A 1\n",
        directed=True,
        stem="grouped-cyclic",
    )
    cases: list[tuple[str, dict, dict]] = []
    for method in SMALL_GRAPH_ALGORITHM_MODES["graph_structure_analysis"]:
        parameters: dict = {"method": method, "limit": 5}
        if method in {"bfs_tree", "dfs_tree"}:
            parameters["source"] = "A"
        snapshot = (
            undirected
            if method in {"is_tree", "is_forest", "is_at_free", "check_planarity", "is_bipartite"}
            else dag
        )
        cases.append(("graph_structure_analysis", parameters, snapshot))
    for method in SMALL_GRAPH_ALGORITHM_MODES["connectivity_analysis"]:
        parameters = {"method": method, "limit": 5}
        if method in {
            "node_connectivity", "edge_connectivity",
            "minimum_node_cut", "minimum_edge_cut",
        }:
            parameters.update(source="A", target="E")
        cases.append((
            "connectivity_analysis", parameters,
            cyclic if method == "condensation" else undirected,
        ))
    for method in SMALL_GRAPH_ALGORITHM_MODES["tree_dag_analysis"]:
        parameters = {"method": method, "limit": 5}
        if method == "lowest_common_ancestor":
            parameters.update(node1="D", node2="E")
        if method in {"immediate_dominators", "dominance_frontiers"}:
            parameters["source"] = "A"
        snapshot = (
            cyclic if method == "is_aperiodic"
            else undirected if method in {"maximum_spanning_tree", "random_spanning_tree"}
            else dag
        )
        cases.append(("tree_dag_analysis", parameters, snapshot))
    for method in SMALL_GRAPH_ALGORITHM_MODES["matching_coloring_analysis"]:
        parameters = {"method": method, "limit": 5}
        if method == "is_matching":
            parameters["edges"] = [["A", "B"], ["C", "D"]]
        if method == "equitable_color":
            parameters["num_colors"] = 5
        cases.append(("matching_coloring_analysis", parameters, undirected))
    for metric in SMALL_GRAPH_ALGORITHM_MODES["distance_measures"]:
        cases.append((
            "distance_measures", {"metric": metric, "limit": 5},
            cyclic if metric in {"reciprocity", "flow_hierarchy"} else undirected,
        ))

    assert len(cases) == 55
    for index, (algorithm, parameters, snapshot) in enumerate(cases):
        with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
            result = LargeGraphAlgorithms(
                graph,
                tmp_path / f"grouped-work-{index}",
                tmp_path / f"grouped-results-{index}",
                workers=1,
            ).run(algorithm, parameters)
        assert result["summary"]["success"] is True, (
            algorithm,
            parameters,
            result["summary"],
        )


def test_grouped_structure_and_connectivity_disk_results_are_exact(tmp_path: Path) -> None:
    path_snapshot = _snapshot_from_text(
        tmp_path,
        "A B 1\nB C 1\nC D 1\n",
        directed=False,
        stem="grouped-exact-path",
    )
    tree = _large_kernel_summary(
        tmp_path / "tree", path_snapshot,
        "graph_structure_analysis", {"method": "is_tree"},
    )
    forest = _large_kernel_summary(
        tmp_path / "forest", path_snapshot,
        "graph_structure_analysis", {"method": "is_forest"},
    )
    bipartite = _large_kernel_summary(
        tmp_path / "bipartite", path_snapshot,
        "graph_structure_analysis", {"method": "is_bipartite"},
    )
    articulation = _large_kernel_summary(
        tmp_path / "articulation", path_snapshot,
        "connectivity_analysis", {"method": "articulation_points", "limit": 20},
    )
    bridges = _large_kernel_summary(
        tmp_path / "bridges", path_snapshot,
        "connectivity_analysis", {"method": "bridges", "limit": 20},
    )
    node_cut = _large_kernel_summary(
        tmp_path / "node-cut", path_snapshot,
        "connectivity_analysis",
        {"method": "minimum_node_cut", "source": "A", "target": "D"},
    )
    edge_cut = _large_kernel_summary(
        tmp_path / "edge-cut", path_snapshot,
        "connectivity_analysis",
        {"method": "minimum_edge_cut", "source": "A", "target": "D"},
    )

    assert tree["value"] is True
    assert forest["value"] is True
    assert bipartite["value"] is True
    assert articulation["count"] == 2
    assert set(articulation["nodes"]) == {"B", "C"}
    assert bridges["count"] == 3
    assert node_cut["size"] == 1
    assert edge_cut["size"] == 1


def test_grouped_dag_matching_and_distance_results_match_networkx(tmp_path: Path) -> None:
    dag_text = "A B 1\nA C 2\nB D 3\nC D 1\nD E 2\nC E 4\n"
    dag_snapshot = _snapshot_from_text(
        tmp_path, dag_text, directed=True, stem="grouped-exact-dag"
    )
    longest = _large_kernel_summary(
        tmp_path / "dag-longest", dag_snapshot,
        "tree_dag_analysis", {"method": "dag_longest_path_length"},
    )
    lca = _large_kernel_summary(
        tmp_path / "dag-lca", dag_snapshot,
        "tree_dag_analysis",
        {"method": "lowest_common_ancestor", "node1": "D", "node2": "E"},
    )
    assert longest["value"] == 6.0
    assert lca["ancestor"] == "D"

    path_text = "A B 1\nB C 1\nC D 1\n"
    path_snapshot = _snapshot_from_text(
        tmp_path, path_text, directed=False, stem="grouped-exact-distance"
    )
    graph = _networkx_from_text(path_text, directed=False)
    for metric in (
        "center", "periphery", "eccentricity", "wiener_index",
        "global_efficiency", "local_efficiency", "average_neighbor_degree",
        "average_degree_connectivity", "rich_club_coefficient", "barycenter", "girth",
    ):
        small = GraphCompute.distance_measures(graph, metric, limit=200)
        large = _large_kernel_summary(
            tmp_path / f"distance-{metric}", path_snapshot,
            "distance_measures", {"metric": metric, "limit": 200},
        )
        if "value" in small:
            assert large["value"] == pytest.approx(small["value"], abs=1.0e-6)
        if "nodes" in small:
            assert set(large["nodes"]) == set(small["nodes"])
        if "values" in small:
            assert {str(row["key"]): row["value"] for row in large["values"]} == pytest.approx(
                {str(row["key"]): row["value"] for row in small["values"]},
                abs=1.0e-6,
            )

    matching = _large_kernel_summary(
        tmp_path / "matching", path_snapshot,
        "matching_coloring_analysis", {"method": "max_weight_matching", "limit": 20},
    )
    assert matching["exact"] is True
    assert matching["size"] == len(nx.max_weight_matching(graph, weight="weight"))


def test_grouped_disk_hash_topology_dominance_and_antichains_match_small_path(
    tmp_path: Path,
) -> None:
    edge_text = "A B 1\nA C 1\nB D 1\nC D 1\nD E 1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=True, stem="grouped-exact-ordering"
    )
    graph = _networkx_from_text(edge_text, directed=True)

    small_hash = GraphCompute.graph_structure_analysis(
        graph, "weisfeiler_lehman_graph_hash", iterations=3
    )
    large_hash = _large_kernel_summary(
        tmp_path / "wl-hash", snapshot,
        "graph_structure_analysis",
        {"method": "weisfeiler_lehman_graph_hash", "iterations": 3},
    )
    assert large_hash["hash"] == small_hash["hash"]

    small_orders = GraphCompute.graph_structure_analysis(
        graph, "all_topological_sorts", limit=200
    )
    large_orders = _large_kernel_summary(
        tmp_path / "all-orders", snapshot,
        "graph_structure_analysis", {"method": "all_topological_sorts", "limit": 200},
    )
    assert large_orders["truncated"] is False
    assert {tuple(order) for order in large_orders["orders"]} == {
        tuple(order) for order in small_orders["orders"]
    }

    for method in ("immediate_dominators", "dominance_frontiers"):
        small = GraphCompute.tree_dag_analysis(
            graph, method, source="A", limit=200
        )
        large = _large_kernel_summary(
            tmp_path / method, snapshot,
            "tree_dag_analysis", {"method": method, "source": "A", "limit": 200},
        )
        small_items = {row["node"]: row["value"] for row in small["items"]}
        large_items = {row["node"]: row["value"] for row in large["items"]}
        assert large_items == small_items

    small_antichains = GraphCompute.tree_dag_analysis(
        graph, "antichains", limit=200
    )
    large_antichains = _large_kernel_summary(
        tmp_path / "antichains", snapshot,
        "tree_dag_analysis", {"method": "antichains", "limit": 200},
    )
    assert large_antichains["truncated"] is False
    assert {tuple(sorted(row)) for row in large_antichains["antichains"]} == {
        tuple(sorted(row)) for row in small_antichains["antichains"]
    }


def test_grouped_global_and_adjacent_cut_semantics_match_small_path(tmp_path: Path) -> None:
    triangle_text = "A B 1\nB C 1\nA C 1\n"
    snapshot = _snapshot_from_text(
        tmp_path, triangle_text, directed=False, stem="grouped-complete-cuts"
    )
    graph = _networkx_from_text(triangle_text, directed=False)
    for method in (
        "node_connectivity", "edge_connectivity", "minimum_node_cut", "minimum_edge_cut",
    ):
        small_global = GraphCompute.connectivity_analysis(graph, method, limit=200)
        large_global = _large_kernel_summary(
            tmp_path / f"global-{method}", snapshot,
            "connectivity_analysis", {"method": method, "limit": 200},
        )
        if "value" in small_global:
            assert large_global["value"] == small_global["value"]
        else:
            assert large_global["size"] == small_global["size"]

        small_local = GraphCompute.connectivity_analysis(
            graph, method, source="A", target="B", limit=200
        )
        large_local = _large_kernel_summary(
            tmp_path / f"local-{method}", snapshot,
            "connectivity_analysis",
            {"method": method, "source": "A", "target": "B", "limit": 200},
        )
        if "value" in small_local:
            assert large_local["value"] == small_local["value"]
        else:
            assert large_local["size"] == small_local["size"]


def test_large_engine_reports_cyclic_topological_sort_as_failure(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with pytest.raises(ValueError, match="存在环"):
        LocalParallelGraphEngine(tmp_path / "topological-artifacts").execute(
            snapshot=snapshot,
            algorithm="topological_sort",
            parameters={},
            user_scope="test__session",
            graph_id=str(snapshot["graph_id"]),
            graph_version=1,
            workers=2,
        )


def test_acyclic_topological_sort_uses_parallel_partition_map(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        "A B\nA C\nB D\nC D\n",
        stem="acyclic-parallel-topological",
    )
    result = LocalParallelGraphEngine(tmp_path / "topological-artifacts").execute(
        snapshot=snapshot,
        algorithm="topological_sort",
        parameters={},
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )

    assert result["summary"]["success"] is True
    assert result["summary"]["parallel"]["workers"] == 2
    assert result["summary"]["parallel"]["processes_used"] == 2


def test_large_has_path_stops_after_target_is_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        "A B\nB C\nC D\nD E\n",
        stem="has-path-early-stop",
    )
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        original_iter_neighbors = graph.iter_neighbors
        expanded: list[str] = []

        def tracked_iter_neighbors(node: str, **kwargs):
            expanded.append(str(node))
            if str(node) == "B":
                raise AssertionError("has_path must not expand the target node")
            return original_iter_neighbors(node, **kwargs)

        monkeypatch.setattr(graph, "iter_neighbors", tracked_iter_neighbors)
        result = LargeGraphAlgorithms(
            graph,
            tmp_path / "has-path-work",
            tmp_path / "has-path-results",
        ).path_analysis({"method": "has_path", "source": "A", "target": "B"})

    assert result["summary"] == {
        "success": True,
        "method": "has_path",
        "has_path": True,
    }
    assert expanded == ["A"]


def test_sparse_ego_subgraph_uses_selected_node_adjacency_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        "A B\nA C\nB C\nC D\n",
        stem="sparse-ego-index",
    )
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        def reject_full_edge_scan():
            raise AssertionError("sparse ego extraction must not scan every edge")
            yield  # pragma: no cover

        monkeypatch.setattr(graph, "iter_edges", reject_full_edge_scan)
        result = LargeGraphAlgorithms(
            graph,
            tmp_path / "sparse-ego-work",
            tmp_path / "sparse-ego-results",
        ).subgraph_extraction({"method": "ego_graph", "node": "A", "k": 1})

    assert result["summary"]["success"] is True
    assert result["summary"]["num_nodes"] == 3
    assert result["summary"]["num_edges"] == 3
    assert result["summary"]["edge_scan_strategy"] == (
        "selected-node-adjacency-index"
    )


def test_large_engine_covers_all_method_variants(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    engine = LocalParallelGraphEngine(tmp_path / "variant-artifacts")
    cases: list[tuple[str, dict]] = [
        ("shortest_path", {"source": "A", "target": "E", "algorithm": "yen_k_shortest", "k": 3}),
        ("shortest_path", {"source": "A", "target": "all", "use_weight": False}),
        ("maximum_cycle", {"optimize_by": "length", "max_cycles": 20}),
        ("maximum_cycle", {"optimize_by": "weight", "max_cycles": 20}),
        *[
            ("community_detection", {"algorithm": mode, "max_iterations": 3})
            for mode in (
                "louvain", "greedy_modularity", "label_propagation",
                "label_propagation_asyn", "leiden", "girvan_newman", "asyn_fluidc",
            )
        ],
        *[
            (
                "node_centrality",
                {
                    "centrality_type": mode, "limit": 3, "max_iterations": 1000,
                    "sample_sources": 5,
                },
            )
            for mode in (
                "degree", "betweenness", "closeness", "pagerank", "eigenvector",
                "harmonic", "katz", "load", "voterank", "percolation",
            )
        ],
        *[
            ("subgraph_extraction", {"method": mode, "node": "A", "nodes": ["A", "B"], "k": 2})
            for mode in ("ego_graph", "k_core", "k_truss", "k_corona", "node_subset")
        ],
        *[
            ("link_prediction_score", {"method": mode, "source": "A", "target": "E"})
            for mode in ("jaccard", "adamic_adar", "resource_allocation", "preferential_attachment")
        ],
        ("path_analysis", {"method": "all_simple_paths", "source": "A", "target": "E", "cutoff": 5}),
        ("clique_analysis", {"method": "find_cliques", "limit": 5}),
        ("clique_analysis", {"method": "number_of_cliques", "node": "A"}),
        ("clique_analysis", {"method": "number_of_cliques", "limit": 3}),
        ("reachability", {"method": "transitive_closure", "limit": 5}),
        ("reachability", {"method": "ancestors", "node": "E"}),
        *[
            ("global_metrics", {"metric": metric})
            for metric in (
                "density", "diameter", "radius", "transitivity",
                "average_clustering", "degree_assortativity",
            )
        ],
    ]
    for algorithm, parameters in cases:
        result = engine.execute(
            snapshot=snapshot,
            algorithm=algorithm,
            parameters=parameters,
            user_scope="test__session",
            graph_id=str(snapshot["graph_id"]),
            graph_version=1,
            workers=2,
        )
        assert result["summary"]["success"] is True, (algorithm, parameters, result)
        assert result["summary"]["parallel"]["workers"] == 2
        assert "sequential" not in result["summary"]["parallel"]["strategy"]


def test_dependency_ordered_kernel_uses_real_partition_processes(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    result = LocalParallelGraphEngine(tmp_path / "hybrid-parallel-artifacts").execute(
        snapshot=snapshot,
        algorithm="shortest_path",
        parameters={"source": "A", "target": "E"},
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )

    parallel = result["summary"]["parallel"]
    assert parallel == {
        "strategy": "adjacency-shard-map/disk-state-reduce",
        "workers": 2,
        "map_partitions": 2,
        "processes_used": 2,
        "node_count_validated": 5,
        "edge_count_validated": 8,
        "negative_edges_observed": 0,
    }
    assert result["metadata"]["parallel_strategy"] == parallel["strategy"]


def test_maximum_cycle_parallel_modes_partition_and_reduce(tmp_path: Path) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        (
            "A B 1\nB A 1\n"
            "C D 10\nD E 10\nE C 10\n"
            "F G 100\nG F 100\n"
        ),
        stem="parallel-maximum-cycle",
    )

    length = _large_summary(
        tmp_path,
        snapshot,
        "maximum_cycle",
        {"optimize_by": "length", "max_cycles": 10},
        workers=2,
    )
    weight = _large_summary(
        tmp_path,
        snapshot,
        "maximum_cycle",
        {"optimize_by": "weight", "max_cycles": 10},
        workers=2,
    )
    bounded = _large_summary(
        tmp_path,
        snapshot,
        "maximum_cycle",
        {"optimize_by": "weight", "max_cycles": 2},
        workers=2,
    )

    assert length["cycle_length"] == 3
    assert set(length["cycle"]) == {"C", "D", "E"}
    assert weight["cycle_length"] == 2
    assert set(weight["cycle"]) == {"F", "G"}
    assert weight["cycle_weight"] == 200.0
    for summary in (length, weight):
        assert summary["cycles_examined"] == 3
        assert summary["truncated"] is False
        assert summary["parallel"] == {
            "strategy": "cycle-start-map/max-reduce",
            "workers": 2,
            "cycles_examined": 3,
        }
    assert bounded["cycles_examined"] == 2
    assert bounded["truncated"] is True
    assert bounded["search_exhausted"] is False


def test_find_cycles_parallel_start_partitions_share_global_limit(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        (
            "A B 1\nB A 1\n"
            "C D 1\nD E 1\nE C 1\n"
            "F G 1\nG F 1\n"
        ),
        stem="parallel-find-cycles",
    )

    complete = _large_summary(
        tmp_path,
        snapshot,
        "find_cycles",
        {"limit": 10},
        workers=2,
    )
    bounded = _large_summary(
        tmp_path,
        snapshot,
        "find_cycles",
        {"limit": 2},
        workers=2,
    )

    assert complete["count_returned"] == 3
    assert complete["truncated"] is False
    assert complete["cycle_artifact_rows"] == 7
    assert complete["parallel"] == {
        "strategy": "cycle-start-map/bounded-artifact-merge",
        "workers": 2,
        "cycles_returned": 3,
    }
    assert bounded["count_returned"] == 2
    assert bounded["truncated"] is True
    assert bounded["total_cycles_at_least"] == 3
    assert bounded["parallel"]["cycles_returned"] == 2


def test_clique_modes_parallelize_by_unique_canonical_root(tmp_path: Path) -> None:
    edge_text = (
        "A B 1\nA C 1\nB C 1\n"
        "C D 1\nC E 1\nD E 1\n"
    )
    snapshot = _snapshot_from_text(
        tmp_path,
        edge_text,
        stem="parallel-cliques",
    )

    found = _large_summary(
        tmp_path,
        snapshot,
        "clique_analysis",
        {"method": "find_cliques", "limit": 10},
        workers=2,
    )
    bounded = _large_summary(
        tmp_path,
        snapshot,
        "clique_analysis",
        {"method": "find_cliques", "limit": 1},
        workers=2,
    )
    clique_number = _large_summary(
        tmp_path,
        snapshot,
        "clique_analysis",
        {"method": "clique_number"},
        workers=2,
    )
    node_count = _large_summary(
        tmp_path,
        snapshot,
        "clique_analysis",
        {"method": "number_of_cliques", "node": "C"},
        workers=2,
    )
    all_counts = _large_summary(
        tmp_path,
        snapshot,
        "clique_analysis",
        {"method": "number_of_cliques", "limit": 10},
        workers=2,
    )

    assert found["count_returned"] == 2
    assert found["clique_artifact_rows"] == 6
    assert {frozenset(clique) for clique in found["cliques"]} == {
        frozenset({"A", "B", "C"}),
        frozenset({"C", "D", "E"}),
    }
    assert found["parallel"] == {
        "strategy": "clique-root-map/bounded-artifact-merge",
        "workers": 2,
        "cliques_returned": 2,
    }
    assert bounded["count_returned"] == 1
    assert bounded["truncated"] is True
    assert clique_number["value"] == 3
    assert clique_number["parallel"]["strategy"] == (
        "clique-root-map/max-reduce"
    )
    assert node_count["value"] == 2
    assert node_count["parallel"]["strategy"] == (
        "clique-root-map/count-reduce"
    )
    counts = {row["node"]: row["clique_count"] for row in all_counts["top_nodes"]}
    assert counts == {"C": 2, "A": 1, "B": 1, "D": 1, "E": 1}
    assert all_counts["parallel"]["strategy"] == (
        "clique-root-map/disk-count-reduce"
    )


def test_transitive_closure_parallelizes_sources_with_one_global_limit(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        "A B 1\nB C 1\nD E 1\n",
        stem="parallel-transitive-closure",
    )

    complete = _large_summary(
        tmp_path,
        snapshot,
        "reachability",
        {"method": "transitive_closure", "limit": 10},
        workers=2,
    )
    bounded = _large_summary(
        tmp_path,
        snapshot,
        "reachability",
        {"method": "transitive_closure", "limit": 2},
        workers=2,
    )

    assert complete["num_edges_at_least"] == 4
    assert complete["closure_artifact_rows"] == 4
    assert complete["truncated"] is False
    assert {
        (row["from"], row["to"]) for row in complete["edges_preview"]
    } == {("A", "B"), ("A", "C"), ("B", "C"), ("D", "E")}
    assert complete["parallel"]["strategy"] == (
        "source-partition-map/bounded-artifact-merge"
    )
    assert complete["parallel"]["workers"] == 2
    assert bounded["closure_artifact_rows"] == 2
    assert bounded["num_edges_at_least"] == 3
    assert bounded["truncated"] is True


def test_multi_dataset_path_artifact_passes_manifest_binding(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    artifact_root = tmp_path / "multi-dataset-artifacts"
    result = LocalParallelGraphEngine(artifact_root).execute(
        snapshot=snapshot,
        algorithm="shortest_path",
        parameters={"source": "A", "target": "all", "use_weight": True},
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )
    artifact = {
        **result,
        "graph_id": str(snapshot["graph_id"]),
        "graph_version": 1,
        "algorithm": "shortest_path",
    }

    _, manifest = validate_artifact_manifest(
        artifact,
        artifact_root=artifact_root,
        expected_scope="test__session",
    )

    assert len(manifest["result_files"]) == int(snapshot["partitions"]) * 2


def test_engine_rejects_unbounded_or_non_finite_control_payload() -> None:
    with pytest.raises(ValueError, match="超过"):
        _validate_control_payload({"value": "x" * (2 * 1024 * 1024)}, [])
    with pytest.raises(ValueError, match="可序列化 JSON"):
        _validate_control_payload({"value": float("nan")}, [])


def test_directed_non_strong_average_path_is_an_explicit_failure(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    graph = _networkx_from_text(_CATALOGUE_EDGES, directed=True)
    small = GraphCompute.path_analysis(graph, "average_shortest_path_length")
    assert small["success"] is False
    assert "强连通" in small["error"]

    with pytest.raises(ValueError, match="强连通"):
        LocalParallelGraphEngine(tmp_path / "path-failure-artifacts").execute(
            snapshot=snapshot,
            algorithm="path_analysis",
            parameters={"method": "average_shortest_path_length"},
            user_scope="test__session",
            graph_id=str(snapshot["graph_id"]),
            graph_version=1,
            workers=2,
        )


def test_global_link_prediction_uses_source_batch_parallelism(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    result = LocalParallelGraphEngine(tmp_path / "parallel-artifacts").execute(
        snapshot=snapshot,
        algorithm="link_prediction_score",
        parameters={"method": "jaccard", "top_k": 5},
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )
    assert result["summary"]["success"] is True
    assert result["summary"]["parallel"]["strategy"] == "source-batch-map/top-k-reduce"
    assert result["summary"]["parallel"]["workers"] == 2


def test_global_minimum_cut_uses_sink_batch_parallelism(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    result = LocalParallelGraphEngine(tmp_path / "cut-artifacts").execute(
        snapshot=snapshot,
        algorithm="global_minimum_cut",
        parameters={"treat_as_undirected": True},
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )
    assert result["summary"]["success"] is True
    assert result["summary"]["parallel"]["strategy"] == "sink-batch-map/min-cut-reduce"
    assert result["summary"]["parallel"]["workers"] == 2


def test_brandes_centrality_uses_source_batch_parallelism(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    serial = LocalParallelGraphEngine(tmp_path / "centrality-serial").execute(
        snapshot=snapshot,
        algorithm="node_centrality",
        parameters={"centrality_type": "betweenness", "sample_sources": 3},
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=1,
    )
    parallel = LocalParallelGraphEngine(tmp_path / "centrality-parallel").execute(
        snapshot=snapshot,
        algorithm="node_centrality",
        parameters={"centrality_type": "betweenness", "sample_sources": 3},
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )
    assert parallel["summary"]["parallel"]["strategy"] == "source-batch-map/dependency-reduce"
    assert parallel["summary"]["top_nodes"] == serial["summary"]["top_nodes"]


def test_brandes_runtime_rejects_sample_sources_above_deployment_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_MAX_SAMPLE_SOURCES", "2")
    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / "brandes-hard-limit"
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        kernel = LargeGraphAlgorithms(
            graph, work_root, tmp_path / "brandes-hard-limit-results", workers=2
        )
        with pytest.raises(ValueError, match="LARGE_GRAPH_MAX_SAMPLE_SOURCES"):
            kernel.node_centrality(
                {"centrality_type": "betweenness", "sample_sources": 3}
            )
    # The recovered/malicious job is rejected before graph-sized worker state
    # is created, independently of the API router.
    assert list(work_root.rglob("*.sqlite3")) == []


def test_parallel_brandes_sends_each_sample_to_only_one_worker(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / "brandes-source-buckets"
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        result = LargeGraphAlgorithms(
            graph, work_root, tmp_path / "brandes-source-buckets-results", workers=2
        ).node_centrality(
            {"centrality_type": "betweenness", "sample_sources": 1, "limit": 20}
        )
    assert result["summary"]["success"] is True
    # A one-source sample has one non-empty bucket. Idle children do not
    # create their own O(V) partial score databases.
    assert len(list(work_root.glob("brandes-*/partial-scores.sqlite3"))) == 1


@pytest.mark.parametrize(("name", "value"), [("alpha", "nan"), ("beta", "inf")])
def test_iterative_centrality_runtime_rejects_non_finite_parameters(
    tmp_path: Path, name: str, value: str
) -> None:
    snapshot = _snapshot(tmp_path)
    work_root = tmp_path / f"finite-{name}"
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        kernel = LargeGraphAlgorithms(
            graph, work_root, tmp_path / f"finite-{name}-results", workers=1
        )
        with pytest.raises(ValueError, match="有限数值"):
            kernel.node_centrality({"centrality_type": "katz", name: value})
    assert list(work_root.rglob("*.sqlite3")) == []


@pytest.mark.parametrize("centrality_type", ["closeness", "harmonic"])
def test_distance_centrality_uses_source_batches_and_matches_serial(
    tmp_path: Path, centrality_type: str
) -> None:
    edge_text = (
        "A B 1\nB C 2\nC A 0.5\nA C 0.25\nC D 1\nD A 1\nB D 0.75\n"
    )
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=True, stem=f"parallel-{centrality_type}"
    )
    parameters = {"centrality_type": centrality_type, "limit": 200}
    serial = _large_summary(
        tmp_path, snapshot, "node_centrality", parameters, workers=1
    )
    parallel = _large_summary(
        tmp_path, snapshot, "node_centrality", parameters, workers=2
    )

    assert parallel["top_nodes"] == serial["top_nodes"]
    assert parallel["value_stats"] == serial["value_stats"]
    assert parallel["parallel"] == {
        "strategy": "source-batch-map/score-disk-reduce",
        "workers": 2,
        "source_count": 4,
    }


@pytest.mark.parametrize(
    ("directed", "edge_text", "largest_size"),
    [
        (True, "A B 1\nB C 2\nC A 3\nA C 4\nC B 1\n", 3),
        (False, "A B 1\nB C 2\nA C 4\nX Y 7\n", 3),
    ],
)
def test_average_shortest_path_uses_source_batches_and_matches_small_graph(
    tmp_path: Path, directed: bool, edge_text: str, largest_size: int
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        edge_text,
        directed=directed,
        stem="parallel-average-directed" if directed else "parallel-average-undirected",
    )
    graph = _networkx_from_text(edge_text, directed=directed)
    small = GraphCompute.path_analysis(graph, "average_shortest_path_length")
    serial = _large_summary(
        tmp_path,
        snapshot,
        "path_analysis",
        {"method": "average_shortest_path_length"},
        workers=1,
    )
    parallel = _large_summary(
        tmp_path,
        snapshot,
        "path_analysis",
        {"method": "average_shortest_path_length"},
        workers=2,
    )

    assert parallel["value"] == serial["value"]
    assert parallel["value"] == pytest.approx(small["value"], abs=1.0e-6)
    assert parallel["parallel"] == {
        "strategy": "source-batch-map/scalar-reduce",
        "workers": 2,
        "source_count": largest_size,
    }


@pytest.mark.parametrize("metric", ["diameter", "radius"])
def test_distance_extrema_use_source_batches_and_match_small_graph(
    tmp_path: Path, metric: str
) -> None:
    edge_text = "A B 1\nB C 1\nC D 1\nA C 1\nX Y 1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=True, stem=f"parallel-{metric}"
    )
    graph = _networkx_from_text(edge_text, directed=True)
    small = GraphCompute.global_metrics(graph, metric)
    serial = _large_summary(
        tmp_path, snapshot, "global_metrics", {"metric": metric}, workers=1
    )
    parallel = _large_summary(
        tmp_path, snapshot, "global_metrics", {"metric": metric}, workers=2
    )

    assert parallel["value"] == serial["value"] == small["value"]
    assert parallel["parallel"] == {
        "strategy": "source-batch-map/scalar-reduce",
        "workers": 2,
        "source_count": 4,
    }


def test_mst_result_is_partitioned_artifact(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    result = LocalParallelGraphEngine(tmp_path / "mst-artifacts").execute(
        snapshot=snapshot,
        algorithm="minimum_spanning_tree",
        parameters={},
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )
    manifest = Path(result["path"])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert len(payload["result_files"]) == 2
    assert result["summary"]["result_processing"] == {
        "strategy": "bounded-queue-partition-writers",
        "workers": 2,
        "queue_rows_per_worker": 512,
    }
    assert result["metadata"]["result_processing"] == result["summary"][
        "result_processing"
    ]
    for relative in payload["result_files"]:
        assert (manifest.parent / relative).is_file()


def test_capability_catalog_marks_approximations_and_complexity(tmp_path: Path) -> None:
    router = ComputeRouter(ResourceManager(tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0))
    community = router.capabilities["community_detection"]
    centrality = router.capabilities["node_centrality"]
    link_prediction = router.capabilities["link_prediction_score"]
    assert "label_propagation" in community.approximate_modes
    assert "percolation" in centrality.approximate_modes
    assert link_prediction.complexity_class == "O(V²)"


@pytest.mark.parametrize("centrality_type", ["harmonic", "eigenvector", "katz"])
def test_directed_centrality_matches_small_graph_networkx_semantics(
    tmp_path: Path, centrality_type: str
) -> None:
    edge_text = (
        "A B 1\nB C 2\nC A 0.5\nA C 0.25\nC D 1\nD A 1\nB D 0.75\n"
    )
    snapshot = _snapshot_from_text(tmp_path, edge_text, stem="centrality")
    graph = _networkx_from_text(edge_text, directed=True)
    small = GraphCompute().node_centrality(
        graph, centrality_type=centrality_type, limit=200
    )
    large = _large_summary(
        tmp_path,
        snapshot,
        "node_centrality",
        {
            "centrality_type": centrality_type,
            "limit": 200,
            "max_iterations": 1000,
            "tolerance": 1.0e-6,
        },
    )

    small_scores = {row["node"]: row["value"] for row in small["top_nodes"]}
    large_scores = {row["node"]: row["value"] for row in large["top_nodes"]}
    assert large_scores.keys() == small_scores.keys()
    for node, expected in small_scores.items():
        assert large_scores[node] == pytest.approx(expected, abs=2.0e-5)
    if centrality_type == "katz":
        assert sum(value * value for value in large_scores.values()) == pytest.approx(
            1.0, abs=3.0e-6
        )


@pytest.mark.parametrize("centrality_type", ["pagerank", "eigenvector", "katz"])
def test_iterative_centrality_nonconvergence_is_not_exact_success(
    tmp_path: Path, centrality_type: str
) -> None:
    edge_text = "A B 1\nB C 2\nC A 0.5\nA C 1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, stem=f"nonconvergent-{centrality_type}"
    )
    with pytest.raises(ValueError, match="未收敛"):
        _large_summary(
            tmp_path,
            snapshot,
            "node_centrality",
            {
                "centrality_type": centrality_type,
                "max_iterations": 1,
                "tolerance": 1.0e-15,
            },
        )


@pytest.mark.parametrize(
    ("directed", "edge_text"),
    [
        (True, "0 1 1\n2 1 1\n2 3 1\n3 4 1\n"),
        (False, "0 1 1\n0 2 1\n0 3 1\n1 4 1\n"),
    ],
)
def test_voterank_matches_networkx_and_returns_only_elected_nodes(
    tmp_path: Path, directed: bool, edge_text: str
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        edge_text,
        directed=directed,
        stem="voterank-directed" if directed else "voterank-undirected",
    )
    graph = _networkx_from_text(edge_text, directed=directed)
    small = GraphCompute().node_centrality(graph, "voterank", limit=200)
    large = _large_summary(
        tmp_path,
        snapshot,
        "node_centrality",
        {"centrality_type": "voterank", "limit": 200},
    )

    assert large["total_nodes"] == small["total_nodes"]
    assert large["top_nodes"] == small["top_nodes"]
    assert large["total_nodes"] < graph.number_of_nodes()


@pytest.mark.parametrize("method", ["k_core", "k_corona"])
def test_directed_core_algorithms_count_reciprocal_arcs_separately(
    tmp_path: Path, method: str
) -> None:
    edge_text = "A B 1\nB A 1\nB C 1\n"
    snapshot = _snapshot_from_text(tmp_path, edge_text, stem=f"directed-{method}")
    graph = _networkx_from_text(edge_text, directed=True)
    small = GraphCompute.subgraph_extraction(
        graph, method, k=2, limit=200, include_full_nodes=True
    )
    large = _large_summary(
        tmp_path,
        snapshot,
        "subgraph_extraction",
        {"method": method, "k": 2, "limit": 200},
    )

    assert set(large["nodes_preview"]) == set(small["_full_nodes"])
    assert large["num_nodes"] == small["num_nodes"] == 2
    assert large["num_edges"] == small["num_edges"] == 2
    assert all("kind" not in edge for edge in large["edges_preview"])


def test_directed_strong_average_path_matches_small_graph(tmp_path: Path) -> None:
    edge_text = "A B 1\nB C 2\nC A 3\nA C 4\nC B 1\n"
    snapshot = _snapshot_from_text(tmp_path, edge_text, stem="strong-path")
    graph = _networkx_from_text(edge_text, directed=True)
    small = GraphCompute.path_analysis(graph, "average_shortest_path_length")
    large = _large_summary(
        tmp_path,
        snapshot,
        "path_analysis",
        {"method": "average_shortest_path_length"},
    )
    assert large["value"] == pytest.approx(small["value"], abs=1.0e-6)


def test_reciprocal_directed_mst_uses_minimum_weight_projection(tmp_path: Path) -> None:
    edge_text = "A B 10\nB A 1\nB C 2\nA C 100\n"
    snapshot = _snapshot_from_text(tmp_path, edge_text, stem="reciprocal-mst")
    graph = _networkx_from_text(edge_text, directed=True)
    small = GraphCompute.minimum_spanning_tree(graph, limit=200)
    large = _large_summary(
        tmp_path, snapshot, "minimum_spanning_tree", {"limit": 200}
    )

    assert small["total_weight"] == large["total_weight"] == 3.0
    assert large["mst_edges_preview"] == small["mst_edges_preview"]


def test_small_graph_pagerank_has_pure_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _networkx_from_text(
        "A B 1\nA C 2\nB C 1\nC A 0.5\n", directed=True
    )
    expected = {
        "A": 0.41012376449455934,
        "B": 0.1662017332734585,
        "C": 0.4236745022319818,
    }

    def missing_scipy(*_args, **_kwargs):
        raise ModuleNotFoundError("No module named 'scipy'")

    monkeypatch.setattr(nx, "pagerank", missing_scipy)
    result = GraphCompute().node_centrality(graph, "pagerank", limit=200)
    actual = {row["node"]: row["value"] for row in result["top_nodes"]}
    for node, value in expected.items():
        assert actual[node] == pytest.approx(value, abs=2.0e-6)


def test_negative_weight_pagerank_matches_small_graph_semantics(tmp_path: Path) -> None:
    edge_text = "A B -0.5\nA C 1.5\nB A 1\nC A 1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=True, stem="negative-pagerank"
    )
    graph = _networkx_from_text(edge_text, directed=True)
    small = GraphCompute().node_centrality(graph, "pagerank", limit=200)
    large = _large_summary(
        tmp_path,
        snapshot,
        "node_centrality",
        {
            "centrality_type": "pagerank",
            "max_iterations": 1000,
            "tolerance": 1.0e-7,
            "limit": 200,
        },
    )

    expected = {row["node"]: row["value"] for row in small["top_nodes"]}
    actual = {row["node"]: row["value"] for row in large["top_nodes"]}
    assert actual == pytest.approx(expected, abs=3.0e-5)


def test_large_pagerank_summary_preserves_sub_micro_scores(tmp_path: Path) -> None:
    """Control-plane rounding must not turn valid large-graph scores into zero."""
    snapshot = _snapshot_from_text(
        tmp_path,
        "source sink 1\nsink sink 1\n",
        directed=True,
        stem="sub-micro-pagerank",
    )
    summary = _large_summary(
        tmp_path,
        snapshot,
        "node_centrality",
        {
            "centrality_type": "pagerank",
            "damping": 0.999999,
            "max_iterations": 10,
            "tolerance": 1.0e-12,
            "limit": 20,
        },
    )

    scores = {row["node"]: row["value"] for row in summary["top_nodes"]}
    assert scores["source"] == pytest.approx(5.0e-7)
    assert summary["value_stats"]["min"] == pytest.approx(5.0e-7)
    assert summary["value_stats"]["median"] > 0.0


def test_centrality_value_stats_include_small_graph_median(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    graph = _networkx_from_text(_CATALOGUE_EDGES, directed=True)
    small = GraphCompute().node_centrality(graph, "degree", limit=200)
    large = _large_summary(
        tmp_path,
        snapshot,
        "node_centrality",
        {"centrality_type": "degree", "limit": 200},
    )
    assert large["value_stats"]["median"] == pytest.approx(
        small["value_stats"]["median"], abs=1.0e-6
    )


def test_single_node_degree_centrality_matches_networkx_with_self_loop(
    tmp_path: Path,
) -> None:
    edge_text = "A A 3\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=True, stem="single-node-degree-centrality"
    )
    graph = _networkx_from_text(edge_text, directed=True)
    small = GraphCompute().node_centrality(graph, "degree", limit=200)
    large = _large_summary(
        tmp_path,
        snapshot,
        "node_centrality",
        {"centrality_type": "degree", "limit": 200},
    )

    assert large["top_nodes"] == small["top_nodes"] == [
        {"node": "A", "value": 1.0}
    ]


def test_link_prediction_self_loop_degree_matches_networkx(tmp_path: Path) -> None:
    edge_text = "A W 1\nB W 1\nW W 1\nA A 1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=True, stem="link-self-loop"
    )
    graph = _networkx_from_text(edge_text, directed=True)
    for method in (
        "jaccard",
        "adamic_adar",
        "resource_allocation",
        "preferential_attachment",
    ):
        small = GraphCompute.link_prediction_score(
            graph, method, source="A", target="B"
        )
        large = _large_summary(
            tmp_path,
            snapshot,
            "link_prediction_score",
            {"method": method, "source": "A", "target": "B"},
        )
        assert large["scores"] == small["scores"], method


def test_graph_sized_sqlite_state_is_reused_within_worker(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)

    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        yen_root = tmp_path / "yen-work"
        LargeGraphAlgorithms(
            graph, yen_root, tmp_path / "yen-results", workers=1
        ).shortest_path(
            {
                "source": "A",
                "target": "E",
                "algorithm": "yen_k_shortest",
                "k": 4,
            }
        )
        assert sorted(path.name for path in yen_root.glob("yen-*.sqlite3")) == [
            "yen-state.sqlite3"
        ]

        cut_root = tmp_path / "cut-work"
        LargeGraphAlgorithms(
            graph, cut_root, tmp_path / "cut-results", workers=2
        ).global_minimum_cut({"treat_as_undirected": True})
        cut_states = sorted(cut_root.glob("global-cut-*/flow-*.sqlite3"))
        assert cut_states
        assert {path.name for path in cut_states} == {"flow-state.sqlite3"}
        assert len(cut_states) <= 2

        brandes_root = tmp_path / "brandes-work"
        LargeGraphAlgorithms(
            graph, brandes_root, tmp_path / "brandes-results", workers=2
        ).node_centrality(
            {
                "centrality_type": "betweenness",
                "sample_sources": 5,
                "limit": 20,
            }
        )
        brandes_states = sorted(brandes_root.glob("brandes-*/source-*.sqlite3"))
        assert brandes_states
        assert {path.name for path in brandes_states} == {"source-state.sqlite3"}
        assert len(brandes_states) <= 2

        for centrality_type in ("closeness", "harmonic"):
            distance_root = tmp_path / f"{centrality_type}-work"
            LargeGraphAlgorithms(
                graph, distance_root, tmp_path / f"{centrality_type}-results", workers=2
            ).node_centrality(
                {"centrality_type": centrality_type, "limit": 20}
            )
            distance_states = sorted(
                distance_root.glob(
                    f"distance-{centrality_type}-*/source-*.sqlite3"
                )
            )
            assert distance_states
            assert {path.name for path in distance_states} == {
                "source-state.sqlite3"
            }
            assert len(distance_states) <= 2

        extrema_root = tmp_path / "distance-extrema-work"
        LargeGraphAlgorithms(
            graph, extrema_root, tmp_path / "distance-extrema-results", workers=2
        ).global_metrics({"metric": "diameter"})
        extrema_states = sorted(
            extrema_root.glob("distance-metric-extrema-*/source-*.sqlite3")
        )
        assert extrema_states
        assert {path.name for path in extrema_states} == {"source-state.sqlite3"}
        assert len(extrema_states) <= 2

    strong_snapshot = _snapshot_from_text(
        tmp_path,
        "A B 1\nB C 2\nC A 3\nA C 4\nC B 1\n",
        directed=True,
        stem="bounded-average-state",
    )
    with PartitionedDiskGraph(strong_snapshot["manifest_path"]) as strong_graph:
        average_root = tmp_path / "distance-average-work"
        LargeGraphAlgorithms(
            strong_graph,
            average_root,
            tmp_path / "distance-average-results",
            workers=2,
        ).path_analysis({"method": "average_shortest_path_length"})
        average_states = sorted(
            average_root.glob("distance-path-average-*/source-*.sqlite3")
        )
        assert average_states
        assert {path.name for path in average_states} == {"source-state.sqlite3"}
        assert len(average_states) <= 2


def test_degree_low_order_and_subgraph_edge_schema_match_small_graph(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    graph = _networkx_from_text(_CATALOGUE_EDGES, directed=True)
    small_degree = GraphCompute.degree_analysis(graph, limit=200)
    large_degree = _large_summary(
        tmp_path, snapshot, "degree_analysis", {"limit": 200}
    )
    assert large_degree["low_degree_nodes"] == small_degree["low_degree_nodes"]

    large_subgraph = _large_summary(
        tmp_path,
        snapshot,
        "subgraph_extraction",
        {"method": "node_subset", "nodes": ["A", "B", "C"], "limit": 200},
    )
    assert large_subgraph["edges_preview"]
    assert all("kind" not in edge for edge in large_subgraph["edges_preview"])


def test_degree_analysis_uses_node_shard_parallelism_and_insertion_ties(
    tmp_path: Path,
) -> None:
    edge_text = "D A 1\nA B 1\nD C 1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=False, stem="parallel-degree-analysis"
    )
    graph = _networkx_from_text(edge_text, directed=False)
    small = GraphCompute.degree_analysis(graph, limit=200)
    serial = _large_summary(
        tmp_path, snapshot, "degree_analysis", {"limit": 200}, workers=1
    )
    parallel = _large_summary(
        tmp_path, snapshot, "degree_analysis", {"limit": 200}, workers=2
    )

    assert parallel["top_degree_nodes"] == serial["top_degree_nodes"]
    assert parallel["top_degree_nodes"] == small["top_degree_nodes"]
    assert parallel["low_degree_nodes"] == small["low_degree_nodes"]
    assert parallel["parallel"] == {
        "strategy": "node-shard-map/degree-disk-reduce",
        "workers": 2,
        "node_count": 4,
    }


def test_node_centrality_ties_preserve_small_graph_insertion_order(
    tmp_path: Path,
) -> None:
    edge_text = "D A 1\nA B 1\nD C 1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=False, stem="centrality-tie-order"
    )
    graph = _networkx_from_text(edge_text, directed=False)
    small = GraphCompute().node_centrality(graph, "degree", limit=200)
    large = _large_summary(
        tmp_path,
        snapshot,
        "node_centrality",
        {"centrality_type": "degree", "limit": 200},
    )
    assert large["top_nodes"] == small["top_nodes"]
    assert large["bottom_nodes"] == small["bottom_nodes"]


def test_degree_assortativity_matches_networkx_after_reciprocal_deduplication(
    tmp_path: Path,
) -> None:
    edge_text = "A B 1\nB A 7\nA C 1\nC D 1\nD E 1\n"
    snapshot = _snapshot_from_text(tmp_path, edge_text, stem="assortativity")
    graph = _networkx_from_text(edge_text, directed=True)
    small = GraphCompute.global_metrics(graph, "degree_assortativity")
    large = _large_summary(
        tmp_path,
        snapshot,
        "global_metrics",
        {"metric": "degree_assortativity"},
    )
    assert large["value"] == pytest.approx(small["value"], abs=1.0e-6)


def test_undirected_betweenness_matches_networkx_normalization(tmp_path: Path) -> None:
    edge_text = "A B 1\nB C 1\nC D 1\nB D 3\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=False, stem="undirected-betweenness"
    )
    graph = _networkx_from_text(edge_text, directed=False)
    small = GraphCompute().node_centrality(graph, "betweenness", limit=200)
    large = _large_summary(
        tmp_path,
        snapshot,
        "node_centrality",
        {"centrality_type": "betweenness", "sample_sources": 100, "limit": 200},
    )
    assert {row["node"]: row["value"] for row in large["top_nodes"]} == pytest.approx(
        {row["node"]: row["value"] for row in small["top_nodes"]}, abs=1.0e-6
    )


def test_undirected_minimum_cut_returns_reverse_canonical_crossing_edge(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path, "D A 5\n", directed=False, stem="reverse-cut"
    )
    large = _large_summary(
        tmp_path, snapshot, "minimum_cut", {"source": "D", "sink": "A"}
    )
    assert large["min_cut_value"] == pytest.approx(5.0)
    assert large["cut_edges_total"] == 1
    assert large["cut_edges_preview"] == [{"from": "D", "to": "A", "weight": 5.0}]


def test_flow_preserves_positive_capacities_below_numeric_preview_precision(
    tmp_path: Path,
) -> None:
    edge_text = "source sink 0.0000000000001\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=True, stem="tiny-capacity"
    )
    graph = _networkx_from_text(edge_text, directed=True)

    small_flow = GraphCompute().maximum_flow(graph, "source", "sink", limit=200)
    large_flow = _large_summary(
        tmp_path,
        snapshot,
        "maximum_flow",
        {"source": "source", "sink": "sink", "limit": 200},
    )
    small_cut = GraphCompute().minimum_cut(graph, "source", "sink")
    large_cut = _large_summary(
        tmp_path,
        snapshot,
        "minimum_cut",
        {"source": "source", "sink": "sink", "limit": 200},
    )

    assert large_flow["max_flow_value"] == pytest.approx(
        small_flow["max_flow_value"], rel=1.0e-12, abs=1.0e-20
    )
    assert large_flow["total_nonzero_edges"] == small_flow["total_nonzero_edges"] == 1
    assert large_cut["min_cut_value"] == pytest.approx(
        small_cut["min_cut_value"], rel=1.0e-12, abs=1.0e-20
    )


@pytest.mark.parametrize("algorithm", ["maximum_flow", "minimum_cut"])
def test_source_sink_cut_algorithms_reject_negative_capacity_consistently(
    tmp_path: Path, algorithm: str
) -> None:
    edge_text = "source sink 2\nunrelated other -1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=True, stem=f"negative-{algorithm}"
    )
    graph = _networkx_from_text(edge_text, directed=True)
    parameters = {"source": "source", "sink": "sink"}

    small = getattr(GraphCompute(), algorithm)(graph, **parameters)
    large = _large_kernel_summary(tmp_path, snapshot, algorithm, parameters)

    assert small == {"success": False, "error": "最大流/最小割不支持负容量"}
    assert large == small


def test_global_cut_distinguishes_zero_capacity_from_disconnected_topology(
    tmp_path: Path,
) -> None:
    edge_text = "A B 3\nB C 0\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=False, stem="zero-capacity-cut"
    )
    graph = _networkx_from_text(edge_text, directed=False)

    small = GraphCompute().global_minimum_cut(
        graph, treat_as_undirected=False, limit=200
    )
    large = _large_summary(
        tmp_path,
        snapshot,
        "global_minimum_cut",
        {"treat_as_undirected": False, "limit": 200},
    )

    assert large["min_cut_value"] == small["min_cut_value"] == 0.0
    assert large["disconnected"] is small["disconnected"] is False
    assert large["algorithm"] == "rooted_repeated_min_cut"
    assert large["treat_as_undirected"] is small["treat_as_undirected"] is False


def test_global_cut_rejects_negative_capacity_on_both_compute_paths(
    tmp_path: Path,
) -> None:
    edge_text = "A B 1\nB C -1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=False, stem="negative-global-cut"
    )
    graph = _networkx_from_text(edge_text, directed=False)

    small = GraphCompute().global_minimum_cut(graph)
    large = _large_kernel_summary(
        tmp_path, snapshot, "global_minimum_cut", {}
    )

    assert small == {"success": False, "error": "最大流/最小割不支持负容量"}
    assert large == small


def test_k_truss_rejects_self_loops_like_networkx(tmp_path: Path) -> None:
    edge_text = "A A 9\nA B 1\nB C 1\nC A 1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=False, stem="truss-self-loop"
    )
    with pytest.raises(ValueError, match="self loops"):
        _large_summary(
            tmp_path,
            snapshot,
            "subgraph_extraction",
            {"method": "k_truss", "k": 3},
        )


def test_k_truss_peel_frontier_is_disk_backed_and_keeps_triangle(
    tmp_path: Path,
) -> None:
    # Most edges are peeled in the first round, while the triangle remains.
    # This exercises a frontier proportional to E without requiring an
    # equally large Python list in the controller process.
    spokes = "".join(f"A leaf_{index:03d} 1\n" for index in range(128))
    snapshot = _snapshot_from_text(
        tmp_path,
        "A B 1\nB C 1\nC A 1\n" + spokes,
        directed=False,
        stem="truss-disk-frontier",
    )
    work_root = tmp_path / "truss-disk-work"
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        connection = LargeGraphAlgorithms(
            graph,
            work_root,
            tmp_path / "truss-disk-results",
            workers=1,
        )._truss_state(3)
        try:
            active_edges = list(
                connection.execute(
                    """
                    SELECT source,target FROM active_edges
                    WHERE value=1 ORDER BY source,target
                    """
                )
            )
            active_nodes = [
                str(row[0])
                for row in connection.execute(
                    "SELECT node FROM active WHERE value=1 ORDER BY node"
                )
            ]
            pending_table = connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type='table' AND name='pending_edges'
                """
            ).fetchone()
            pending_count = int(
                connection.execute("SELECT COUNT(*) FROM pending_edges").fetchone()[0]
            )
        finally:
            connection.close()

    assert active_edges == [("A", "B"), ("A", "C"), ("B", "C")]
    assert active_nodes == ["A", "B", "C"]
    assert pending_table is not None
    assert "WITHOUT ROWID" in str(pending_table[0]).upper()
    # The reusable frontier is cleared by the final fixed-point round.
    assert pending_count == 0


def test_high_degree_community_votes_are_disk_backed_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Each leaf starts with a distinct label, so the hub's first-round vote
    # table has O(degree) rows.  A unique heavier edge makes the expected
    # synchronous label choice unambiguous.
    edge_text = "".join(
        f"hub leaf_{index:03d} {9 if index == 73 else 1}\n"
        for index in range(160)
    )
    snapshot = _snapshot_from_text(
        tmp_path,
        edge_text,
        directed=False,
        stem="community-disk-votes",
    )
    work_root = tmp_path / "community-disk-work"
    # Preserve this one round only long enough to inspect the worker's private
    # SQLite schema. Other community tests assert normal iteration cleanup.
    monkeypatch.setattr(
        "graph_data_service.large_graph.algorithms.shutil.rmtree",
        lambda *_args, **_kwargs: None,
    )
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        result = LargeGraphAlgorithms(
            graph,
            work_root,
            tmp_path / "community-disk-results",
            workers=1,
        ).community_detection(
            {"algorithm": "label_propagation", "max_iterations": 1}
        )

    with sqlite3.connect(work_root / "communities.sqlite3") as connection:
        labels = {
            str(node): str(label)
            for node, label in connection.execute("SELECT node,label FROM labels")
        }
    votes_path = (
        work_root
        / "community-label-map"
        / "iteration-00001"
        / "worker-00000"
        / "votes.sqlite3"
    )
    with sqlite3.connect(votes_path) as connection:
        votes_table = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type='table' AND name='label_votes'
            """
        ).fetchone()
        # The table is reused rather than accumulated across nodes.  The last
        # leaf has only the hub as a neighbour, so one vote remains.
        final_vote_count = int(
            connection.execute("SELECT COUNT(*) FROM label_votes").fetchone()[0]
        )

    assert result["summary"]["iterations"] == 1
    assert result["summary"]["num_communities"] == 2
    assert labels["hub"] == "leaf_073"
    assert all(labels[f"leaf_{index:03d}"] == "hub" for index in range(160))
    assert votes_table is not None
    assert "WITHOUT ROWID" in str(votes_table[0]).upper()
    assert final_vote_count == 1


def test_parallel_community_node_map_matches_serial_labels(tmp_path: Path) -> None:
    edge_text = (
        "A B 3\nA C 1\nB C 2\nC D 4\nD E 1\nE F 2\nF D 3\n"
        "G H 2\nH I 2\nI G 2\n"
    )
    snapshot = _snapshot_from_text(
        tmp_path,
        edge_text,
        directed=False,
        stem="community-parallel-equivalence",
    )

    def execute(workers: int, stem: str) -> tuple[dict, dict[str, str]]:
        work_root = tmp_path / f"{stem}-work"
        with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
            result = LargeGraphAlgorithms(
                graph,
                work_root,
                tmp_path / f"{stem}-results",
                workers=workers,
            ).community_detection(
                {"algorithm": "label_propagation", "max_iterations": 6}
            )
        with sqlite3.connect(work_root / "communities.sqlite3") as connection:
            labels = {
                str(node): str(label)
                for node, label in connection.execute(
                    "SELECT node,label FROM labels ORDER BY node"
                )
            }
        return result["summary"], labels

    serial_summary, serial_labels = execute(1, "community-serial")
    parallel_summary, parallel_labels = execute(2, "community-parallel")

    assert parallel_labels == serial_labels
    assert parallel_summary["iterations"] == serial_summary["iterations"]
    assert parallel_summary["num_communities"] == serial_summary["num_communities"]
    assert parallel_summary["modularity"] == serial_summary["modularity"]
    assert serial_summary["parallel"]["workers"] == 1
    assert parallel_summary["parallel"]["strategy"] == (
        "node-partition-map/sqlite-label-reduce"
    )
    assert parallel_summary["parallel"]["workers"] == 2
    assert parallel_summary["parallel"]["iterations"] == parallel_summary["iterations"]
    assert parallel_summary["parallel"]["map_partitions"] == 2
    for worker in range(2):
        # The stable node assignment proves two independent map partitions
        # were generated, while per-iteration outputs are reclaimed after
        # their reduce so disk remains O(V), not O(iterations * V).
        assert (
            tmp_path
            / "community-parallel-work"
            / "community-label-map"
            / "nodes"
            / f"part-{worker:05d}.jsonl"
        ).is_file()
    assert not (
        tmp_path
        / "community-parallel-work"
        / "community-label-map"
        / "iteration-00001"
    ).exists()


def test_community_detection_returns_focus_node_memberships(tmp_path: Path) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        "A B 1\nB C 1\nD E 1\n",
        directed=False,
        stem="community-focus-nodes",
    )
    work_root = tmp_path / "community-focus-work"
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        summary = LargeGraphAlgorithms(
            graph,
            work_root,
            tmp_path / "community-focus-results",
            workers=1,
        ).community_detection(
            {
                "algorithm": "label_propagation",
                "max_iterations": 2,
                "focus_nodes": ["A", "D", "missing", "A"],
            }
        )["summary"]

    memberships = {
        row["node_id"]: row for row in summary["focus_node_communities"]
    }
    assert set(memberships) == {"A", "D"}
    assert summary["missing_focus_nodes"] == ["missing"]
    with sqlite3.connect(work_root / "communities.sqlite3") as connection:
        for node, membership in memberships.items():
            label = str(
                connection.execute(
                    "SELECT label FROM labels WHERE node=?", (node,)
                ).fetchone()[0]
            )
            size = int(
                connection.execute(
                    "SELECT size FROM community_sizes WHERE label=?", (label,)
                ).fetchone()[0]
            )
            assert membership["community_id"] == label
            assert membership["community_size"] == size
            assert membership["representative_nodes"]


def test_parallel_community_empty_map_shard_is_safe(tmp_path: Path) -> None:
    # These two ids deliberately hash to the same worker.  The other worker
    # must still run and publish a valid empty JSONL map shard.
    assert _stable_partition("A", 2) == _stable_partition("B", 2)
    empty_worker = 1 - _stable_partition("A", 2)
    snapshot = _snapshot_from_text(
        tmp_path,
        "A B 1\n",
        directed=False,
        stem="community-empty-map-shard",
    )
    work_root = tmp_path / "community-empty-map-work"
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        summary = LargeGraphAlgorithms(
            graph,
            work_root,
            tmp_path / "community-empty-map-results",
            workers=2,
        ).community_detection(
            {"algorithm": "label_propagation", "max_iterations": 1}
        )["summary"]

    node_shard = (
        work_root
        / "community-label-map"
        / "nodes"
        / f"part-{empty_worker:05d}.jsonl"
    )
    output_iteration = (
        work_root
        / "community-label-map"
        / "iteration-00001"
    )
    assert summary["success"] is True
    assert summary["parallel"]["workers"] == 2
    assert summary["parallel"]["map_partitions"] == 2
    assert summary["parallel"]["nonempty_map_partitions"] == 1
    assert node_shard.is_file() and node_shard.read_text(encoding="utf-8") == ""
    assert not output_iteration.exists()


def test_undirected_find_cycles_includes_self_loop(tmp_path: Path) -> None:
    snapshot = _snapshot_from_text(
        tmp_path, "A A 1\nA B 1\n", directed=False, stem="cycle-self-loop"
    )
    large = _large_summary(tmp_path, snapshot, "find_cycles", {"limit": 20})
    assert ["A"] in large["cycles"]


@pytest.mark.parametrize("centrality_type", ["pagerank", "eigenvector", "katz"])
def test_undirected_self_loop_iterative_centrality_matches_networkx(
    tmp_path: Path, centrality_type: str
) -> None:
    edge_text = "A A 2\nA B 1\nB C 3\nC A 1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=False, stem=f"self-loop-{centrality_type}"
    )
    graph = _networkx_from_text(edge_text, directed=False)
    small = GraphCompute().node_centrality(graph, centrality_type, limit=200)
    large = _large_summary(
        tmp_path,
        snapshot,
        "node_centrality",
        {
            "centrality_type": centrality_type,
            "max_iterations": 1000,
            "tolerance": 1.0e-7,
            "limit": 200,
        },
    )
    expected = {row["node"]: row["value"] for row in small["top_nodes"]}
    actual = {row["node"]: row["value"] for row in large["top_nodes"]}
    assert actual == pytest.approx(expected, abs=3.0e-5)


def test_singleton_degree_centrality_matches_networkx(tmp_path: Path) -> None:
    snapshot = _snapshot_from_text(
        tmp_path, "only only 1\n", directed=False, stem="singleton-degree"
    )
    large = _large_summary(
        tmp_path, snapshot, "degree_centrality", {"top_k": 20}
    )
    assert large["top"] == [
        {"node_id": "only", "node": "only", "score": 1.0}
    ]


def test_voterank_tie_uses_original_node_insertion_order(tmp_path: Path) -> None:
    edge_text = "D A 1\nA B 1\nD C 1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=False, stem="voterank-order"
    )
    graph = _networkx_from_text(edge_text, directed=False)
    small = GraphCompute().node_centrality(graph, "voterank", limit=200)
    large = _large_summary(
        tmp_path,
        snapshot,
        "node_centrality",
        {"centrality_type": "voterank", "limit": 200},
    )
    assert large["top_nodes"] == small["top_nodes"]


@pytest.mark.parametrize(
    "edge_text",
    [
        "B A 9\nA B 2\nA C 3\nB C 4\nC C 20\n",
        "A B -1\nB C -2\nC A -3\n",
    ],
)
def test_weighted_average_clustering_matches_networkx_projection(
    tmp_path: Path, edge_text: str
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=True, stem="weighted-clustering"
    )
    graph = _networkx_from_text(edge_text, directed=True)
    small = GraphCompute.global_metrics(graph, "average_clustering")
    large = _large_summary(
        tmp_path, snapshot, "global_metrics", {"metric": "average_clustering"}
    )
    assert large["value"] == pytest.approx(small["value"], abs=1.0e-6)


@pytest.mark.parametrize("metric", ["transitivity", "average_clustering"])
def test_triangle_metrics_use_node_partition_parallelism_and_match_small_graph(
    tmp_path: Path, metric: str
) -> None:
    edge_text = "A B 2\nB C 3\nC A 4\nC D 1\nD E 5\nE C 2\nF F 7\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=True, stem=f"parallel-{metric}"
    )
    graph = _networkx_from_text(edge_text, directed=True)
    small = GraphCompute.global_metrics(graph, metric)
    serial = _large_summary(
        tmp_path, snapshot, "global_metrics", {"metric": metric}, workers=1
    )
    parallel = _large_summary(
        tmp_path, snapshot, "global_metrics", {"metric": metric}, workers=2
    )

    assert parallel["value"] == serial["value"]
    assert parallel["value"] == pytest.approx(small["value"], abs=1.0e-6)
    assert parallel["parallel"] == {
        "strategy": "node-partition-map/scalar-reduce",
        "workers": 2,
        "node_count": 6,
    }


def test_undefined_assortativity_is_json_safe(tmp_path: Path) -> None:
    edge_text = "A B 1\nB C 1\nC A 1\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=False, stem="regular-assortativity"
    )
    graph = _networkx_from_text(edge_text, directed=False)
    small = GraphCompute.global_metrics(graph, "degree_assortativity")
    large = _large_summary(
        tmp_path, snapshot, "global_metrics", {"metric": "degree_assortativity"}
    )
    assert small["value"] is None and small["defined"] is False
    assert large["value"] is None and large["defined"] is False
    assert small["undefined_reason_code"] == "zero_endpoint_degree_variance"
    assert large["undefined_reason_code"] == "zero_endpoint_degree_variance"
    assert "Pearson 相关系数的分母为 0" in small["undefined_reason"]
    assert large["undefined_reason"] == small["undefined_reason"]
    json.dumps({"small": small, "large": large}, allow_nan=False)


def test_edgelist_service_default_quality_matches_legacy_loader(tmp_path: Path) -> None:
    source = tmp_path / "defaults.txt"
    source.write_text("A b 1\na b 7\nA b 3\n", encoding="utf-8")
    service = LargeGraphService(tmp_path / "large", embedded_worker=False)
    try:
        result = service.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="s1",
            source_path=str(source),
            source_name=source.name,
            source_token="token-1",
            directed=True,
            partitions=2,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert result["success"] is True
        version = service.list_versions(
            user_id="alice", session_id="s1", graph_id="s1"
        )[0]
        assert version["policy"]["node_normalization"] == "exact"
        assert version["policy"]["weight_conflict"] == "last"
    finally:
        service.close()


def test_empty_snapshot_has_explicit_boundary_result_for_complete_catalogue(
    tmp_path: Path,
) -> None:
    """An empty, but valid, snapshot must never trigger an incidental crash."""
    snapshot = _snapshot_from_text(
        tmp_path, "", directed=True, stem="empty-catalogue"
    )
    cases = {
        "shortest_path": ({"source": "missing", "target": "missing"}, False),
        "all_shortest_paths": ({"source": "missing", "target": "missing"}, False),
        "maximum_flow": ({"source": "missing", "sink": "missing"}, False),
        "minimum_cut": ({"source": "missing", "sink": "missing"}, False),
        "global_minimum_cut": ({}, False),
        "minimum_spanning_tree": ({}, True),
        "community_detection": ({"algorithm": "label_propagation"}, True),
        "node_centrality": ({"centrality_type": "pagerank"}, True),
        "graph_connectivity": ({}, True),
        "find_cycles": ({}, True),
        "maximum_cycle": ({}, True),
        "topological_sort": ({}, True),
        "degree_analysis": ({}, True),
        "subgraph_extraction": ({"method": "node_subset", "nodes": []}, True),
        "link_prediction_score": ({"method": "jaccard"}, True),
        "path_analysis": ({"method": "average_shortest_path_length"}, False),
        "clique_analysis": ({"method": "clique_number"}, True),
        "reachability": ({"method": "transitive_closure"}, True),
        "global_metrics": ({"metric": "density"}, True),
        "graph_structure_analysis": ({"method": "is_tree"}, False),
        "connectivity_analysis": ({"method": "bridges"}, True),
        "tree_dag_analysis": ({"method": "dag_longest_path"}, True),
        "matching_coloring_analysis": ({"method": "greedy_color"}, True),
        "distance_measures": ({"metric": "center"}, False),
    }
    assert set(cases) == LargeGraphAlgorithms.SUPPORTED

    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        for algorithm, (parameters, expected_success) in cases.items():
            kernel = LargeGraphAlgorithms(
                graph,
                tmp_path / "empty-work" / algorithm,
                tmp_path / "empty-results" / algorithm,
                workers=1,
            )
            result = kernel.run(algorithm, parameters)
            assert result["summary"]["success"] is expected_success, algorithm
            assert isinstance(result["preview"], list), algorithm
            assert isinstance(result["result_files"], list), algorithm
            json.dumps(result, allow_nan=False)


def test_singleton_self_loop_has_explicit_result_for_complete_catalogue(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path, "only only 3\n", directed=True, stem="singleton-catalogue"
    )
    cases = {
        "shortest_path": ({"source": "only", "target": "only"}, True),
        "all_shortest_paths": ({"source": "only", "target": "only"}, True),
        "maximum_flow": ({"source": "only", "sink": "only"}, False),
        "minimum_cut": ({"source": "only", "sink": "only"}, False),
        "global_minimum_cut": ({}, False),
        "minimum_spanning_tree": ({}, True),
        "community_detection": ({"algorithm": "label_propagation"}, True),
        "node_centrality": ({"centrality_type": "pagerank"}, True),
        "graph_connectivity": ({}, True),
        "find_cycles": ({}, True),
        "maximum_cycle": ({}, True),
        "topological_sort": ({}, False),
        "degree_analysis": ({}, True),
        "subgraph_extraction": (
            {"method": "node_subset", "nodes": ["only"]},
            True,
        ),
        "link_prediction_score": ({"method": "jaccard"}, True),
        "path_analysis": ({"method": "average_shortest_path_length"}, True),
        "clique_analysis": ({"method": "clique_number"}, True),
        "reachability": ({"method": "descendants", "node": "only"}, True),
        "global_metrics": ({"metric": "density"}, True),
        "graph_structure_analysis": ({"method": "is_tree"}, True),
        "connectivity_analysis": ({"method": "bridges"}, True),
        "tree_dag_analysis": ({"method": "dag_longest_path"}, False),
        "matching_coloring_analysis": ({"method": "greedy_color"}, True),
        "distance_measures": ({"metric": "center"}, True),
    }
    assert set(cases) == LargeGraphAlgorithms.SUPPORTED

    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        for algorithm, (parameters, expected_success) in cases.items():
            kernel = LargeGraphAlgorithms(
                graph,
                tmp_path / "singleton-work" / algorithm,
                tmp_path / "singleton-results" / algorithm,
                workers=1,
            )
            result = kernel.run(algorithm, parameters)
            assert result["summary"]["success"] is expected_success, algorithm
            assert isinstance(result["preview"], list), algorithm
            assert isinstance(result["result_files"], list), algorithm
            json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    ("algorithm", "expected_top"),
    [
        ("degree", [{"node_id": "only", "node": "only", "score": 2.0}]),
        ("in_degree", [{"node_id": "only", "node": "only", "score": 1.0}]),
        ("out_degree", [{"node_id": "only", "node": "only", "score": 1.0}]),
        (
            "degree_centrality",
            [{"node_id": "only", "node": "only", "score": 1.0}],
        ),
    ],
)
def test_degree_fast_paths_handle_singleton_self_loop(
    tmp_path: Path, algorithm: str, expected_top: list[dict]
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        "only only 1\n",
        directed=True,
        stem=f"singleton-fast-{algorithm}",
    )
    result = LocalParallelGraphEngine(tmp_path / "fast-artifacts").execute(
        snapshot=snapshot,
        algorithm=algorithm,
        parameters={"top_k": 20},
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )

    assert result["summary"]["result_count"] == 1
    assert result["summary"]["top"] == expected_top


@pytest.mark.parametrize(
    "algorithm", ["degree", "in_degree", "out_degree", "degree_centrality"]
)
def test_degree_fast_paths_handle_empty_snapshot(
    tmp_path: Path, algorithm: str
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path, "", directed=True, stem=f"empty-fast-{algorithm}"
    )
    result = LocalParallelGraphEngine(tmp_path / "empty-fast-artifacts").execute(
        snapshot=snapshot,
        algorithm=algorithm,
        parameters={"top_k": 20},
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )

    assert result["summary"]["result_count"] == 0
    assert result["summary"]["score_min"] == 0.0
    assert result["summary"]["score_max"] == 0.0
    assert result["summary"]["score_mean"] == 0.0
    assert result["summary"]["top"] == []


@pytest.mark.parametrize("directed", [False, True])
def test_singleton_self_loop_community_modularity_matches_small_graph(
    tmp_path: Path, directed: bool
) -> None:
    edge_text = "only only 3\n"
    snapshot = _snapshot_from_text(
        tmp_path,
        edge_text,
        directed=directed,
        stem=f"singleton-community-{'directed' if directed else 'undirected'}",
    )
    graph = _networkx_from_text(edge_text, directed=directed)
    small = GraphCompute().community_detection(graph, "label_propagation")
    large = _large_summary(
        tmp_path,
        snapshot,
        "community_detection",
        {"algorithm": "label_propagation"},
        workers=1,
    )

    assert large["num_communities"] == 1
    assert large["modularity"] == pytest.approx(small["modularity"], abs=1.0e-6)


def test_all_singleton_self_loop_centralities_are_finite_and_match_small_graph(
    tmp_path: Path,
) -> None:
    edge_text = "only only 3\n"
    snapshot = _snapshot_from_text(
        tmp_path, edge_text, directed=True, stem="singleton-all-centralities"
    )
    graph = _networkx_from_text(edge_text, directed=True)
    centrality_types = (
        "degree",
        "betweenness",
        "closeness",
        "pagerank",
        "eigenvector",
        "harmonic",
        "katz",
        "load",
        "voterank",
        "percolation",
    )

    with PartitionedDiskGraph(snapshot["manifest_path"]) as disk_graph:
        for centrality_type in centrality_types:
            small = GraphCompute().node_centrality(
                graph, centrality_type, limit=20
            )
            kernel = LargeGraphAlgorithms(
                disk_graph,
                tmp_path / "singleton-centrality-work" / centrality_type,
                tmp_path / "singleton-centrality-results" / centrality_type,
                workers=1,
            )
            large = kernel.run(
                "node_centrality",
                {"centrality_type": centrality_type, "max_iterations": 1000},
            )["summary"]

            assert large["success"] is True, centrality_type
            assert large["total_nodes"] == 1, centrality_type
            assert large["top_nodes"][0]["node"] == "only", centrality_type
            assert large["top_nodes"][0]["value"] == pytest.approx(
                small["top_nodes"][0]["value"], abs=1.0e-6
            ), centrality_type
            json.dumps(large, allow_nan=False)


def _engine_artifact_rows(result: dict, stem: str) -> list[dict]:
    manifest_path = Path(result["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    prefix = f"results/{stem}/"
    for relative in manifest["result_files"]:
        if not str(relative).startswith(prefix):
            continue
        with (manifest_path.parent / relative).open(encoding="utf-8") as source:
            rows.extend(json.loads(line) for line in source if line.strip())
    return rows


def _long_chain_text(size: int, *, close_cycle: bool = False) -> str:
    nodes = [f"N{index:04d}" for index in range(size)]
    edges = [
        f"{nodes[index]} {nodes[index + 1]} 1"
        for index in range(size - 1)
    ]
    if close_cycle:
        edges.append(f"{nodes[-1]} {nodes[0]} 1")
    return "\n".join(edges) + "\n"


def test_long_shortest_path_uses_bounded_control_payload_and_complete_artifact(
    tmp_path: Path,
) -> None:
    node_count = 64
    snapshot = _snapshot_from_text(
        tmp_path, _long_chain_text(node_count), stem="long-shortest"
    )
    result = LocalParallelGraphEngine(tmp_path / "long-shortest-artifacts").execute(
        snapshot=snapshot,
        algorithm="shortest_path",
        parameters={"source": "N0000", "target": f"N{node_count - 1:04d}"},
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )
    summary = result["summary"]

    assert summary["path_nodes_total"] == node_count
    assert summary["hop_count"] == node_count - 1
    assert summary["path_truncated"] is True
    assert summary["edges_truncated"] is True
    assert len(summary["path"]) == 20
    assert len(summary["edges"]) == 20
    assert len(result["preview"]) == 20
    assert len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) < 64 * 1024

    rows = _engine_artifact_rows(result, "shortest-path")
    assert len(rows) == node_count
    ordered = sorted(rows, key=lambda row: int(row["position"]))
    assert [row["node"] for row in ordered] == [
        f"N{index:04d}" for index in range(node_count)
    ]


def test_pair_shortest_path_uses_bidirectional_bfs_for_unit_weight_snapshot(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        "A B\nB C\nC D\nD E\nA X\nX Y\nY E\n",
        directed=True,
        stem="bidirectional-unit-path",
    )
    progress: list[tuple[str, float, str, dict]] = []
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        assert graph.has_weights is False
        result = LargeGraphAlgorithms(
            graph,
            tmp_path / "bidirectional-unit-work",
            tmp_path / "bidirectional-unit-results",
            progress=lambda stage, value, message, detail: progress.append(
                (stage, value, message, detail)
            ),
        ).shortest_path(
            {"source": "A", "target": "E", "use_weight": True}
        )

    summary = result["summary"]
    assert summary["found"] is True
    assert summary["execution_strategy"] == "bidirectional_bfs"
    assert summary["unit_weight_fallback"] is True
    assert summary["path"] == ["A", "X", "Y", "E"]
    assert summary["hop_count"] == 3
    assert summary["total_length"] == 3.0
    assert summary["forward_visited"] > 0
    assert summary["backward_visited"] > 0
    assert progress[0][2] == "执行双向 BFS"


def test_unweighted_request_uses_bidirectional_bfs_but_weighted_request_keeps_dijkstra(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        "A B 1\nB D 1\nA D 100\n",
        directed=True,
        stem="bidirectional-versus-dijkstra",
    )
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        kernel = LargeGraphAlgorithms(
            graph,
            tmp_path / "bidirectional-versus-dijkstra-work",
            tmp_path / "bidirectional-versus-dijkstra-results",
        )
        unweighted = kernel.shortest_path(
            {"source": "A", "target": "D", "use_weight": False}
        )["summary"]
        weighted = kernel.shortest_path(
            {"source": "A", "target": "D", "use_weight": True}
        )["summary"]

    assert unweighted["execution_strategy"] == "bidirectional_bfs"
    assert unweighted["unit_weight_fallback"] is False
    assert unweighted["path"] == ["A", "D"]
    assert unweighted["hop_count"] == 1
    assert unweighted["total_length"] == 1.0
    assert weighted["execution_strategy"] == "disk_dijkstra"
    assert weighted["path"] == ["A", "B", "D"]
    assert weighted["total_length"] == 2.0


def test_bidirectional_bfs_obeys_direction_and_reports_unreachable_pair(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path,
        "A B\nB C\n",
        directed=True,
        stem="bidirectional-directed",
    )
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        result = LargeGraphAlgorithms(
            graph,
            tmp_path / "bidirectional-directed-work",
            tmp_path / "bidirectional-directed-results",
        ).shortest_path(
            {"source": "C", "target": "A", "use_weight": False}
        )

    assert result["summary"]["found"] is False
    assert result["summary"]["execution_strategy"] == "bidirectional_bfs"
    assert result["result_files"] == []


def test_yen_runtime_path_state_has_a_hard_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot_from_text(
        tmp_path, _long_chain_text(64), stem="yen-state-byte-limit"
    )
    monkeypatch.setenv("LARGE_GRAPH_YEN_MAX_PATH_STATE_BYTES", "1024")
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        kernel = LargeGraphAlgorithms(
            graph, tmp_path / "yen-byte-work", tmp_path / "yen-byte-results"
        )
        with pytest.raises(ValueError, match="Yen 路径状态超过运行时内存上限"):
            kernel.shortest_path(
                {
                    "source": "N0000",
                    "target": "N0063",
                    "algorithm": "yen_k_shortest",
                    "k": 2,
                }
            )


def test_simple_path_dfs_does_not_materialise_a_wide_frontier(
    tmp_path: Path,
) -> None:
    class WideGraph:
        partitions = 1

        def __init__(self) -> None:
            self.source_rows = 0

        def iter_out_edges(self, node: str):
            if node == "source":
                for index in range(10_000):
                    self.source_rows += 1
                    yield f"branch-{index:05d}", 1.0
            elif node == "branch-00000":
                yield "target", 1.0

    graph = WideGraph()
    kernel = LargeGraphAlgorithms(
        graph, tmp_path / "wide-path-work", tmp_path / "wide-path-results"
    )
    paths = kernel._iter_simple_paths("source", "target", 2)
    assert next(iter(paths)) == ["source", "branch-00000", "target"]
    assert graph.source_rows == 1


def test_cycle_dfs_does_not_materialise_a_wide_frontier(tmp_path: Path) -> None:
    class WideCycleGraph:
        partitions = 1
        directed = True

        def __init__(self) -> None:
            self.source_rows = 0

        def iter_node_ids(self):
            yield "source"

        def iter_neighbors(self, node: str, *, direction: str = "out"):
            if node == "source":
                for index in range(10_000):
                    self.source_rows += 1
                    yield f"zbranch-{index:05d}", 1.0
            elif node == "zbranch-00000":
                yield "source", 1.0

    graph = WideCycleGraph()
    kernel = LargeGraphAlgorithms(
        graph, tmp_path / "wide-cycle-work", tmp_path / "wide-cycle-results"
    )
    cycles = kernel._iter_cycles()
    assert next(iter(cycles)) == ["source", "zbranch-00000"]
    assert graph.source_rows == 1


def test_clique_dfs_does_not_materialise_a_wide_frontier(
    tmp_path: Path,
) -> None:
    class WideCliqueGraph:
        partitions = 1

        def __init__(self) -> None:
            self.source_rows = 0

        def iter_node_ids(self):
            yield "source"

        def iter_neighbors(self, node: str, *, undirected: bool = False):
            if node == "source":
                for index in range(10_000):
                    self.source_rows += 1
                    yield f"zbranch-{index:05d}", 1.0
            elif node == "zbranch-00000":
                yield "source", 1.0

        @staticmethod
        def edge_weight(source: str, target: str, *, undirected: bool = False):
            if {source, target} == {"source", "zbranch-00000"}:
                return 1.0
            return None

    graph = WideCliqueGraph()
    kernel = LargeGraphAlgorithms(
        graph, tmp_path / "wide-clique-work", tmp_path / "wide-clique-results"
    )
    cliques = kernel._iter_maximal_cliques()
    assert next(iter(cliques)) == ["source", "zbranch-00000"]
    assert graph.source_rows == 1


def test_lazy_clique_dfs_preserves_exact_maximal_cliques(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        kernel = LargeGraphAlgorithms(
            graph, tmp_path / "clique-exact-work", tmp_path / "clique-exact-results"
        )
        actual = {frozenset(clique) for clique in kernel._iter_maximal_cliques()}

    reference = _networkx_from_text(_CATALOGUE_EDGES, directed=True).to_undirected()
    expected = {frozenset(clique) for clique in nx.find_cliques(reference)}
    assert actual == expected


def test_topological_cycle_samples_release_complete_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CyclicGraph:
        partitions = 1
        directed = True
        node_count = 1

        @staticmethod
        def iter_node_ids():
            yield "A"

        @staticmethod
        def iter_edges():
            yield "A", "A", 1.0, ""

        @staticmethod
        def iter_out_edges(node: str):
            yield "A", 1.0

        @staticmethod
        def display_name(node: str) -> str:
            return node

    class TrackedCycle(list):
        live = 0

        def __init__(self, values):
            super().__init__(values)
            type(self).live += 1

        def __del__(self):
            type(self).live -= 1

    def cycles():
        for index in range(5):
            # The caller may still hold the cycle being replaced, but it must
            # not retain every previously sampled full membership list.
            assert TrackedCycle.live <= 1
            yield TrackedCycle([f"N{index}-{member}" for member in range(10_000)])

    kernel = LargeGraphAlgorithms(
        CyclicGraph(), tmp_path / "topological-work", tmp_path / "topological-results"
    )
    monkeypatch.setattr(kernel, "_iter_cycles", cycles)
    result = kernel.topological_sort({})

    assert result["summary"]["sample_cycle_lengths"] == [10_000] * 5
    assert all(
        len(cycle) == 20 for cycle in result["summary"]["sample_cycles"]
    )


@pytest.mark.parametrize(
    ("algorithm", "parameters", "artifact_stem", "count_key"),
    [
        (
            "all_shortest_paths",
            {"source": "N0000", "target": "N0063", "limit": 10},
            "all-shortest-paths",
            "count_returned",
        ),
        (
            "path_analysis",
            {
                "method": "all_simple_paths",
                "source": "N0000",
                "target": "N0063",
                "cutoff": 100,
                "limit": 10,
            },
            "all-simple-paths",
            "count",
        ),
    ],
)
def test_long_enumerated_paths_stream_membership_to_artifact(
    tmp_path: Path,
    algorithm: str,
    parameters: dict,
    artifact_stem: str,
    count_key: str,
) -> None:
    node_count = 64
    snapshot = _snapshot_from_text(
        tmp_path, _long_chain_text(node_count), stem=f"long-{algorithm}"
    )
    result = LocalParallelGraphEngine(tmp_path / f"{algorithm}-artifacts").execute(
        snapshot=snapshot,
        algorithm=algorithm,
        parameters=parameters,
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )
    summary = result["summary"]

    assert summary[count_key] == 1
    assert summary["path_node_counts_preview"] == [node_count]
    assert summary["paths_preview_truncated"] is True
    assert len(summary["paths"]) == 1
    assert len(summary["paths"][0]) == 20
    rows = _engine_artifact_rows(result, artifact_stem)
    assert len(rows) == node_count
    assert {int(row["path_nodes_total"]) for row in rows} == {node_count}


@pytest.mark.parametrize(
    ("algorithm", "artifact_stem"),
    [("find_cycles", "simple-cycles"), ("maximum_cycle", "maximum-cycle")],
)
def test_long_cycle_uses_bounded_preview_and_complete_edge_artifact(
    tmp_path: Path, algorithm: str, artifact_stem: str
) -> None:
    node_count = 64
    snapshot = _snapshot_from_text(
        tmp_path,
        _long_chain_text(node_count, close_cycle=True),
        stem=f"long-{algorithm}",
    )
    result = LocalParallelGraphEngine(tmp_path / f"{algorithm}-artifacts").execute(
        snapshot=snapshot,
        algorithm=algorithm,
        parameters={"limit": 10} if algorithm == "find_cycles" else {"max_cycles": 10},
        user_scope="test__session",
        graph_id=str(snapshot["graph_id"]),
        graph_version=1,
        workers=2,
    )
    summary = result["summary"]

    if algorithm == "find_cycles":
        assert summary["count_returned"] == 1
        assert summary["cycle_lengths_preview"] == [node_count]
        assert len(summary["cycles"][0]) == 20
        assert summary["cycles_preview_truncated"] is True
    else:
        assert summary["cycle_length"] == node_count
        assert len(summary["cycle"]) == 20
        assert len(summary["edges"]) == 20
        assert summary["cycle_truncated"] is True
        assert summary["edges_truncated"] is True
    rows = _engine_artifact_rows(result, artifact_stem)
    assert len(rows) == node_count
    assert {int(row["cycle_nodes_total"]) for row in rows} == {node_count}


def test_long_clique_membership_is_not_embedded_in_control_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_count = 64
    snapshot = _snapshot_from_text(
        tmp_path, _long_chain_text(node_count), stem="long-clique-preview"
    )
    result_root = tmp_path / "results"
    with PartitionedDiskGraph(snapshot["manifest_path"]) as graph:
        nodes = list(graph.iter_node_ids())
        kernel = LargeGraphAlgorithms(
            graph, tmp_path / "long-clique-work", result_root, workers=1
        )
        # A real 64-clique makes this intentionally simple reference enumerator
        # exponential.  Injecting one valid long enumeration result isolates
        # and verifies the result/control-plane contract under test.
        monkeypatch.setattr(kernel, "_iter_maximal_cliques", lambda: iter([nodes]))
        result = kernel.clique_analysis({"method": "find_cliques", "limit": 10})

    summary = result["summary"]
    assert summary["count_returned"] == 1
    assert summary["clique_sizes_preview"] == [node_count]
    assert len(summary["cliques"][0]) == 20
    assert summary["cliques_preview_truncated"] is True
    rows: list[dict] = []
    for relative in result["result_files"]:
        with (tmp_path / relative).open(encoding="utf-8") as source:
            rows.extend(json.loads(line) for line in source if line.strip())
    assert len(rows) == node_count
    assert {int(row["clique_size"]) for row in rows} == {node_count}

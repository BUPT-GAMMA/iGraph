from __future__ import annotations

import json

import networkx as nx

from graph_data_service.compute import NX_TOOL_NAMES, run_nx_tool
from graph_data_service.tool_catalog import GRAPH_TOOLS
from graph_engine.tool_execution import execution_policy


EXPANDED_TOOLS = {
    "graph_structure_analysis",
    "connectivity_analysis",
    "tree_dag_analysis",
    "matching_coloring_analysis",
    "distance_measures",
}


def _spec(name: str) -> dict:
    return next(item for item in GRAPH_TOOLS if item["name"] == name)


def test_expanded_yigraph_tools_are_catalogued_read_only_compute_tools() -> None:
    assert EXPANDED_TOOLS <= NX_TOOL_NAMES
    for name in EXPANDED_TOOLS:
        spec = _spec(name)
        assert spec["category"] == "compute"
        assert spec["inputSchema"]["required"]
        assert execution_policy(name).read_only is True


def test_structure_tree_and_dag_operators() -> None:
    graph = nx.DiGraph([("A", "C"), ("B", "C"), ("C", "D")])

    dag = run_nx_tool(
        "graph_structure_analysis",
        graph,
        {"method": "is_directed_acyclic_graph"},
    )
    bfs = run_nx_tool(
        "graph_structure_analysis",
        graph,
        {"method": "bfs_tree", "source": "A"},
    )
    v_structures = run_nx_tool(
        "tree_dag_analysis",
        graph,
        {"method": "v_structures"},
    )
    longest = run_nx_tool(
        "tree_dag_analysis",
        graph,
        {"method": "dag_longest_path"},
    )

    assert dag == {
        "success": True,
        "method": "is_directed_acyclic_graph",
        "value": True,
    }
    assert bfs["success"] is True
    assert bfs["edges_preview"] == [{"from": "A", "to": "C"}, {"from": "C", "to": "D"}]
    assert v_structures["structures"] == [
        {"left": "A", "center": "C", "right": "B"}
    ]
    assert longest["path"] in (["A", "C", "D"], ["B", "C", "D"])


def test_connectivity_matching_coloring_and_distance_operators() -> None:
    graph = nx.DiGraph()
    for left, right, weight in [
        ("A", "B", 3.0),
        ("B", "C", 2.0),
        ("C", "A", 1.0),
        ("C", "D", 4.0),
    ]:
        graph.add_edge(left, right, weight=weight)
        graph.add_edge(right, left, weight=weight)

    cut_vertices = run_nx_tool(
        "connectivity_analysis",
        graph,
        {"method": "articulation_points"},
    )
    matching = run_nx_tool(
        "matching_coloring_analysis",
        graph,
        {"method": "max_weight_matching"},
    )
    coloring = run_nx_tool(
        "matching_coloring_analysis",
        graph,
        {"method": "greedy_color"},
    )
    center = run_nx_tool("distance_measures", graph, {"metric": "center"})

    assert cut_vertices["nodes"] == ["C"]
    assert matching["success"] is True
    assert matching["size"] == 2
    assert coloring["success"] is True
    assert coloring["num_colors"] >= 2
    assert center["success"] is True
    assert center["nodes"] == ["C"]


def test_expanded_catalog_exposes_representative_yigraph_methods() -> None:
    structure_methods = set(
        _spec("graph_structure_analysis")["inputSchema"]["properties"]["method"]["enum"]
    )
    connectivity_methods = set(
        _spec("connectivity_analysis")["inputSchema"]["properties"]["method"]["enum"]
    )
    distance_metrics = set(
        _spec("distance_measures")["inputSchema"]["properties"]["metric"]["enum"]
    )

    assert {"check_planarity", "is_bipartite", "weisfeiler_lehman_graph_hash"} <= structure_methods
    assert {"minimum_node_cut", "bridges", "condensation"} <= connectivity_methods
    assert {"wiener_index", "rich_club_coefficient", "estrada_index", "girth"} <= distance_metrics


def test_expanded_operators_return_structured_failures_for_invalid_graph_domains() -> None:
    disconnected = nx.Graph([("A", "B"), ("C", "D")])
    isolated = nx.Graph([("A", "B")])
    isolated.add_node("C")

    cases = [
        ("graph_structure_analysis", nx.Graph(), {"method": "is_tree"}),
        ("connectivity_analysis", disconnected, {"method": "minimum_node_cut"}),
        ("tree_dag_analysis", nx.DiGraph([("A", "B")]), {"method": "is_aperiodic"}),
        (
            "tree_dag_analysis",
            nx.relabel_nodes(nx.path_graph(3), str),
            {"method": "immediate_dominators", "source": "0"},
        ),
        ("matching_coloring_analysis", isolated, {"method": "min_edge_cover"}),
        ("matching_coloring_analysis", nx.complete_graph(4), {"method": "equitable_color", "num_colors": 2}),
        ("matching_coloring_analysis", nx.path_graph(3), {"method": "greedy_color", "strategy": "invalid"}),
        ("distance_measures", nx.path_graph(3), {"metric": "flow_hierarchy"}),
        ("distance_measures", nx.Graph(), {"metric": "center"}),
    ]

    for tool_name, graph, arguments in cases:
        result = run_nx_tool(tool_name, graph, arguments)
        assert result["success"] is False, (tool_name, arguments, result)
        assert result.get("error"), (tool_name, arguments, result)


def test_girth_of_acyclic_graph_is_json_safe() -> None:
    result = run_nx_tool("distance_measures", nx.path_graph(5), {"metric": "girth"})

    assert result == {
        "success": True,
        "metric": "girth",
        "value": None,
        "acyclic": True,
    }
    json.dumps(result, allow_nan=False)


def test_score_sequence_honors_preview_limit() -> None:
    graph = nx.DiGraph()
    graph.add_nodes_from(str(index) for index in range(100))

    result = run_nx_tool(
        "graph_structure_analysis",
        graph,
        {"method": "score_sequence", "limit": 5},
    )

    assert result["success"] is True
    assert len(result["score_sequence"]) == 5
    assert result["total_nodes"] == 100
    assert result["truncated"] is True


def test_collision_enumeration_is_bounded_before_materialization() -> None:
    graph = nx.DiGraph()
    graph.add_edges_from((f"parent-{index}", "center") for index in range(100))

    for method in ("colliders", "v_structures"):
        result = run_nx_tool(
            "tree_dag_analysis",
            graph,
            {"method": method, "limit": 5},
        )

        assert result["success"] is True
        assert result["returned_count"] == 5
        assert len(result["structures"]) == 5
        assert result["truncated"] is True
        if method == "colliders":
            assert result["count"] == 4950
            assert result["count_is_exact"] is True
        else:
            assert result["count"] is None
            assert result["count_is_exact"] is False


def test_every_expanded_operator_has_a_working_smoke_case() -> None:
    dag = nx.DiGraph([("A", "C"), ("B", "C"), ("C", "D")])
    connected = nx.DiGraph()
    for left, right, weight in [
        ("A", "B", 3.0),
        ("B", "C", 2.0),
        ("C", "A", 1.0),
        ("C", "D", 4.0),
    ]:
        connected.add_edge(left, right, weight=weight)
        connected.add_edge(right, left, weight=weight)
    aperiodic = nx.DiGraph([("A", "B"), ("B", "C"), ("C", "A"), ("A", "C")])

    cases: list[tuple[str, nx.DiGraph, dict]] = []
    for method in _spec("graph_structure_analysis")["inputSchema"]["properties"]["method"]["enum"]:
        arguments = {"method": method}
        if method in {"bfs_tree", "dfs_tree"}:
            arguments["source"] = "A"
        cases.append(("graph_structure_analysis", dag, arguments))

    for method in _spec("connectivity_analysis")["inputSchema"]["properties"]["method"]["enum"]:
        cases.append(("connectivity_analysis", connected, {"method": method}))

    for method in _spec("tree_dag_analysis")["inputSchema"]["properties"]["method"]["enum"]:
        graph = dag
        arguments = {"method": method}
        if method == "lowest_common_ancestor":
            arguments.update({"node1": "C", "node2": "D"})
        elif method in {"immediate_dominators", "dominance_frontiers"}:
            arguments["source"] = "A"
        elif method == "is_aperiodic":
            graph = aperiodic
        elif method in {"maximum_spanning_tree", "random_spanning_tree"}:
            graph = connected
        cases.append(("tree_dag_analysis", graph, arguments))

    for method in _spec("matching_coloring_analysis")["inputSchema"]["properties"]["method"]["enum"]:
        arguments = {"method": method}
        if method == "is_matching":
            arguments["edges"] = [["A", "B"], ["C", "D"]]
        elif method == "equitable_color":
            arguments["num_colors"] = 4
        cases.append(("matching_coloring_analysis", connected, arguments))

    for metric in _spec("distance_measures")["inputSchema"]["properties"]["metric"]["enum"]:
        cases.append(("distance_measures", connected, {"metric": metric}))

    for tool_name, graph, arguments in cases:
        result = run_nx_tool(tool_name, graph, arguments)
        assert result.get("success") is True, (tool_name, arguments, result)

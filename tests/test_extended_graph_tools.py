"""Algorithm answers, lossless retrieval and boundaries for the 34 new tools."""
from copy import deepcopy
import json
from pathlib import Path

import networkx as nx
import pytest

from graph_data_service.compute import NX_TOOL_NAMES, run_nx_tool
from graph_data_service.tool_catalog import get_all_public_tools
from graph_engine.extended_tools import (
    EXTENDED_TOOL_NAMES, EXTENDED_TOOLS, EXTENDED_RETRIEVAL_NAMES,
    EXTENDED_THEORY_NAMES, extended_tool_limits,
)
from graph_engine.tool_execution import execution_policy, tools_conflict


def run(name, graph, **arguments):
    result = run_nx_tool(name, graph, arguments)
    assert result["success"], result
    json.dumps(result, allow_nan=False)
    return result


def scores(result):
    return {row["node"]: row["value"] for row in result["items"]}


def cycle():
    return nx.relabel_nodes(nx.cycle_graph(4), {0: "A", 1: "B", 2: "C", 3: "D"})


@pytest.mark.parametrize("name", sorted(EXTENDED_TOOL_NAMES))
def test_every_tool_executes_without_mutating_shared_graph(name):
    graph = cycle()
    args = {}
    if name in {"get_source_nodes", "get_sink_nodes"}:
        graph = nx.DiGraph([("A", "B"), ("B", "C")])
    if name == "get_common_neighbors":
        args = {"source": "A", "target": "C"}
    if name in {"get_node_boundary", "get_edge_boundary"}:
        args = {"nodes": ["A"]}
    for node in graph:
        graph.nodes[node]["properties"] = {"tags": ["keep"]}
    for _, _, data in graph.edges(data=True):
        data["weight"] = 17
        data["properties"] = {"tags": ["keep"]}
    before = deepcopy((dict(graph.nodes(data=True)), list(graph.edges(data=True))))
    nx.freeze(graph)
    run(name, graph, **args)
    assert before == (dict(graph.nodes(data=True)), list(graph.edges(data=True)))


def test_registration_labels_and_readonly_scheduling_cover_all_new_tools():
    catalog = {spec["name"]: spec for spec in get_all_public_tools(include_graph_ml=True, include_reporting=True)}
    labels = json.loads((Path(__file__).resolve().parents[1] / "frontend/src/toolLabels.json").read_text(encoding="utf-8"))
    assert len(EXTENDED_RETRIEVAL_NAMES) == 10
    assert len(EXTENDED_THEORY_NAMES) == 24
    assert len(catalog) >= 111  # Deployments may register additional backend tools.
    assert EXTENDED_TOOL_NAMES <= catalog.keys() & labels.keys() & NX_TOOL_NAMES
    for name in EXTENDED_TOOL_NAMES:
        assert execution_policy(name).read_only
        assert tools_conflict(execution_policy(name), execution_policy("load_graph"))
        assert not tools_conflict(execution_policy(name), execution_policy("get_graph_info"))


def test_retrieval_preserves_parallel_relationship_ids_and_pagination():
    graph = nx.MultiDiGraph()
    graph.add_node("A", node_type="company")
    graph.add_node("B", node_type="person")
    graph.add_node("Z")
    graph.add_edge("A", "B", key=2, id="r1", edge_type="owns")
    graph.add_edge("A", "B", key=5, id="r2", edge_type="owns")
    graph.add_edge("B", "A", key=7, id="r3", edge_type="knows")
    first = run("list_graph_edges", graph, limit=1, edge_type="owns")
    second = run("list_graph_edges", graph, limit=1, offset=first["next_offset"], edge_type="owns")
    assert first["total"] == 2 and first["truncated"]
    assert {first["items"][0]["edge_id"], second["items"][0]["edge_id"]} == {"r1", "r2"}
    assert second["next_offset"] is None
    assert run("list_graph_edges", graph, offset=100)["items"] == []
    assert {r["edge_type"]: r["count"] for r in run("get_edge_type_counts", graph)["items"]} == {"owns": 2, "knows": 1}
    assert run("get_node_type_counts", graph)["entity_count"] == 3
    assert [r["node"] for r in run("get_isolated_nodes", graph)["items"]] == ["Z"]
    assert [r["edge_id"] for r in run("get_edge_boundary", graph, nodes=["A"], direction="in")["items"]] == ["r3"]
    assert [r["node"] for r in run("list_graph_nodes", graph, node_type="person")["items"]] == ["B"]


def test_directional_neighbors_boundaries_sources_and_sinks():
    graph = nx.DiGraph([("S", "A"), ("S", "B"), ("A", "T"), ("B", "T")])
    graph.add_node("isolated")
    for name, expected in [("get_source_nodes", "S"), ("get_sink_nodes", "T")]:
        assert [r["node"] for r in run(name, graph)["items"]] == [expected]
    assert [r["node"] for r in run("get_common_neighbors", graph, source="A", target="B", direction="in")["items"]] == ["S"]
    assert [r["node"] for r in run("get_common_neighbors", graph, source="A", target="B", direction="out")["items"]] == ["T"]
    assert [r["node"] for r in run("get_node_boundary", graph, nodes=["A", "B"], direction="both")["items"]] == ["S", "T"]


def test_triangle_clustering_core_and_onion_answers():
    graph = nx.Graph([("A", "B"), ("B", "C"), ("C", "A"), ("C", "D")])
    triangles = run("triangle_count", graph)
    assert triangles["triangle_count"] == 1
    assert scores(triangles) == {"A": 1, "B": 1, "C": 1, "D": 0}
    page = run("triangle_count", graph, limit=1, offset=3)
    assert scores(page) == {"D": 0} and page["truncated"]
    assert page["count_scope"] == "full_graph"
    assert page["sum_of_node_triangle_counts"] == 3 * page["triangle_count"] == 3
    assert scores(run("node_clustering", graph))["C"] == pytest.approx(1 / 3)
    assert scores(run("core_numbers", graph)) == {"A": 2, "B": 2, "C": 2, "D": 1}
    assert scores(run("onion_layers", graph)) == {"A": 2, "B": 2, "C": 2, "D": 1}
    assert set(scores(run("square_clustering", cycle())).values()) == {1}


def test_structural_holes_vitality_and_finite_json():
    graph = nx.Graph([("A", "B"), ("B", "C")])
    assert scores(run("structural_constraint", graph))["B"] == pytest.approx(0.5)
    assert scores(run("effective_size", graph))["B"] == pytest.approx(2)
    vitality = {r["node"]: r for r in run("closeness_vitality", graph)["items"]}
    assert vitality["B"]["value"] is None
    assert vitality["B"]["nonfinite"] == "negative_infinity"
    graph.add_node("Z")
    assert scores(run("structural_constraint", graph))["Z"] is None
    assert scores(run("effective_size", graph))["Z"] is None
    redundancy = scores(run("bipartite_redundancy", cycle()))
    assert set(redundancy.values()) == {1}
    assert scores(run("bipartite_redundancy", graph))["A"] is None


def test_hits_on_repeated_eigenvalue_and_directed_hub():
    result = run("hits_scores", cycle())
    assert all(row["hub"] == pytest.approx(.25) and row["authority"] == pytest.approx(.25) for row in result["items"])
    graph = nx.DiGraph([("A", "T"), ("B", "T")])
    result = {r["node"]: r for r in run("hits_scores", graph)["items"]}
    assert result["T"]["authority"] == pytest.approx(1)
    assert result["A"]["hub"] == pytest.approx(.5)


def test_cycle_bases_connectivity_and_edge_scores():
    graph = cycle()
    for name in ["cycle_basis", "minimum_cycle_basis"]:
        result = run(name, graph)
        assert result["total"] == result["cycle_rank"] == 1
        assert set(result["items"][0]) == set(graph)
    assert run("average_node_connectivity", graph)["value"] == 2
    assert run("s_metric", graph)["value"] == 16
    assert all(r["value"] == pytest.approx(1 / 3) for r in run("edge_betweenness", graph)["items"])


def test_heuristics_are_valid_sets_without_claiming_optimality():
    graph = nx.relabel_nodes(nx.path_graph(8), str)
    for name in ["dominating_set", "approx_min_dominating_set"]:
        result = run(name, graph)
        assert nx.is_dominating_set(graph, result["items"])
        assert result["optimality"] == "not_guaranteed"
    independent = set(run("maximal_independent_set", graph)["items"])
    assert not graph.subgraph(independent).number_of_edges()
    assert nx.is_dominating_set(graph, independent)
    cover = run("approx_min_vertex_cover", graph)
    assert all(u in cover["items"] or v in cover["items"] for u, v in graph.edges())
    assert cover["approximation_factor"] == 2


def test_eulerian_sequence_preview_and_source_validation():
    graph = nx.Graph([("A", "B"), ("B", "C")])
    assert run("is_eulerian", graph)["value"] is False
    assert run("is_semieulerian", graph)["value"] is True
    assert run("has_eulerian_path", graph, source="A")["value"] is True
    assert run("has_eulerian_path", graph, source="B")["value"] is False
    preview = run("eulerian_path", graph, source="A", limit=1)
    assert preview["items"] == [{"source": "A", "target": "B"}]
    assert preview["total"] == 2 and preview["truncated"]
    assert run("eulerian_path", graph, source="A", offset=1)["items"] == [{"source": "B", "target": "C"}]
    circuit = run("eulerian_circuit", cycle(), source="A")
    assert len(circuit["items"]) == 4
    assert circuit["items"][0]["source"] == circuit["items"][-1]["target"] == "A"


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": "all"},
    {"offset": -1}, {"offset": 20001}, {"unknown": 1}, {"treat_as_undirected": "false"}])
def test_direct_calls_reject_invalid_parameters(args):
    assert run_nx_tool("triangle_count", cycle(), args)["code"] == "INVALID_ARGUMENT"


def test_graph_preconditions_never_silently_change_semantics():
    graph = nx.DiGraph(cycle())
    assert not run_nx_tool("triangle_count", graph, {})["success"]
    assert run("triangle_count", graph, treat_as_undirected=True)["projected_to_undirected"]
    assert run_nx_tool("triangle_count", nx.MultiGraph(cycle()), {})["code"] == "UNSUPPORTED_MULTIGRAPH_OPERATION"
    assert not run_nx_tool("core_numbers", nx.Graph([("A", "A")]), {})["success"]
    assert not run_nx_tool("cycle_basis", nx.Graph(), {})["success"]
    assert not run_nx_tool("get_node_boundary", cycle(), {"nodes": ["missing"]})["success"]
    assert run("list_graph_nodes", nx.Graph())["total"] == 0


def test_resource_limits_apply_before_algorithm_and_response_bytes():
    max_nodes, _ = extended_tool_limits("minimum_cycle_basis")
    assert run_nx_tool("minimum_cycle_basis", nx.empty_graph(max_nodes + 1), {})["code"] == "EXTENDED_TOOL_GRAPH_TOO_LARGE"
    graph = nx.Graph()
    graph.add_node("x" * (129 * 1024))
    assert run_nx_tool("list_graph_nodes", graph, {})["code"] == "RESULT_TOO_LARGE"

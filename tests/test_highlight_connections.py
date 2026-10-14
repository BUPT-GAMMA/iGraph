from copy import deepcopy

import networkx as nx
import pytest

from graph_data_service.compute import GraphCompute
from graph_data_service.graph_data import GraphData


@pytest.mark.parametrize('kind,field', [('paths', 'node_sequence'), ('subgraphs', 'nodes')])
def test_null_highlight_edges_preserve_node_selection(kind, field):
    graph = nx.DiGraph([('a', 'b')])
    highlights = {kind: [{field: ['a', 'b'], 'edges': None}]}
    before = deepcopy(highlights)
    result = GraphCompute().visualize_subgraph(graph, GraphData(), include_community=False, highlights=highlights)
    assert result['success']
    assert result['highlights'][kind][0][field] == ['a', 'b']
    assert result['highlights'][kind][0]['edges'] == ([['a', 'b']] if kind == 'paths' else [])
    assert highlights == before


@pytest.mark.parametrize('kind,field', [('nodes', 'ids'), ('paths', 'node_sequence'), ('subgraphs', 'nodes')])
def test_invalid_highlight_groups_do_not_discard_valid_groups(kind, field):
    graph = nx.DiGraph([('a', 'b')])
    highlights = {kind: [None, 'invalid group', {field: ['a', 'b']}]}
    before = deepcopy(highlights)
    result = GraphCompute().visualize_subgraph(graph, GraphData(), include_community=False, highlights=highlights)
    assert result['success']
    assert len(result['highlights'][kind]) == 1
    assert result['highlights'][kind][0][field] == ['a', 'b']
    assert highlights == before


def ownership_graph():
    graph = nx.MultiDiGraph()
    for node in ["a", "b", "c", "d", "e", "outside"]:
        graph.add_node(node, type="institution_main")
    # A branching SCC with reciprocal edges, a chord, and parallel facts.
    facts = [("ab", "a", "b"), ("ba", "b", "a"), ("bc", "b", "c"),
             ("ca", "c", "a"), ("ab-again", "a", "b"),
             ("de", "d", "e"), ("ed", "e", "d"),
             ("between", "c", "d"), ("external", "outside", "a")]
    for record, holder, target in facts:
        graph.add_node(record, type="institution_shareholder_relationship")
        graph.add_edge(record, holder, id=f"{record}-holder")
        graph.add_edge(record, target, id=f"{record}-target")
    graph.add_node("unrelated", type="loan_record")
    graph.add_edges_from([("unrelated", "a"), ("unrelated", "b")])
    return graph


def test_node_only_scc_groups_include_all_real_internal_fk_edges_and_keep_groups_separate():
    graph = ownership_graph()
    original = deepcopy(graph)
    highlights = {"nodes": [{"ids": ["a", "b", "c"], "color": "#ef4444"},
                            {"ids": ["d", "e"], "color": "#2563eb"}]}
    original_highlights = deepcopy(highlights)
    result = GraphCompute().visualize_subgraph(graph, GraphData(), ["a", "b", "c", "d", "e"], False, highlights)
    expected_records = {"ab", "ba", "bc", "ca", "ab-again", "de", "ed"}
    assert {node["id"] for node in result["nodes"]} == expected_records | {"a", "b", "c", "d", "e"}
    expected_edges = {(source, target) for source, target in graph.edges() if source in expected_records}
    assert {(edge["src"], edge["dst"]) for edge in result["edges"]} == expected_edges
    assert len(result["edges"]) == 14
    assert {tuple(edge) for edge in result["highlights"]["paths"][0]["edges"]} == expected_edges
    assert result["highlights"]["paths"][0]["color"] == "#f97316"
    assert set(result["highlights"]["nodes"][0]["ids"]) == {"a", "b", "c", "ab", "ba", "bc", "ca", "ab-again"}
    assert highlights == original_highlights
    assert nx.utils.graphs_equal(graph, original)


def test_completion_works_in_background_overview_and_node_defined_subgraphs():
    graph = ownership_graph()
    result = GraphCompute().visualize_subgraph(graph, GraphData(), include_community=False,
        highlights={"subgraphs": [{"nodes": ["d", "e"]}]})
    assert {node["id"] for node in result["nodes"]} == set(graph)
    assert set(result["highlights"]["subgraphs"][0]["nodes"]) == {"d", "e", "de", "ed"}
    assert len(result["highlights"]["paths"][0]["edges"]) == 4


def test_edge_only_highlights_survive_normalization_and_keep_direction():
    graph = ownership_graph()
    for kind in ("paths", "subgraphs"):
        result = GraphCompute().visualize_subgraph(graph, GraphData(), include_community=False,
            highlights={kind: [{"edges": [["ab", "a"], {"source": "ab", "target": "b"},
                                         ["a", "ab"], ["missing", "b"]], "color": "#ef4444"}]})
        assert result["highlights"][kind][0]["edges"] == [["ab", "a"], ["ab", "b"]]
        assert result["highlights"][kind][0]["color"] == "#ef4444"


def test_explicit_path_is_not_expanded_to_other_ownership_records():
    graph = ownership_graph()
    result = GraphCompute().visualize_subgraph(graph, GraphData(), ["a", "b", "ab"], False,
        {"nodes": [{"ids": ["a", "b"]}], "paths": [{"edges": [["ab", "a"], ["ab", "b"]]}]})
    assert {node["id"] for node in result["nodes"]} == {"a", "b", "ab"}


def test_highlighted_scc_edges_take_priority_over_background_forest(monkeypatch):
    graph = ownership_graph()
    graph.add_edges_from(("outside", f"background-{i}") for i in range(100))
    compute = GraphCompute()
    monkeypatch.setattr(compute, "_MAX_VIZ_EDGES", 14)
    result = compute.visualize_subgraph(graph, GraphData(), include_community=False,
        highlights={"nodes": [{"ids": ["a", "b", "c"]}, {"ids": ["d", "e"]}]})
    assert result["edges_truncated"] is True
    assert len(result["edges"]) == 14
    assert {edge["src"] for edge in result["edges"]} == {"ab", "ba", "bc", "ca", "ab-again", "de", "ed"}


def test_self_ownership_keeps_both_fk_edge_identities():
    graph = nx.MultiDiGraph()
    graph.add_node("a", type="institution_main")
    graph.add_node("self", type="institution_shareholder_relationship")
    graph.add_edge("self", "a", id="holder")
    graph.add_edge("self", "a", id="target")
    result = GraphCompute().visualize_subgraph(graph, GraphData(), ["a"], False, {"nodes": [{"ids": ["a"]}]})
    assert {node["id"] for node in result["nodes"]} == {"a", "self"}
    assert {edge["id"] for edge in result["edges"]} == {"holder", "target"}

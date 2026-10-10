"""Node and edge inspection share exact graph-local reference resolution."""
import pytest

from graph_data_service.compute import GraphCompute
from graph_data_service.construction import to_graph_data


def graph(nodes=None):
    return to_graph_data({"dataset_id": "reference-test", "revision": 1, "graph": {
        "directed": True,
        "nodes": [{"id": identity, "type": "人物", "properties": properties}
                  for identity, properties in (nodes or [
                      ("hash-x", {"data": {"name": "萧峰"}}),
                      ("hash-q", {"name": "乔峰"}),
                      ("hash-d", {"data": {"name": "段誉"}})])],
        "edges": [{"id": f"edge-{i}", "source": "hash-x", "target": "hash-d",
                   "type": relation, "properties": {"evidence": relation}}
                  for i, relation in enumerate(["结义", "相助"])],
    }})


@pytest.mark.parametrize("reference", ["萧峰", "hash-x", "0"])
def test_node_and_edge_queries_accept_the_same_reference(reference):
    gd = graph()
    compute = GraphCompute()
    node = compute.get_node_info(gd, reference)
    assert node["success"] and node["node"] == "hash-x"
    edge = compute.get_edge_info(gd, reference, "段誉")
    assert edge["success"] and edge["exists"]
    assert (edge["src"], edge["dst"]) == ("hash-x", "hash-d")
    assert edge["parallel_edges"] and edge["edge_count"] == 2
    assert {row["properties"]["evidence"] for row in edge["edges"]} == {"结义", "相助"}
    assert compute.check_edge_exists(gd, reference, "2")["exists"]


def test_directions_and_distinct_alias_nodes_are_not_merged():
    gd = graph()
    compute = GraphCompute()
    assert compute.get_node_info(gd, "乔峰")["node"] == "hash-q"
    assert not compute.get_edge_info(gd, "乔峰", "段誉")["exists"]
    assert not compute.get_edge_info(gd, "段誉", "萧峰")["exists"]
    assert not compute.check_edge_exists(gd, "段誉", "萧峰")["exists"]


def test_ambiguous_names_require_an_explicit_identity():
    gd = graph([("hash-x", {"name": "同名"}), ("hash-q", {"data": {"name": "同名"}}),
                ("hash-d", {"name": "段誉"})])
    compute = GraphCompute()
    for result in [compute.get_node_info(gd, "同名"), compute.get_edge_info(gd, "同名", "段誉"),
                   compute.check_edge_exists(gd, "段誉", "同名")]:
        assert not result["success"] and result["code"] == "AMBIGUOUS_NODE"
        assert result["candidates_total"] == 2
        assert {row["node_name"] for row in result["candidates"]} == {"hash-x", "hash-q"}
    assert compute.get_edge_info(gd, "hash-x", "hash-d")["exists"]


def test_resolution_does_not_leak_between_graphs_or_use_fuzzy_matches():
    compute = GraphCompute()
    first = graph()
    second = graph([("hash-x", {"name": "张三"}), ("hash-q", {"name": "乔峰"}),
                    ("hash-d", {"name": "李四"})])
    assert compute.get_edge_info(first, "萧峰", "段誉")["exists"]
    assert not compute.get_edge_info(second, "萧峰", "段誉")["success"]
    assert compute.get_edge_info(second, "张三", "李四")["exists"]
    assert not compute.get_node_info(first, "萧")["success"]


def test_canonical_ids_take_priority_over_conflicting_display_names():
    gd = graph([("hash-x", {"name": "乔峰"}), ("hash-q", {"name": "hash-x"}),
                ("hash-d", {"name": "段誉"})])
    assert GraphCompute().get_node_info(gd, "hash-x")["node"] == "hash-x"


def test_missing_and_out_of_range_nodes_are_explicit_errors():
    gd = graph()
    compute = GraphCompute()
    assert not compute.get_edge_info(gd, "missing", "段誉")["success"]
    missing = compute.check_edge_exists(gd, "萧峰", "missing")
    assert missing["success"] and not missing["exists"]
    assert not compute.get_node_info(gd, "99999")["success"]

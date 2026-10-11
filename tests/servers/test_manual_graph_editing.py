import copy
import tempfile
from pathlib import Path
import pytest
from graph_data_service.construction import ConstructionError, ConstructionStore, optimize_graph

def graph():
    return {"directed": True, "nodes": [
        {"id": "a", "type": "Person", "properties": {"data": {"name": "无崖子"}}},
        {"id": "b", "type": "Person", "properties": {"data": {"name": "王夫人"}}}],
        "edges": []}

def edge():
    return {"op": "add_edge", "source": "a", "target": "b", "edge_type": "父亲",
            "properties": {"provenance": {"origin": "manual", "evidence": "核实的父女关系"}}}

def test_preview_commit_and_reload_keep_relation_and_evidence():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.txt"
        source.write_text("source")
        store = ConstructionStore(root)
        store.register_algorithm("txt", lambda *_: graph())
        initial = store.build("u", "d", source, "source.txt")
        revision = initial["revision"]
        before = copy.deepcopy(store.graph("u", "d"))
        preview = store.optimize("u", "d", revision, [edge()])
        assert store.graph("u", "d") == before
        committed = store.optimize("u", "d", revision, [edge()], commit=True)
        assert preview["graph"] == committed["graph"]
        loaded = ConstructionStore(root).graph("u", "d")
        assert loaded["graph"]["edges"][0]["properties"] == edge()["properties"]
        assert loaded["revision"] == revision + 1
        with pytest.raises(ConstructionError) as error:
            store.optimize("u", "d", revision, [edge()], commit=True)
        assert error.value.detail["code"] == "revision_conflict"

def test_add_node_has_stable_id_and_can_be_connected_in_same_preview():
    original = graph()
    op = {"op": "add_node", "node_name": "李秋水", "node_type": "人物"}
    added, _ = optimize_graph(original, [op])
    identity = added["nodes"][-1]["id"]
    connected, summary = optimize_graph(original, [op, {**edge(), "source": identity, "edge_type": "母亲"}])
    assert connected["nodes"][-1]["properties"]["data"]["name"] == "李秋水"
    assert connected["edges"][0]["source"] == identity
    assert summary["after"] == {"node_count": 3, "edge_count": 1}
    assert not original["edges"] and len(original["nodes"]) == 2

@pytest.mark.parametrize("op", [
    {**edge(), "target": "missing"}, {**edge(), "edge_type": " "},
    {**edge(), "properties": []}, {**edge(), "properties": {"weight": float("nan")}},
    {"op": "add_node", "node_name": "", "node_type": "person"},
])
def test_invalid_manual_edits_do_not_mutate_graph(op):
    original = graph()
    saved = copy.deepcopy(original)
    with pytest.raises(ConstructionError):
        optimize_graph(original, [op])
    assert original == saved

def test_duplicate_edges_and_size_limits_are_checked():
    original = graph()
    with pytest.raises(ConstructionError):
        optimize_graph(original, [edge(), edge()])
    undirected = {**original, "directed": False}
    with pytest.raises(ConstructionError):
        optimize_graph(undirected, [edge(), {**edge(), "source": "b", "target": "a"}])
    with pytest.raises(ConstructionError):
        optimize_graph(original, [{"op": "add_node", "node_name": "c", "node_type": "Person"}], max_nodes=2)

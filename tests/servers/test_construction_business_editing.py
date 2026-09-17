"""Public editing must understand current SQL/CSV rows and preserve live values."""
import copy

import pytest

from graph_data_service.construction import ConstructionError, ConstructionStore, optimize_graph
from graph_engine.construction.sources import initial_graph, load_source, source_integrity_errors
from graph_engine.construction.workload import evaluate_workload, generate_workload


@pytest.fixture
def built_source(tmp_path):
    path = tmp_path / "customers.sql"
    path.write_text("""
        CREATE TABLE customers(id INTEGER PRIMARY KEY, phone TEXT, name TEXT);
        CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id REFERENCES customers(id));
        INSERT INTO customers VALUES(1,'13800000000','Alice'),(2,'13800000000','Bob');
        INSERT INTO orders VALUES(10,1),(11,2);
    """)
    source = load_source(path)
    try:
        yield path, source, initial_graph(source)
    finally:
        source.close()


@pytest.mark.parametrize("kind", ["extract_property_node", "node_property_to_edge"])
@pytest.mark.parametrize("remove", [False, True])
def test_public_edit_preserves_source_values_queries_and_saved_revision(tmp_path, built_source, kind, remove):
    path, source, graph = built_source
    original = copy.deepcopy(graph)
    customers = [n for n in graph["nodes"] if n["properties"]["table"] == "customers"]
    operation = {"op": kind, "property": "phone", "remove_from_node": remove}
    if kind == "extract_property_node":
        operation.update(node_ids=[n["id"] for n in customers], node_type="Phone", edge_type="has_phone")
    else:
        operation.update(node_id=customers[0]["id"], edge_ids=[graph["edges"][0]["id"]], target_property="customer_phone")
    store = ConstructionStore(tmp_path)
    store.register_algorithm("sql", lambda *_: graph)
    assert store.build("user", "dataset", path, path.name)["revision"] == 1
    preview = store.optimize("user", "dataset", 1, [operation], commit=False)
    assert store.graph("user", "dataset")["graph"] == original
    assert source_integrity_errors(source, preview["graph"]) == []
    evaluation = evaluate_workload(source, preview["graph"], generate_workload(source, 8), repeats=1)
    assert evaluation["correct"] == evaluation["total"] > 0
    saved = store.optimize("user", "dataset", 1, [operation], commit=True)
    assert saved["revision"] == 2
    assert saved["graph"] == preview["graph"]
    assert ConstructionStore(tmp_path).graph("user", "dataset")["graph"] == saved["graph"]
    edited = next(n for n in saved["graph"]["nodes"] if n["id"] == customers[0]["id"])
    assert ("phone" in edited["properties"]["data"]) == (not remove)
    assert graph == original


def test_nested_delete_preserves_metadata_and_literal_column_names(built_source):
    _, _, graph = built_source
    node = graph["nodes"][0]
    node["properties"]["data"].update(table="business table", data={"value": 1}, **{"a.b": None})
    original = copy.deepcopy(graph)
    edited, _ = optimize_graph(graph, [{"op": "delete", "target": "node", "ids": [node["id"]], "property": prop}
                                     for prop in ("table", "data", "a.b")])
    assert edited["nodes"][0]["properties"]["table"] == "customers"
    assert edited["nodes"][0]["properties"]["row_index"] == 0
    assert edited["nodes"][0]["properties"]["data"] == {"id": 1, "phone": "13800000000", "name": "Alice"}
    assert graph == original
    with pytest.raises(ConstructionError):
        optimize_graph(graph, [{"op": "delete", "target": "node", "ids": [node["id"]], "property": "row_index"}])


def test_legacy_data_objects_are_not_mistaken_for_tabular_rows():
    graph = {"directed": True, "nodes": [{"id": "a", "type": "Legacy", "properties": {"data": {"phone": "123"}, "phone": "456"}}], "edges": []}
    edited, _ = optimize_graph(graph, [{"op": "delete", "target": "node", "ids": ["a"], "property": "phone"}])
    assert edited["nodes"][0]["properties"] == {"data": {"phone": "123"}}


def test_relocated_value_is_editable_and_conflicting_destination_is_atomic(built_source):
    _, source, graph = built_source
    node, edge = graph["nodes"][0], graph["edges"][0]
    operation = {"op": "node_property_to_edge", "node_id": node["id"], "edge_ids": [edge["id"]],
                 "property": "phone", "target_property": "contact", "remove_from_node": False}
    moved, _ = optimize_graph(graph, [operation])
    assert source_integrity_errors(source, moved) == []
    deleted, _ = optimize_graph(moved, [{"op": "delete", "target": "edge", "ids": [edge["id"]], "property": "contact"}])
    assert "relocated_attributes" not in deleted["edges"][0]["properties"]
    assert source_integrity_errors(source, deleted) == []
    graph["edges"][0]["properties"]["contact"] = "another number"
    original = copy.deepcopy(graph)
    with pytest.raises(ConstructionError, match="contact"):
        optimize_graph(graph, [operation])
    assert graph == original


@pytest.mark.parametrize("column,value", [("data", {"nested": [None, 1]}), ("table", "customer table"), ("a.b", None)])
def test_extraction_preserves_reserved_and_literal_business_columns(built_source, column, value):
    _, _, graph = built_source
    node = graph["nodes"][0]
    node["properties"]["data"][column] = value
    edited, _ = optimize_graph(graph, [{"op": "extract_property_node", "node_ids": [node["id"]],
        "property": column, "node_type": "BusinessValue", "edge_type": "has_value"}])
    from graph_engine.construction.representation import resolve_rows
    resolved = next(row for row in resolve_rows(edited) if row["id"] == node["id"])
    assert resolved["properties"]["data"][column] == value


def test_nested_and_legacy_extraction_can_share_one_operation(built_source):
    _, source, graph = built_source
    graph["nodes"].append({"id": "legacy", "type": "Customer", "properties": {"phone": "13800000000"}})
    edited, _ = optimize_graph(graph, [{"op": "extract_property_node", "node_ids": [graph["nodes"][0]["id"], "legacy"],
        "property": "phone", "node_type": "Phone", "edge_type": "has_phone"}])
    assert source_integrity_errors(source, edited) == []
    assert next(n for n in edited["nodes"] if n["id"] == "legacy")["properties"] == {}


def test_legacy_empty_relocation_field_is_not_removed_by_an_unrelated_edit():
    graph = {"directed": True, "nodes": [{"id": "a", "type": "Legacy", "properties": {"phone": "123", "relocated_attributes": []}}], "edges": []}
    edited, _ = optimize_graph(graph, [{"op": "delete", "target": "node", "ids": ["a"], "property": "phone"}])
    assert edited["nodes"][0]["properties"] == {"relocated_attributes": []}

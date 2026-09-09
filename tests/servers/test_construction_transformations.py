"""GalaxyWeaver rewrites query current entities and preserve relational truth."""
import copy
import tempfile
import unittest
from pathlib import Path

from graph_engine.construction.representation import RepresentationError, resolve_rows
from graph_engine.construction.sources import (
    ForeignKey, RelationalSource, Table, initial_graph, load_source, source_integrity_errors,
)
from graph_engine.construction.tools import transform
from graph_engine.construction.workload import evaluate_workload


def source_and_graph():
    tables = [
        Table("people", ["id", "name"], [{"id": "p1", "name": "Alice"}], ["id"]),
        Table("products", ["id", "label"], [{"id": "s1", "label": "Book"}], ["id"]),
        Table("orders", ["id", "person", "product", "quantity"],
              [{"id": "o1", "person": "p1", "product": "s1", "quantity": 2}], ["id"],
              [ForeignKey(["person"], "people", ["id"]), ForeignKey(["product"], "products", ["id"])])]
    source = RelationalSource({table.name: table for table in tables})
    return source, initial_graph(source)


def row(graph, table):
    return next(node for node in graph["nodes"] if node["properties"].get("table") == table)


def fk(table, columns, parent, parent_columns):
    return {"table": table, "columns": columns, "ref_table": parent, "ref_columns": parent_columns}


PERSON = fk("orders", ["person"], "people", ["id"])
PRODUCT = fk("orders", ["product"], "products", ["id"])


def join(definition, alias, from_alias="t0", direction="forward", kind="inner"):
    return {"from": from_alias, "alias": alias,
            "table": definition["ref_table"] if direction == "forward" else definition["table"],
            "direction": direction, "kind": kind, "foreign_key": definition}


def workload():
    return [
        {"id": "order_details", "question": "谁购买了哪件商品，各买多少？", "root": {"table": "orders", "alias": "t0"},
         "joins": [join(PERSON, "t1"), join(PRODUCT, "t2")],
         "projection": [{"column": "t0.id", "as": "id"}, {"column": "t1.name", "as": "person"},
                        {"column": "t2.label", "as": "product"}, {"column": "t0.quantity", "as": "quantity"}]},
        {"id": "customer_totals", "question": "每位客户的商品购买总量是多少？", "root": {"table": "people", "alias": "t0"},
         "joins": [join(PERSON, "t1", direction="reverse", kind="left"), join(PRODUCT, "t2", from_alias="t1", kind="left")],
         "projection": [{"column": "t0.name", "as": "name"}], "group_by": ["t0.name"],
         "aggregations": [{"function": "sum", "column": "t1.quantity", "as": "quantity"},
                          {"function": "count", "column": "t2.id", "as": "products"}]},
        {"id": "quantity_total", "question": "所有订单的购买总量是多少？", "root": {"table": "orders", "alias": "t0"},
         "aggregations": [{"function": "sum", "column": "t0.quantity", "as": "quantity"}]},
        {"id": "named_customer", "question": "查询Alice的客户编号。", "root": {"table": "people", "alias": "t0"},
         "filters": [{"column": "t0.name", "op": "eq", "value": "Alice"}],
         "projection": [{"column": "t0.id", "as": "id"}, {"column": "t0.name", "as": "name"}]},
    ]


class BusinessTransformationsTest(unittest.TestCase):
    def setUp(self):
        self.source, self.graph = source_and_graph()
        self.original_graph = copy.deepcopy(self.graph)
        self.original_rows = {name: copy.deepcopy(table.rows) for name, table in self.source.tables.items()}

    def tearDown(self):
        self.assertEqual(self.graph, self.original_graph, "Tool mutated its input graph")
        self.assertEqual({name: table.rows for name, table in self.source.tables.items()}, self.original_rows,
                         "Tool modified the relational truth used by its oracle")

    def apply(self, *operations):
        return transform(self.graph, self.source, "optimize_graph", {"operations": list(operations)})

    def assert_correct(self, graph):
        self.assertEqual(source_integrity_errors(self.source, graph), [])
        report = evaluate_workload(self.source, graph, workload(), repeats=1)
        self.assertEqual(report["accuracy"], 1, report["results"])

    def test_two_outgoing_fk_bridge_collapses_to_live_edge_row(self):
        order = row(self.graph, "orders")
        collapsed = self.apply({"op": "collapse_node", "node_id": order["id"], "edge_type": "purchase"})
        self.assertEqual(len(collapsed["nodes"]), len(self.graph["nodes"]) - 1)
        self.assertEqual(len(collapsed["edges"]), len(self.graph["edges"]) - 1)
        bridge = next(edge for edge in collapsed["edges"] if edge["properties"].get("table") == "orders")
        self.assertEqual(bridge["properties"]["data"]["quantity"], 2)
        self.assertEqual(len(bridge["properties"]["row_endpoints"]), 2)
        self.assertNotIn("_collapsed_node", bridge["properties"])
        self.assert_correct(collapsed)
        bridge["properties"]["data"]["quantity"] = 99
        self.assertTrue(source_integrity_errors(self.source, collapsed))
        self.assertLess(evaluate_workload(self.source, collapsed, workload(), repeats=1)["accuracy"], 1)

    def test_extracted_business_column_is_read_from_live_shared_entity(self):
        person = row(self.graph, "people")
        extracted = self.apply({"op": "extract_property_node", "node_ids": [person["id"]], "property": "name",
                                "node_type": "person_name", "edge_type": "has_name", "remove_from_node": True})
        self.assertNotIn("name", row(extracted, "people")["properties"]["data"])
        entity = next(node for node in extracted["nodes"] if node["type"] == "person_name")
        self.assertEqual(entity["properties"]["value"], "Alice")
        self.assert_correct(extracted)
        entity["properties"]["value"] = "Mallory"
        self.assertTrue(source_integrity_errors(self.source, extracted))
        self.assertLess(evaluate_workload(self.source, extracted, workload(), repeats=1)["accuracy"], 1)

    def test_business_property_moves_to_live_incident_edge(self):
        order = row(self.graph, "orders")
        moved = self.apply({"op": "node_property_to_edge", "node_id": order["id"], "property": "quantity",
                            "edge_ids": [self.graph["edges"][0]["id"]], "target_property": "purchased_units",
                            "remove_from_node": True})
        self.assertNotIn("quantity", row(moved, "orders")["properties"]["data"])
        record = moved["edges"][0]["properties"]["relocated_attributes"][0]
        self.assertEqual(record["name"], "purchased_units")
        self.assertEqual(record["value"], 2)
        self.assert_correct(moved)
        record["value"] = 123
        self.assertTrue(source_integrity_errors(self.source, moved))
        self.assertLess(evaluate_workload(self.source, moved, workload(), repeats=1)["accuracy"], 1)

    def test_failed_operation_batch_has_no_partial_input_mutation(self):
        person = row(self.graph, "people")
        with self.assertRaises(ValueError):
            self.apply({"op": "extract_property_node", "node_ids": [person["id"]], "property": "name",
                        "node_type": "person_name", "edge_type": "has_name"},
                       {"op": "collapse_node", "node_id": "missing", "edge_type": "purchase"})

    def test_independent_operations_compose_without_source_or_snapshot_answers(self):
        person = row(self.graph, "people")
        order = row(self.graph, "orders")
        result = self.apply({"op": "collapse_node", "node_id": order["id"], "edge_type": "purchase"},
                            {"op": "extract_property_node", "node_ids": [person["id"]], "property": "name",
                             "node_type": "person_name", "edge_type": "has_name"})
        self.assert_correct(result)

    def test_relocated_attribute_unknown_fields_are_rejected(self):
        order = row(self.graph, "orders")
        moved = self.apply({"op": "node_property_to_edge", "node_id": order["id"], "property": "quantity",
                            "edge_ids": [self.graph["edges"][0]["id"]]})
        moved["edges"][0]["properties"]["relocated_attributes"][0]["original_value"] = 2
        with self.assertRaises(RepresentationError):
            resolve_rows(moved)

    def test_equal_values_on_multiple_edges_resolve_once_and_conflicts_fail(self):
        order = row(self.graph, "orders")
        moved = self.apply({"op": "node_property_to_edge", "node_id": order["id"], "property": "quantity",
                            "edge_ids": [edge["id"] for edge in self.graph["edges"]]})
        self.assertEqual(len(resolve_rows(moved)), len(self.source.tables))
        self.assert_correct(moved)
        report = evaluate_workload(self.source, moved, workload(), repeats=1)
        total = next(result for result in report["results"] if result["id"] == "quantity_total")
        self.assertEqual(total["actual_preview"], [[2]])
        moved["edges"][1]["properties"]["relocated_attributes"][0]["value"] = 3
        with self.assertRaisesRegex(RepresentationError, "conflicting"):
            resolve_rows(moved)
        self.assertTrue(source_integrity_errors(self.source, moved))

    def test_connect_columns_repairs_using_original_sql_parent_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.sql"
            path.write_text("""
                CREATE TABLE parent(id INTEGER PRIMARY KEY, code TEXT COLLATE NOCASE UNIQUE);
                CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id TEXT REFERENCES parent(id), code TEXT REFERENCES parent(code));
                INSERT INTO parent VALUES(7,'AbC');
                INSERT INTO child VALUES(1,'007','abc');
            """, encoding="utf-8")
            source = load_source(path)
            try:
                baseline = initial_graph(source)
                self.assertEqual(len(baseline["edges"]), 2)
                damaged = copy.deepcopy(baseline)
                damaged["edges"] = []
                for definition in source.tables["child"].foreign_keys:
                    damaged = transform(damaged, source, "connect_columns", {
                        "table": "child", "columns": definition.columns,
                        "ref_table": definition.ref_table, "ref_columns": definition.ref_columns})
                self.assertEqual(len(damaged["edges"]), 2)
                self.assertEqual(source_integrity_errors(source, damaged), [])
                queries = [{"id": "sql_relationships", "question": "找出满足两个键关系的子项。",
                            "root": {"table": "child", "alias": "t0"},
                            "joins": [join(fk("child", ["parent_id"], "parent", ["id"]), "t1"),
                                      join(fk("child", ["code"], "parent", ["code"]), "t2")],
                            "projection": [{"column": "t0.id", "as": "id"}, {"column": "t2.code", "as": "code"}]}]
                report = evaluate_workload(source, damaged, queries, repeats=1)
                self.assertEqual(report["accuracy"], 1, report)
            finally:
                source.close()


if __name__ == "__main__":
    unittest.main()

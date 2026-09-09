"""Logical rows are resolved exclusively from live graph entities."""
import copy
import unittest

from graph_engine.construction.representation import RepresentationError, resolve_rows
from graph_engine.construction.sources import (
    ForeignKey, RelationalSource, Table, initial_graph, source_integrity_errors,
)


def source_and_graph():
    people = Table("people", ["id", "name"], [{"id": "p1", "name": "Alice"}], ["id"],
                   column_types={"id": "TEXT", "name": "TEXT"})
    products = Table("products", ["id", "label"], [{"id": "s1", "label": "Book"}], ["id"],
                     column_types={"id": "TEXT", "label": "TEXT"})
    orders = Table("orders", ["id", "person", "product", "quantity"],
                   [{"id": "o1", "person": "p1", "product": "s1", "quantity": 2}], ["id"],
                   [ForeignKey(["person"], "people", ["id"]), ForeignKey(["product"], "products", ["id"])],
                   column_types={"id": "TEXT", "person": "TEXT", "product": "TEXT", "quantity": "INTEGER"})
    source = RelationalSource({table.name: table for table in (people, products, orders)})
    return source, initial_graph(source)


def row(graph, table):
    return next(entity for entity in graph["nodes"] if entity["properties"].get("table") == table)


def collapsed_order(graph):
    result = copy.deepcopy(graph)
    order = row(result, "orders")
    edges = [edge for edge in result["edges"] if edge["source"] == order["id"]]
    result["nodes"].remove(order)
    result["edges"] = [edge for edge in result["edges"] if edge not in edges]
    result["edges"].append({"id": "order_edge", "type": "purchase", "source": edges[0]["target"],
                            "target": edges[1]["target"], "properties": {
                                **order["properties"], "row_endpoints": [
                                    {"foreign_key": edge["properties"]["foreign_key"], "node_id": edge["target"]}
                                    for edge in edges]}})
    return result


class CurrentRepresentationTest(unittest.TestCase):
    def test_native_rows_are_copied_and_remain_source_faithful(self):
        source, graph = source_and_graph()
        rows = resolve_rows(graph)
        self.assertEqual(len(rows), 3)
        self.assertEqual(source_integrity_errors(source, graph), [])
        rows[0]["properties"]["data"]["name"] = "changed"
        self.assertEqual(row(graph, "people")["properties"]["data"]["name"], "Alice")

    def test_bridge_edge_is_the_current_row_and_endpoints_are_current_fks(self):
        source, graph = source_and_graph()
        collapsed = collapsed_order(graph)
        rows = resolve_rows(collapsed)
        order = next(value for value in rows if value["id"] == "order_edge")
        self.assertIn("source", order)
        self.assertEqual(order["properties"]["data"]["quantity"], 2)
        self.assertEqual(source_integrity_errors(source, collapsed), [])
        collapsed["edges"][-1]["properties"]["data"]["quantity"] = 7
        self.assertEqual(next(value for value in resolve_rows(collapsed) if value["id"] == "order_edge")["properties"]["data"]["quantity"], 7)
        self.assertTrue(any("values changed" in error for error in source_integrity_errors(source, collapsed)))

    def test_old_collapsed_snapshots_cannot_restore_a_missing_row(self):
        source, graph = source_and_graph()
        order = row(graph, "orders")
        snapshot = copy.deepcopy(order)
        graph["nodes"].remove(order)
        graph["edges"] = [{"id": "fake_bridge", "type": "purchase", "source": row(graph, "people")["id"],
                           "target": row(graph, "products")["id"], "properties": {"_collapsed_node": snapshot}}]
        self.assertEqual(len(resolve_rows(graph)), 2)
        self.assertTrue(any("values changed or lost" in error for error in source_integrity_errors(source, graph)))

    def test_endpoint_provenance_must_match_actual_edge_endpoints(self):
        source, graph = source_and_graph()
        graph = collapsed_order(graph)
        graph["nodes"].append({"id": "unrelated", "type": "entity", "properties": {"value": "elsewhere"}})
        graph["edges"][-1]["properties"]["row_endpoints"][0]["node_id"] = "unrelated"
        with self.assertRaisesRegex(RepresentationError, "actual endpoint"):
            resolve_rows(graph)
        self.assertTrue(source_integrity_errors(source, graph))

    def test_endpoint_with_wrong_parent_table_fails_source_audit(self):
        source, graph = source_and_graph()
        graph = collapsed_order(graph)
        edge = graph["edges"][-1]
        edge["properties"]["row_endpoints"][0]["node_id"] = edge["target"]
        self.assertTrue(any("contradicts" in error for error in source_integrity_errors(source, graph)))

    def test_extracted_cells_come_from_current_entity_value(self):
        source, graph = source_and_graph()
        person = row(graph, "people")
        person["properties"]["data"].pop("name")
        graph["nodes"].append({"id": "name_entity", "type": "name", "properties": {"value": "Alice"}})
        graph["edges"].append({"id": "has_name", "type": "name", "source": person["id"], "target": "name_entity",
                               "properties": {"extracted_attribute": {"table": "people", "column": "name"}}})
        self.assertEqual(source_integrity_errors(source, graph), [])
        self.assertNotIn("name", person["properties"]["data"])
        graph["nodes"][-1]["properties"]["value"] = "Bob"
        self.assertEqual(next(item for item in resolve_rows(graph) if item["id"] == person["id"])["properties"]["data"]["name"], "Bob")
        self.assertTrue(any("values changed" in error for error in source_integrity_errors(source, graph)))

    def test_extracted_cell_deleted_value_cannot_fall_back_to_original_data(self):
        source, graph = source_and_graph()
        person = row(graph, "people")
        person["properties"]["_original_data"] = person["properties"]["data"].copy()
        person["properties"]["data"].pop("name")
        graph["nodes"].append({"id": "name_entity", "type": "name", "properties": {"old_value": "Alice"}})
        graph["edges"].append({"id": "has_name", "type": "name", "source": person["id"], "target": "name_entity",
                               "properties": {"extracted_attribute": {"table": "people", "column": "name"}}})
        with self.assertRaisesRegex(RepresentationError, "no value"):
            resolve_rows(graph)
        self.assertTrue(source_integrity_errors(source, graph))

    def test_relocated_cells_come_from_current_incident_edge(self):
        source, graph = source_and_graph()
        order = row(graph, "orders")
        order["properties"]["data"].pop("quantity")
        edge = graph["edges"][0]
        edge["properties"]["relocated_attributes"] = [{"row_id": order["id"], "column": "quantity", "value": 2}]
        self.assertEqual(source_integrity_errors(source, graph), [])
        edge["properties"]["relocated_attributes"][0]["value"] = 9
        self.assertEqual(next(item for item in resolve_rows(graph) if item["id"] == order["id"])["properties"]["data"]["quantity"], 9)
        self.assertTrue(any("values changed" in error for error in source_integrity_errors(source, graph)))

    def test_relocation_cannot_claim_an_unrelated_row(self):
        _, graph = source_and_graph()
        edge = next(edge for edge in graph["edges"] if edge["target"] == row(graph, "people")["id"])
        edge["properties"]["relocated_attributes"] = [{"row_id": row(graph, "products")["id"], "column": "label", "value": "Book"}]
        with self.assertRaisesRegex(RepresentationError, "incident"):
            resolve_rows(graph)

    def test_conflicting_current_storage_and_duplicate_storage_are_rejected(self):
        _, graph = source_and_graph()
        order = row(graph, "orders")
        edge = graph["edges"][0]
        attribute = {"row_id": order["id"], "column": "quantity", "value": 9}
        edge["properties"]["relocated_attributes"] = [attribute]
        with self.assertRaisesRegex(RepresentationError, "conflicting"):
            resolve_rows(graph)
        attribute["value"] = 2
        edge["properties"]["relocated_attributes"].append(attribute.copy())
        with self.assertRaisesRegex(RepresentationError, "duplicate records"):
            resolve_rows(graph)


if __name__ == "__main__":
    unittest.main()

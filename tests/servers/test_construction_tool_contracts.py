"""Tool contracts and atomic composite candidates, using only synthetic data."""
from __future__ import annotations

import copy
import json
import re
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from graph_engine.construction_catalog import _OPERATIONS
from graph_engine.construction.sources import initial_graph, load_source, source_integrity_errors
from graph_engine.construction.tools import SINGLE_TRANSFORM_NAMES, TRANSFORM_NAMES, tool_specs, transform
from graph_engine.construction.workload import evaluate_workload, generate_workload


def check_schema(value, schema):
    """Validate the finite JSON Schema vocabulary used by construction tools.

    This test helper has no runtime dependency and also checks the schemas
    actually passed to the model, including every oneOf branch.
    """
    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                check_schema(value, branch)
            except ValueError:
                pass
            else:
                matches += 1
        if matches != 1:
            raise ValueError("Expected exactly one matching schema branch")
    kind = schema.get("type")
    classes = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}
    if kind and (not isinstance(value, classes[kind]) or kind == "integer" and isinstance(value, bool)):
        raise ValueError("Incorrect type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Unknown enum value")
    if kind == "object":
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys():
            raise ValueError("Missing required field")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ValueError("Unexpected field")
        for key, item in value.items():
            if key in properties:
                check_schema(item, properties[key])
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            raise ValueError("Array length outside bounds")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise ValueError("Repeated array element")
        for item in value:
            check_schema(item, schema.get("items", {}))
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", float("inf")):
            raise ValueError("String length outside bounds")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ValueError("String pattern mismatch")


def tool_schema(name):
    return next(spec["function"]["parameters"] for spec in tool_specs() if spec["function"]["name"] == name)


class ToolSchemaTest(unittest.TestCase):
    def setUp(self):
        self.operations = [
            {"op": "collapse_node", "node_id": "bridge", "edge_type": "purchase"},
            {"op": "node_property_to_edge", "node_id": "row", "property": "amount", "edge_ids": ["edge"]},
            {"op": "extract_property_node", "node_ids": ["row"], "property": "Name", "node_type": "BadgeName", "edge_type": "has_name"},
            {"op": "delete", "target": "node", "ids": ["row"]},
            {"op": "reverse_edges", "edge_ids": ["edge"]},
            {"op": "rename_type", "target": "node", "old_type": "row", "new_type": "record"},
        ]

    def test_each_operation_has_its_own_required_and_allowed_fields(self):
        schema = tool_schema("optimize_graph")
        for operation in self.operations:
            with self.subTest(operation=operation["op"]):
                check_schema({"operations": [operation]}, schema)
                for field in operation:
                    missing = {key: value for key, value in operation.items() if key != field}
                    with self.subTest(missing=field), self.assertRaises(ValueError):
                        check_schema({"operations": [missing]}, schema)
                with self.assertRaises(ValueError):
                    check_schema({"operations": [{**operation, "unexpected": True}]}, schema)
        wrong_branch = {**self.operations[-1], "node_ids": ["row"]}
        with self.assertRaises(ValueError):
            check_schema({"operations": [wrong_branch]}, schema)
        self.assertEqual(set(_OPERATIONS["items"]["properties"]["op"]["enum"]),
                         {operation["op"] for operation in self.operations})

    def test_real_failure_shape_is_rejected_until_node_scope_is_supplied(self):
        operation = {"op": "extract_property_node", "property": "Name", "node_type": "Badges_Name",
                     "edge_type": "has_Name", "remove_from_node": True}
        with self.assertRaises(ValueError):
            check_schema({"operations": [operation]}, tool_schema("optimize_graph"))
        check_schema({"operations": [{**operation, "node_ids": ["badge-1"]}]}, tool_schema("optimize_graph"))

    def test_optional_flags_are_optional_but_have_boolean_types(self):
        for operation, flag in ((self.operations[1], "remove_from_node"),
                                (self.operations[2], "remove_from_node"), (self.operations[3], "cascade")):
            for value in (True, False):
                check_schema([dict(operation, **{flag: value})], _OPERATIONS)
            with self.assertRaises(ValueError):
                check_schema([dict(operation, **{flag: "true"})], _OPERATIONS)

    def test_operation_and_scope_lists_are_nonempty_unique_and_bounded(self):
        for values in ([], [self.operations[-1]] * 101):
            with self.assertRaises(ValueError):
                check_schema(values, _OPERATIONS)
        for values in ([], ["row", "row"], [""], ["   "], ["x" * 1025], [1]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                check_schema([{**self.operations[2], "node_ids": values}], _OPERATIONS)
        properties = _OPERATIONS["items"]["properties"]
        self.assertEqual(properties["node_ids"]["maxItems"], 200_000)
        self.assertEqual(properties["edge_ids"]["maxItems"], 1_000_000)

    def test_batch_step_schema_reuses_each_single_tool_contract(self):
        batch = tool_schema("transform_batch")
        branches = batch["properties"]["steps"]["items"]["oneOf"]
        self.assertEqual({branch["properties"]["tool"]["enum"][0] for branch in branches}, set(SINGLE_TRANSFORM_NAMES))
        self.assertIn("transform_batch", TRANSFORM_NAMES)
        self.assertNotIn("transform_batch", SINGLE_TRANSFORM_NAMES)
        for branch in branches:
            name = branch["properties"]["tool"]["enum"][0]
            self.assertEqual(branch["properties"]["arguments"], tool_schema(name))
        step = {"tool": "optimize_graph", "arguments": {"operations": [self.operations[-1]]}}
        check_schema({"steps": [step] * 6}, batch)
        for invalid in ({"steps": []}, {"steps": [step] * 7},
                        {"steps": [{"tool": "transform_batch", "arguments": {"steps": [step]}}]},
                        {"steps": [{"tool": "inspect_schema", "arguments": {}}]},
                        {"steps": [{"tool": "optimize_graph", "arguments": {"operations": [{"op": "extract_property_node"}]}}]},
                        {"steps": [{**step, "ignored": True}]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                check_schema(invalid, batch)


SQL = """
CREATE TABLE regions(id INTEGER PRIMARY KEY, label TEXT);
CREATE TABLE customers(id INTEGER PRIMARY KEY, region_id INTEGER REFERENCES regions(id), name TEXT);
CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), status TEXT, amount REAL);
INSERT INTO regions VALUES(1,'East'),(2,'West');
INSERT INTO customers VALUES(10,1,'Alice'),(11,2,'Bob');
INSERT INTO orders VALUES(100,10,'paid',12),(101,10,'paid',8),(102,11,'pending',4);
"""


class CompositeTransformTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "source.sql"
        self.path.write_text(SQL, encoding="utf-8")
        self.source = load_source(self.path)
        self.addCleanup(self.source.close)
        self.graph = initial_graph(self.source)
        self.original = copy.deepcopy(self.graph)
        self.queries = generate_workload(self.source, 15)
        customer = {"table": "orders", "columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["id"]}
        region = {"table": "customers", "columns": ["region_id"], "ref_table": "regions", "ref_columns": ["id"]}
        self.forward = [dict(customer, direction="forward"), dict(region, direction="forward")]
        self.reverse = [dict(region, direction="reverse"), dict(customer, direction="reverse")]
        self.entity = {"tool": "generate_entity_table", "arguments": {"table": "orders", "column": "status", "node_type": "OrderStatus"}}
        self.join = {"tool": "materialize_join", "arguments": {"path": self.forward}}

    def tearDown(self):
        self.assertEqual(self.graph, self.original, "A composite candidate mutated its input graph")

    def run_batch(self, steps):
        args = {"steps": steps}
        check_schema(args, tool_schema("transform_batch"))
        return transform(self.graph, self.source, "transform_batch", args)

    def assert_verified(self, graph):
        self.assertEqual(source_integrity_errors(self.source, graph), [])
        evaluation = evaluate_workload(self.source, graph, self.queries, repeats=1)
        self.assertEqual((evaluation["correct"], evaluation["total"]), (15, 15))

    def test_combination_retains_entity_changes_and_both_materialized_paths(self):
        graph = self.run_batch([self.entity, self.join,
                                {"tool": "materialize_join", "arguments": {"path": self.reverse}}])
        self.assertTrue(any(node["type"] == "OrderStatus" for node in graph["nodes"]))
        paths = {json.dumps(edge["properties"]["materialized_join"]["path"], sort_keys=True)
                 for edge in graph["edges"] if "materialized_join" in edge["properties"]}
        self.assertEqual(paths, {json.dumps(path, sort_keys=True) for path in (self.forward, self.reverse)})
        self.assert_verified(graph)

    def test_late_runtime_failure_returns_no_partial_candidate(self):
        original_rows = copy.deepcopy({name: table.rows for name, table in self.source.tables.items()})
        failing = {"tool": "generate_entity_table", "arguments": {"table": "orders", "column": "unknown", "node_type": "Unknown"}}
        with self.assertRaisesRegex(ValueError, "step 2"):
            self.run_batch([self.entity, failing])
        self.assertEqual({name: table.rows for name, table in self.source.tables.items()}, original_rows)
        self.assertEqual(self.graph, self.original)

    def test_reversed_order_is_explicitly_rejected_without_erasing_shortcuts(self):
        with self.assertRaisesRegex(ValueError, "before terminal materialize_join"):
            self.run_batch([self.join, self.entity])

    def test_nested_unknown_empty_and_oversized_batches_are_runtime_errors(self):
        for steps in ([], [self.entity] * 7,
                      [{"tool": "transform_batch", "arguments": {"steps": [self.entity]}}],
                      [{"tool": "inspect_schema", "arguments": {}}],
                      [{"tool": "generate_entity_table", "arguments": []}],
                      [{"tool": "generate_entity_table", "arguments": {}, "extra": True}]):
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                transform(self.graph, self.source, "transform_batch", {"steps": steps})

    def test_extraction_with_explicit_scope_executes_and_preserves_source(self):
        node_ids = [node["id"] for node in self.graph["nodes"] if node["properties"].get("table") == "orders"]
        operation = {"op": "extract_property_node", "property": "status", "node_type": "OrderStatus", "edge_type": "has_status"}
        with self.assertRaisesRegex(ValueError, "node_ids"):
            transform(self.graph, self.source, "optimize_graph", {"operations": [operation]})
        operation["node_ids"] = node_ids
        graph = self.run_batch([{"tool": "optimize_graph", "arguments": {"operations": [operation]}}, self.join])
        self.assertTrue(any("extracted_attribute" in edge["properties"] for edge in graph["edges"]))
        self.assert_verified(graph)

    def test_deadline_exception_is_not_downgraded_to_invalid_candidate(self):
        with patch("graph_engine.construction.tools._budget", side_effect=TimeoutError("budget expired")):
            with self.assertRaises(TimeoutError):
                transform(self.graph, self.source, "transform_batch", {"steps": [self.entity]})


if __name__ == "__main__":
    unittest.main()

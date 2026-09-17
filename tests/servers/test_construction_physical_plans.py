"""Current graph access paths and bounded timing, without wall-clock assertions."""
from __future__ import annotations

import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from graph_engine.construction import workload
from graph_engine.construction.budget import ConstructionBudgetExceeded
from graph_engine.construction.sources import initial_graph, load_source
from graph_engine.construction.tools import transform


class PhysicalAttributeAccessTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "events.sql"
        db = sqlite3.connect(":memory:")
        db.executescript("""
            CREATE TABLE events(id INTEGER PRIMARY KEY, category TEXT COLLATE NOCASE,
                                amount NUMERIC, note TEXT COLLATE RTRIM);
            CREATE TABLE details(id INTEGER PRIMARY KEY, event_id INTEGER REFERENCES events(id), payload TEXT);
        """)
        for index in range(120):
            category = None if index % 10 == 0 else ("Group" if index % 12 < 6 else "group") + str(index % 6)
            db.execute("INSERT INTO events VALUES(?,?,?,?)", (index, category, index % 15,
                                                               "token " if index % 2 else "token"))
            if index % 3 == 0:
                db.executemany("INSERT INTO details VALUES(?,?,?)", [(index * 2, index, "same"), (index * 2 + 1, index, "same")])
        self.path.write_text("\n".join(db.iterdump()), encoding="utf-8")
        db.close()
        self.source = load_source(self.path)
        self.addCleanup(self.source.close)
        self.graph = initial_graph(self.source)

    def query(self, filters=(), **parts):
        query = {"id": "test", "question": "Read current graph records.",
                 "root": {"table": "events", "alias": "t0"},
                 "filters": list(filters), "projection": [{"column": "t0.id", "as": "id"}]}
        query.update(parts)
        return workload.validate_queries(self.source, [query])[0]

    def test_executor_accepts_only_explicit_numeric_resource_limits(self):
        executor = workload.GraphExecutor(self.graph, max_intermediate_rows=7,
                                          query_timeout_seconds=2.5)
        self.assertEqual(executor.max_intermediate_rows, 7)
        self.assertEqual(executor.query_timeout_seconds, 2.5)
        for arguments in ({"max_intermediate_rows": True},
                          {"max_intermediate_rows": object()},
                          {"query_timeout_seconds": "600"},
                          {"query_timeout_seconds": float("nan")}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                workload.GraphExecutor(self.graph, **arguments)
        with self.assertRaises(TypeError):
            workload.GraphExecutor(self.graph, source=self.source)

    def derived(self, column="category", graph=None):
        return transform(graph or self.graph, self.source, "generate_entity_table", {
            "table": "events", "column": column, "node_type": "Value_" + column,
        })

    def evaluate(self, graph, query):
        result = workload.evaluate_workload(self.source, graph, [query], repeats=1, min_query_seconds=0)
        self.assertEqual(result["correct"], 1, result["results"])
        return result["results"][0]

    def test_entity_equality_uses_postings_and_examines_fewer_root_rows(self):
        query = self.query([{"column": "t0.category", "op": "eq", "value": "GROUP1"}])
        baseline = self.evaluate(self.graph, query)
        result = self.evaluate(self.derived(), query)
        self.assertEqual(result["actual_preview"], baseline["actual_preview"])
        self.assertEqual(baseline["source_rows_examined"], 120)
        self.assertEqual(result["source_rows_examined"], 20)
        self.assertEqual(result["attribute_index_hits"], 1)
        self.assertEqual(result["attribute_values_examined"], 1)
        self.assertEqual(result["physical_plan"]["root_access"], "attribute_index")
        self.assertEqual(result["physical_plan"]["attribute_paths"][0]["storage_kinds"], ["entity_column"])

    def test_extracted_live_cells_create_a_real_access_path(self):
        ids = [node["id"] for node in self.graph["nodes"] if node["properties"].get("table") == "events"]
        graph = transform(self.graph, self.source, "optimize_graph", {"operations": [{
            "op": "extract_property_node", "node_ids": ids, "property": "category", "node_type": "Category",
            "edge_type": "has_category", "remove_from_node": True,
        }]})
        self.assertTrue(all("category" not in node["properties"]["data"] for node in graph["nodes"]
                            if node["properties"].get("table") == "events"))
        result = self.evaluate(graph, self.query([{"column": "t0.category", "op": "eq", "value": "group1"}]))
        self.assertEqual(result["source_rows_examined"], 20)
        self.assertEqual(result["physical_plan"]["attribute_paths"][0]["storage_kinds"], ["extracted_attribute"])

    def test_partial_or_stale_entity_paths_cannot_hide_matching_rows(self):
        graph = self.derived()
        links = [edge for edge in graph["edges"] if edge["properties"].get("column") == "category"]
        for corruption in ("missing_link", "stale_value", "wrong_target"):
            with self.subTest(corruption=corruption):
                changed = copy.deepcopy(graph)
                if corruption == "missing_link":
                    changed["edges"] = [edge for edge in changed["edges"] if edge["id"] != links[0]["id"]]
                else:
                    target = next(node for node in changed["nodes"] if node["id"] == links[0]["target"])
                    if corruption == "stale_value":
                        target["properties"]["value"] = "forged"
                    else:
                        target["properties"].pop("value")
                result = self.evaluate(changed, self.query([{"column": "t0.category", "op": "eq", "value": "group1"}]))
                self.assertEqual(result["physical_plan"]["root_access"], "table_scan")
                self.assertEqual(result["source_rows_examined"], 120)

    def test_stale_index_never_repairs_changed_current_data_from_source(self):
        graph = self.derived()
        row = next(node for node in graph["nodes"] if node["properties"].get("table") == "events"
                   and node["properties"]["data"]["category"] is not None)
        row["properties"]["data"]["category"] = "current_changed_value"
        query = self.query([{"column": "t0.category", "op": "eq", "value": "current_changed_value"}])
        result = workload.evaluate_workload(self.source, graph, [query], repeats=1, min_query_seconds=0)["results"][0]
        self.assertFalse(result["correct"])
        self.assertEqual(result["actual_count"], 1)
        self.assertEqual(result["expected_count"], 0)
        self.assertEqual(result["physical_plan"]["root_access"], "table_scan")

    def test_dangling_entity_edge_fails_instead_of_using_stale_postings(self):
        graph = self.derived()
        target = next(edge["target"] for edge in graph["edges"] if edge["properties"].get("column") == "category")
        graph["nodes"] = [node for node in graph["nodes"] if node["id"] != target]
        with self.assertRaises(ValueError):
            workload.GraphExecutor(graph)

    def test_null_in_and_comparison_semantics_match_sqlite(self):
        graph = self.derived()
        predicates = [{"op": "is_null"}, {"op": "not_null"}, {"op": "eq", "value": None},
                      {"op": "in", "value": [None, "GROUP1", "group1"]},
                      *[{"op": op, "value": "GrOuP3"} for op in ("ne", "lt", "le", "gt", "ge")]]
        for predicate in predicates:
            with self.subTest(predicate=predicate):
                result = self.evaluate(graph, self.query([{"column": "t0.category", **predicate}]))
                self.assertEqual(result["attribute_index_hits"], 1)
        nulls = self.evaluate(graph, self.query([{"column": "t0.category", "op": "is_null"}]))
        self.assertEqual(nulls["source_rows_examined"], 12)

    def test_contains_respects_raw_case_despite_nocase_equality_groups(self):
        graph = self.derived()
        for value in ("Group1", "group1", "GROUP1"):
            with self.subTest(value=value):
                query = self.query([{"column": "t0.category", "op": "contains", "value": value}])
                result = self.evaluate(graph, query)
                self.assertEqual(result["attribute_index_hits"], 1)
        self.assertEqual(self.evaluate(graph, self.query([{"column": "t0.category", "op": "contains", "value": "Group1"}]))["actual_count"], 10)

    def test_rtrim_lookup_and_contains_have_distinct_semantics(self):
        graph = self.derived("note")
        eq = self.evaluate(graph, self.query([{"column": "t0.note", "op": "eq", "value": "token   "}]))
        contains = self.evaluate(graph, self.query([{"column": "t0.note", "op": "contains", "value": " "}]))
        self.assertEqual(eq["actual_count"], 120)
        self.assertEqual(contains["actual_count"], 60)

    def test_blob_storage_classes_keep_numbers_and_text_distinct(self):
        path = self.path.parent / "mixed.sql"
        path.write_text("CREATE TABLE mixed(id INTEGER PRIMARY KEY, value BLOB);"
                        "INSERT INTO mixed VALUES(1,NULL),(2,2),(3,'2'),(4,1.5),(5,'a'),(6,''),(7,0);", encoding="utf-8")
        source = load_source(path)
        self.addCleanup(source.close)
        graph = transform(initial_graph(source), source, "generate_entity_table", {
            "table": "mixed", "column": "value", "node_type": "MixedValue",
        })
        for op in ("eq", "ne", "lt", "le", "gt", "ge", "in"):
            with self.subTest(op=op):
                query = {"id": "mixed", "question": "Compare storage classes.", "root": {"table": "mixed", "alias": "t0"},
                         "filters": [{"column": "t0.value", "op": op, "value": [2, None] if op == "in" else 2}],
                         "projection": [{"column": "t0.id", "as": "id"}]}
                result = workload.evaluate_workload(source, graph, [query], repeats=1, min_query_seconds=0)["results"][0]
                self.assertTrue(result["correct"])
                self.assertEqual(result["attribute_index_hits"], 1)
                if op in ("eq", "in"):
                    self.assertEqual(result["actual_count"], 1)

    def test_corrupted_extracted_storage_is_not_replaced_with_source_answers(self):
        ids = [node["id"] for node in self.graph["nodes"] if node["properties"].get("table") == "events"]
        graph = transform(self.graph, self.source, "optimize_graph", {"operations": [{
            "op": "extract_property_node", "node_ids": ids, "property": "category", "node_type": "Category",
            "edge_type": "has_category", "remove_from_node": True,
        }]})
        entity = next(node for node in graph["nodes"] if node["properties"].get("value") == "Group1")
        entity["properties"]["value"] = "current_changed_value"
        query = self.query([{"column": "t0.category", "op": "eq", "value": "current_changed_value"}])
        result = workload.evaluate_workload(self.source, graph, [query], repeats=1, min_query_seconds=0)["results"][0]
        self.assertFalse(result["correct"])
        self.assertEqual(result["actual_count"], 10)
        self.assertEqual(result["expected_count"], 0)

    def test_numeric_affinity_and_duplicate_in_values_use_current_index(self):
        graph = self.derived("amount")
        for predicate in ({"op": "eq", "value": "2"}, {"op": "in", "value": [2, "2", None]},
                          *[{"op": op, "value": "7"} for op in ("ne", "lt", "le", "gt", "ge")]):
            with self.subTest(predicate=predicate):
                result = self.evaluate(graph, self.query([{"column": "t0.amount", **predicate}]))
                self.assertEqual(result["attribute_index_hits"], 1)
        result = self.evaluate(graph, self.query([{"column": "t0.amount", "op": "in", "value": [2, "2"]}]))
        self.assertEqual(result["actual_count"], 8)

    def test_intersection_of_two_current_attribute_paths(self):
        graph = self.derived("amount", self.derived())
        result = self.evaluate(graph, self.query([
            {"column": "t0.category", "op": "eq", "value": "group1"},
            {"column": "t0.amount", "op": "in", "value": [1, 7]},
        ]))
        self.assertEqual(result["attribute_index_hits"], 2)
        self.assertEqual(result["source_rows_examined"], 8)

    def test_left_reverse_join_preserves_duplicate_rows_after_root_lookup(self):
        fk = {"table": "details", "columns": ["event_id"], "ref_table": "events", "ref_columns": ["id"]}
        query = self.query([{"column": "t0.category", "op": "eq", "value": "group3"}],
            joins=[{"from": "t0", "alias": "t1", "table": "details", "direction": "reverse", "kind": "left", "foreign_key": fk}],
            projection=[{"column": "t0.category", "as": "category"}, {"column": "t1.payload", "as": "payload"}])
        result = self.evaluate(self.derived(), query)
        self.assertEqual(result["source_rows_examined"], 20)
        self.assertEqual(result["actual_count"], 40)
        unmatched = copy.deepcopy(query)
        unmatched["filters"][0]["value"] = "group1"
        result = self.evaluate(self.derived(), unmatched)
        self.assertEqual(result["actual_count"], 20)

    def test_joined_alias_predicate_does_not_change_root_access(self):
        graph = transform(self.graph, self.source, "generate_entity_table", {
            "table": "details", "column": "payload", "node_type": "Payload",
        })
        fk = {"table": "details", "columns": ["event_id"], "ref_table": "events", "ref_columns": ["id"]}
        query = self.query([{"column": "t1.payload", "op": "eq", "value": "same"}],
            joins=[{"from": "t0", "alias": "t1", "table": "details", "direction": "reverse", "kind": "left", "foreign_key": fk}])
        result = self.evaluate(graph, query)
        self.assertEqual(result["attribute_index_hits"], 0)
        self.assertEqual(result["actual_count"], 80)

    def test_duplicate_entity_links_do_not_duplicate_logical_rows(self):
        graph = self.derived()
        edge = copy.deepcopy(next(edge for edge in graph["edges"] if edge["properties"].get("column") == "category"))
        edge["id"] += "_duplicate"
        graph["edges"].append(edge)
        result = self.evaluate(graph, self.query([{"column": "t0.category", "op": "eq", "value": "group1"}]))
        self.assertEqual(result["actual_count"], 20)
        self.assertEqual(result["attribute_index_hits"], 1)

    def test_partial_extraction_and_token_entities_fall_back(self):
        identity = next(node["id"] for node in self.graph["nodes"] if node["properties"].get("table") == "events")
        partial = transform(self.graph, self.source, "optimize_graph", {"operations": [{
            "op": "extract_property_node", "node_ids": [identity], "property": "category", "node_type": "Category",
            "edge_type": "has_category", "remove_from_node": True,
        }]})
        result = self.evaluate(partial, self.query([{"column": "t0.category", "op": "eq", "value": "group1"}]))
        self.assertEqual(result["attribute_index_hits"], 0)
        tokens = transform(self.graph, self.source, "unfold_multi_category", {
            "table": "events", "column": "note", "node_type": "NoteToken", "delimiter": "o",
        })
        result = self.evaluate(tokens, self.query([{"column": "t0.note", "op": "eq", "value": "token"}]))
        self.assertEqual(result["attribute_index_hits"], 0)

    def test_report_timing_samples_include_warmup_and_run_counts(self):
        query = self.query(projection=[], aggregations=[{"function": "count", "column": "*", "as": "n"}])
        result = workload.evaluate_workload(self.source, self.graph, [query], repeats=3, min_query_seconds=0)["results"][0]
        self.assertEqual(result["warmup_runs"], 1)
        self.assertEqual(result["timing_iterations"], [1, 1, 1])
        self.assertEqual(result["measured_query_runs"], 3)
        self.assertEqual(len(result["latency_samples_ms"]), 3)

    def test_adaptive_samples_measure_per_execution_cost_with_fake_clock(self):
        query = self.query(projection=[], aggregations=[{"function": "count", "column": "*", "as": "n"}])
        clock = [0.0]

        class TimedExecutor:
            materialized_hits = 0
            last_execution_stats = {}

            def __init__(self, graph, *, deadline=None, max_intermediate_rows=None,
                         query_timeout_seconds=None):
                pass

            def execute(self, query, *, deadline=None):
                clock[0] += 0.0005
                return [[120]]

        with patch.object(workload, "GraphExecutor", TimedExecutor), patch.object(workload.time, "perf_counter", side_effect=lambda: clock[0]):
            result = workload.evaluate_workload(self.source, self.graph, [query], repeats=2, min_query_seconds=0.003)["results"][0]
        self.assertTrue(result["correct"])
        self.assertEqual(len(result["latency_samples_ms"]), 2)
        self.assertTrue(all(6 <= count <= workload.MAX_BENCHMARK_INNER_ITERATIONS for count in result["timing_iterations"]))
        for value in result["latency_samples_ms"]:
            self.assertAlmostEqual(value, 0.5)
        self.assertEqual(result["measured_query_runs"], sum(result["timing_iterations"]))

    def test_clock_resolution_cannot_create_an_unbounded_timing_loop(self):
        query = self.query(projection=[], aggregations=[{"function": "count", "column": "*", "as": "n"}])

        class FastExecutor:
            materialized_hits = 0
            last_execution_stats = {}

            def __init__(self, graph, *, deadline=None, max_intermediate_rows=None,
                         query_timeout_seconds=None):
                pass

            def execute(self, query, *, deadline=None):
                return [[120]]

        with patch.object(workload, "GraphExecutor", FastExecutor), patch.object(workload.time, "perf_counter", return_value=0):
            result = workload.evaluate_workload(self.source, self.graph, [query], repeats=1)["results"][0]
        self.assertEqual(result["timing_iterations"], [workload.MAX_BENCHMARK_INNER_ITERATIONS])

    def test_sampling_keeps_global_budget_checks_and_validates_duration(self):
        query = self.query()
        for value in (True, -1, float("nan"), float("inf"), 1.01):
            with self.subTest(value=value), self.assertRaises(ValueError):
                workload.evaluate_workload(self.source, self.graph, [query], min_query_seconds=value)
        with patch.object(workload.GraphExecutor, "execute", side_effect=ConstructionBudgetExceeded("expired")):
            with self.assertRaises(ConstructionBudgetExceeded):
                workload.evaluate_workload(self.source, self.graph, [query])


if __name__ == "__main__":
    unittest.main()

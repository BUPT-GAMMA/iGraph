from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from graph_data_service.construction import ConstructionStore, validate_graph
from graph_engine.construction import register_builtin_algorithms
from graph_engine.construction.agent import ConstructionFailed, EvolutionConfig, TabularConstructionSubAgent
from graph_engine.construction.sources import initial_graph, load_source
from graph_engine.construction.tools import fidelity_errors, transform
from graph_engine.construction.workload import evaluate_workload, generate_workload


SQL = """
CREATE TABLE regions(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE customers(id INTEGER PRIMARY KEY, region_id INTEGER REFERENCES regions(id), name TEXT);
CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), amount REAL, status TEXT);
INSERT INTO regions VALUES(1,'East'),(2,'North');
INSERT INTO customers VALUES(10,1,'Alice'),(11,1,'Bob'),(12,2,'Carol');
INSERT INTO orders VALUES(100,10,10.5,'paid'),(101,10,20,'paid'),(102,11,NULL,'pending'),(103,12,0,'cancelled'),(104,NULL,4,'pending');
"""


def call(name, args, number=1):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call_{number}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class ScriptedLLM:
    model = "test-double"

    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def complete(self, messages, tools, *, timeout=None):
        self.messages.append(copy.deepcopy(messages))
        return self.responses.pop(0)


class TabularConstructionAgentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "retail.sql"
        self.path.write_text(SQL, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def source(self):
        return load_source(self.path, {"domain": "Retail fulfillment"})

    def script(self, extra=()):
        source = self.source()
        try:
            queries = generate_workload(source, 15)
            edge = initial_graph(source)["edges"][0]
        finally:
            source.close()
        return ScriptedLLM([call("set_workload", {"queries": queries}),
            call("optimize_graph", {"operations": [{"op": "delete", "target": "edge", "ids": [edge["id"]]}]}, 2),
            call("optimize_graph", {"operations": [{"op": "rename_type", "target": "node", "old_type": "orders", "new_type": "Order"}]}, 4),
            *extra, call("finish", {"reason": "Preserve the verified graph after rejected candidate."}, 9)])

    def test_default_15_rules_end_to_end_final_graph_contract(self):
        result = TabularConstructionSubAgent().run(self.path, {"mode": "rules", "domain": "Retail fulfillment"})
        self.assertEqual((result.report["final"]["correct"], result.report["final"]["total"]), (15, 15))
        self.assertFalse(result.report["llm_used"])
        self.assertEqual(set(result.graph), {"directed", "nodes", "edges"})
        self.assertEqual(validate_graph(result.graph), result.graph)
        self.assertTrue(result.report["history"])

    def test_sql_foreign_key_actions_complete_the_full_verified_build(self):
        self.path.write_text(SQL.replace("REFERENCES regions(id)", "REFERENCES regions(id) ON UPDATE CASCADE")
                            .replace("REFERENCES customers(id)", "REFERENCES customers(id) ON UPDATE SET NULL ON DELETE CASCADE"), encoding="utf-8")
        result = TabularConstructionSubAgent().run(self.path, {"mode": "rules", "query_count": 7, "experience_enabled": False})
        self.assertEqual((result.report["final"]["correct"], result.report["final"]["total"]), (7, 7))
        source = self.source()
        try:
            self.assertEqual(fidelity_errors(source, result.graph), [])
        finally:
            source.close()

    def test_llm_tool_loop_freezes_queries_rejects_loss_and_returns_only_graph(self):
        llm = self.script()
        result = TabularConstructionSubAgent(llm_factory=lambda _: llm).run(self.path, {"domain": "retail"})
        self.assertTrue(result.report["llm_used"])
        self.assertEqual(result.report["workload_origin"], "llm")
        self.assertEqual(result.report["final"]["accuracy"], 1)
        event = result.report["history"][0]
        self.assertFalse(event["accepted"])
        self.assertEqual(event["reason"], "source_fidelity_failed")
        self.assertEqual(len(result.graph["edges"]), 7)
        self.assertIn("source_fidelity_failed", json.dumps(llm.messages[-1]))

    def test_invalid_workload_tool_feedback_allows_repair_without_fake_oracle(self):
        llm = self.script()
        llm.responses.insert(0, call("set_workload", {"queries": [{"id": "bad", "question": "fake", "root": {"table": "invented", "alias": "t0"}}]}))
        result = TabularConstructionSubAgent(llm_factory=lambda _: llm).run(self.path)
        self.assertEqual(result.report["final"]["correct"], 15)
        self.assertIn("Expected 15 queries", json.dumps(llm.messages[1]))
        self.assertIn("Expected 15 queries", result.report["workload_validation_errors"][0]["error"])

    def test_workload_cannot_be_replaced_after_freeze(self):
        llm = self.script(extra=[call("set_workload", {"queries": []}, 3)])
        result = TabularConstructionSubAgent(llm_factory=lambda _: llm).run(self.path)
        self.assertEqual(len(result.report["queries"]), 15)
        self.assertIn("workload is immutable", json.dumps(llm.messages[-1]))

    def test_source_fidelity_checks_unqueried_records(self):
        source = self.source()
        try:
            graph = initial_graph(source)
            graph["nodes"][0]["properties"]["data"]["name"] = "wrong"
            self.assertTrue(fidelity_errors(source, graph))
        finally:
            source.close()

    def test_verified_faster_candidate_is_retained_after_a_later_rejection(self):
        source = self.source()
        queries = generate_workload(source)
        edge_id = initial_graph(source)["edges"][0]["id"]
        source.close()
        llm = ScriptedLLM([
            call("set_workload", {"queries": queries}),
            call("optimize_graph", {"operations": [{"op": "rename_type", "target": "node", "old_type": "orders", "new_type": "Order"}]}),
            call("optimize_graph", {"operations": [{"op": "delete", "target": "edge", "ids": [edge_id]}]}),
            call("finish", {"reason": "retain verified candidate"}),
        ])

        def timed_oracle(source, graph, queries, repeats=3):
            evaluation = evaluate_workload(source, graph, queries, repeats=1)
            # Deterministic timing fixture tests winner publication/rollback;
            # correctness still comes from real graph/source execution.
            evaluation["latency_ms"] = 1 if any(n["type"] == "Order" for n in graph["nodes"]) else 10
            evaluation["preparation_ms"] = 0
            return evaluation

        result = TabularConstructionSubAgent(llm_factory=lambda _: llm, evaluator=timed_oracle).run(self.path)
        self.assertEqual(result.report["accepted_candidates"], 1)
        self.assertTrue(any(n["type"] == "Order" for n in result.graph["nodes"]))
        self.assertEqual(len(result.graph["edges"]), 7)
        self.assertFalse(result.report["history"][1]["accepted"])
        self.assertEqual(result.report["final"]["correct"], 15)

    def test_final_validation_cannot_be_skipped_by_finish(self):
        calls = 0

        def evaluator(source, graph, queries, repeats=3):
            nonlocal calls
            calls += 1
            result = evaluate_workload(source, graph, queries, repeats=1)
            if calls >= 4:
                result.update(correct=14, accuracy=14 / 15)
            return result

        report_path = self.root / "private-report.json"
        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: self.script(), evaluator=evaluator).run(self.path, report_path=report_path)
        self.assertEqual(caught.exception.code, "query_accuracy_target_not_met")
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["status"], "failed")

    def test_invalid_candidates_do_not_count_as_actual_evolution(self):
        source = self.source()
        queries = generate_workload(source)
        source.close()
        llm = ScriptedLLM([call("set_workload", {"queries": queries}),
                           call("materialize_join", {"path": []}), call("finish", {"reason": "done"})])
        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: llm).run(self.path, {"max_iterations": 2})
        self.assertEqual(caught.exception.code, "no_evolution_candidate_evaluated")

    def test_tool_budget_includes_workload_generation(self):
        llm = self.script()
        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: llm).run(self.path, {"max_tool_calls": 1})
        self.assertEqual(caught.exception.code, "tool_call_budget_exhausted")

    def test_llm_cannot_replace_multitable_workload_with_only_counts(self):
        source = self.source()
        simple = [q for q in generate_workload(source, 100) if not q["joins"]][:15]
        source.close()
        llm = ScriptedLLM([call("set_workload", {"queries": simple})])
        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: llm).run(self.path, {"workload_attempts": 1})
        self.assertEqual(caught.exception.code, "workload_generation_failed")

    def test_missing_llm_credentials_fail_instead_of_claiming_rules_are_llm(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent().run(self.path)
        self.assertIn("construction_llm_unavailable", caught.exception.report["error"])

    def test_parameter_bounds_reject_booleans_nan_and_large_workloads(self):
        for options in ({"query_count": True}, {"query_count": 101}, {"max_iterations": 0},
                        {"max_seconds": float("nan")}, {"mode": "auto"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                EvolutionConfig.from_options(options)

    def test_query_count_is_configurable(self):
        result = TabularConstructionSubAgent().run(self.path, {"mode": "rules", "query_count": 7})
        self.assertEqual(result.report["final"]["total"], 7)

    def test_registration_store_publication_failure_and_restart(self):
        store = register_builtin_algorithms(ConstructionStore(self.root))
        self.assertEqual([x["algorithm_available"] for x in store.capabilities()["formats"]], [True, True, False])
        result = store.build("u1", "d1", self.path, "retail.sql", {"mode": "rules", "query_count": 7})
        self.assertEqual(result["status"], "ready")
        saved = store.graph("u1", "d1")
        self.assertEqual(ConstructionStore(self.root).graph("u1", "d1"), saved)
        self.assertNotIn("queries", json.dumps(saved))
        self.assertTrue((self.root / "users/u1/datasets/d1/construction/evolution-report.json").exists())
        # Existing ready graph is reused even when options change.
        self.assertTrue(store.build("u1", "d1", options={"query_count": 99})["reused"])
        with patch.dict("os.environ", {}, clear=True):
            failed = store.build("u1", "d2", self.path, "retail.sql")
        self.assertEqual((failed["status"], failed["revision"]), ("failed", 0))

    def test_uploaded_csv_uses_business_filename_instead_of_retention_hash(self):
        csv = self.root / "customers.csv"
        csv.write_text("customer_id,name\nc1,Alice\nc2,Bob\n", encoding="utf-8")
        store = register_builtin_algorithms(ConstructionStore(self.root))
        result = store.build("u1", "csv1", csv, csv.name, {"mode": "rules", "query_count": 7})
        self.assertEqual(result["status"], "ready")
        graph = store.graph("u1", "csv1")["graph"]
        self.assertEqual({n["type"] for n in graph["nodes"]}, {"customers"})
        report = json.loads((self.root / "users/u1/datasets/csv1/construction/evolution-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["source_schema"]["tables"][0]["primary_key"], ["customer_id"])

    def test_invocation_state_is_isolated_under_parallel_builds(self):
        agent = TabularConstructionSubAgent()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = list(pool.map(lambda count: agent.run(self.path, {"mode": "rules", "query_count": count}), (7, 15)))
        self.assertEqual([a.report["final"]["total"], b.report["final"]["total"]], [7, 15])
        self.assertNotEqual(a.report["workload_sha256"], b.report["workload_sha256"])

    def test_build_does_not_block_status_or_main_agent_dataset_listing(self):
        store = ConstructionStore(self.root)
        entered, release = threading.Event(), threading.Event()

        def algorithm(path, options):
            entered.set()
            release.wait(5)
            source = load_source(path, options)
            try:
                return initial_graph(source)
            finally:
                source.close()

        store.register_algorithm("sql", algorithm)
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(store.build, "u1", "d1", self.path, "retail.sql")
            try:
                self.assertTrue(entered.wait(2))
                state = pool.submit(store.status, "u1", "d1").result(timeout=1)
                self.assertEqual(state["status"], "building")
                self.assertEqual(pool.submit(store.list_ready, "u1").result(timeout=1), [])
            finally:
                release.set()
            self.assertEqual(running.result(timeout=2)["status"], "ready")

    def test_specialized_tools_materialize_real_paths_and_preserve_source(self):
        source = self.source()
        try:
            graph = initial_graph(source)
            queries = generate_workload(source)
            query = next(q for q in queries if len(q["joins"]) >= 2)
            path = [{**j["foreign_key"], "direction": j["direction"]} for j in query["joins"]]
            optimized = transform(graph, source, "materialize_join", {"path": path})
            self.assertGreater(len(optimized["edges"]), len(graph["edges"]))
            evaluation = evaluate_workload(source, optimized, queries, repeats=1)
            self.assertEqual(evaluation["correct"], 15)
            self.assertTrue(any(r["materialized_path_hits"] for r in evaluation["results"]))
            self.assertFalse(fidelity_errors(source, optimized))
        finally:
            source.close()

    def test_autog_tools_do_not_overwrite_source_values(self):
        source = self.source()
        try:
            graph = initial_graph(source)
            for name, arguments in (("generate_entity_table", {"table": "orders", "column": "status", "node_type": "OrderStatus"}),
                                    ("unfold_multi_category", {"table": "orders", "column": "status", "node_type": "Category"}),
                                    ("add_primary_key", {"table": "orders", "columns": ["id"]}),
                                    ("remove_primary_key", {"table": "orders"})):
                with self.subTest(name=name):
                    candidate = transform(graph, source, name, arguments)
                    self.assertFalse(fidelity_errors(source, candidate))
                    self.assertEqual(evaluate_workload(source, candidate, generate_workload(source, 7), repeats=1)["accuracy"], 1)
        finally:
            source.close()


if __name__ == "__main__":
    unittest.main()

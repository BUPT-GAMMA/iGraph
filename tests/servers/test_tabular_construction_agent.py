from __future__ import annotations

import copy
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from graph_data_service.construction import ConstructionStore, validate_graph
from graph_engine.construction import register_builtin_algorithms
from graph_engine.construction.agent import ConstructionFailed, EvolutionConfig, QUERY_DSL, TabularConstructionSubAgent
from graph_engine.construction.sources import initial_graph, load_source
from graph_engine.construction.tools import fidelity_errors, transform
from graph_engine.construction.workload import evaluate_workload, generate_workload, validate_queries


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

    def test_rules_publish_only_pk_fk_without_optimizer_model_or_experience(self):
        progress_path = self.root / "ready-progress.json"
        source = self.source()
        try:
            expected = initial_graph(source)
        finally:
            source.close()
        unused = Mock(side_effect=AssertionError("Rules mode must not optimize"))
        memory = Mock()
        with patch("graph_engine.construction.agent.generate_workload", unused), \
                patch("graph_engine.construction.agent.transform", unused), \
                patch("graph_engine.construction.agent.domain_profile", unused):
            result = TabularConstructionSubAgent(llm_factory=unused, evaluator=unused, experience_store=memory).run(
                self.path, {"mode": "rules", "domain": "Retail fulfillment", "experience_enabled": True},
                progress_path=progress_path, build_id="ready-build")
        unused.assert_not_called()
        self.assertEqual(memory.mock_calls, [])
        self.assertEqual(result.graph, expected)
        self.assertTrue(result.report["final"]["valid"])
        self.assertTrue(result.report["final"]["source_fidelity"])
        self.assertFalse(result.report["llm_used"])
        self.assertEqual(set(result.graph), {"directed", "nodes", "edges"})
        self.assertEqual(validate_graph(result.graph), result.graph)
        self.assertEqual(result.report["history"], [])
        self.assertEqual(result.report["stop_reason"], "pk_fk_complete")
        self.assertEqual(result.report["initial_size"], result.report["final_size"])
        self.assertEqual(result.report["experience"]["recorded_observations"], 0)
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        self.assertEqual(progress["mode"], "rules")
        self.assertNotIn("query_count", progress["metrics"])
        self.assertFalse(any("Agent" in event["message"] or event["kind"] == "candidate" for event in progress["events"]))
        self.assertEqual((progress["status"], progress["stage"], progress["current_message"]),
                         ("ready", "ready", "PK/FK 构图完成，已通过结构与源数据一致性校验。"))

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

    def test_aggregate_alias_error_explains_repair_without_relaxing_validation(self):
        llm = self.script()
        args = json.loads(llm.responses[0]["tool_calls"][0]["function"]["arguments"])
        invalid = copy.deepcopy(args["queries"])
        invalid[0]["projection"] = [{"column": "cnt", "as": "copied_count"}]
        llm.responses.insert(0, call("set_workload", {"queries": invalid}, 0))

        result = TabularConstructionSubAgent(llm_factory=lambda _: llm).run(self.path)

        feedback = json.loads(llm.messages[1][-1]["content"])
        self.assertFalse(feedback["success"])
        self.assertIn("Invalid column reference: 'cnt'", feedback["error"])
        self.assertIn("reference it only in order_by", feedback["repair_hint"])
        self.assertIn("There is no HAVING support", feedback["repair_hint"])
        self.assertEqual(result.report["workload_origin"], "llm")
        self.assertTrue(result.report["llm_used"])
        self.assertEqual(result.report["final"]["accuracy"], 1)
        self.assertEqual(result.report["queries"], args["queries"])

    def test_join_direction_error_explains_repair_without_changing_frozen_workload(self):
        llm = self.script()
        args = json.loads(llm.responses[0]["tool_calls"][0]["function"]["arguments"])
        invalid = copy.deepcopy(args["queries"])
        join = next(query["joins"][0] for query in invalid if query["joins"])
        join["direction"] = "reverse" if join["direction"] == "forward" else "forward"
        llm.responses.insert(0, call("set_workload", {"queries": invalid}, 0))

        result = TabularConstructionSubAgent(llm_factory=lambda _: llm).run(self.path)

        feedback = json.loads(llm.messages[1][-1]["content"])
        self.assertFalse(feedback["success"])
        self.assertEqual(feedback["error"], "Join tables disagree with foreign-key direction")
        self.assertIn("forward: child -> parent", feedback["repair_hint"])
        self.assertIn("reverse: parent -> child", feedback["repair_hint"])
        self.assertIn("Do not swap these fields", feedback["repair_hint"])
        self.assertIn("validated seed plans unchanged", feedback["repair_hint"])
        self.assertEqual(result.report["workload_origin"], "llm")
        self.assertEqual(result.report["queries"], args["queries"])
        self.assertEqual(result.report["final"]["accuracy"], 1)

    def test_documented_forward_reverse_joins_validate_and_preserve_key_orientation(self):
        source = self.source()
        key = {"table": "orders", "columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["id"]}
        queries = []
        try:
            for direction, root, target in (("forward", "orders", "customers"), ("reverse", "customers", "orders")):
                query = {"id": direction, "question": f"Traverse declared relationship {direction}",
                         "root": {"table": root, "alias": "t0"},
                         "joins": [{"from": "t0", "alias": "t1", "table": target, "direction": direction,
                                    "kind": "inner", "foreign_key": copy.deepcopy(key)}],
                         "projection": [{"column": "t0.id", "as": "source_id"}, {"column": "t1.id", "as": "target_id"}]}
                queries.append(query)
                self.assertIn(f"table:'{target}',direction:'{direction}'", QUERY_DSL)
                invalid = copy.deepcopy(query)
                invalid["joins"][0]["direction"] = "reverse" if direction == "forward" else "forward"
                with self.assertRaisesRegex(ValueError, "Join tables disagree with foreign-key direction"):
                    validate_queries(source, [invalid])
            validated = validate_queries(source, queries)
            self.assertEqual(validated[0]["joins"][0]["foreign_key"], validated[1]["joins"][0]["foreign_key"])
            self.assertEqual(evaluate_workload(source, initial_graph(source), validated, repeats=1)["accuracy"], 1)
        finally:
            source.close()

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
        report_path = self.root / "private-report.json"
        progress_path = self.root / "failed-progress.json"

        def evaluator(source, graph, queries, repeats=3):
            result = evaluate_workload(source, graph, queries, repeats=1)
            # Fail the actual final-validation phase, independently of how many
            # repeated candidate comparisons preceded it.
            state = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
            if state.get("stage") == "validating_final":
                result.update(correct=14, accuracy=14 / 15)
            return result

        with self.assertRaises(ConstructionFailed) as caught:
            TabularConstructionSubAgent(llm_factory=lambda _: self.script(), evaluator=evaluator).run(
                self.path, report_path=report_path, progress_path=progress_path, build_id="failed-build")
        self.assertEqual(caught.exception.code, "query_accuracy_target_not_met")
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["status"], "failed")
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        self.assertEqual((progress["status"], progress["stage"], progress["current_message"]),
                         ("failed", "failed", "构图未完成，源文件已保留，可调整参数后重试。"))
        self.assertNotIn(caught.exception.code, json.dumps(progress, ensure_ascii=False))

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
        source = self.source()
        try:
            queries = generate_workload(source, 7)
        finally:
            source.close()
        llm = self.script()
        llm.responses[0] = call("set_workload", {"queries": queries})
        result = TabularConstructionSubAgent(llm_factory=lambda _: llm).run(self.path, {"query_count": 7})
        self.assertEqual(result.report["final"]["total"], 7)

    def test_rules_cli_reports_structural_validation_without_query_benchmarks(self):
        from graph_engine.construction.__main__ import main
        output = self.root / "cli-result"
        stdout = io.StringIO()
        with patch("sys.argv", ["construction", str(self.path), "--mode", "rules", "--output", str(output)]), redirect_stdout(stdout):
            self.assertEqual(main(), 0)
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["status"], "ready")
        self.assertTrue(summary["validation"]["source_fidelity"])
        self.assertNotIn("correct", summary)
        self.assertTrue((output / "graph.json").exists())
        self.assertFalse((self.root / ".construction-experience").exists())

    def test_registration_store_publication_failure_and_restart(self):
        store = register_builtin_algorithms(ConstructionStore(self.root))
        self.assertEqual([x["algorithm_available"] for x in store.capabilities()["formats"]], [True, True, True])
        result = store.build("u1", "d1", self.path, "retail.sql", {"mode": "rules", "query_count": 7})
        self.assertEqual(result["status"], "draft")
        # Builds stay drafts until the explicit save publishes them for analysis.
        self.assertEqual(store.optimize("u1", "d1", result["revision"], [], commit=True)["status"], "ready")
        saved = store.graph("u1", "d1")
        self.assertEqual(ConstructionStore(self.root).graph("u1", "d1"), saved)
        self.assertNotIn("queries", json.dumps(saved))
        self.assertTrue((self.root / "users/u1/datasets/d1/construction/evolution-report.json").exists())
        self.assertFalse((self.root / "users/u1/construction-experience").exists())
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
        self.assertEqual(result["status"], "draft")
        graph = store.graph("u1", "csv1")["graph"]
        self.assertEqual({n["type"] for n in graph["nodes"]}, {"customers"})
        report = json.loads((self.root / "users/u1/datasets/csv1/construction/evolution-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["source_schema"]["tables"][0]["primary_key"], ["customer_id"])

    def test_uploaded_txt_uses_business_filename_and_builtin_worker(self):
        txt = self.root / "customers.txt"
        txt.write_text("customer_id\tname\nc1\tAlice\nc2\tBob\n", encoding="utf-8")
        store = register_builtin_algorithms(ConstructionStore(self.root))
        self.assertIn("txt", store._process_algorithms)
        result = store.build("u1", "txt1", txt, txt.name, {"mode": "rules", "query_count": 7})
        self.assertEqual(result["status"], "draft")
        self.assertEqual((result["source_type"], result["algorithm_available"]), ("txt", True))
        graph = store.graph("u1", "txt1")["graph"]
        self.assertEqual({node["type"] for node in graph["nodes"]}, {"customers"})
        self.assertEqual(graph["nodes"][0]["properties"]["data"]["name"], "Alice")
        report = json.loads((self.root / "users/u1/datasets/txt1/construction/evolution-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["source_schema"]["tables"][0]["primary_key"], ["customer_id"])

    def test_invocation_state_is_isolated_under_parallel_builds(self):
        agent = TabularConstructionSubAgent()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = list(pool.map(lambda count: agent.run(self.path, {"mode": "rules", "query_count": count}), (7, 15)))
        self.assertEqual([a.report["config"]["query_count"], b.report["config"]["query_count"]], [7, 15])
        self.assertEqual(a.graph, b.graph)
        a.graph["nodes"].clear()
        self.assertTrue(b.graph["nodes"])

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
            self.assertEqual(running.result(timeout=2)["status"], "draft")

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

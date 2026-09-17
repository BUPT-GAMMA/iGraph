"""Deterministic search acceptance checks; these are not speedup measurements."""
import unittest

from graph_engine.construction.agent import EvolutionConfig, TabularConstructionSubAgent, _paired_comparison, _prioritize_workload, _candidate_examples
from graph_engine.construction.sources import load_source, initial_graph
from graph_engine.construction.workload import generate_workload, evaluate_workload
import json
import tempfile
from pathlib import Path


def evaluation(cost, correct=1):
    return {"correct": correct, "total": 1, "latency_ms": cost, "preparation_ms": 0,
            "results": [{"id": "q", "latency_ms": cost, "correct": bool(correct)}]}


class SearchAcceptanceTest(unittest.TestCase):
    def test_consistent_gain_passes_in_both_orders(self):
        order = []
        def evaluate(graph, queries, repeats):
            order.append(graph)
            return evaluation(5 if graph == "candidate" else 10)
        _, _, result = _paired_comparison(evaluate, "baseline", "candidate", [{}], EvolutionConfig())
        self.assertEqual(order, ["candidate", "baseline", "baseline", "candidate", "candidate", "baseline"])
        self.assertTrue(result["accepted"])
        self.assertTrue(result["both_orders_improve"])

    def test_execution_order_bias_is_not_a_schema_gain(self):
        calls = []
        def evaluate(graph, queries, repeats):
            calls.append(graph)
            return evaluation(5 if len(calls) % 2 else 10)
        _, _, result = _paired_comparison(evaluate, "baseline", "candidate", [{}], EvolutionConfig())
        self.assertGreater(result["median_relative_gain"], 0.05)
        self.assertFalse(result["both_orders_improve"])
        self.assertFalse(result["accepted"])

    def test_one_wrong_round_rejects_fast_candidate(self):
        candidate_runs = []
        def evaluate(graph, queries, repeats):
            if graph == "candidate":
                candidate_runs.append(True)
                return evaluation(1, 0 if len(candidate_runs) == 2 else 1)
            return evaluation(10)
        _, _, result = _paired_comparison(evaluate, "baseline", "candidate", [{}], EvolutionConfig())
        self.assertFalse(result["all_queries_correct"])
        self.assertFalse(result["accepted"])

    def test_hotspot_priority_uses_measured_coverage(self):
        ranked = _prioritize_workload([
            {"tool": "materialize_join", "query_ids": ["cold"], "priority": 0.4},
            {"tool": "generate_entity_table", "query_ids": ["hot"], "priority": 0.0},
        ], {"results": [{"id": "hot", "latency_ms": 9}, {"id": "cold", "latency_ms": 1}]})
        self.assertEqual(ranked[0]["tool"], "generate_entity_table")
        self.assertEqual(ranked[0]["measured_query_cost_share"], 0.9)

    def test_config_rejects_single_order_comparison(self):
        with self.assertRaises(ValueError):
            EvolutionConfig.from_options({"comparison_rounds": 1})
        with self.assertRaises(ValueError):
            EvolutionConfig.from_options({"min_query_seconds": -1})

    def test_complementary_paths_and_filters_have_batch_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.sql"
            path.write_text("CREATE TABLE r(id INTEGER PRIMARY KEY, name TEXT);"
                            "CREATE TABLE c(id INTEGER PRIMARY KEY, rid INTEGER REFERENCES r(id), status TEXT);"
                            "CREATE TABLE o(id INTEGER PRIMARY KEY, cid INTEGER REFERENCES c(id));"
                            "INSERT INTO r VALUES(1,'east');INSERT INTO c VALUES(1,1,'paid');INSERT INTO o VALUES(1,1);")
            source = load_source(path)
            try:
                queries = generate_workload(source, 15)
                proposals = _candidate_examples(source, initial_graph(source), queries)
                self.assertTrue(any(p["tool"] == "generate_entity_table" for p in proposals))
                batch = next(p for p in proposals if p["tool"] == "transform_batch")
                self.assertGreaterEqual(len(batch["arguments"]["steps"]), 2)
                self.assertLessEqual(len(batch["arguments"]["steps"]), 6)
            finally:
                source.close()

    def test_transient_incumbent_failure_cannot_admit_a_slower_next_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.sql"
            path.write_text("CREATE TABLE orders(id INTEGER PRIMARY KEY, status TEXT);"
                            "INSERT INTO orders VALUES(1,'paid'),(2,'pending');")
            source = load_source(path)
            try:
                queries = generate_workload(source, 7)
            finally:
                source.close()
            def call(name, args):
                return {"role": "assistant", "tool_calls": [{"id": name, "function": {"name": name, "arguments": json.dumps(args)}}]}
            responses = iter([
                call("optimize_graph", {"operations": [{"op": "rename_type", "target": "node", "old_type": "orders", "new_type": "SlowOne"}]}),
                call("optimize_graph", {"operations": [{"op": "rename_type", "target": "node", "old_type": "orders", "new_type": "SlowTwo"}]}),
                call("finish", {"reason": "Keep verified baseline"}),
            ])
            class Planner:
                def complete(self, *args, **kwargs):
                    return next(responses)
            baseline_runs = 0
            def measured(source, graph, queries, repeats):
                nonlocal baseline_runs
                baseline = all(n["type"] == "orders" for n in graph["nodes"])
                result = evaluate_workload(source, graph, queries, repeats=1, min_query_seconds=0)
                result.update(latency_ms=10 if baseline else 12, preparation_ms=0)
                if baseline:
                    baseline_runs += 1
                    if baseline_runs == 4:
                        result.update(correct=6, accuracy=6 / 7)
                return result
            result = TabularConstructionSubAgent(llm_factory=lambda _: Planner(), evaluator=measured).run(
                path, {"queries": queries, "query_count": 7, "experience_enabled": False})
            self.assertEqual(result.report["accepted_candidates"], 0)
            self.assertEqual(result.report["history"][0]["reason"], "comparison_query_failure")
            self.assertEqual(result.report["history"][1]["reason"], "no_measured_improvement")
            self.assertTrue(all(n["type"] == "orders" for n in result.graph["nodes"]))

    def test_explicit_failed_measurement_does_not_turn_slow_change_into_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.sql"
            path.write_text("CREATE TABLE orders(id INTEGER PRIMARY KEY, status TEXT);"
                            "INSERT INTO orders VALUES(1,'paid'),(2,'pending');")
            source = load_source(path)
            try:
                queries = generate_workload(source, 7)
            finally:
                source.close()

            def call(name, args):
                return {"role": "assistant", "tool_calls": [{"id": name, "function": {"name": name, "arguments": json.dumps(args)}}]}

            responses = iter([
                call("evaluate_queries", {}),
                call("optimize_graph", {"operations": [{"op": "rename_type", "target": "node", "old_type": "orders", "new_type": "Slower"}]}),
                call("finish", {"reason": "Keep verified baseline"}),
            ])
            feedback = []

            class Planner:
                def complete(self, messages, *args, **kwargs):
                    feedback.extend(json.loads(message["content"]) for message in messages
                                    if message.get("role") == "tool" and message.get("tool_call_id") == "evaluate_queries")
                    return next(responses)

            baseline_runs = 0

            def measured(source, graph, queries, repeats):
                nonlocal baseline_runs
                baseline = all(n["type"] == "orders" for n in graph["nodes"])
                result = evaluate_workload(source, graph, queries, repeats=1, min_query_seconds=0)
                result.update(latency_ms=10 if baseline else 12, preparation_ms=0)
                if baseline:
                    baseline_runs += 1
                    # The first baseline is correct. Only the explicit
                    # diagnostic measurement reports a simulated failure.
                    if baseline_runs == 2:
                        result.update(correct=6, accuracy=6 / 7)
                return result

            result = TabularConstructionSubAgent(llm_factory=lambda _: Planner(), evaluator=measured).run(
                path, {"queries": queries, "query_count": 7, "experience_enabled": False})
            self.assertEqual(result.report["accepted_candidates"], 0)
            self.assertEqual(result.report["history"][0]["reason"], "no_measured_improvement")
            self.assertTrue(result.report["history"][0]["comparison"]["all_queries_correct"])
            self.assertTrue(all(n["type"] == "orders" for n in result.graph["nodes"]))
            self.assertTrue(any(item["correct"] == 6 and item["quality_preserved"] for item in feedback))

    def test_retaining_failed_initial_quality_still_allows_real_correctness_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rows.sql"
            path.write_text("CREATE TABLE orders(id INTEGER PRIMARY KEY, status TEXT);"
                            "INSERT INTO orders VALUES(1,'paid'),(2,'pending');")
            source = load_source(path)
            try:
                queries = generate_workload(source, 7)
            finally:
                source.close()

            def call(name, args):
                return {"role": "assistant", "tool_calls": [{"id": name, "function": {"name": name, "arguments": json.dumps(args)}}]}

            responses = iter([
                call("evaluate_queries", {}),
                call("optimize_graph", {"operations": [{"op": "rename_type", "target": "node", "old_type": "orders", "new_type": "Correct"}]}),
                call("finish", {"reason": "Keep corrected graph"}),
            ])

            class Planner:
                def complete(self, *args, **kwargs):
                    return next(responses)

            def measured(source, graph, queries, repeats):
                baseline = all(n["type"] == "orders" for n in graph["nodes"])
                result = evaluate_workload(source, graph, queries, repeats=1, min_query_seconds=0)
                result.update(latency_ms=10 if baseline else 12, preparation_ms=0)
                if baseline:
                    result.update(correct=6, accuracy=6 / 7)
                return result

            result = TabularConstructionSubAgent(llm_factory=lambda _: Planner(), evaluator=measured).run(
                path, {"queries": queries, "query_count": 7, "experience_enabled": False})
            self.assertEqual(result.report["accepted_candidates"], 1)
            self.assertEqual(result.report["history"][0]["reason"], "correctness_improved")
            self.assertTrue(all(n["type"] == "Correct" for n in result.graph["nodes"]))


if __name__ == "__main__":
    unittest.main()

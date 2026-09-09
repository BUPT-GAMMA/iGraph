"""Cross-build strategy transfer; timing fixtures are not performance claims."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from graph_data_service.construction import ConstructionStore
from graph_engine.construction import register_builtin_algorithms
from graph_engine.construction.agent import ConstructionFailed, EvolutionConfig, TabularConstructionSubAgent
from graph_engine.construction.experience import DomainExperienceStore
from graph_engine.construction.workload import evaluate_workload
from tests.servers.test_tabular_construction_agent import SQL, call


class CurrentSchemaPlanner:
    model = "test-double"

    def __init__(self):
        self.messages = []
        self.proposal = None

    def complete(self, messages, tools, *, timeout=None):
        self.messages.append(copy.deepcopy(messages))
        task = json.loads(next(m["content"] for m in messages if m["role"] == "user"))
        if "seed_plans" in task:
            return call("set_workload", {"queries": task["seed_plans"]})
        if self.proposal is None:
            self.proposal = next(x for x in task["candidate_examples"] if x["tool"] == "materialize_join")
            return call(self.proposal["tool"], self.proposal["arguments"], 2)
        return call("finish", {"reason": "Current data was independently evaluated"}, 3)


def controlled_cost(source, graph, queries, repeats=3):
    result = evaluate_workload(source, graph, queries, repeats=1)
    # Exact source/graph correctness is real. Controlled costs let this test
    # check policy transfer without pretending microbenchmark timing is stable.
    has_shortcut = any("materialized_join" in edge["properties"] for edge in graph["edges"])
    result.update(latency_ms=1.0 if has_shortcut else 10.0, preparation_ms=0.0)
    return result


class DomainLearningIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "first.sql"
        self.source.write_text(SQL, encoding="utf-8")
        self.store_path = self.root / "private-experience" / "strategies.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def run_build(self, path=None, *, category="finance", enabled=True, evaluator=controlled_cost, store=None):
        planner = CurrentSchemaPlanner()
        agent = TabularConstructionSubAgent(llm_factory=lambda _: planner, evaluator=evaluator,
            experience_store=store if store is not None else DomainExperienceStore(self.store_path))
        return agent.run(path or self.source, {"domain_category": category, "experience_enabled": enabled}), planner

    def test_new_build_retrieves_abstract_strategy_and_binds_current_schema(self):
        first, _ = self.run_build()
        self.assertEqual(first.report["experience"]["status"], "active")
        self.assertEqual(first.report["accepted_candidates"], 1)
        self.assertEqual(first.report["experience"]["retrieved"], [])
        second_source = self.root / "heldout.sql"
        renamed = SQL.replace("orders", "payment_events").replace("customers", "client_accounts").replace("regions", "zone_branches").replace("Alice", "PrivateHoldoutValue")
        second_source.write_text(renamed, encoding="utf-8")
        second, planner = self.run_build(second_source)
        evidence = second.report["experience"]["retrieved"]
        self.assertTrue(evidence)
        self.assertEqual(evidence[0]["independent_sources"], 1)
        self.assertTrue(planner.proposal["learned_evidence"])
        self.assertNotIn("learned_strategies", json.dumps(planner.messages[0]))
        self.assertNotIn("orders", json.dumps(planner.proposal["arguments"]))
        self.assertEqual(second.report["final"]["correct"], 15)
        self.assertEqual(set(second.graph), {"directed", "nodes", "edges"})
        self.assertEqual(second.report["experience"]["policy_after"][0]["independent_sources"], 2)
        # Even a literal SQL-string cell, old table name, or source pathname must
        # never appear in durable strategy records or SQLite pages.
        for path in self.store_path.parent.glob("*"):
            data = path.read_bytes()
            for secret in (b"PrivateHoldoutValue", b"Alice", b"orders", b"CREATE TABLE client_accounts"):
                self.assertFalse(secret in data, "Historical source content leaked into strategy storage")

    def test_finance_evidence_is_not_retrieved_for_academic_domain(self):
        self.run_build(category="finance")
        academic, _ = self.run_build(category="academic")
        self.assertEqual(academic.report["experience"]["retrieved"], [])
        self.assertEqual(academic.report["domain_profile"]["key"], "academic")

    def test_reformatted_same_logical_source_does_not_inflate_source_count(self):
        self.run_build()
        renamed_file = self.root / "same-data-new-file.sql"
        renamed_file.write_text("-- A different filename and dump formatting\n" + SQL.replace(";", ";\n"), encoding="utf-8")
        repeated, _ = self.run_build(renamed_file)
        self.assertEqual(repeated.report["experience"]["policy_after"][0]["independent_sources"], 1)

    def test_historical_success_does_not_override_current_failed_validation(self):
        self.run_build()
        novel = self.root / "novel.sql"
        novel.write_text(SQL.replace("Alice", "New input"), encoding="utf-8")

        def candidate_regression(source, graph, queries, repeats=3):
            result = controlled_cost(source, graph, queries, repeats)
            if any("materialized_join" in e["properties"] for e in graph["edges"]):
                result.update(correct=14, accuracy=14 / 15)
            return result

        result, _ = self.run_build(novel, evaluator=candidate_regression)
        self.assertTrue(result.report["experience"]["retrieved"])
        self.assertEqual(result.report["accepted_candidates"], 0)
        self.assertEqual(result.report["final"]["correct"], 15)
        self.assertFalse(any("materialized_join" in e["properties"] for e in result.graph["edges"]))
        after = result.report["experience"]["policy_after"][0]
        self.assertEqual(after["unsafe"], 1)
        self.assertLess(after["policy_score"], result.report["experience"]["retrieved"][0]["policy_score"])

    def test_final_failure_does_not_commit_positive_strategy_evidence(self):
        calls = 0

        def final_fails(source, graph, queries, repeats=3):
            nonlocal calls
            calls += 1
            result = controlled_cost(source, graph, queries, repeats)
            if calls >= 4:
                result.update(correct=14, accuracy=14 / 15)
            return result

        with self.assertRaises(ConstructionFailed) as caught:
            self.run_build(evaluator=final_fails)
        report = caught.exception.report
        self.assertEqual(report["failure_code"], "query_accuracy_target_not_met")
        self.assertEqual(DomainExperienceStore(self.store_path).retrieve("finance", report["experience"]["context"]), [])

    def test_disable_means_neither_read_nor_write_nor_directory_creation(self):
        store = Mock()
        result, _ = self.run_build(enabled=False, store=store)
        store.retrieve.assert_not_called()
        store.record_run.assert_not_called()
        self.assertEqual(result.report["experience"]["status"], "disabled")
        self.assertFalse(self.store_path.parent.exists())

    def test_experience_failure_is_reported_without_losing_verified_graph(self):
        store = Mock()
        store.retrieve.side_effect = OSError("private path must not enter report")
        result, _ = self.run_build(store=store)
        self.assertEqual(result.report["experience"]["status"], "unavailable")
        self.assertNotIn("private path", json.dumps(result.report))
        self.assertEqual(result.report["final"]["correct"], 15)
        store.record_run.assert_not_called()

    def test_busy_write_does_not_claim_experience_was_recorded(self):
        store = Mock()
        store.retrieve.return_value = []
        store.record_run.return_value = {"stored": False, "duplicate": False, "status": "busy", "evidence_count": 0}
        result, _ = self.run_build(store=store)
        self.assertEqual(result.report["experience"]["status"], "write_failed")
        self.assertEqual(result.report["experience"]["recorded_observations"], 0)
        self.assertEqual(result.report["final"]["correct"], 15)

    def test_registered_workers_derive_user_scope_and_ignore_option_paths(self):
        store = register_builtin_algorithms(ConstructionStore(self.root))
        options = {"mode": "rules", "domain_category": "finance", "experience_path": str(self.root / "forged"), "user_id": "bob"}
        for user, dataset in (("alice", "first"), ("alice", "second"), ("bob", "first")):
            result = store.build(user, dataset, self.source, "source.sql", options)
            self.assertEqual(result["status"], "ready")
        def report(user, dataset):
            return json.loads((self.root / "users" / user / "datasets" / dataset / "construction/evolution-report.json").read_text(encoding="utf-8"))
        self.assertTrue(report("alice", "second")["experience"]["retrieved"])
        self.assertEqual(report("bob", "first")["experience"]["retrieved"], [])
        self.assertFalse((self.root / "forged").exists())
        self.assertTrue((self.root / "users/alice/construction-experience/strategies.sqlite3").exists())
        self.assertTrue((self.root / "users/bob/construction-experience/strategies.sqlite3").exists())

    def test_new_options_are_typed_and_bounded(self):
        for options in ({"experience_enabled": "false"}, {"experience_enabled": 0}, {"domain_category": "../../other"}, {"experience_top_k": 0}, {"experience_top_k": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                EvolutionConfig.from_options(options)


if __name__ == "__main__":
    unittest.main()

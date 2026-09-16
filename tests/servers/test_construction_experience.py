"""Domain evidence is private, independent, bounded and able to change its mind."""
import copy
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from graph_engine.construction.experience import DomainExperienceStore, ExperienceError


CONTEXT = {"source_kind": "sqlite", "table_count": 4, "row_count": 100,
           "column_count": 20, "fk_count": 3, "query_count": 15, "max_join_hops": 3,
           "has_composite_keys": False, "has_nullable_fks": True,
           "query_families": ["lookup", "multi_hop", "aggregation"],
           "query_mix": {"lookup": 3, "multi_hop": 8, "aggregation": 4}}
PATTERN = {"join_hops": 2, "fk_arity": 1, "affected_table_count": 3,
           "relationship_shape": "chain", "fanout_bucket": "few",
           "row_count_bucket": "small", "query_family": "multi_hop"}


def observation(strategy="materialize_join", outcome="accepted", gain=0.3, **changes):
    result = {"strategy_id": strategy, "pattern": copy.deepcopy(PATTERN), "outcome": outcome,
              "relative_gain": gain, "query_correct": 15, "query_total": 15}
    result.update(changes)
    return result


class DomainExperienceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "private" / "experience.sqlite"
        self.store = DomainExperienceStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, run="run-1", source="source-1", domain="commerce", observations=None, **kwargs):
        return self.store.record_run(run, source, domain, copy.deepcopy(CONTEXT),
                                     observations if observations is not None else [observation()],
                                     final_correct=kwargs.get("final_correct", True))

    def test_constructor_is_lazy_and_empty_retrieval_is_private(self):
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.store.retrieve("commerce", CONTEXT), [])
        self.assertTrue(self.path.is_file())

    def test_cross_run_restart_retains_supported_independent_evidence(self):
        for index in range(3):
            self.assertTrue(self.record(f"run-{index}", f"source-{index}")["stored"])
        before = self.store.retrieve("commerce", CONTEXT)
        after = DomainExperienceStore(self.path).retrieve("commerce", CONTEXT)
        self.assertEqual(before, after)
        self.assertEqual(len(after), 1)
        summary = after[0]
        self.assertEqual((summary["independent_sources"], summary["trials"], summary["accepted"]), (3, 3, 3))
        self.assertEqual(summary["confidence_status"], "supported")
        self.assertEqual(summary["pattern"], PATTERN)
        self.assertEqual(summary["mean_gain"], 0.3)
        self.assertEqual(summary["context_similarity"], 1.0)
        self.assertGreater(summary["policy_score"], 0)

    def test_domains_and_user_database_paths_are_isolated(self):
        self.record()
        self.assertEqual(self.store.retrieve("finance", CONTEXT), [])
        other_user = DomainExperienceStore(Path(self.temp.name) / "another-user.sqlite")
        self.assertEqual(other_user.retrieve("commerce", CONTEXT), [])
        self.record("finance-run", "finance-source", "finance", [observation(gain=0.8)])
        self.assertEqual(self.store.retrieve("finance", CONTEXT)[0]["mean_gain"], 0.8)
        self.assertEqual(self.store.retrieve("commerce", CONTEXT)[0]["mean_gain"], 0.3)

    def test_retry_is_idempotent_and_same_source_cannot_manufacture_independence(self):
        self.assertTrue(self.record()["stored"])
        self.assertTrue(self.record()["duplicate"])
        for index in range(8):
            self.record(f"retry-tuning-{index}", "source-1", observations=[observation(gain=0.1 + index / 100)])
        summary = self.store.retrieve("commerce", CONTEXT)[0]
        self.assertEqual((summary["independent_sources"], summary["trials"], summary["accepted"]), (1, 1, 1))
        self.assertEqual(summary["mean_gain"], 0.17)
        self.assertEqual(summary["confidence_status"], "candidate")
        # Even retrying the original older run cannot overwrite newer evidence.
        self.assertTrue(self.record()["duplicate"])
        self.assertEqual(self.store.retrieve("commerce", CONTEXT)[0]["mean_gain"], 0.17)
        with self.assertRaises(ExperienceError):
            self.record(observations=[observation(gain=0.9)])

    def test_last_observation_in_a_run_is_the_single_source_evidence(self):
        result = self.record(observations=[observation(gain=0.4), observation(outcome="no_gain", gain=0)])
        self.assertEqual(result["evidence_count"], 1)
        summary = self.store.retrieve("commerce", CONTEXT)[0]
        self.assertEqual((summary["trials"], summary["accepted"], summary["no_gain"]), (1, 0, 1))

    def test_negative_feedback_reverses_an_old_supported_strategy_ranking(self):
        for index in range(3):
            self.record(f"good-{index}", f"data-{index}", observations=[
                observation(gain=0.5), observation("collapse_node", gain=0.2)])
        initial = self.store.retrieve("commerce", CONTEXT)
        self.assertEqual(initial[0]["strategy_id"], "materialize_join")
        self.assertEqual(initial[0]["confidence_status"], "supported")
        for index in range(3):
            self.record(f"unsafe-{index}", f"data-{index}", observations=[
                observation(outcome="unsafe", gain=-0.2, query_correct=10)])
        updated = self.store.retrieve("commerce", CONTEXT)
        self.assertEqual(updated[0]["strategy_id"], "collapse_node")
        unsafe = next(item for item in updated if item["strategy_id"] == "materialize_join")
        self.assertEqual((unsafe["accepted"], unsafe["unsafe"], unsafe["trials"]), (0, 3, 3))
        self.assertEqual(unsafe["confidence_status"], "cautioned")
        self.assertLess(unsafe["policy_score"], 0)

    def test_a_new_unsafe_or_no_gain_source_cannot_amplify_a_prior_success(self):
        for outcome in ("unsafe", "no_gain"):
            with self.subTest(outcome=outcome):
                domain = "feedback-" + outcome
                self.record("warm-" + outcome, "successful-source", domain, [observation(gain=0.9)])
                before = self.store.retrieve(domain, CONTEXT)[0]
                self.record("negative-" + outcome, "independent-failed-source", domain, [
                    observation(outcome=outcome, gain=0.02, query_correct=0 if outcome == "unsafe" else 15)])
                after = self.store.retrieve(domain, CONTEXT)[0]
                self.assertEqual((before["independent_sources"], after["independent_sources"]), (1, 2))
                self.assertLess(after["policy_score"], before["policy_score"])
                self.assertEqual(after[outcome], 1)
                self.assertEqual(after["mean_gain"], 0.9 if outcome == "unsafe" else 0.46)

    def test_small_measured_gain_below_acceptance_threshold_is_preserved_but_penalized(self):
        self.record()
        initial = self.store.retrieve("commerce", CONTEXT)[0]["policy_score"]
        self.record("no-gain", observations=[observation(outcome="no_gain", gain=0.02)])
        summary = self.store.retrieve("commerce", CONTEXT)[0]
        self.assertEqual(summary["mean_gain"], 0.02)
        self.assertEqual(summary["gain_samples"], 1)
        self.assertLess(summary["policy_score"], initial)
        self.assertEqual(summary["confidence_status"], "cautioned")

    def test_failed_final_run_cannot_create_positive_evidence(self):
        result = self.record(final_correct=False)
        self.assertEqual(result["evidence_count"], 0)
        self.assertEqual(self.store.retrieve("commerce", CONTEXT), [])
        self.record("failed-unsafe", observations=[observation(outcome="unsafe", query_correct=0)], final_correct=False)
        summary = self.store.retrieve("commerce", CONTEXT)[0]
        self.assertEqual((summary["accepted"], summary["unsafe"]), (0, 1))
        self.assertLess(summary["policy_score"], 0)
        self.assertEqual((summary["mean_gain"], summary["gain_samples"]), (0, 0))

    def test_unsafe_results_are_excluded_from_mean_gain_measurements(self):
        self.record(observations=[observation(gain=0.4)])
        self.record("unsafe-run", "unsafe-source", observations=[observation(outcome="unsafe", gain=-0.8, query_correct=1)])
        summary = self.store.retrieve("commerce", CONTEXT)[0]
        self.assertEqual((summary["trials"], summary["accepted"], summary["unsafe"]), (2, 1, 1))
        self.assertEqual((summary["mean_gain"], summary["gain_samples"]), (0.4, 1))

    def test_incorrect_or_unexecuted_queries_never_support_an_accepted_claim(self):
        for index, counts in enumerate([(14, 15), (0, 0)]):
            self.record(f"bad-query-{index}", f"source-{index}", observations=[
                observation(query_correct=counts[0], query_total=counts[1])])
        summary = self.store.retrieve("commerce", CONTEXT)[0]
        self.assertEqual((summary["accepted"], summary["unsafe"]), (0, 2))
        self.assertNotEqual(summary["confidence_status"], "supported")

    def test_context_similarity_filters_irrelevant_queries_and_source_semantics(self):
        self.record()
        self.assertEqual(self.store.retrieve("commerce", {**CONTEXT, "source_kind": "csv"}), [])
        self.assertEqual(self.store.retrieve("commerce", {**CONTEXT, "max_join_hops": 0}), [])
        self.assertEqual(self.store.retrieve("commerce", {**CONTEXT, "query_families": ["lookup"], "query_mix": {"lookup": 15}}), [])
        nearby = self.store.retrieve("commerce", {**CONTEXT, "row_count": 500})
        self.assertTrue(nearby)
        self.assertLess(nearby[0]["context_similarity"], 1)

    def test_parallel_writes_are_atomic_and_parallel_same_run_is_idempotent(self):
        self.store.retrieve("commerce", CONTEXT)  # Initialize before write contention.
        stores = [self.store, DomainExperienceStore(self.path), DomainExperienceStore(self.path)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda index: stores[index % len(stores)].record_run(
                f"parallel-{index}", f"source-{index}", "commerce", copy.deepcopy(CONTEXT), [observation()],
                final_correct=True), range(12)))
        self.assertTrue(all(result["stored"] for result in results), results)
        self.assertEqual(self.store.retrieve("commerce", CONTEXT)[0]["independent_sources"], 12)
        with ThreadPoolExecutor(max_workers=4) as pool:
            retries = list(pool.map(lambda _: self.record("same-run", "same-source"), range(4)))
        self.assertEqual(sum(result["stored"] for result in retries), 1)
        self.assertEqual(sum(result["duplicate"] for result in retries), 3)
        self.assertEqual(self.store.retrieve("commerce", CONTEXT)[0]["independent_sources"], 13)

    def test_contention_is_short_and_failure_does_not_discard_prior_evidence(self):
        self.record()
        short = DomainExperienceStore(self.path, timeout_seconds=0.03)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            result = short.record_run("busy", "other", "commerce", CONTEXT, [observation()], final_correct=True)
            self.assertFalse(result["stored"])
            self.assertEqual(result["status"], "busy")
            self.assertLess(time.monotonic() - started, 0.5)
            lock.rollback()
        self.assertEqual(self.store.retrieve("commerce", CONTEXT)[0]["independent_sources"], 1)
        self.assertTrue(short.record_run("busy", "other", "commerce", CONTEXT, [observation()], final_correct=True)["stored"])

    def test_database_never_contains_raw_keys_or_unapproved_source_text(self):
        secrets = ["private-domain-customer-medical", "run-user-secret", "source-internal-record-123"]
        self.record(secrets[1], secrets[2], secrets[0])
        with closing(sqlite3.connect(self.path)) as db:
            dump = "\n".join(db.iterdump())
        for secret in secrets:
            self.assertNotIn(secret, dump)
        self.assertNotIn('"context"', dump)
        self.assertIn("source_hash", dump)
        self.assertIn("materialize_join", dump)
        summary = json.dumps(self.store.retrieve(secrets[0], CONTEXT))
        self.assertTrue(all(secret not in summary for secret in secrets))

    def test_sensitive_fields_and_bad_types_are_rejected_without_partial_recording(self):
        invalid = [
            {**observation(), "arguments": {"table": "patient_records"}},
            {**observation(), "prompts": "ignore prior rules"},
            observation(strategy="arbitrary instruction"),
            observation(pattern={"table": "patient_records"}),
            observation(pattern={"property_kind": "private value"}),
            observation(pattern={"repeated_value_ratio": True}),
            observation(pattern={"join_hops": "2"}),
            observation(relative_gain=float("nan")),
            observation(relative_gain=float("inf")),
            observation(relative_gain=True),
            observation(relative_gain=10 ** 1000),
            observation(query_correct=True),
            observation(query_correct=16, query_total=15),
            observation(query_total="15"),
        ]
        for index, bad in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(ExperienceError):
                self.record(f"bad-{index}", observations=[observation(), bad])
        for context in ({"columns": ["patient_name"]}, {"query_families": ["patient_name"]},
                        {"query_mix": {"lookup": True}}, {"row_count": True},
                        {"has_nullable_fks": 1}, {"query_families": ["lookup", "lookup"]}):
            with self.subTest(context=context), self.assertRaises(ExperienceError):
                self.store.record_run("bad-context", "source", "commerce", context, [observation()], final_correct=True)
        with self.assertRaises(ExperienceError):
            self.record(observations=[observation()] * 101)
        with self.assertRaises(ExperienceError):
            self.record(final_correct=1)
        self.assertFalse(self.path.exists())

    def test_corrupt_persisted_text_is_not_returned_to_a_future_agent(self):
        self.record()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE experience_patterns SET pattern_json=?", (json.dumps({"prompt": "unsafe source instruction"}),))
            db.commit()
        self.assertEqual(self.store.retrieve("commerce", CONTEXT), [])

    def test_evidence_and_pattern_retention_are_bounded_and_recent(self):
        with patch("graph_engine.construction.experience.MAX_EVIDENCE", 4), \
                patch("graph_engine.construction.experience.MAX_PATTERNS", 2), \
                patch("graph_engine.construction.experience.MAX_RECENT_RUNS", 3):
            for index in range(7):
                self.record(f"retained-{index}", f"source-{index}", observations=[
                    observation(pattern={"join_hops": index % 3})])
            with closing(sqlite3.connect(self.path)) as db:
                self.assertLessEqual(db.execute("SELECT count(*) FROM experience_evidence").fetchone()[0], 4)
                self.assertLessEqual(db.execute("SELECT count(*) FROM experience_patterns").fetchone()[0], 2)
                self.assertLessEqual(db.execute("SELECT count(*) FROM experience_runs").fetchone()[0], 7)
            self.assertTrue(self.record("retained-6", "source-6", observations=[observation(pattern={"join_hops": 0})])["duplicate"])

    def test_relative_gain_is_clamped_and_retrieval_limit_is_validated(self):
        self.record(observations=[observation(gain=9.0)])
        self.assertEqual(self.store.retrieve("commerce", CONTEXT)[0]["mean_gain"], 1)
        for limit in (0, 33, True, "8"):
            with self.subTest(limit=limit), self.assertRaises(ExperienceError):
                self.store.retrieve("commerce", CONTEXT, limit)

    def test_unavailable_retrieval_is_visible_and_failed_writes_do_not_claim_storage(self):
        unavailable = DomainExperienceStore(Path(self.temp.name))  # Existing directory is not a SQLite file.
        with self.assertRaises((sqlite3.Error, OSError)):
            unavailable.retrieve("commerce", CONTEXT)
        result = unavailable.record_run("run", "source", "commerce", CONTEXT, [observation()], final_correct=True)
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["stored"])
        self.assertNotIn(self.temp.name, json.dumps(result))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import json
import multiprocessing
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from graph_data_service.construction import (
    ConstructionError, ConstructionStore, _atomic_json, graph_schema, optimize_graph, validate_graph,
)


def _process_build(root, started, release):
    """Separate process with its own Python RLocks and the real shared file lock."""
    store = ConstructionStore(root)

    def algorithm(_source, _options):
        started.set()
        if not release.wait(15):
            raise RuntimeError("Test did not release its worker")
        return sample_graph()

    store.register_algorithm("csv", algorithm)
    store.build("user-a", "dataset-1")


def sample_graph():
    return {
        "directed": True,
        "nodes": [
            {"id": "alice", "type": "person", "properties": {"phone": "10086", "rank": 3}},
            {"id": "bridge", "type": "connection", "properties": {"label": "introduced"}},
            {"id": "bob", "type": "person", "properties": {"phone": "10086"}},
            {"id": "unused", "type": "unused", "properties": {"redundant": True}},
        ],
        "edges": [
            {"id": "e1", "source": "alice", "target": "bridge", "type": "knows", "properties": {"weight": 1}},
            {"id": "e2", "source": "bridge", "target": "bob", "type": "knows", "properties": {"weight": 2}},
        ],
    }


class GraphOptimizationTest(unittest.TestCase):
    def setUp(self):
        self.graph = sample_graph()
        self.original = copy.deepcopy(self.graph)

    def tearDown(self):
        self.assertEqual(self.graph, self.original, "Preview must never mutate the source graph")

    def test_collapse_connecting_node_preserves_original_properties(self):
        graph, summary = optimize_graph(self.graph, [{"op": "collapse_node", "node_id": "bridge", "edge_type": "introduced_by"}])
        self.assertEqual(len(graph["nodes"]), 3)
        self.assertEqual(len(graph["edges"]), 1)
        edge = graph["edges"][0]
        self.assertEqual((edge["source"], edge["target"], edge["type"]), ("alice", "bob", "introduced_by"))
        self.assertEqual(edge["properties"]["_collapsed_node"], self.original["nodes"][1])
        self.assertEqual(edge["properties"]["_collapsed_edges"], self.original["edges"])
        self.assertEqual(summary["before"], {"node_count": 4, "edge_count": 2})
        self.assertEqual(summary["after"], {"node_count": 3, "edge_count": 1})

    def test_property_can_move_to_edge_or_be_retained_on_node(self):
        for remove in (True, False):
            graph, _ = optimize_graph(self.graph, [{"op": "node_property_to_edge", "node_id": "alice",
                                                   "property": "rank", "edge_ids": ["e1"],
                                                   "target_property": "source_rank", "remove_from_node": remove}])
            self.assertEqual(graph["edges"][0]["properties"]["source_rank"], 3)
            self.assertEqual("rank" in graph["nodes"][0]["properties"], not remove)
            self.assertEqual(graph["edges"][1], self.original["edges"][1])

    def test_property_extraction_shares_identical_values_between_entities(self):
        graph, _ = optimize_graph(self.graph, [{"op": "extract_property_node", "node_ids": ["alice", "bob"],
                                               "property": "phone", "node_type": "phone", "edge_type": "has_phone",
                                               "remove_from_node": True}])
        phones = [node for node in graph["nodes"] if node["type"] == "phone"]
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0]["properties"], {"phone": "10086"})
        phone_edges = [edge for edge in graph["edges"] if edge["type"] == "has_phone"]
        self.assertEqual({edge["source"] for edge in phone_edges}, {"alice", "bob"})
        self.assertEqual({edge["target"] for edge in phone_edges}, {phones[0]["id"]})
        self.assertNotIn("phone", graph["nodes"][0]["properties"])

    def test_delete_supports_nodes_edges_and_properties(self):
        graph, _ = optimize_graph(self.graph, [
            {"op": "delete", "target": "node", "ids": ["unused"]},
            {"op": "delete", "target": "node", "ids": ["alice"], "property": "rank"},
            {"op": "delete", "target": "edge", "ids": ["e2"], "property": "weight"},
            {"op": "delete", "target": "edge", "ids": ["e1"]},
        ])
        self.assertEqual({node["id"] for node in graph["nodes"]}, {"alice", "bridge", "bob"})
        self.assertNotIn("rank", graph["nodes"][0]["properties"])
        self.assertEqual(graph["edges"], [{**self.original["edges"][1], "properties": {}}])

    def test_connected_node_deletion_requires_explicit_cascade(self):
        with self.assertRaises(ConstructionError):
            optimize_graph(self.graph, [{"op": "delete", "target": "node", "ids": ["bridge"]}])
        graph, _ = optimize_graph(self.graph, [{"op": "delete", "target": "node", "ids": ["bridge"], "cascade": True}])
        self.assertEqual(graph["edges"], [])
        self.assertEqual(len(graph["nodes"]), 3)

    def test_reverse_direction_preserves_ids_types_properties_and_other_edges(self):
        graph, _ = optimize_graph(self.graph, [{"op": "reverse_edges", "edge_ids": ["e1"]}])
        self.assertEqual(graph["edges"][0], {**self.original["edges"][0], "source": "bridge", "target": "alice"})
        self.assertEqual(graph["edges"][1], self.original["edges"][1])

    def test_rename_node_and_edge_business_types(self):
        graph, _ = optimize_graph(self.graph, [{"op": "rename_type", "target": "node", "old_type": "person", "new_type": "customer"},
                                               {"op": "rename_type", "target": "edge", "old_type": "knows", "new_type": "introduces"}])
        self.assertEqual([node["type"] for node in graph["nodes"]], ["customer", "connection", "customer", "unused"])
        self.assertEqual({edge["type"] for edge in graph["edges"]}, {"introduces"})

    def test_failed_late_operation_and_property_conflict_are_atomic(self):
        operations = [{"op": "reverse_edges", "edge_ids": ["e1"]},
                      {"op": "node_property_to_edge", "node_id": "alice", "property": "rank",
                       "target_property": "weight", "edge_ids": ["e1"], "remove_from_node": True}]
        with self.assertRaises(ConstructionError) as caught:
            optimize_graph(self.graph, operations)
        self.assertEqual(caught.exception.detail["code"], "property_conflict")
        self.assertEqual(caught.exception.detail["operation_index"], 1)

    def test_malformed_operations_have_validation_errors(self):
        for operation in ({"op": []}, {"op": "reverse_edges", "edge_ids": ["missing"]},
                          {"op": "delete", "target": "node", "ids": ["alice"], "cascade": "true"},
                          {"op": "reverse_edges", "edge_ids": ["e1"], "unexpected": 1}):
            with self.subTest(operation=operation), self.assertRaises(ConstructionError):
                optimize_graph(self.graph, [operation])

    def test_graph_validation_rejects_invalid_endpoints_duplicate_ids_and_non_json_properties(self):
        for endpoint in ([], {}, "missing", 123):
            graph = sample_graph()
            graph["edges"][0]["target"] = endpoint
            with self.subTest(endpoint=endpoint), self.assertRaises(ConstructionError):
                validate_graph(graph)
        graph = sample_graph()
        graph["nodes"].append(copy.deepcopy(graph["nodes"][0]))
        with self.assertRaises(ConstructionError):
            validate_graph(graph)
        for value in (float("nan"), float("inf"), {1: "numeric-key"}):
            graph = sample_graph()
            graph["nodes"][0]["properties"]["bad"] = value
            with self.subTest(value=value), self.assertRaises(ConstructionError):
                validate_graph(graph)

    def test_schema_is_deterministic_recursive_and_contains_no_property_values(self):
        graph = {
            "directed": True,
            "nodes": [
                {"id": "alice", "type": "Person", "properties": {
                    "profile": {"active": True, "score": 1},
                    "aliases": ["PRIVATE_ALIAS", {"lang": "PRIVATE_LANGUAGE"}],
                    "a.b": None,
                }},
                {"id": "acme", "type": "Company", "properties": {"name": "PRIVATE_COMPANY"}},
                {"id": "bob", "type": "Person", "properties": {
                    "profile": {"active": None, "score": 1.5}, "aliases": [],
                }},
            ],
            "edges": [
                {"id": "works-2", "source": "bob", "target": "acme", "type": "WORKS_AT",
                 "properties": {"roles": [{"title": "PRIVATE_ROLE"}]}},
                {"id": "works-1", "source": "alice", "target": "acme", "type": "WORKS_AT",
                 "properties": {"roles": []}},
            ],
        }
        schema = graph_schema(graph)
        reversed_graph = {**graph, "nodes": list(reversed(graph["nodes"])),
                          "edges": list(reversed(graph["edges"]))}
        self.assertEqual(schema, graph_schema(reversed_graph))
        self.assertEqual((schema["node_count"], schema["edge_count"]), (3, 2))
        self.assertEqual((schema["node_type_count"], schema["edge_type_count"]), (2, 1))
        self.assertEqual(schema["graph"]["visualization_kind"], "schema")
        self.assertEqual([node["id"] for node in schema["graph"]["nodes"]], ["Company", "Person"])
        edge = schema["graph"]["edges"][0]
        self.assertEqual((edge["source"], edge["target"], edge["type"], edge["label"]),
                         ("Person", "Company", "WORKS_AT", "WORKS_AT"))
        self.assertEqual(edge["properties"]["relationship_count"], 2)

        person = next(row for row in schema["node_types"] if row["type"] == "Person")
        paths = {row["path"]: row for row in person["property_paths"]}
        self.assertEqual(paths["profile.active"], {
            "path": "profile.active", "types": ["null", "boolean"],
            "type_counts": {"null": 1, "boolean": 1}, "present_count": 2, "value_count": 2,
        })
        self.assertEqual(paths["profile.score"]["type_counts"], {"integer": 1, "number": 1})
        self.assertEqual(paths["aliases[]"]["present_count"], 1)
        self.assertEqual(paths["aliases[]"]["value_count"], 2)
        self.assertIn('["a.b"]', paths)
        serialized = json.dumps(schema, ensure_ascii=False)
        for private in ("PRIVATE_ALIAS", "PRIVATE_LANGUAGE", "PRIVATE_COMPANY", "PRIVATE_ROLE"):
            self.assertNotIn(private, serialized)

    def test_undirected_schema_combines_opposite_endpoint_orientations(self):
        graph = {
            "directed": False,
            "nodes": [
                {"id": "a", "type": "A", "properties": {}},
                {"id": "b", "type": "B", "properties": {}},
            ],
            "edges": [
                {"id": "ab", "source": "a", "target": "b", "type": "REL", "properties": {}},
                {"id": "ba", "source": "b", "target": "a", "type": "REL", "properties": {}},
            ],
        }
        schema = graph_schema(graph)
        self.assertEqual(schema["edge_type_count"], 1)
        self.assertEqual(schema["edge_types"][0]["relationship_count"], 2)
        self.assertEqual((schema["edge_types"][0]["source_type"], schema["edge_types"][0]["target_type"]),
                         ("A", "B"))


class ConstructionStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ConstructionStore(self.root)
        self.source = self.root / "sources" / "example.csv"
        self.source.parent.mkdir()
        self.source.write_text("source data retained verbatim", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def build_ready(self, user="user-a", dataset="dataset-1"):
        self.store.register_algorithm("csv", lambda _path, _options: sample_graph())
        result = self.store.build(user, dataset, self.source, "example.csv")
        self.assertTrue(result["success"])
        self.assertEqual(result["revision"], 1)
        return self.store.graph(user, dataset)

    def test_all_three_algorithms_are_unavailable_without_fake_outputs(self):
        capabilities = self.store.capabilities()
        self.assertEqual({row["source_type"] for row in capabilities["formats"]}, {"sql", "csv", "txt"})
        self.assertFalse(any(row["algorithm_available"] for row in capabilities["formats"]))
        for kind in ("sql", "csv", "txt"):
            result = self.store.build("user-a", f"dataset-{kind}", self.source, f"source.{kind}")
            self.assertFalse(result["success"])
            self.assertEqual(result["status"], "algorithm_unavailable")
            self.assertEqual((result["revision"], result["node_count"], result["edge_count"]), (0, 0, 0))
            with self.assertRaises(ConstructionError) as caught:
                self.store.graph("user-a", f"dataset-{kind}")
            self.assertEqual(caught.exception.detail["code"], "graph_not_ready")

    def test_retained_unavailable_source_becomes_buildable_when_algorithm_is_installed(self):
        result = self.store.build("user-a", "dataset-txt", self.source, "notes.txt")
        self.assertEqual(result["status"], "algorithm_unavailable")
        upgraded = ConstructionStore(self.root)
        upgraded.register_algorithm("txt", lambda _source, _options: sample_graph())
        state = upgraded.status("user-a", "dataset-txt")
        self.assertEqual(state["status"], "not_built")
        self.assertTrue(state["algorithm_available"])
        self.assertEqual(state["revision"], 0)
        self.assertEqual(state["last_build_status"], "algorithm_unavailable")
        self.assertIn("已就绪", state["message"])
        self.assertNotIn("尚未接入", state["message"])
        self.assertEqual(upgraded.build("user-a", "dataset-txt")["status"], "ready")

    def test_status_exposes_only_bounded_whitelisted_agent_progress(self):
        self.store.register_source("user-a", "progress", self.source, "example.csv")
        directory = self.store._directory("user-a", "progress")
        record = self.store._read(directory, "progress")
        record.update(status="building", build_id="current-build", executor_instance="live-instance")
        _atomic_json(directory / "record.json", record)
        _atomic_json(directory / "construction-progress.json", {
            "version": "construction_progress.v1", "build_id": "current-build", "status": "running",
            "stage": "evolving", "current_message": "Agent 正在验收候选。", "elapsed_seconds": 12.5,
            "queries": [{"secret": True}], "source_schema": {"private": True},
            "metrics": {"iterations": 2, "evaluated_candidates": 3, "secret_count": 999},
            "events": [{"id": "candidate:3", "kind": "candidate", "stage": "evolving",
                        "message": "未采用候选：结构优化（未达到稳定性能收益门槛）。",
                        "outcome": "rejected", "arguments": {"private": True}}],
        })
        progress = self.store.status("user-a", "progress")["progress"]
        self.assertEqual(progress["stage"], "evolving")
        self.assertEqual(progress["metrics"], {"iterations": 2, "evaluated_candidates": 3})
        self.assertEqual(set(progress["events"][0]), {"id", "kind", "stage", "message", "outcome"})
        self.assertNotIn("queries", progress)
        self.assertNotIn("source_schema", progress)
        _atomic_json(directory / "construction-progress.json", {
            "version": "construction_progress.v1", "build_id": "current-build", "status": "running",
            "stage": "extracting_entities", "current_message": "正在分块识别实体与关系。",
            "elapsed_seconds": 2.5, "source_text": "must remain private",
            "metrics": {"chunks_completed": 2, "chunks_total": 5, "extracted_nodes": 7,
                        "extracted_edges": 4, "raw_triple_count": 11, "secret_count": 999},
            "events": [{"id": "stage:extracting_entities", "kind": "stage",
                        "stage": "extracting_entities", "message": "正在分块识别实体与关系。",
                        "prompt": "must remain private"}],
        })
        semantic_progress = self.store.status("user-a", "progress")["progress"]
        self.assertEqual(semantic_progress["stage"], "extracting_entities")
        self.assertEqual(semantic_progress["metrics"], {
            "chunks_completed": 2, "chunks_total": 5,
            "extracted_nodes": 7, "extracted_edges": 4,
        })
        self.assertEqual(set(semantic_progress["events"][0]),
                         {"id", "kind", "stage", "message"})
        self.assertNotIn("source_text", semantic_progress)
        _atomic_json(directory / "construction-progress.json", {
            "version": "construction_progress.v1", "build_id": "stale-build", "stage": "evolving", "events": []})
        self.assertNotIn("progress", self.store.status("user-a", "progress"))
        (directory / "construction-progress.json").write_text("{", encoding="utf-8")
        self.assertNotIn("progress", self.store.status("user-a", "progress"))

    def test_semantic_llm_unavailable_is_a_build_failure_not_an_unavailable_algorithm(self):
        class SemanticLLMUnavailable(RuntimeError):
            code = "text_extraction_llm_unavailable"

        def algorithm(_source, _options):
            raise SemanticLLMUnavailable("private provider configuration details")

        self.store.register_algorithm("txt", algorithm)
        result = self.store.build("user-a", "semantic-failure", self.source, "notes.txt",
                                  options={"txt_mode": "entities", "mode": "llm"})

        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["last_build_status"], "failed")
        self.assertEqual(result["failure_code"], "text_extraction_llm_unavailable")
        self.assertTrue(result["algorithm_available"])
        self.assertEqual(result["revision"], 0)
        self.assertIn("构图模型配置", result["message"])
        self.assertNotIn("private provider", result["message"])
        with self.assertRaises(ConstructionError) as caught:
            self.store.graph("user-a", "semantic-failure")
        self.assertEqual(caught.exception.detail["code"], "graph_not_ready")

    def test_plugin_builds_retained_source_and_ready_graph_survives_restart(self):
        calls = []
        self.store.register_source("user-a", "dataset-1", self.source, "example.csv")
        self.source.unlink()

        def algorithm(source, options):
            calls.append((source.read_text(encoding="utf-8"), copy.deepcopy(options)))
            options["nested"]["mutated"] = True
            return sample_graph()

        options = {"nested": {"mode": "custom"}}
        self.store.register_algorithm("csv", algorithm)
        result = self.store.build("user-a", "dataset-1", options=options)
        self.assertTrue(result["success"])
        self.assertEqual(calls, [("source data retained verbatim", {"nested": {"mode": "custom"}})])
        self.assertEqual(options, {"nested": {"mode": "custom"}})
        restarted = ConstructionStore(self.root)
        self.assertEqual(restarted.graph("user-a", "dataset-1")["graph"], sample_graph())
        reused = restarted.build("user-a", "dataset-1")
        self.assertTrue(reused["success"])
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["revision"], 1)
        self.assertEqual(len(calls), 1)

    def test_schema_requires_a_graph_and_tracks_committed_revision(self):
        with self.assertRaises(ConstructionError) as caught:
            self.store.schema("user-a", "dataset-1")
        self.assertEqual(caught.exception.detail["code"], "graph_not_ready")
        self.build_ready()
        first = self.store.schema("user-a", "dataset-1")
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["schema"]["node_type_count"], 3)
        self.store.optimize("user-a", "dataset-1", 1,
                            [{"op": "rename_type", "target": "node", "old_type": "unused",
                              "new_type": "person"}], commit=True)
        second = self.store.schema("user-a", "dataset-1")
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["schema"]["node_type_count"], 2)

    def test_schema_exposes_source_keys_without_private_report_content(self):
        original = self.build_ready()
        directory = self.store._directory("user-a", "dataset-1")
        report = {"source_schema": {"tables": [{
            "name": "people", "columns": ["id", "manager_id"], "primary_key": ["id"],
            "foreign_keys": [{"columns": ["manager_id"], "ref_table": "people", "ref_columns": ["id"]}],
            "create_sql": "private SQL", "rows": [{"id": "private-record-value"}],
        }]}, "queries": ["private query"]}
        _atomic_json(directory / "evolution-report.json", report)
        schema = self.store.schema("user-a", "dataset-1")
        source = schema["schema"]["source_tables"][0]
        self.assertEqual(source["primary_key"], ["id"])
        self.assertEqual(source["foreign_keys"][0]["ref_table"], "people")
        self.assertNotIn("private", json.dumps(schema))
        self.assertEqual(self.store.graph("user-a", "dataset-1"), original)

    def test_preview_is_readonly_commit_persists_and_stale_revision_is_rejected(self):
        original = self.build_ready()
        operations = [{"op": "reverse_edges", "edge_ids": ["e1"]}]
        preview = self.store.optimize("user-a", "dataset-1", 1, operations)
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(self.store.graph("user-a", "dataset-1"), original)
        commit = self.store.optimize("user-a", "dataset-1", 1, operations, commit=True)
        self.assertEqual(commit["graph"], preview["graph"])
        self.assertEqual(commit["revision"], 2)
        persisted = ConstructionStore(self.root).graph("user-a", "dataset-1")
        self.assertEqual(persisted["graph"], commit["graph"])
        with self.assertRaises(ConstructionError) as caught:
            self.store.optimize("user-a", "dataset-1", 1, operations, commit=True)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "revision_conflict")
        self.assertEqual(self.store.graph("user-a", "dataset-1"), persisted)

    def test_invalid_commit_never_changes_saved_graph_or_revision(self):
        original = self.build_ready()
        with self.assertRaises(ConstructionError):
            self.store.optimize("user-a", "dataset-1", 1, [
                {"op": "reverse_edges", "edge_ids": ["e1"]},
                {"op": "delete", "target": "node", "ids": ["missing"]},
            ], commit=True)
        self.assertEqual(self.store.graph("user-a", "dataset-1"), original)

    def test_concurrent_commits_have_one_winner(self):
        self.build_ready()

        def commit(edge_id):
            try:
                return self.store.optimize("user-a", "dataset-1", 1, [{"op": "reverse_edges", "edge_ids": [edge_id]}], commit=True)["revision"]
            except ConstructionError as exc:
                return exc.detail["code"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, ["e1", "e2"]))
        self.assertCountEqual(results, [2, "revision_conflict"])
        self.assertEqual(self.store.status("user-a", "dataset-1")["revision"], 2)

    def test_datasets_and_deletion_are_isolated_between_users(self):
        original = self.build_ready()
        self.assertEqual(self.store.status("user-b", "dataset-1")["status"], "not_built")
        with self.assertRaises(ConstructionError):
            self.store.graph("user-b", "dataset-1")
        self.build_ready(user="user-b")
        self.store.optimize("user-b", "dataset-1", 1, [{"op": "delete", "target": "node", "ids": ["unused"]}], commit=True)
        self.assertEqual(self.store.graph("user-a", "dataset-1"), original)
        self.store.delete("user-b", "dataset-1")
        self.assertEqual(self.store.graph("user-a", "dataset-1"), original)

    def test_invalid_identity_and_source_path_are_rejected(self):
        for identity in ("../other", "/absolute", "x/y", "..", "", "user\\other"):
            with self.subTest(identity=identity), self.assertRaises(ConstructionError):
                self.store.status(identity, "d1")
        with tempfile.TemporaryDirectory() as outside:
            source = Path(outside) / "source.csv"
            source.write_text("outside", encoding="utf-8")
            with self.assertRaises(ConstructionError) as caught:
                self.store.register_source("user-a", "dataset-1", source, "source.csv")
            self.assertEqual(caught.exception.status_code, 403)

    def test_failed_plugin_does_not_publish_partial_graph_and_can_be_retried(self):
        self.store.register_algorithm("csv", lambda _source, _options: {"bad": "graph"})
        result = self.store.build("user-a", "dataset-1", self.source, "example.csv")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["revision"], 0)
        with self.assertRaises(ConstructionError):
            self.store.graph("user-a", "dataset-1")
        self.store.register_algorithm("csv", lambda _source, _options: sample_graph())
        self.assertTrue(self.store.build("user-a", "dataset-1")["success"])

    def test_status_does_not_wait_for_a_live_worker_thread(self):
        started, release = threading.Event(), threading.Event()

        def algorithm(_source, _options):
            started.set()
            if not release.wait(10):
                raise RuntimeError("Test did not release its worker")
            return sample_graph()

        self.store.register_algorithm("csv", algorithm)
        with ThreadPoolExecutor(max_workers=2) as pool:
            build = pool.submit(self.store.build, "user-a", "dataset-1", self.source, "example.csv")
            try:
                self.assertTrue(started.wait(5))
                status = pool.submit(self.store.status, "user-a", "dataset-1").result(timeout=2)
                self.assertEqual(status["status"], "building")
                self.assertEqual(status["last_build_status"], "building")
            finally:
                release.set()
            self.assertTrue(build.result(timeout=5)["success"])
        self.assertEqual(self.store.status("user-a", "dataset-1")["status"], "ready")

    def test_status_inside_the_builder_is_not_mistaken_for_an_abandoned_reentrant_lock(self):
        observed = []

        def algorithm(_source, _options):
            observed.append(self.store.status("user-a", "dataset-1"))
            return sample_graph()

        self.store.register_algorithm("csv", algorithm)
        result = self.store.build("user-a", "dataset-1", self.source, "example.csv")
        self.assertTrue(result["success"])
        self.assertEqual(observed[0]["status"], "building")
        self.assertEqual(observed[0]["last_build_status"], "building")

    def test_live_process_lock_prevents_recovery_and_termination_allows_retry(self):
        self.store.register_source("user-a", "dataset-1", self.source, "example.csv")
        context = multiprocessing.get_context("spawn")
        started, release = context.Event(), context.Event()
        process = context.Process(target=_process_build, args=(str(self.root), started, release))
        process.start()
        try:
            self.assertTrue(started.wait(8), "Separate worker did not start")
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    status = pool.submit(self.store.status, "user-a", "dataset-1").result(timeout=2)
                    self.assertEqual(status["status"], "building")
                    self.assertEqual(status["last_build_status"], "building")
                finally:
                    # Abrupt death releases the OS lock without publishing ready/failed.
                    process.terminate()
                    process.join(5)
            self.assertFalse(process.is_alive())
            restarted = ConstructionStore(self.root)
            status = restarted.status("user-a", "dataset-1")
            self.assertEqual((status["status"], status["last_build_status"], status["revision"]),
                             ("failed", "interrupted", 0))
            self.assertIn("中断", status["message"])
            persisted = ConstructionStore(self.root).status("user-a", "dataset-1")
            self.assertEqual(persisted, status)
            restarted.register_algorithm("csv", lambda _source, _options: sample_graph())
            self.assertTrue(restarted.build("user-a", "dataset-1")["success"])
            self.assertEqual(restarted.graph("user-a", "dataset-1")["graph"], sample_graph())
        finally:
            # A terminated process may leave a multiprocessing synchronization
            # primitive locked. Do not touch its Events after abrupt termination.
            if process.is_alive():
                process.terminate()
                process.join(5)
            process.close()

    def test_interrupted_build_preserves_an_already_saved_graph_and_revision(self):
        original = self.build_ready()
        with self.store._lock("user-a", "dataset-1") as directory:
            record = self.store._read(directory, "dataset-1")
            record.update(status="building", last_build_status="building")
            _atomic_json(directory / "record.json", record)
        restarted = ConstructionStore(self.root)
        status = restarted.status("user-a", "dataset-1")
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["last_build_status"], "interrupted")
        self.assertEqual(status["revision"], original["revision"])
        self.assertEqual(restarted.graph("user-a", "dataset-1"), original)
        self.assertEqual(restarted.list_ready("user-a")[0]["dataset_id"], "dataset-1")

    def test_recovery_rereads_after_taking_the_lock_and_keeps_a_newer_completed_record(self):
        self.build_ready()
        record = self.store._read(self.store._directory("user-a", "dataset-1"), "dataset-1")
        stale_snapshot = {**copy.deepcopy(record), "status": "building", "last_build_status": "building"}
        with patch.object(self.store, "_read", side_effect=[stale_snapshot, record]), \
                patch("graph_data_service.construction._atomic_json") as write:
            status = self.store.status("user-a", "dataset-1")
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["last_build_status"], "ready")
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()

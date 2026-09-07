from __future__ import annotations

import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from graph_data_service.construction import ConstructionError, ConstructionStore, optimize_graph, validate_graph


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


if __name__ == "__main__":
    unittest.main()

"""Domain-level ordering guarantees shared by native and service execution."""

from __future__ import annotations

import json
import unittest

from graph_data_service.tool_catalog import (
    GRAPH_TOOLS, GRAPH_ML_TOOLS, REPORTING_TOOLS, INTERNAL_TOOLS,
    get_all_public_tools,
)
from graph_engine.construction_catalog import CONSTRUCTION_TOOLS
from graph_engine.tool_execution import (
    REGISTERED_TOOL_NAMES, execution_policy, get_tool_execution_metadata,
    resource_scope, resources_overlap, tools_conflict,
)


class ToolExecutionPolicyTests(unittest.TestCase):
    def test_every_catalog_tool_has_an_explicit_json_safe_contract(self):
        specs = [*GRAPH_TOOLS, *GRAPH_ML_TOOLS, *REPORTING_TOOLS,
                 *INTERNAL_TOOLS, *CONSTRUCTION_TOOLS]
        names = {spec["name"] for spec in specs}
        # set_session_config is still registered directly by the MCP server.
        self.assertEqual(names | {"set_session_config"}, REGISTERED_TOOL_NAMES)
        self.assertEqual(len(names), len(specs), "Tool names must be unique")
        for spec in specs:
            with self.subTest(tool=spec["name"]):
                self.assertEqual(spec["execution"], get_tool_execution_metadata(spec["name"]))
                json.dumps(spec["execution"])
        for spec in get_all_public_tools(include_graph_ml=True, include_reporting=True):
            self.assertIn("execution", spec)

    def test_independent_graph_questions_can_overlap(self):
        # These answers all describe the same graph and need no prior result.
        names = ["degree_analysis", "node_centrality", "graph_connectivity",
                 "community_detection", "shortest_path", "search_node",
                 "search_edge", "get_graph_info", "visualize_subgraph",
                 "create_analysis_report"]
        for left in names:
            for right in names:
                with self.subTest(left=left, right=right):
                    self.assertFalse(tools_conflict(execution_policy(left), execution_policy(right)))

    def test_graph_replacement_orders_both_readers_and_other_writers(self):
        readers = ["get_graph_info", "shortest_path", "list_graph_versions", "infer"]
        writers = ["load_graph", "load_graph_from_upload", "load_graph_from_text",
                   "load_graph_from_triples", "clear_graph", "undo_graph",
                   "load_graph_version", "discard_graph_version", "to_undirected",
                   "load_dataset", "load_constructed_graph", "load_graph_classification_graph"]
        for name in writers:
            writer = execution_policy(name)
            for other in readers + writers:
                with self.subTest(writer=name, other=other):
                    policy = execution_policy(other)
                    self.assertTrue(tools_conflict(writer, policy))
                    self.assertTrue(tools_conflict(policy, writer))

    def test_subgraph_preview_does_not_replace_the_current_graph(self):
        reader = execution_policy("degree_analysis")
        self.assertFalse(tools_conflict(
            execution_policy("subgraph_extraction", {"create_version": False}), reader))
        for arguments in ({}, {"create_version": True}, {"create_version": None},
                          {"create_version": "false"}, {"create_version": 0}):
            with self.subTest(arguments=arguments):
                self.assertTrue(tools_conflict(execution_policy("subgraph_extraction", arguments), reader))

    def test_construction_reads_and_commits_only_conflict_for_same_dataset(self):
        commit = execution_policy("commit_graph_optimization", {"dataset_id": "papers"})
        for name in ("get_graph_construction_status", "get_constructed_graph",
                     "preview_graph_optimization", "load_constructed_graph"):
            self.assertTrue(tools_conflict(commit, execution_policy(name, {"dataset_id": "papers"})))
            self.assertFalse(tools_conflict(commit, execution_policy(name, {"dataset_id": "authors"})))
        self.assertTrue(tools_conflict(commit, execution_policy("list_datasets")))
        self.assertFalse(tools_conflict(commit, execution_policy("degree_analysis")))
        # Missing or unresolved IDs stay conservative until arguments resolve.
        self.assertTrue(tools_conflict(commit, execution_policy("get_constructed_graph")))
        self.assertTrue(tools_conflict(commit, execution_policy("get_constructed_graph", {
            "dataset_id": {"$ref": "load.dataset_id"}})))

    def test_dataset_save_waits_for_graph_and_updates_its_binding(self):
        save = execution_policy("save_as_dataset")
        self.assertTrue(tools_conflict(save, execution_policy("load_graph")))
        self.assertTrue(tools_conflict(save, execution_policy("list_datasets")))
        self.assertTrue(tools_conflict(save, execution_policy("load_dataset")))
        self.assertFalse(tools_conflict(save, execution_policy("degree_analysis")))

    def test_document_ingestion_waits_for_retrieval_but_not_graph_analysis(self):
        upload = execution_policy("upload_document")
        self.assertTrue(tools_conflict(upload, upload))
        self.assertTrue(tools_conflict(upload, execution_policy("retrieve_context")))
        self.assertFalse(tools_conflict(upload, execution_policy("degree_analysis")))
        self.assertFalse(tools_conflict(upload, execution_policy("load_graph")))

    def test_ml_bundle_training_and_model_dependencies_are_preserved(self):
        bundle = execution_policy("load_graph_classification_dataset")
        train = execution_policy("train")
        self.assertTrue(tools_conflict(bundle, train))
        self.assertTrue(tools_conflict(bundle, execution_policy("load_graph_classification_graph")))
        self.assertTrue(tools_conflict(train, execution_policy("infer")))
        self.assertTrue(tools_conflict(train, execution_policy("visualize_model_architecture")))
        self.assertFalse(tools_conflict(bundle, execution_policy("degree_analysis")))
        self.assertFalse(tools_conflict(train, execution_policy("degree_analysis")))
        # Upload format detection may bind a classification dataset.
        self.assertTrue(tools_conflict(execution_policy("load_graph_from_upload"), train))

    def test_nebula_sync_orders_queries_and_graph_replacement(self):
        sync = execution_policy("nebula_sync_graph")
        for name in ("nebula_status", "nebula_get_neighbors", "nebula_get_node_info",
                     "nebula_shortest_path", "load_graph"):
            self.assertTrue(tools_conflict(sync, execution_policy(name)))

    def test_job_launches_are_marked_for_lifetime_tracking(self):
        for name in ("load_graph_from_upload", "load_graph_from_text",
                     "load_graph_classification_dataset", "train", "infer", "nebula_sync_graph"):
            self.assertTrue(execution_policy(name).background)
        self.assertFalse(execution_policy("degree_analysis").background)

    def test_unknown_and_lifecycle_calls_are_barriers(self):
        for name in ("not_reviewed_yet", "register_session", "delete_graph_session",
                     "set_session_config", "cancel_job"):
            with self.subTest(name=name):
                self.assertTrue(execution_policy(name).barrier)
                self.assertTrue(tools_conflict(execution_policy(name), execution_policy("retrieve_context")))
                self.assertTrue(tools_conflict(execution_policy(name), execution_policy("get_graph_construction_capabilities")))

    def test_resource_scopes_and_wildcards_do_not_leak_between_datasets(self):
        self.assertEqual(resource_scope("datasets"), "user")
        self.assertEqual(resource_scope("construction:papers"), "user")
        for name in ("graph", "models", "graph_ml_dataset", "documents", "dataset_binding"):
            self.assertEqual(resource_scope(name), "session")
        self.assertTrue(resources_overlap("construction:*", "construction:papers"))
        self.assertTrue(resources_overlap("construction:papers", "construction:*"))
        self.assertFalse(resources_overlap("construction:paper", "construction:papers"))
        self.assertFalse(resources_overlap("construction:*", "graph"))
        literal_star = execution_policy("commit_graph_optimization", {"dataset_id": "*"})
        self.assertFalse(tools_conflict(literal_star, execution_policy(
            "get_constructed_graph", {"dataset_id": "papers"})))


if __name__ == "__main__":
    unittest.main()

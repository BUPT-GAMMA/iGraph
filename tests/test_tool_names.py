from __future__ import annotations

import unittest

from benchmark_platform.tool_names import (
    CANONICAL_TOOL_NAMES,
    GRAPH_INSIGHT_ACTIONS,
    GRAPH_IO_ACTIONS,
    GRAPH_ML_ACTIONS,
    GRAPH_RETRIEVAL_ACTIONS,
    GRAPH_THEORY_ACTIONS,
    canonicalize_tool_name,
    expected_tool_path,
    normalize_trace_tool_calls,
)


class CanonicalToolNamesTest(unittest.TestCase):
    def test_remote_skill_groups_are_in_canonical_whitelist(self) -> None:
        expected = {
            *GRAPH_RETRIEVAL_ACTIONS,
            *GRAPH_IO_ACTIONS,
            *GRAPH_THEORY_ACTIONS,
            *GRAPH_INSIGHT_ACTIONS,
            *GRAPH_ML_ACTIONS,
            "graph_task_solver",
        }
        self.assertEqual(CANONICAL_TOOL_NAMES, expected)

    def test_question_networkx_aliases_are_canonicalized(self) -> None:
        task = {
            "required_tools": [
                "extract_subgraph",
                "nx.triangles",
                "nx.average_neighbor_degree",
                "min_vertex_cover",
            ]
        }
        self.assertEqual(
            expected_tool_path(task),
            [
                "extract_n_hop_ego_subgraph",
                "get_triangles",
                "get_avg_neighbor_degree",
                "get_min_vertex_cover",
            ],
        )

    def test_retrieval_question_aliases_are_canonicalized(self) -> None:
        self.assertEqual(
            expected_tool_path(
                {
                    "required_tools": [
                        "graph.degree",
                        "graph.has_edge",
                        "graph.neighbors",
                        "len(graph.nodes())",
                        "len(graph.edges())",
                    ]
                }
            ),
            [
                "get_node_degree",
                "check_edge_existence",
                "get_node_neighbors",
                "get_node_number",
                "get_edge_number",
            ],
        )

    def test_local_catalog_concepts_map_to_remote_actions(self) -> None:
        self.assertEqual(
            expected_tool_path(
                {
                    "required_tools": [
                        "subgraph_extraction",
                        "shortest_path",
                        "graph_connectivity",
                        "minimum_spanning_tree",
                    ]
                }
            ),
            [
                "extract_n_hop_ego_subgraph",
                "calculate_shortest_path",
                "connectivity_checking",
                "get_minimum_spanning_edges",
            ],
        )

    def test_edge_perturbation_name_used_by_real_insight_corpus_is_canonical(self):
        self.assertEqual(
            expected_tool_path(
                {"task_type": "insight", "required_tools": ["simulate_edge_perturbation"]}
            ),
            ["simulate_edge_perturbation"],
        )

    def test_ml_base_names_expand_to_train_then_infer(self) -> None:
        self.assertEqual(
            expected_tool_path(
                {
                    "required_tools": [
                        "structural_node_embedding_classification",
                        "structural_node_embedding_link_prediction",
                        "structural_graph_embedding_classification",
                    ]
                }
            ),
            [
                "structural_node_embedding_classification_train",
                "structural_node_embedding_classification_infer",
                "structural_node_embedding_link_prediction_train",
                "structural_node_embedding_link_prediction_infer",
                "structural_graph_embedding_classification_train",
                "structural_graph_embedding_classification_infer",
            ],
        )

    def test_empty_ml_requirements_are_inferred_from_task_type(self) -> None:
        cases = (
            (
                {"task_type": "NC", "required_tools": []},
                [
                    "structural_node_embedding_classification_train",
                    "structural_node_embedding_classification_infer",
                ],
            ),
            (
                {"task_type": "LP", "required_tools": None},
                [
                    "structural_node_embedding_link_prediction_train",
                    "structural_node_embedding_link_prediction_infer",
                ],
            ),
            (
                {"task_type": "GC"},
                [
                    "structural_graph_embedding_classification_train",
                    "structural_graph_embedding_classification_infer",
                ],
            ),
        )
        for task, expected in cases:
            with self.subTest(task_type=task["task_type"]):
                self.assertEqual(expected_tool_path(task), expected)

    def test_empty_non_ml_requirements_remain_empty(self) -> None:
        for required_tools in ([], None, ""):
            with self.subTest(required_tools=required_tools):
                self.assertEqual(
                    expected_tool_path(
                        {
                            "task_type": "theory",
                            "required_tools": required_tools,
                        }
                    ),
                    [],
                )

    def test_generic_legacy_train_requires_task_hint(self) -> None:
        self.assertEqual(
            canonicalize_tool_name(
                "train", task={"arguments": {"task": "link_prediction"}}
            ),
            ("structural_node_embedding_link_prediction_train",),
        )
        with self.assertRaisesRegex(ValueError, "unknown benchmark graph tool"):
            canonicalize_tool_name("train")

        self.assertEqual(
            expected_tool_path(
                {"task_type": "GC", "required_tools": ["train", "infer"]}
            ),
            [
                "structural_graph_embedding_classification_train",
                "structural_graph_embedding_classification_infer",
            ],
        )

    def test_unknown_question_tool_is_rejected_or_skipped(self) -> None:
        task = {"required_tools": ["nx.not_a_real_action", "nx.density"]}
        with self.assertRaisesRegex(ValueError, "unknown benchmark graph tool"):
            expected_tool_path(task)
        self.assertEqual(expected_tool_path(task, strict=False), ["get_density"])


class NormalizeToolTraceTest(unittest.TestCase):
    def test_structured_calls_keep_raw_canonical_and_status(self) -> None:
        normalized = normalize_trace_tool_calls(
            [
                {
                    "tool_name": "nx.density",
                    "arguments": {},
                    "status": "completed",
                },
                {
                    "name": "compute_ego_density",
                    "arguments": {"node_id": 4},
                    "success": False,
                },
            ]
        )
        self.assertEqual(
            normalized.strict_path,
            ("get_density", "compute_ego_density"),
        )
        self.assertEqual(normalized.successful_path, ("get_density",))
        self.assertEqual(normalized.calls[0]["raw_name"], "nx.density")
        self.assertEqual(normalized.calls[0]["canonical_name"], "get_density")
        self.assertEqual(normalized.calls[0]["status"], "success")
        self.assertEqual(normalized.calls[1]["status"], "failed")

    def test_openai_function_shape_is_supported(self) -> None:
        normalized = normalize_trace_tool_calls(
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "get_local_neighbors_by_hop",
                            "arguments": '{"node_id": 10, "hop_num": 2}',
                        },
                        "status": "ok",
                    }
                ]
            }
        )
        self.assertEqual(
            normalized.successful_path,
            ("get_local_neighbors_by_hop",),
        )
        self.assertEqual(normalized.calls[0]["call_id"], "call-1")

    def test_result_event_updates_status_without_duplicating_call(self) -> None:
        normalized = normalize_trace_tool_calls(
            {
                "events": [
                    {"type": "assistant", "content": "working"},
                    {
                        "type": "function_call",
                        "call_id": "call-7",
                        "name": "get_density",
                        "arguments": {},
                    },
                    {
                        "type": "tool_result",
                        "tool_call_id": "call-7",
                        "name": "get_density",
                        "ok": True,
                    },
                ]
            }
        )
        self.assertEqual(normalized.strict_path, ("get_density",))
        self.assertEqual(normalized.successful_path, ("get_density",))

    def test_exec_command_extracts_action_after_script(self) -> None:
        normalized = normalize_trace_tool_calls(
            [
                {
                    "name": "exec_command",
                    "arguments": {
                        "cmd": (
                            "python /root/.codex/skills/graph_analysis_skill/"
                            "graph_theory_tools.py check_edge_existence cora.pt "
                            "'{\"u\":1,\"v\":2}'"
                        )
                    },
                    "exit_code": 0,
                }
            ]
        )
        self.assertEqual(normalized.strict_path, ("check_edge_existence",))
        self.assertEqual(normalized.successful_path, ("check_edge_existence",))
        self.assertEqual(normalized.calls[0]["raw_name"], "exec_command")
        self.assertEqual(normalized.calls[0]["source"], "command")

    def test_structured_script_call_uses_action_argument(self) -> None:
        normalized = normalize_trace_tool_calls(
            [
                {
                    "name": "/skills/graph_ml_tools.py",
                    "arguments": {
                        "action": "structural_node_embedding_link_prediction_infer"
                    },
                    "status": "success",
                }
            ]
        )
        self.assertEqual(
            normalized.strict_path,
            ("structural_node_embedding_link_prediction_infer",),
        )

    def test_shell_command_can_contain_multiple_actions(self) -> None:
        normalized = normalize_trace_tool_calls(
            [
                {
                    "tool": "shell",
                    "input": {
                        "command": (
                            "python graph_io_tools.py extract_key_to_numpy_without_pyg "
                            "cora.pt x && python graph_ml_tools.py "
                            "structural_node_embedding_classification_train "
                            "--node_features x.npy"
                        )
                    },
                    "status": "success",
                }
            ]
        )
        self.assertEqual(
            normalized.strict_path,
            (
                "extract_key_to_numpy_without_pyg",
                "structural_node_embedding_classification_train",
            ),
        )
        self.assertEqual(normalized.calls[0]["source_index"], 0)
        self.assertEqual(normalized.calls[1]["source_index"], 0)

    def test_solver_script_has_one_canonical_name(self) -> None:
        normalized = normalize_trace_tool_calls(
            [
                {
                    "name": "exec_command",
                    "args": {
                        "cmd": "python graph_task_solver.py cora.pt --source 2456 --pretty"
                    },
                    "status": "passed",
                }
            ]
        )
        self.assertEqual(normalized.strict_path, ("graph_task_solver",))

    def test_combined_ml_observation_expands_like_expected_path(self) -> None:
        normalized = normalize_trace_tool_calls(
            [
                {
                    "name": "structural_graph_embedding_classification",
                    "status": "success",
                }
            ]
        )
        self.assertEqual(
            normalized.strict_path,
            (
                "structural_graph_embedding_classification_train",
                "structural_graph_embedding_classification_infer",
            ),
        )

    def test_failed_and_unknown_calls_remain_in_strict_path(self) -> None:
        normalized = normalize_trace_tool_calls(
            [
                {"name": "get_density", "status": "failed"},
                {"name": "misspelled_tool", "status": "success"},
                {"name": "get_radius", "status": "success"},
            ]
        )
        self.assertEqual(
            normalized.strict_path,
            ("get_density", "misspelled_tool", "get_radius"),
        )
        self.assertEqual(
            normalized.successful_path,
            ("misspelled_tool", "get_radius"),
        )
        self.assertIsNone(normalized.calls[1]["canonical_name"])

    def test_missing_status_is_conservative_but_configurable(self) -> None:
        unknown = normalize_trace_tool_calls([{"name": "get_density"}])
        assumed = normalize_trace_tool_calls(
            [{"name": "get_density"}], default_status="success"
        )
        self.assertEqual(unknown.strict_path, ("get_density",))
        self.assertEqual(unknown.successful_path, ())
        self.assertEqual(assumed.successful_path, ("get_density",))

    def test_to_dict_is_json_friendly(self) -> None:
        payload = normalize_trace_tool_calls(
            [{"name": "get_density", "ok": True}]
        ).to_dict()
        self.assertEqual(payload["strict_path"], ["get_density"])
        self.assertEqual(payload["successful_path"], ["get_density"])
        self.assertEqual(payload["calls"][0]["status"], "success")


if __name__ == "__main__":
    unittest.main()

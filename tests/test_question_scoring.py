from __future__ import annotations

import unittest

from benchmark_platform.scoring import (
    score_attempt,
    score_ground_truth,
    score_tool_path,
    validate_tool_path_spec,
)


def _shell_call(script: str, action: str, *, success: bool) -> dict:
    return {
        "raw_name": "shell",
        "arguments": {
            "command": [
                "python",
                f"/root/.codex/skills/graph_analysis_skills/{script}",
                action,
                "dataset.pt",
                "{}",
            ]
        },
        "success": success,
    }


class GroundTruthScoringTests(unittest.TestCase):
    def test_fenced_theory_answer_can_match_but_violates_protocol(self):
        task = {
            "task_type": "theory",
            "ground_truth": {
                "get_density": {"success": True, "density": 0.125},
                "get_node_degree": {"degree": 4},
            },
        }
        answer = """```json
{"get_density":{"success":true,"density":0.12500001},"get_node_degree":{"degree":4},"extra":"ignored"}
```"""
        score = score_ground_truth(task, answer, numeric_tolerance=1e-5)
        self.assertFalse(score["protocol_valid"])
        self.assertFalse(score["json_valid"])
        self.assertTrue(score["answer_match"])
        self.assertFalse(score["exact"])
        self.assertEqual(score["accuracy"], 1.0)

    def test_bare_json_theory_answer_matches_and_satisfies_protocol(self):
        task = {
            "task_type": "theory",
            "ground_truth": {
                "get_density": {"success": True, "density": 0.125},
                "get_node_degree": {"degree": 4},
            },
        }
        answer = (
            '{"get_density":{"success":true,"density":0.12500001},'
            '"get_node_degree":{"degree":4}}'
        )
        score = score_ground_truth(task, answer, numeric_tolerance=1e-5)
        self.assertTrue(score["protocol_valid"])
        self.assertTrue(score["json_valid"])
        self.assertTrue(score["answer_match"])
        self.assertTrue(score["exact"])

    def test_retrieval_list_order_is_scored_strictly(self):
        task = {
            "task_type": "retrieval",
            "ground_truth": {"neighbors": [2, 4, 8]},
        }
        correct = score_ground_truth(task, {"neighbors": [2, 4, 8]})
        reordered = score_ground_truth(task, {"neighbors": [8, 4, 2]})
        self.assertTrue(correct["exact"])
        self.assertFalse(reordered["exact"])
        self.assertEqual(reordered["accuracy"], 1 / 3)

    def test_ml_boolean_is_not_equal_to_integer_one(self):
        task = {"task_type": "LP", "ground_truth": True}
        self.assertTrue(
            score_ground_truth(task, {"has_edge": True})["exact"]
        )
        self.assertFalse(score_ground_truth(task, {"has_edge": 1})["exact"])

    def test_ml_negative_one_unwraps_single_prediction(self):
        task = {"task_type": "NC", "ground_truth": -1}
        score = score_ground_truth(task, {"predictions": [-1]})
        self.assertTrue(score["exact"])
        self.assertEqual(score["matched_leaves"], 1)

    def test_nested_prediction_ground_truth_scores_every_leaf(self):
        task = {
            "task_type": "prediction",
            "ground_truth": {
                "structural_node_embedding_link_prediction": {
                    "success": True,
                    "node1": 12,
                    "node2": 19,
                    "has_edge": False,
                }
            },
        }
        answer = {
            "structural_node_embedding_link_prediction": {
                "success": True,
                "node1": 12,
                "node2": 19,
                "has_edge": True,
            }
        }
        score = score_ground_truth(task, answer)
        self.assertFalse(score["exact"])
        self.assertEqual(score["matched_leaves"], 3)
        self.assertEqual(score["total_leaves"], 4)
        self.assertEqual(score["accuracy"], 0.75)

    def test_matching_empty_containers_are_exact(self):
        for ground_truth, answer in (({}, {}), ([], [])):
            with self.subTest(container=type(ground_truth).__name__):
                score = score_ground_truth(
                    {"task_type": "insight", "ground_truth": ground_truth},
                    answer,
                )
                self.assertTrue(score["answer_match"])
                self.assertTrue(score["exact"])
                self.assertEqual(score["matched_leaves"], 1)
                self.assertEqual(score["total_leaves"], 1)

    def test_recursive_json_like_strings_keep_their_string_type(self):
        task = {
            "task_type": "insight",
            "ground_truth": {
                "payload": {"number_text": "1", "boolean_text": "true"}
            },
        }
        correct = score_ground_truth(
            task,
            '{"payload":{"number_text":"1","boolean_text":"true"}}',
        )
        coerced = score_ground_truth(
            task,
            '{"payload":{"number_text":1,"boolean_text":true}}',
        )

        self.assertTrue(correct["exact"])
        self.assertNotIn("expected", correct)
        self.assertEqual(correct["expected_type"], "object")
        self.assertEqual(len(correct["expected_sha256"]), 64)
        self.assertGreater(correct["expected_bytes"], 0)
        self.assertFalse(coerced["answer_match"])
        self.assertFalse(coerced["exact"])
        self.assertEqual(coerced["matched_leaves"], 0)
        self.assertEqual(coerced["total_leaves"], 2)


class ToolPathScoringTests(unittest.TestCase):
    def test_strict_path_keeps_failed_call_while_successful_path_filters_it(self):
        task = {"required_tools": ["nx.density", "graph.degree"]}
        calls = [
            _shell_call("graph_theory_tools.py", "get_density", success=False),
            _shell_call("graph_theory_tools.py", "get_density", success=True),
            _shell_call("graph_theory_tools.py", "get_node_degree", success=True),
        ]

        score = score_tool_path(task, calls)

        self.assertEqual(score["expected"], ["get_density", "get_node_degree"])
        self.assertEqual(
            score["strict_actual"],
            ["get_density", "get_density", "get_node_degree"],
        )
        self.assertEqual(
            score["successful_actual"], ["get_density", "get_node_degree"]
        )
        self.assertFalse(score["strict"]["exact"])
        self.assertTrue(score["successful"]["exact"])
        self.assertEqual(len(score["normalized_tool_calls"]), 3)

    def test_combined_ml_requirement_expands_to_canonical_train_then_infer(self):
        task = {
            "required_tools": ["structural_node_embedding_link_prediction"],
            "task_type": "prediction",
        }
        calls = [
            {
                "raw_name": "structural_node_embedding_link_prediction_train",
                "arguments": {},
                "success": True,
            },
            {
                "raw_name": "structural_node_embedding_link_prediction_infer",
                "arguments": {},
                "success": True,
            },
        ]
        score = score_tool_path(task, calls)
        self.assertEqual(
            score["expected"],
            [
                "structural_node_embedding_link_prediction_train",
                "structural_node_embedding_link_prediction_infer",
            ],
        )
        self.assertTrue(score["strict"]["exact"])
        self.assertTrue(score["successful"]["exact"])

    def test_empty_required_tools_use_pure_ml_task_type_path(self):
        task = {"task_type": "LP", "required_tools": []}
        calls = [
            {
                "name": "structural_node_embedding_link_prediction_train",
                "success": True,
            },
            {
                "name": "structural_node_embedding_link_prediction_infer",
                "success": True,
            },
        ]

        score = score_tool_path(task, calls)

        self.assertTrue(score["available"])
        self.assertTrue(score["strict"]["exact"])
        self.assertTrue(score["successful"]["exact"])

    def test_score_attempt_returns_both_answer_and_path_details(self):
        task = {
            "task_type": "LP",
            "ground_truth": False,
            "required_tools": ["link_prediction"],
        }
        calls = [
            {
                "name": "structural_node_embedding_link_prediction_train",
                "success": True,
            },
            {
                "name": "structural_node_embedding_link_prediction_infer",
                "success": True,
            },
        ]
        score = score_attempt(task, {"has_edge": False}, calls)
        self.assertTrue(score["ground_truth"]["exact"])
        self.assertTrue(score["tool_path"]["successful"]["exact"])

    def test_citation_solver_is_an_accepted_skill_path(self):
        task = {
            "category": "retrieval",
            "dataset": "cora",
            "question": "What are the neighbors of node 947 in this local neighborhood?",
            "required_tools": ["extract_subgraph", "graph.neighbors"],
        }
        score = score_tool_path(
            task,
            [{"name": "graph_task_solver", "status": "success"}],
        )
        self.assertIn(["graph_task_solver"], score["accepted_paths"])
        self.assertTrue(score["successful"]["exact"])
        self.assertEqual(
            score["successful"]["matched_expected"], ["graph_task_solver"]
        )

    def test_published_tsa_uses_reference_length_not_actual_length(self):
        task = {"required_tools": ["get_density", "get_node_degree"]}
        calls = [
            {"name": "unrelated_tool", "status": "success"},
            {"name": "get_density", "status": "success"},
            {"name": "get_node_degree", "status": "success"},
            {"name": "another_tool", "status": "success"},
        ]

        score = score_tool_path(task, calls)["strict"]

        self.assertEqual(score["tsa_numerator"], 2)
        self.assertEqual(score["tsa_denominator"], 2)
        self.assertEqual(score["tool_selection_accuracy"], 1.0)
        self.assertEqual(score["precision"], 0.5)
        self.assertFalse(score["exact"])

    def test_published_tsa_respects_order_and_uses_strict_selected_path(self):
        task = {
            "required_tools": [
                "get_density",
                "get_node_degree",
                "get_clustering_coefficient",
            ]
        }
        calls = [
            {"name": "get_node_degree", "status": "failed"},
            {"name": "get_density", "status": "success"},
            {"name": "get_clustering_coefficient", "status": "success"},
        ]

        score = score_tool_path(task, calls)

        self.assertEqual(score["strict"]["tsa_numerator"], 2)
        self.assertEqual(score["strict"]["tsa_denominator"], 3)
        self.assertAlmostEqual(
            score["strict"]["tool_selection_accuracy"], 2 / 3
        )
        self.assertEqual(score["successful"]["tsa_numerator"], 2)

    def test_normalized_capture_can_be_scored_without_losing_command_action(self):
        task = {"required_tools": ["get_density"]}
        first = score_tool_path(
            task,
            [_shell_call("graph_theory_tools.py", "get_density", success=True)],
        )

        replayed = score_tool_path(task, first["normalized_tool_calls"])

        self.assertEqual(replayed["strict_actual"], ["get_density"])
        self.assertTrue(replayed["strict"]["exact"])

    def test_accepted_path_selection_prefers_published_tsa(self):
        task = {
            "required_tools": [
                "get_density",
                "get_node_degree",
                "get_clustering_coefficient",
            ],
            "accepted_tool_paths": [["get_density", "get_node_degree"]],
        }
        score = score_tool_path(
            task,
            [
                {"name": "get_density", "status": "success"},
                {"name": "get_node_degree", "status": "success"},
                {"name": "unrelated_tool", "status": "success"},
            ],
        )["strict"]

        self.assertEqual(
            score["matched_expected"], ["get_density", "get_node_degree"]
        )
        self.assertEqual(score["tool_selection_accuracy"], 1.0)

    def test_empty_accepted_tool_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            validate_tool_path_spec(
                {
                    "required_tools": ["get_density"],
                    "accepted_tool_paths": [[]],
                }
            )


if __name__ == "__main__":
    unittest.main()

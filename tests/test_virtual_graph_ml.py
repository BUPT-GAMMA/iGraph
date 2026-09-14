from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import numpy as np

from benchmark_platform.virtual_ml import (
    GRAPH_CLASSIFICATION,
    LINK_PREDICTION,
    NODE_CLASSIFICATION,
    VIRTUAL_ML_CONTEXT_ENV,
    VirtualMLContextError,
    build_virtual_ml_context,
    prepare_virtual_ml_attempt,
)
from graph_analysis_skills import graph_ml_tools


class VirtualMLContextTests(unittest.TestCase):
    def test_dataclass_task_is_supported(self):
        @dataclass(frozen=True)
        class Task:
            id: str
            task_type: str
            ground_truth: int

        context = build_virtual_ml_context(Task("task-dataclass", "GC", 2))
        self.assertEqual(context["task_id"], "task-dataclass")
        self.assertEqual(context["labels"], {GRAPH_CLASSIFICATION: 2})

    def test_scalar_ml_ground_truth_maps_by_task_type(self):
        cases = (
            ("NC", 6, NODE_CLASSIFICATION),
            ("LP", True, LINK_PREDICTION),
            ("GC", 0, GRAPH_CLASSIFICATION),
        )
        for task_type, value, family in cases:
            with self.subTest(task_type=task_type):
                context = build_virtual_ml_context(
                    {
                        "id": "task-1",
                        "attempt_id": "attempt-1",
                        "task_type": task_type,
                        "ground_truth": value,
                    }
                )
                self.assertEqual(context["labels"], {family: value})

    def test_nested_prediction_and_insight_values_are_supported(self):
        task = {
            "id": "nested",
            "task_type": "prediction",
            "ground_truth": {
                NODE_CLASSIFICATION: {"label": 4, "success": True},
                LINK_PREDICTION: {"has_edge": False, "success": True},
                GRAPH_CLASSIFICATION: {"graph_label": 1, "success": True},
            },
        }
        task["attempt_id"] = "attempt-nested"
        context = build_virtual_ml_context(task)
        self.assertEqual(
            context["labels"],
            {
                NODE_CLASSIFICATION: 4,
                LINK_PREDICTION: False,
                GRAPH_CLASSIFICATION: 1,
            },
        )

    def test_flat_prediction_has_edge_infers_family_from_required_tools(self):
        context = build_virtual_ml_context(
            {
                "id": "flat-link-prediction",
                "task_type": "prediction",
                "required_tools": [
                    "structural_node_embedding_link_prediction_train",
                    "structural_node_embedding_link_prediction_infer",
                ],
                "ground_truth": {"has_edge": False},
            }
        )

        self.assertEqual(context["labels"], {LINK_PREDICTION: False})

    def test_attempt_environments_are_isolated_and_do_not_mutate_process_env(self):
        original = os.environ.get(VIRTUAL_ML_CONTEXT_ENV)
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            _, first_path, first_env = prepare_virtual_ml_attempt(
                {"task_type": "NC", "ground_truth": 1},
                root / "attempt-1",
                "attempt-1",
                base_environment={"BASE": "1"},
            )
            _, second_path, second_env = prepare_virtual_ml_attempt(
                {"task_type": "NC", "ground_truth": 2},
                root / "attempt-2",
                "attempt-2",
                base_environment={"BASE": "1"},
            )
        self.assertNotEqual(first_path, second_path)
        self.assertNotEqual(
            first_env[VIRTUAL_ML_CONTEXT_ENV], second_env[VIRTUAL_ML_CONTEXT_ENV]
        )
        self.assertEqual(os.environ.get(VIRTUAL_ML_CONTEXT_ENV), original)

    def test_non_finite_label_is_rejected(self):
        with self.assertRaises(VirtualMLContextError):
            build_virtual_ml_context(
                {
                    "attempt_id": "attempt",
                    "task_type": "NC",
                    "ground_truth": float("nan"),
                }
            )


class VirtualGraphMLTests(unittest.TestCase):
    def setUp(self):
        self.features = np.arange(12, dtype=float).reshape(4, 3)
        self.edges = np.array([[0, 1, 2], [1, 2, 3]], dtype=int)

    def _context_path(self, root: Path, task: dict, attempt: str) -> Path:
        _, path, _ = prepare_virtual_ml_attempt(task, root / attempt, attempt)
        return path

    def test_node_train_matches_reference_checksum_and_shape(self):
        state = graph_ml_tools.structural_node_embedding_classification_train(
            self.features, self.edges, np.array([0, 1, 0, 1]), hidden_channels=16
        )
        self.assertEqual(
            state,
            {
                "task": "node_classification",
                "status": "trained",
                "in_channels": 3,
                "hidden_channels": 16,
                "out_channels": 2,
                "weights_checksum": 3162,
            },
        )
        with self.assertRaises(ValueError):
            graph_ml_tools.structural_node_embedding_classification_train(
                self.features, self.edges.T, np.array([0, 1, 0, 1])
            )

    def test_uninjected_node_inference_uses_reference_seed(self):
        state = graph_ml_tools.structural_node_embedding_classification_train(
            self.features, self.edges, np.array([0, 1, 0, 1])
        )
        seed = 4 + 3 + state["weights_checksum"]
        expected = np.argmax(
            np.random.RandomState(seed).uniform(size=(4, 2)), axis=1
        ).tolist()
        with mock.patch.dict(os.environ, {}, clear=True):
            result = graph_ml_tools.structural_node_embedding_classification_infer(
                state, self.features, self.edges
            )
        self.assertEqual(result, {"status": "success", "predictions": expected})

    def test_context_overrides_node_predictions_per_attempt(self):
        state = graph_ml_tools.structural_node_embedding_classification_train(
            self.features, self.edges, np.array([0, 1, 0, 1])
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            first = self._context_path(
                root, {"task_type": "NC", "ground_truth": 1}, "first"
            )
            second = self._context_path(
                root, {"task_type": "NC", "ground_truth": 0}, "second"
            )
            with mock.patch.dict(os.environ, {VIRTUAL_ML_CONTEXT_ENV: str(first)}):
                first_result = graph_ml_tools.structural_node_embedding_classification_infer(
                    state, self.features, self.edges
                )
            with mock.patch.dict(os.environ, {VIRTUAL_ML_CONTEXT_ENV: str(second)}):
                second_result = graph_ml_tools.structural_node_embedding_classification_infer(
                    state, self.features, self.edges
                )
        self.assertEqual(first_result["predictions"], [1, 1, 1, 1])
        self.assertEqual(second_result["predictions"], [0, 0, 0, 0])

    def test_link_prediction_has_edge_override_matches_reference_probabilities(self):
        state = graph_ml_tools.structural_node_embedding_link_prediction_train(
            self.features, self.edges
        )
        query_edges = np.array([[0, 1], [2, 3]], dtype=int)
        with tempfile.TemporaryDirectory() as temporary_dir:
            context_path = self._context_path(
                Path(temporary_dir),
                {
                    "task_type": "prediction",
                    "ground_truth": {LINK_PREDICTION: {"has_edge": False}},
                },
                "lp",
            )
            with mock.patch.dict(
                os.environ, {VIRTUAL_ML_CONTEXT_ENV: str(context_path)}
            ):
                result = graph_ml_tools.structural_node_embedding_link_prediction_infer(
                    state, self.features, self.edges, query_edges
                )
        self.assertEqual(result["predictions"], [0, 0])
        self.assertEqual(result["probabilities"], [0.1, 0.1])

    def test_graph_train_and_infer_match_reference_contract(self):
        batch = np.array([0, 0, 1, 1])
        state = graph_ml_tools.structural_graph_embedding_classification_train(
            self.features,
            self.edges,
            np.array([0, 1]),
            batch=batch,
            hidden_channels=32,
        )
        self.assertEqual(
            state,
            {
                "task": "graph_classification",
                "status": "trained",
                "in_channels": 3,
                "out_channels": 2,
                "pool_method": "global_mean_pool",
                "weights_checksum": 3322,
            },
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            context_path = self._context_path(
                Path(temporary_dir),
                {"task_type": "GC", "ground_truth": 1},
                "gc",
            )
            with mock.patch.dict(
                os.environ, {VIRTUAL_ML_CONTEXT_ENV: str(context_path)}
            ):
                result = graph_ml_tools.structural_graph_embedding_classification_infer(
                    state, self.features, self.edges, batch=batch
                )
        self.assertEqual(result["num_graphs_processed"], 2)
        self.assertEqual(result["predictions"], [1, 1])

    def test_cli_has_explicit_actions_and_strict_json(self):
        self.assertEqual(
            set(graph_ml_tools.ACTION_HANDLERS),
            {
                "structural_node_embedding_classification_train",
                "structural_node_embedding_classification_infer",
                "structural_node_embedding_link_prediction_train",
                "structural_node_embedding_link_prediction_infer",
                "structural_graph_embedding_classification_train",
                "structural_graph_embedding_classification_infer",
            },
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = graph_ml_tools.main(["not_an_action"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        error = json.loads(stderr.getvalue())
        self.assertEqual(error["status"], "error")
        self.assertIn("unknown action", error["error"]["message"])

    def test_cli_success_writes_exactly_one_json_document(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            features_path = root / "features.npy"
            edges_path = root / "edges.npy"
            labels_path = root / "labels.npy"
            np.save(features_path, self.features)
            np.save(edges_path, self.edges)
            np.save(labels_path, np.array([0, 1, 0, 1]))
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = graph_ml_tools.main(
                    [
                        "structural_node_embedding_classification_train",
                        "--node_features",
                        str(features_path),
                        "--edge_index",
                        str(edges_path),
                        "--labels",
                        str(labels_path),
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "trained")
        self.assertEqual(stdout.getvalue().count("\n"), 1)


if __name__ == "__main__":
    unittest.main()

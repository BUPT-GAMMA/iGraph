from __future__ import annotations

import math
import unittest

import numpy as np
import torch
import torch.nn.functional as F

from graph_data_service.graph_ml.metrics import (
    binary_average_precision,
    binary_roc_auc,
    evaluate_graph_classification,
    graph_classification_selection_metric_name,
    masked_binary_cross_entropy_with_logits,
    multitask_average_precision,
)


class GraphClassificationMetricsTest(unittest.TestCase):
    def test_masked_bce_ignores_nan_and_preserves_gradient(self) -> None:
        logits = torch.tensor(
            [[0.0, 1.5], [math.log(3.0), -math.log(3.0)]],
            dtype=torch.float32,
            requires_grad=True,
        )
        targets = torch.tensor(
            [[0.0, torch.nan], [1.0, 0.0]],
            dtype=torch.float32,
        )

        result = masked_binary_cross_entropy_with_logits(logits, targets)
        expected = F.binary_cross_entropy_with_logits(
            logits[[0, 1, 1], [0, 0, 1]],
            torch.tensor([0.0, 1.0, 0.0]),
        )

        self.assertFalse(result.skipped)
        self.assertEqual(result.num_valid_labels, 3)
        self.assertEqual(result.num_masked_labels, 1)
        self.assertIsNotNone(result.loss)
        self.assertTrue(torch.allclose(result.loss, expected))
        result.loss.backward()
        self.assertEqual(float(logits.grad[0, 1]), 0.0)
        self.assertNotEqual(float(logits.grad[0, 0]), 0.0)

    def test_masked_bce_reports_all_missing_batch_without_nan_loss(self) -> None:
        logits = torch.zeros((2, 3), dtype=torch.float32, requires_grad=True)
        targets = torch.full((2, 3), torch.nan, dtype=torch.float32)

        result = masked_binary_cross_entropy_with_logits(logits, targets)

        self.assertTrue(result.skipped)
        self.assertIsNone(result.loss)
        self.assertEqual(result.num_valid_labels, 0)
        self.assertEqual(result.num_masked_labels, 6)

    def test_roc_auc_matches_hand_values_and_handles_ties(self) -> None:
        self.assertEqual(binary_roc_auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]), 1.0)
        self.assertEqual(binary_roc_auc([0, 0, 1, 1], [0.9, 0.8, 0.2, 0.1]), 0.0)
        self.assertAlmostEqual(
            binary_roc_auc([0, 1, 0, 1], [0.1, 0.4, 0.4, 0.8]),
            0.875,
        )
        self.assertEqual(
            binary_roc_auc([0, 1, np.nan], [0.1, 0.9, np.nan]),
            1.0,
        )
        self.assertIsNone(binary_roc_auc([1, 1], [0.2, 0.8]))
        self.assertIsNone(binary_roc_auc([0, 0], [0.2, 0.8]))

    def test_average_precision_matches_hand_values_and_tie_groups(self) -> None:
        self.assertAlmostEqual(
            binary_average_precision([1, 0, 1, 0], [0.9, 0.8, 0.7, 0.1]),
            5.0 / 6.0,
        )
        self.assertAlmostEqual(
            binary_average_precision([1, 0, 1, 0], [0.8, 0.8, 0.2, 0.1]),
            7.0 / 12.0,
        )
        self.assertIsNone(binary_average_precision([0, 0], [0.2, 0.8]))
        self.assertIsNone(binary_average_precision([1, 1], [0.2, 0.8]))

    def test_multitask_ap_macro_averages_tasks_instead_of_flattening(self) -> None:
        labels = np.asarray(
            [
                [1.0, 1.0, np.nan, 0.0, 1.0],
                [0.0, 0.0, np.nan, 0.0, 1.0],
                [1.0, np.nan, np.nan, 0.0, 1.0],
                [0.0, np.nan, np.nan, 0.0, 1.0],
            ]
        )
        scores = np.asarray(
            [
                [0.9, 0.6, np.nan, 0.4, 0.9],
                [0.8, 0.5, np.nan, 0.3, 0.8],
                [0.7, np.nan, np.nan, 0.2, 0.7],
                [0.1, np.nan, np.nan, 0.1, 0.6],
            ]
        )

        result = multitask_average_precision(labels, scores)
        flattened_ap = binary_average_precision(labels.reshape(-1), scores.reshape(-1))

        self.assertAlmostEqual(result["per_task_ap"][0], 5.0 / 6.0)
        self.assertEqual(result["per_task_ap"][1], 1.0)
        self.assertIsNone(result["per_task_ap"][2])
        self.assertIsNone(result["per_task_ap"][3])
        self.assertIsNone(result["per_task_ap"][4])
        self.assertAlmostEqual(result["ap"], 11.0 / 12.0)
        self.assertEqual(result["num_tasks"], 5)
        self.assertEqual(result["num_valid_tasks"], 2)
        self.assertEqual(result["num_skipped_tasks"], 3)
        self.assertEqual(
            [row["reason"] for row in result["skipped_tasks"]],
            ["all_labels_missing", "no_positive_labels", "no_negative_labels"],
        )
        self.assertIsNotNone(flattened_ap)
        self.assertNotAlmostEqual(result["ap"], flattened_ap)

    def test_metric_dispatch_reports_reasons_and_selection_names(self) -> None:
        valid_hiv = evaluate_graph_classification(
            "roc_auc",
            torch.tensor([[0.0], [1.0], [0.0], [1.0]]),
            torch.tensor([[0.1], [0.9], [0.2], [0.8]]),
        )
        invalid_hiv = evaluate_graph_classification(
            "roc_auc",
            [[1.0], [1.0]],
            [[0.1], [0.2]],
        )
        pcba = evaluate_graph_classification(
            "ap",
            [[0.0, 1.0], [1.0, 0.0]],
            [[0.1, 0.9], [0.8, 0.2]],
        )

        self.assertEqual(valid_hiv["roc_auc"], 1.0)
        self.assertTrue(valid_hiv["valid"])
        self.assertIsNone(valid_hiv["reason"])
        self.assertIsNone(invalid_hiv["roc_auc"])
        self.assertFalse(invalid_hiv["valid"])
        self.assertEqual(invalid_hiv["reason"], "no_negative_labels")
        self.assertEqual(pcba["ap"], 1.0)
        self.assertEqual(
            graph_classification_selection_metric_name("roc_auc"),
            "val_roc_auc",
        )
        self.assertEqual(
            graph_classification_selection_metric_name("ap"),
            "val_ap",
        )
        no_valid_pcba = multitask_average_precision(
            [[np.nan, 0.0], [np.nan, 0.0]],
            [[np.nan, 0.1], [np.nan, 0.2]],
        )
        self.assertFalse(no_valid_pcba["valid"])
        self.assertEqual(no_valid_pcba["reason"], "no_valid_tasks")

    def test_invalid_shapes_labels_and_scores_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "shape 不一致"):
            masked_binary_cross_entropy_with_logits(
                torch.zeros((2, 1)),
                torch.zeros((2, 2)),
            )
        with self.assertRaisesRegex(ValueError, "只能包含 0、1 或 NaN"):
            masked_binary_cross_entropy_with_logits(
                torch.zeros((1, 1)),
                torch.tensor([[2.0]]),
            )
        with self.assertRaisesRegex(ValueError, "有效标签位置"):
            binary_roc_auc([0, 1], [0.1, np.nan])
        with self.assertRaisesRegex(ValueError, "只能包含 0、1 或 NaN"):
            binary_average_precision([0, 2], [0.1, 0.9])
        with self.assertRaisesRegex(ValueError, "二维"):
            multitask_average_precision([0, 1], [0.1, 0.9])
        with self.assertRaisesRegex(ValueError, "仅支持 roc_auc 或 ap"):
            evaluate_graph_classification("accuracy", [0, 1], [0.1, 0.9])


if __name__ == "__main__":
    unittest.main()

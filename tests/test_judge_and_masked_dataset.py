from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

try:
    import torch
except ImportError:  # pragma: no cover - exercised only by dependency-light CI
    torch = None

from benchmark_platform.judge_protocol import execute_judge_command
from benchmark_platform.masked_dataset import (
    DatasetBinding,
    MaskedDatasetError,
    clear_masked_cache,
    materialize_task_dataset,
    resolve_masked_index,
)


def _judge_command(stdout_payload: str) -> list[str]:
    """Return an argv-only adapter that consumes stdin and emits exact bytes."""

    program = (
        "import json, sys; "
        "json.load(sys.stdin); "
        "sys.stdout.write(sys.argv[1])"
    )
    return [sys.executable, "-c", program, stdout_payload]


class JudgeProtocolTests(unittest.TestCase):
    def _execute(self, stdout_payload: str):
        with tempfile.TemporaryDirectory() as temporary_dir:
            return execute_judge_command(
                _judge_command(stdout_payload),
                {
                    "original_question": "Which result is correct?",
                    "ground_truth": {"private": "expected"},
                    "final_answer": {"answer": 1},
                },
                cwd=Path(temporary_dir),
                inherit_environment=True,
            )

    def test_accepts_binary_integer_scores(self) -> None:
        for score in (0, 1):
            with self.subTest(score=score):
                execution = self._execute(json.dumps({"score": score}))

                self.assertEqual(execution.status, "success")
                self.assertEqual(execution.score, score)
                self.assertIsInstance(execution.score, int)
                self.assertIsNone(execution.error)

    def test_rejects_non_binary_or_non_strict_json_output(self) -> None:
        invalid_outputs = {
            "boolean": '{"score": true}',
            "string": '{"score": "1"}',
            "out_of_range": '{"score": 2}',
            "non_finite": '{"score": NaN}',
            "duplicate_key": '{"score": 0, "score": 1}',
            "trailing_text": '{"score": 1}\nnot-json',
        }

        for case, stdout_payload in invalid_outputs.items():
            with self.subTest(case=case):
                execution = self._execute(stdout_payload)

                self.assertEqual(execution.status, "invalid_output")
                self.assertIsNone(execution.score)
                self.assertIsNotNone(execution.error)

    def test_default_result_does_not_retain_reason_or_output_body(self) -> None:
        private_reason = "PRIVATE_REASON_MUST_NOT_BE_PERSISTED"
        execution = self._execute(
            json.dumps({"score": 1, "reason": private_reason})
        )

        self.assertEqual(execution.status, "success")
        self.assertEqual(execution.score, 1)
        self.assertIsNone(execution.reason)
        self.assertTrue(execution.raw_summary["reason_present"])
        self.assertNotIn("stdout", execution.raw_summary)
        self.assertNotIn("stderr", execution.raw_summary)
        self.assertNotIn("body", execution.raw_summary)
        self.assertNotIn(
            private_reason,
            json.dumps(execution.to_dict(), ensure_ascii=False),
        )

    def test_timeout_error_does_not_echo_secret_command_arguments(self) -> None:
        secret = "SECRET_ARG_MUST_NOT_BE_PERSISTED"
        with tempfile.TemporaryDirectory() as temporary_dir:
            execution = execute_judge_command(
                [sys.executable, "-c", "import time; time.sleep(5)", secret],
                {
                    "original_question": "q",
                    "ground_truth": "g",
                    "final_answer": "a",
                    "rubric": "binary",
                },
                cwd=Path(temporary_dir),
                timeout_seconds=0.05,
                inherit_environment=True,
            )

        self.assertEqual(execution.status, "timeout")
        self.assertNotIn(secret, json.dumps(execution.to_dict()))


@unittest.skipUnless(torch is not None, "requires PyTorch")
class MaskedDatasetTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_masked_cache()
        self.temporary_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_dir.name)
        self.base_path = self.root / "base.pt"
        self.masked_path = self.root / "pure_ml_bench_masked_data.pt"

        self.base = {
            "num_nodes": 3,
            "x": torch.tensor(
                [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]],
                dtype=torch.float32,
            ),
            "edge_index": torch.tensor(
                [[0, 1], [1, 2]],
                dtype=torch.int64,
            ),
            "y": torch.tensor([9, 9, 9], dtype=torch.int64),
        }
        self.masked = {
            0: {
                "labels": torch.tensor([0, 0, 0], dtype=torch.int64),
                "edge_index": torch.tensor([[0], [2]], dtype=torch.int64),
            },
            1: {
                "labels": torch.tensor([2, 1, 0], dtype=torch.int64),
                "edge_index": torch.tensor(
                    [[0, 2], [2, 1]],
                    dtype=torch.int64,
                ),
            },
        }
        torch.save(self.base, self.base_path)
        torch.save(self.masked, self.masked_path)
        self.binding = DatasetBinding(
            base_path=self.base_path,
            masked_path=self.masked_path,
            category="ml",
            dataset="tiny",
        )

    def tearDown(self) -> None:
        clear_masked_cache()
        self.temporary_dir.cleanup()

    @staticmethod
    def _task(task_kind: str, source_index: int) -> dict:
        return {
            "composite_id": f"ml/tiny/task-{source_index}",
            "task_type": task_kind,
            "required_tools": [],
            "ground_truth": {},
            "context": {},
            "source_index": source_index,
        }

    def _load_materialized(self, path: Path):
        return torch.load(path, map_location="cpu", weights_only=True)

    def test_source_index_is_stable_and_nc_labels_replace_y(self) -> None:
        # The selected list position is intentionally absent: the original
        # source index must select entry 1 even after filtering or reordering.
        task = self._task("NC", source_index=1)
        self.assertEqual(resolve_masked_index(task), 1)

        materialized = materialize_task_dataset(
            self.binding,
            task,
            self.root / "attempts" / "nc.pt",
        )
        payload = self._load_materialized(materialized.path)

        self.assertEqual(materialized.masked_index, 1)
        self.assertEqual(materialized.task_kind, "NC")
        self.assertEqual(materialized.applied_field, "y")
        self.assertTrue(torch.equal(payload["y"], self.masked[1]["labels"]))
        self.assertTrue(
            torch.equal(payload["edge_index"], self.base["edge_index"])
        )

    def test_lp_edge_index_replaces_base_edges(self) -> None:
        materialized = materialize_task_dataset(
            self.binding,
            self._task("LP", source_index=1),
            self.root / "attempts" / "lp.pt",
        )
        payload = self._load_materialized(materialized.path)

        self.assertEqual(materialized.task_kind, "LP")
        self.assertEqual(materialized.applied_field, "edge_index")
        self.assertTrue(
            torch.equal(payload["edge_index"], self.masked[1]["edge_index"])
        )
        self.assertTrue(torch.equal(payload["y"], self.base["y"]))

    def test_gc_container_replaces_only_selected_graph_label(self) -> None:
        clear_masked_cache()
        graph_base = {
            "graphs": [
                {
                    "x": torch.tensor([1, 2]),
                    "edge_index": torch.tensor([[0], [1]]),
                    "y": torch.tensor([7]),
                },
                {
                    "x": torch.tensor([3, 4, 5]),
                    "edge_index": torch.tensor([[0, 1], [1, 2]]),
                    "y": torch.tensor([8]),
                },
            ],
            "texts": ["fixture"],
            "split": [[0], [1]],
        }
        masked = {
            0: {
                "labels": torch.tensor([2]),
                "edge_index": torch.tensor([[0, 1], [1, 2]]),
            }
        }
        torch.save(graph_base, self.base_path)
        torch.save(masked, self.masked_path)
        task = self._task("GC", source_index=0)
        task["context"] = {"graph_idx": 1}

        materialized = materialize_task_dataset(
            self.binding,
            task,
            self.root / "attempts" / "gc.pt",
        )
        payload = self._load_materialized(materialized.path)

        self.assertEqual(materialized.applied_field, "graphs[1].y")
        self.assertTrue(torch.equal(payload["graphs"][0]["y"], torch.tensor([7])))
        self.assertTrue(torch.equal(payload["graphs"][1]["y"], torch.tensor([2])))
        self.assertTrue(
            torch.equal(
                payload["graphs"][1]["edge_index"],
                graph_base["graphs"][1]["edge_index"],
            )
        )
        self.assertNotIn("y", payload)

    def _materialize_gc_fallback_task(
        self,
        *,
        context: dict,
        question: str = "Classify molecular compound Graph #0.",
        ground_truth_graph_idx: int | None = None,
    ):
        clear_masked_cache()
        graph_base = {
            "graphs": [
                {
                    "x": torch.tensor([1, 2]),
                    "edge_index": torch.tensor([[0], [1]]),
                    "y": torch.tensor([7]),
                },
                {
                    "x": torch.tensor([3, 4]),
                    "edge_index": torch.tensor([[0], [1]]),
                    "y": torch.tensor([8]),
                },
            ]
        }
        masked = {
            0: {
                "labels": torch.tensor([2]),
                "edge_index": torch.tensor([[0], [1]]),
            }
        }
        torch.save(graph_base, self.base_path)
        torch.save(masked, self.masked_path)
        task = self._task("GC", source_index=0)
        classification_truth: object = 2
        if ground_truth_graph_idx is not None:
            classification_truth = {
                "graph_idx": ground_truth_graph_idx,
                "prediction": 2,
            }
        task.update(
            {
                "task_type": "prediction",
                "question": question,
                "required_tools": ["structural_graph_embedding_classification"],
                "ground_truth": {
                    "structural_graph_embedding_classification": classification_truth,
                },
                "context": context,
            }
        )
        return materialize_task_dataset(
            self.binding,
            task,
            self.root / "attempts" / "prediction-gc.pt",
        )

    def test_gc_prediction_resolves_graph_index_from_anchor_descriptions(self) -> None:
        materialized = self._materialize_gc_fallback_task(
            context={
                "anchor_nodes": {
                    "node1_desc": "the atom with Node ID 3 in Graph #1",
                    "node2_desc": "the atom with Node ID 7 in graph 1",
                }
            },
            # Structured anchor metadata is authoritative when free text differs.
            question="Classify molecular compound Graph #0.",
        )
        payload = self._load_materialized(materialized.path)

        self.assertEqual(materialized.applied_field, "graphs[1].y")
        self.assertTrue(torch.equal(payload["graphs"][0]["y"], torch.tensor([7])))
        self.assertTrue(torch.equal(payload["graphs"][1]["y"], torch.tensor([2])))

    def test_gc_prediction_prefers_private_ground_truth_graph_index(self) -> None:
        materialized = self._materialize_gc_fallback_task(
            context={
                "anchor_nodes": {
                    "node1_desc": "the atom with Node ID 3 in Graph #0",
                    "node2_desc": "the atom with Node ID 7 in Graph #0",
                }
            },
            question="Classify molecular compound Graph #0.",
            ground_truth_graph_idx=1,
        )

        self.assertEqual(materialized.applied_field, "graphs[1].y")

    def test_gc_prediction_rejects_context_and_ground_truth_index_mismatch(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            MaskedDatasetError,
            "context graph index and structural graph-classification ground-truth "
            "index disagree",
        ):
            self._materialize_gc_fallback_task(
                context={"graph_idx": 0},
                ground_truth_graph_idx=1,
            )

    def test_gc_prediction_explicit_graph_index_has_priority(self) -> None:
        materialized = self._materialize_gc_fallback_task(
            context={
                "graph_idx": 0,
                "anchor_nodes": {
                    "node1_desc": "the atom with Node ID 3 in Graph #1",
                    "node2_desc": "the atom with Node ID 7 in Graph #1",
                },
            },
            question="Classify molecular compound Graph #1.",
        )

        self.assertEqual(materialized.applied_field, "graphs[0].y")

    def test_gc_prediction_uses_question_only_without_anchor_graph_index(self) -> None:
        materialized = self._materialize_gc_fallback_task(
            context={"anchor_nodes": {"node1": 3, "node2": 7}},
            question="Classify molecular compound Graph #1.",
        )

        self.assertEqual(materialized.applied_field, "graphs[1].y")

    def test_gc_prediction_rejects_conflicting_anchor_graph_indices(self) -> None:
        with self.assertRaisesRegex(
            MaskedDatasetError,
            "anchor_nodes descriptions contain conflicting graph indices",
        ):
            self._materialize_gc_fallback_task(
                context={
                    "anchor_nodes": {
                        "node1_desc": "the atom with Node ID 3 in Graph #0",
                        "node2_desc": "the atom with Node ID 7 in Graph #1",
                    }
                }
            )

    def test_gc_prediction_rejects_out_of_range_fallback_graph_index(self) -> None:
        with self.assertRaisesRegex(
            MaskedDatasetError,
            "context.graph_idx 9 is outside 0..1",
        ):
            self._materialize_gc_fallback_task(
                context={
                    "anchor_nodes": {
                        "node1_desc": "the atom with Node ID 3 in Graph #9",
                        "node2_desc": "the atom with Node ID 7 in Graph #9",
                    }
                }
            )

    def test_nc_preserves_label_alias_and_accepts_transposed_base_edges(self) -> None:
        clear_masked_cache()
        alias_base = {
            "node_feature": self.base["x"],
            "edge_index": torch.tensor(
                [[0, 1], [1, 2], [2, 0]],
                dtype=torch.int64,
            ),
            "label": self.base["y"],
        }
        torch.save(alias_base, self.base_path)

        materialized = materialize_task_dataset(
            self.binding,
            self._task("NC", source_index=1),
            self.root / "attempts" / "alias-nc.pt",
        )
        payload = self._load_materialized(materialized.path)

        self.assertTrue(torch.equal(payload["y"], self.masked[1]["labels"]))
        self.assertTrue(torch.equal(payload["label"], self.masked[1]["labels"]))
        self.assertEqual(tuple(payload["edge_index"].shape), (2, 3))
        self.assertTrue(
            torch.equal(
                payload["edge_index"],
                alias_base["edge_index"].transpose(0, 1),
            )
        )

    def test_masked_mapping_cache_hit_and_clear(self) -> None:
        first = materialize_task_dataset(
            self.binding,
            self._task("NC", source_index=0),
            self.root / "first.pt",
        )
        second = materialize_task_dataset(
            self.binding,
            self._task("NC", source_index=1),
            self.root / "second.pt",
        )

        self.assertFalse(first.masked_cache_hit)
        self.assertTrue(second.masked_cache_hit)
        self.assertEqual(clear_masked_cache(), 1)

        after_clear = materialize_task_dataset(
            self.binding,
            self._task("NC", source_index=0),
            self.root / "after-clear.pt",
        )
        self.assertFalse(after_clear.masked_cache_hit)

    def test_conflicting_source_and_context_indices_are_rejected(self) -> None:
        task = self._task("NC", source_index=1)
        task["context"] = {"task_index": 0}

        with self.assertRaisesRegex(
            MaskedDatasetError,
            "source_index and context.task_index disagree",
        ):
            resolve_masked_index(task)

    def test_missing_base_or_masked_checkpoint_is_rejected(self) -> None:
        bindings = {
            "base": DatasetBinding(
                base_path=self.root / "missing-base.pt",
                masked_path=self.masked_path,
                category="ml",
                dataset="tiny",
            ),
            "masked": DatasetBinding(
                base_path=self.base_path,
                masked_path=self.root / "missing-masked.pt",
                category="ml",
                dataset="tiny",
            ),
        }

        for missing, binding in bindings.items():
            with self.subTest(missing=missing):
                with self.assertRaises(FileNotFoundError):
                    materialize_task_dataset(
                        binding,
                        self._task("NC", source_index=0),
                        self.root / f"never-{missing}.pt",
                    )


if __name__ == "__main__":
    unittest.main()

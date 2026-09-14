from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import benchmark_graph_classification as runner


def _namespace(path: Path, **overrides) -> argparse.Namespace:
    values = {
        "dataset": "hiv",
        "path": path,
        "models": None,
        "epochs": 1,
        "batch_size": 256,
        "seed": 42,
        "hidden_dim": 8,
        "num_layers": 2,
        "device": "cpu",
        "expected_sha256": None,
        "allow_fingerprint_mismatch": False,
        "output_dir": path.parent / "outputs",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class BenchmarkRunnerHelperTest(unittest.TestCase):
    def test_dataset_specific_default_models(self) -> None:
        self.assertEqual(runner._default_models("hiv"), ["GCN", "GAT", "SAGE"])
        self.assertEqual(runner._default_models("pcba"), ["GCN"])

    def test_device_argument_accepts_supported_values(self) -> None:
        for value in ("auto", "cpu", "cuda", "cuda:0", "CUDA:12"):
            with self.subTest(value=value):
                self.assertEqual(runner._device_argument(value), value.lower())

    def test_device_argument_rejects_unknown_value(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            runner._device_argument("mps")

    def test_sha256_argument_normalizes_and_validates(self) -> None:
        value = "A" * 64
        self.assertEqual(runner._sha256_argument(value), value.lower())
        with self.assertRaises(argparse.ArgumentTypeError):
            runner._sha256_argument("not-a-sha256")

    def test_validation_requires_existing_pt_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.pt"
            with self.assertRaises(runner.BenchmarkFailure) as caught:
                runner._validate_arguments(_namespace(missing))
        self.assertEqual(caught.exception.exit_code, runner.EXIT_DATASET)
        self.assertEqual(caught.exception.stage, "input")

    def test_validation_rejects_single_layer_before_training(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset = Path(temp_dir) / "hiv.pt"
            dataset.touch()
            with self.assertRaises(runner.BenchmarkFailure) as caught:
                runner._validate_arguments(_namespace(dataset, num_layers=1))
        self.assertEqual(caught.exception.exit_code, runner.EXIT_USAGE)
        self.assertEqual(caught.exception.stage, "configuration")

    def test_git_state_degrades_when_git_metadata_is_unavailable(self) -> None:
        unavailable = subprocess.CompletedProcess(
            args=["git", "rev-parse", "HEAD"],
            returncode=128,
            stdout="",
            stderr="not a git repository",
        )
        with mock.patch.object(runner.subprocess, "run", return_value=unavailable):
            state = runner._git_state(Path("/source/archive"))
        self.assertEqual(
            state,
            {
                "available": False,
                "commit": None,
                "working_tree_dirty": None,
            },
        )

    def test_git_state_records_commit_and_dirty_flag(self) -> None:
        commit = subprocess.CompletedProcess(
            args=["git", "rev-parse", "HEAD"],
            returncode=0,
            stdout="abc123\n",
            stderr="",
        )
        status = subprocess.CompletedProcess(
            args=["git", "status", "--porcelain"],
            returncode=0,
            stdout=" M file.py\n",
            stderr="",
        )
        with mock.patch.object(runner.subprocess, "run", side_effect=[commit, status]):
            state = runner._git_state(Path("/repo"))
        self.assertEqual(
            state,
            {
                "available": True,
                "commit": "abc123",
                "working_tree_dirty": True,
            },
        )

    def test_run_id_and_result_path_are_stable_for_supplied_inputs(self) -> None:
        now = datetime(2026, 9, 7, 1, 2, 3, tzinfo=timezone.utc)
        run_id = runner._new_run_id("hiv", now=now, token="deadbeef")
        self.assertEqual(run_id, "hiv-20260907T010203Z-deadbeef")
        self.assertEqual(
            runner._result_path(Path("results"), run_id).name,
            "hiv-20260907T010203Z-deadbeef.json",
        )

    def test_atomic_json_write_leaves_only_complete_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nested" / "result.json"
            payload = {"status": "success", "value": "图"}
            runner._atomic_write_json(path, payload)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_atomic_json_write_rejects_non_finite_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "result.json"
            original = {"status": "failed", "value": 0.5}
            runner._atomic_write_json(path, original)

            with self.assertRaises(ValueError):
                runner._atomic_write_json(
                    path,
                    {"status": "success", "value": float("nan")},
                )

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_minimal_session_keeps_binding_in_memory(self) -> None:
        source = Path("/datasets/hiv.pt")
        session = runner._BenchmarkSession("benchmark-test", source)
        self.assertEqual(session.resolve_upload_by_name("hiv.pt").saved_path, str(source))
        self.assertIsNone(session.resolve_upload_by_name("other.pt"))
        session.bind_graph_ml_dataset(
            dataset_id="gds_123",
            source_filename="hiv.pt",
            fingerprint="abc",
            dataset_profile="hiv",
        )
        self.assertEqual(session.dataset_binding["dataset_id"], "gds_123")

    def test_official_fingerprint_is_required_unless_explicitly_overridden(self) -> None:
        args = _namespace(Path("hiv.pt"))
        dataset = {"fingerprint": "0" * 64}
        with self.assertRaises(runner.BenchmarkFailure) as caught:
            runner._verify_dataset_fingerprint(args, dataset)
        self.assertEqual(caught.exception.stage, "dataset_fingerprint")
        self.assertFalse(dataset["fingerprint_verified"])

        args.allow_fingerprint_mismatch = True
        runner._verify_dataset_fingerprint(args, dataset)
        self.assertTrue(dataset["fingerprint_mismatch_allowed"])

    def test_invalid_test_metric_cannot_be_reported_as_success(self) -> None:
        with self.assertRaises(runner.BenchmarkFailure):
            runner._verify_metrics("hiv", {"val_roc_auc": 0.5, "test_roc_auc": None})
        with self.assertRaises(runner.BenchmarkFailure):
            runner._verify_metrics("hiv", {"val_roc_auc": 1.01, "test_roc_auc": 0.5})
        with self.assertRaises(runner.BenchmarkFailure):
            runner._verify_metrics(
                "pcba",
                {
                    "val_ap": 0.1,
                    "test_ap": 0.1,
                    "val_valid_tasks": 1,
                    "test_valid_tasks": 0,
                },
            )

    def test_invalid_inference_probability_cannot_be_reported_as_success(self) -> None:
        architecture = {"success": True, "task": "graph_classification"}
        with self.assertRaises(runner.BenchmarkFailure):
            runner._verify_model_outputs(
                "hiv",
                "GCN",
                7,
                {
                    "success": True,
                    "returned_count": 1,
                    "predictions": [
                        {"graph_index": 7, "probability": float("nan")}
                    ],
                },
                architecture,
            )
        with self.assertRaises(runner.BenchmarkFailure):
            runner._verify_model_outputs(
                "pcba",
                "GCN",
                7,
                {
                    "success": True,
                    "returned_count": 1,
                    "predictions": [{"graph_index": 7, "top_predictions": []}],
                },
                architecture,
            )

    def test_main_persists_structured_failure_without_loading_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset = Path(temp_dir) / "hiv.pt"
            dataset.touch()
            output_dir = Path(temp_dir) / "results"
            failure = runner.BenchmarkFailure(
                runner.EXIT_DATASET,
                "dataset_load",
                "invalid fixture",
            )
            with (
                mock.patch.object(runner, "_execute", side_effect=failure),
                mock.patch.object(
                    runner,
                    "_git_state",
                    return_value={
                        "available": False,
                        "commit": None,
                        "working_tree_dirty": None,
                    },
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = runner.main(
                    ["hiv", str(dataset), "--output-dir", str(output_dir)]
                )
            self.assertEqual(exit_code, runner.EXIT_DATASET)
            result_files = list(output_dir.glob("*.json"))
            self.assertEqual(len(result_files), 1)
            result = json.loads(result_files[0].read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["stage"], "dataset_load")

    def test_main_returns_exit_six_and_safe_json_when_result_is_not_serializable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset = Path(temp_dir) / "hiv.pt"
            dataset.touch()
            output_dir = Path(temp_dir) / "results"
            stdout = io.StringIO()

            def inject_non_finite_value(
                _args: argparse.Namespace,
                result: dict[str, object],
            ) -> None:
                result["runs"] = [{"model": "GCN", "train_loss": float("nan")}]

            with (
                mock.patch.object(runner, "_execute", side_effect=inject_non_finite_value),
                mock.patch.object(
                    runner,
                    "_git_state",
                    return_value={
                        "available": False,
                        "commit": None,
                        "working_tree_dirty": None,
                    },
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = runner.main(
                    ["hiv", str(dataset), "--output-dir", str(output_dir)]
                )

            self.assertEqual(exit_code, runner.EXIT_OUTPUT)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["stage"], "output")
            self.assertIsNone(result["result_file"])
            self.assertEqual(list(output_dir.glob("*.json")), [])
            self.assertEqual(list(output_dir.glob("*.tmp")), [])

    def test_main_runs_synthetic_dataset_end_to_end(self) -> None:
        import torch

        from tests.fixtures.graph_classification import (
            make_graph_classification_payload,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset = root / "hiv-smoke.pt"
            payload = make_graph_classification_payload("hiv", num_graphs=18)
            payload["split"][0] = {
                "train": list(range(10)),
                "valid": list(range(10, 14)),
                "test": list(range(14, 18)),
            }
            torch.save(payload, dataset)
            output_dir = root / "results"
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = runner.main(
                    [
                        "hiv",
                        str(dataset),
                        "--models",
                        "GCN",
                        "--epochs",
                        "1",
                        "--batch-size",
                        "4",
                        "--hidden-dim",
                        "8",
                        "--num-layers",
                        "2",
                        "--device",
                        "cpu",
                        "--allow-fingerprint-mismatch",
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(exit_code, runner.EXIT_OK)
            result_files = list(output_dir.glob("*.json"))
            self.assertEqual(len(result_files), 1)
            result = json.loads(result_files[0].read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "success")
            self.assertFalse(result["dataset"]["fingerprint_verified"])
            self.assertEqual([item["model"] for item in result["runs"]], ["GCN"])
            self.assertEqual(result["runs"][0]["inference"]["returned_count"], 1)
            self.assertEqual(
                result["runs"][0]["architecture"]["task"],
                "graph_classification",
            )


if __name__ == "__main__":
    unittest.main()

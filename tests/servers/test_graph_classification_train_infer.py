from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import torch

from backend.logging.logger import close_service_loggers
from graph_data_service.graph_ml.artifact_store import ArtifactStore
from graph_data_service.graph_ml.infer import GraphMLInferencer
from graph_data_service.graph_ml.models import GraphClassifier
from graph_data_service.graph_ml.schemas import InferRequest, TrainRequest
from graph_data_service.graph_ml.trainer import GraphMLTrainer
from graph_data_service.graph_ml.trainer.trainer_graph_classification import (
    GraphClassificationTrainer,
)
from tests.fixtures.graph_classification import (
    make_trainable_graph_classification_bundle,
)


class _BoundDatasetStore:
    def __init__(self, bundle=None) -> None:
        self.bundle = bundle

    def current_dataset_id(self, _session_id: str):
        return None if self.bundle is None else self.bundle.dataset_id

    def resolve_for_session(self, _session_id: str, dataset_id=None):
        if self.bundle is None:
            raise ValueError(
                "当前会话内存中不存在图分类数据集，请重新调用 "
                "load_graph_classification_dataset"
            )
        if dataset_id and dataset_id != self.bundle.dataset_id:
            raise ValueError(f"dataset_id 未绑定到当前会话: {dataset_id}")
        return self.bundle


def _request(model: str, **train_config) -> TrainRequest:
    config = {
        "epochs": 1,
        "lr": 0.01,
        "batch_size": 4,
        **train_config,
    }
    return TrainRequest.model_validate(
        {
            "task": "graph_classification",
            "model": model,
            "train_config": config,
            "algo_config": {
                "device": "cpu",
                "node_embedding_dim": 8,
                "hidden_dim": 8,
                "embedding_dim": 8,
                "num_layers": 2,
                "dropout": 0.0,
            },
        }
    )


class GraphClassificationTrainingTest(unittest.TestCase):
    def test_all_backbones_train_without_loading_a_single_graph(self) -> None:
        bundle = make_trainable_graph_classification_bundle("hiv")
        graph_loader_calls: list[str] = []

        def forbidden_graph_loader(session_id: str):
            graph_loader_calls.append(session_id)
            raise AssertionError("graph classification must not load GraphData")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = ArtifactStore(temp_dir)
            trainer = GraphMLTrainer(
                forbidden_graph_loader,
                store,
                _BoundDatasetStore(bundle),
            )
            for backbone in ("GCN", "GAT", "SAGE"):
                progress = []
                with self.subTest(backbone=backbone):
                    result = trainer.train(
                        "session-train",
                        _request(backbone),
                        progress_callback=progress.append,
                    )
                    self.assertTrue(result["success"])
                    self.assertEqual(result["model"], backbone)
                    self.assertEqual(result["dataset_id"], bundle.dataset_id)
                    self.assertEqual(
                        result["metrics"]["selection_metric"],
                        "val_roc_auc",
                    )
                    self.assertEqual(progress[0]["kind"], "train_epoch")
                    self.assertIn("val_roc_auc", progress[0])

                    ref = store.resolve_model("session-train", result["model_id"])
                    self.assertIsNotNone(ref)
                    assert ref is not None
                    self.assertEqual(
                        {path.name for path in Path(ref.model_dir).iterdir()},
                        {
                            "checkpoint.pt",
                            "meta.json",
                            "metrics.json",
                            "per_task_metrics.json",
                        },
                    )
                    meta = store.load_json(ref.model_dir, "meta.json")
                    self.assertEqual(meta["dataset_fingerprint"], bundle.fingerprint)
                    self.assertEqual(meta["model_config"]["backbone_name"], backbone)
                    self.assertFalse(meta["uses_edge_features"])
                    self.assertIn("trained_at", meta)

        self.assertEqual(graph_loader_calls, [])

    def test_windows_invalid_session_key_is_safe_for_logs_and_artifacts(self) -> None:
        bundle = make_trainable_graph_classification_bundle("hiv")
        session_id = "admin:91ba0949"

        with tempfile.TemporaryDirectory() as artifact_root, tempfile.TemporaryDirectory() as log_root, patch.dict(
            os.environ,
            {"IGRAPH_LOGS_DIR": log_root},
            clear=False,
        ):
            store = ArtifactStore(artifact_root)
            trainer = GraphMLTrainer(
                lambda _session_id: (_ for _ in ()).throw(AssertionError()),
                store,
                _BoundDatasetStore(bundle),
            )
            try:
                result = trainer.train(session_id, _request("GCN"))
            finally:
                close_service_loggers(session_id)

            ref = store.resolve_model(session_id, result["model_id"])
            self.assertIsNotNone(ref)
            assert ref is not None
            self.assertTrue(result["success"])
            self.assertEqual(Path(ref.model_dir).parent.name, "admin_91ba0949")
            self.assertTrue(
                (Path(log_root) / "admin_91ba0949" / "graph_ml.log").exists()
            )

    def test_early_stop_finishes_selection_before_touching_test_split(self) -> None:
        bundle = make_trainable_graph_classification_bundle("hiv")
        progress: list[dict] = []
        captured: dict[str, object] = {}
        real_builder = __import__(
            "graph_data_service.graph_ml.trainer.trainer_graph_classification",
            fromlist=["build_graph_classification_dataloaders"],
        ).build_graph_classification_dataloaders
        original_evaluate = GraphClassificationTrainer._evaluate_split

        def recording_builder(*args, **kwargs):
            loaders = real_builder(*args, **kwargs)
            captured["test"] = loaders.test
            return loaders

        test_evaluation_calls = 0

        def recording_evaluate(model, loader, dataset, device):
            nonlocal test_evaluation_calls
            if loader is captured.get("test"):
                test_evaluation_calls += 1
                self.assertEqual(
                    len([event for event in progress if event["kind"] == "train_epoch"]),
                    2,
                )
            return original_evaluate(model, loader, dataset, device)

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "graph_data_service.graph_ml.trainer.trainer_graph_classification."
            "build_graph_classification_dataloaders",
            side_effect=recording_builder,
        ), patch.object(
            GraphClassificationTrainer,
            "_evaluate_split",
            side_effect=recording_evaluate,
        ):
            trainer = GraphMLTrainer(
                lambda _session_id: (_ for _ in ()).throw(AssertionError()),
                ArtifactStore(temp_dir),
                _BoundDatasetStore(bundle),
            )
            result = trainer.train(
                "session-early-stop",
                _request(
                    "GCN",
                    epochs=5,
                    early_stop_patience=1,
                    early_stop_min_delta=2.0,
                ),
                progress_callback=progress.append,
            )

        self.assertEqual(result["metrics"]["trained_epochs"], 2)
        self.assertEqual(result["metrics"]["best_epoch"], 1)
        self.assertTrue(result["metrics"]["stopped_early"])
        self.assertEqual(test_evaluation_calls, 1)

    def test_pcba_masks_nan_batches_and_selects_by_multitask_ap(self) -> None:
        bundle = make_trainable_graph_classification_bundle("pcba")
        bundle.graphs[bundle.split_indices["train"][0]].y.fill_(torch.nan)
        with tempfile.TemporaryDirectory() as temp_dir:
            trainer = GraphMLTrainer(
                lambda _session_id: (_ for _ in ()).throw(AssertionError()),
                ArtifactStore(temp_dir),
                _BoundDatasetStore(bundle),
            )
            result = trainer.train(
                "session-pcba",
                _request("GCN", batch_size=1),
            )

        metrics = result["metrics"]
        self.assertEqual(metrics["selection_metric"], "val_ap")
        self.assertEqual(metrics["val_valid_tasks"], 128)
        self.assertEqual(metrics["test_valid_tasks"], 128)
        self.assertEqual(metrics["skipped_train_batches"], 1)

    def test_fixed_seed_reproduces_metrics_and_checkpoint(self) -> None:
        bundle = make_trainable_graph_classification_bundle("hiv")
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_store = ArtifactStore(temp_dir)
            trainer = GraphMLTrainer(
                lambda _session_id: (_ for _ in ()).throw(AssertionError()),
                artifact_store,
                _BoundDatasetStore(bundle),
            )
            request = _request("GCN", epochs=2)
            first = trainer.train("session-reproducible", request)
            second = trainer.train("session-reproducible", request)
            first_ref = artifact_store.resolve_model(
                "session-reproducible",
                first["model_id"],
            )
            second_ref = artifact_store.resolve_model(
                "session-reproducible",
                second["model_id"],
            )
            assert first_ref is not None and second_ref is not None
            first_state = artifact_store.load_graph_classification_checkpoint(
                first_ref.model_dir
            )
            second_state = artifact_store.load_graph_classification_checkpoint(
                second_ref.model_dir
            )

        self.assertEqual(first["metrics"], second["metrics"])
        self.assertEqual(set(first_state), set(second_state))
        self.assertTrue(
            all(torch.equal(first_state[key], second_state[key]) for key in first_state)
        )


class GraphClassificationArtifactAndInferTest(unittest.TestCase):
    def test_dedicated_checkpoint_loader_is_safe_and_dimension_checked(self) -> None:
        model = GraphClassifier(
            "GCN",
            node_vocab_size=4,
            num_tasks=1,
            node_embedding_dim=8,
            hidden_dim=8,
            embedding_dim=8,
            dropout=0.0,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ArtifactStore(temp_dir)
            ref = store.create_model_dir("session-artifact", "graph_classification")
            store.save_graph_classification_checkpoint(ref.model_dir, model.state_dict())
            real_load = torch.load
            calls = []

            def recording_load(*args, **kwargs):
                calls.append(kwargs.copy())
                return real_load(*args, **kwargs)

            with patch.object(torch, "load", side_effect=recording_load):
                loaded = store.load_graph_classification_checkpoint(
                    ref.model_dir,
                    expected_state_dict=model.state_dict(),
                )
            self.assertEqual(calls[0]["weights_only"], True)
            self.assertEqual(calls[0]["map_location"], "cpu")
            self.assertTrue(all(not tensor.is_cuda for tensor in loaded.values()))

            first_key = next(iter(loaded))
            corrupted = dict(loaded)
            corrupted[first_key] = corrupted[first_key].reshape(-1)[:1]
            torch.save({"model": corrupted}, Path(ref.model_dir) / "checkpoint.pt")
            with self.assertRaisesRegex(ValueError, "参数维度"):
                store.load_graph_classification_checkpoint(
                    ref.model_dir,
                    expected_state_dict=model.state_dict(),
                )

            torch.save(
                {"model": model.state_dict(), "optimizer": {}},
                Path(ref.model_dir) / "checkpoint.pt",
            )
            with self.assertRaisesRegex(ValueError, "只能包含 model"):
                store.load_graph_classification_checkpoint(ref.model_dir)

            invalid_state = dict(model.state_dict())
            invalid_state[first_key] = "not-a-tensor"
            torch.save({"model": invalid_state}, Path(ref.model_dir) / "checkpoint.pt")
            with self.assertRaisesRegex(ValueError, "必须是 Tensor"):
                store.load_graph_classification_checkpoint(ref.model_dir)

            store.save_torch(ref.model_dir, "legacy.pt", {"value": torch.tensor(1)})
            calls.clear()
            with patch.object(torch, "load", side_effect=recording_load):
                store.load_torch(ref.model_dir, "legacy.pt")
            self.assertEqual(calls[0]["weights_only"], False)

    def test_hiv_split_default_pagination_and_graph_indices(self) -> None:
        bundle = make_trainable_graph_classification_bundle("hiv")
        dataset_store = _BoundDatasetStore(bundle)
        graph_loader_calls: list[str] = []

        def forbidden_graph_loader(session_id: str):
            graph_loader_calls.append(session_id)
            raise AssertionError("graph classification infer must not load GraphData")

        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_store = ArtifactStore(temp_dir)
            trainer = GraphMLTrainer(
                forbidden_graph_loader,
                artifact_store,
                dataset_store,
            )
            trained = trainer.train("session-infer", _request("GCN"))
            inferencer = GraphMLInferencer(
                forbidden_graph_loader,
                artifact_store,
                dataset_store,
            )
            default_result = inferencer.infer(
                "session-infer",
                InferRequest(
                    model_id=trained["model_id"],
                    infer_config={
                        "device": "cpu",
                        "batch_size": 3,
                        "result_limit": 2,
                    },
                ),
            )
            specified = inferencer.infer(
                "session-infer",
                InferRequest(
                    model_id=trained["model_id"],
                    query={"graph_indices": [3, 1]},
                    infer_config={"device": "cpu", "result_limit": 10},
                ),
            )

            self.assertEqual(
                default_result["evaluation"]["roc_auc"],
                trained["metrics"]["test_roc_auc"],
            )
            self.assertEqual(default_result["query"], {"split": "test"})
            self.assertEqual(default_result["returned_count"], 2)
            self.assertTrue(default_result["truncated"])
            self.assertEqual(
                [item["graph_index"] for item in specified["predictions"]],
                [3, 1],
            )
            self.assertEqual(specified["mode"], "specified_graphs")

            original_fingerprint = bundle.fingerprint
            bundle.fingerprint = "different-fingerprint"
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                inferencer.infer(
                    "session-infer",
                    InferRequest(
                        model_id=trained["model_id"],
                        infer_config={"device": "cpu"},
                    ),
                )
            bundle.fingerprint = original_fingerprint

            missing_inferencer = GraphMLInferencer(
                forbidden_graph_loader,
                artifact_store,
                _BoundDatasetStore(),
            )
            with self.assertRaisesRegex(ValueError, "请重新加载该图分类数据集"):
                missing_inferencer.infer(
                    "session-infer",
                    InferRequest(
                        model_id=trained["model_id"],
                        infer_config={"device": "cpu"},
                    ),
                )

        self.assertEqual(graph_loader_calls, [])

    def test_pcba_default_is_compact_and_full_arrays_are_opt_in(self) -> None:
        bundle = make_trainable_graph_classification_bundle("pcba")
        dataset_store = _BoundDatasetStore(bundle)
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_store = ArtifactStore(temp_dir)
            trainer = GraphMLTrainer(
                lambda _session_id: (_ for _ in ()).throw(AssertionError()),
                artifact_store,
                dataset_store,
            )
            trained = trainer.train("session-pcba-infer", _request("SAGE"))
            inferencer = GraphMLInferencer(
                lambda _session_id: (_ for _ in ()).throw(AssertionError()),
                artifact_store,
                dataset_store,
            )
            compact = inferencer.infer(
                "session-pcba-infer",
                InferRequest(
                    model_id=trained["model_id"],
                    query={"graph_indices": [0, 1]},
                    infer_config={
                        "device": "cpu",
                        "top_k_tasks": 3,
                        "result_limit": 2,
                    },
                ),
            )
            test_split = inferencer.infer(
                "session-pcba-infer",
                InferRequest(
                    model_id=trained["model_id"],
                    query={"split": "test"},
                    infer_config={"device": "cpu", "result_limit": 1},
                ),
            )
            full = inferencer.infer(
                "session-pcba-infer",
                InferRequest(
                    model_id=trained["model_id"],
                    query={"graph_indices": [0]},
                    infer_config={
                        "device": "cpu",
                        "return_all_task_probs": True,
                        "result_limit": 1,
                    },
                ),
            )

        self.assertEqual(len(compact["predictions"][0]["top_predictions"]), 3)
        self.assertNotIn("probabilities", compact["predictions"][0])
        self.assertEqual(
            test_split["evaluation"]["ap"],
            trained["metrics"]["test_ap"],
        )
        self.assertEqual(len(full["predictions"][0]["probabilities"]), 128)
        self.assertEqual(len(full["predictions"][0]["true_labels"]), 128)


if __name__ == "__main__":
    unittest.main()

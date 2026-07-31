"""Opt-in integration tests for the supplied HIV/PCBA graph datasets.

The large datasets are never discovered from the repository.  Set the documented
environment variables explicitly to enable the corresponding checks.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from graph_data_service.graph_ml.artifact_store import ArtifactStore
from graph_data_service.graph_ml.datasets import load_graph_classification_dataset
from graph_data_service.graph_ml.infer import GraphMLInferencer
from graph_data_service.graph_ml.schemas import InferRequest, TrainRequest
from graph_data_service.graph_ml.trainer import GraphMLTrainer


HIV_PATH_ENV = "GRAPH_ML_TEST_HIV_PT"
PCBA_PATH_ENV = "GRAPH_ML_TEST_PCBA_PT"
RUN_HIV_TRAINING_ENV = "GRAPH_ML_RUN_REAL_HIV_TRAINING"
RUN_PCBA_TRAINING_ENV = "GRAPH_ML_RUN_REAL_PCBA_TRAINING"


class _RegisteredUploadSession:
    def __init__(self, path: str) -> None:
        source_path = Path(path).expanduser().resolve()
        self.session_id = f"real-{source_path.stem}"
        self._record = SimpleNamespace(
            original_filename=source_path.name,
            saved_path=str(source_path),
        )

    def resolve_upload_by_name(self, filename: str):
        if filename == self._record.original_filename:
            return self._record
        return None


class _BoundDatasetStore:
    def __init__(self, bundle) -> None:
        self.bundle = bundle

    def current_dataset_id(self, _session_id: str) -> str:
        return self.bundle.dataset_id

    def resolve_for_session(self, _session_id: str, dataset_id=None):
        if dataset_id and dataset_id != self.bundle.dataset_id:
            raise ValueError(f"dataset_id 未绑定到当前会话: {dataset_id}")
        return self.bundle


def _enabled(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def _load_real_bundle(path: str, profile: str):
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise AssertionError(f"真实数据文件不存在: {source_path}")
    session = _RegisteredUploadSession(str(source_path))
    return load_graph_classification_dataset(
        session,
        source_path.name,
        profile,
    )


def _request(model: str, *, batch_size: int) -> TrainRequest:
    return TrainRequest.model_validate(
        {
            "task": "graph_classification",
            "model": model,
            "train_config": {
                "epochs": 1,
                "lr": 0.001,
                "weight_decay": 0.0,
                "seed": 42,
                "early_stop_patience": 0,
                "batch_size": batch_size,
                "num_workers": 0,
            },
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


class RealGraphClassificationDatasetTest(unittest.TestCase):
    @unittest.skipUnless(os.getenv(HIV_PATH_ENV), f"需要设置 {HIV_PATH_ENV}")
    def test_hiv_full_loader_statistics(self) -> None:
        bundle = _load_real_bundle(os.environ[HIV_PATH_ENV], "hiv")
        self.assertEqual(bundle.name, "hiv")
        self.assertEqual(len(bundle.graphs), 41_127)
        self.assertEqual(bundle.num_tasks, 1)
        self.assertEqual(bundle.metric, "roc_auc")
        self.assertEqual(
            bundle.fingerprint,
            "680ccc639603c2230e4e61fb2e584cecb66c38b482a467be9227d9edd115b241",
        )
        self.assertEqual(bundle.node_vocab_size, 382)
        self.assertEqual(bundle.edge_vocab_size, 8)
        self.assertEqual(
            {name: len(indices) for name, indices in bundle.split_indices.items()},
            {"train": 32_901, "val": 4_113, "test": 4_113},
        )
        self.assertFalse(bundle.metadata["has_missing_labels"])
        self.assertEqual(bundle.metadata["num_missing_labels"], 0)
        self.assertEqual(bundle.metadata["num_total_labels"], 41_127)

    @unittest.skipUnless(os.getenv(PCBA_PATH_ENV), f"需要设置 {PCBA_PATH_ENV}")
    def test_pcba_full_loader_statistics(self) -> None:
        bundle = _load_real_bundle(os.environ[PCBA_PATH_ENV], "pcba")
        self.assertEqual(bundle.name, "pcba")
        self.assertEqual(len(bundle.graphs), 437_092)
        self.assertEqual(bundle.num_tasks, 128)
        self.assertEqual(bundle.metric, "ap")
        self.assertEqual(
            bundle.fingerprint,
            "d863bfd9072ff6379392d9ed4c741cf258e45a599d19d2a1c057f134eac2b8a8",
        )
        self.assertEqual(bundle.node_vocab_size, 232)
        self.assertEqual(bundle.edge_vocab_size, 11)
        self.assertEqual(
            {name: len(indices) for name, indices in bundle.split_indices.items()},
            {"train": 349_854, "val": 43_650, "test": 43_588},
        )
        self.assertEqual(bundle.metadata["num_edgeless_graphs"], 11)
        self.assertTrue(bundle.metadata["has_missing_labels"])
        self.assertEqual(bundle.metadata["num_total_labels"], 55_947_776)
        self.assertEqual(bundle.metadata["num_missing_labels"], 21_930_606)
        self.assertAlmostEqual(
            bundle.metadata["missing_label_rate"],
            0.3919835169140593,
        )


class RealGraphClassificationTrainingTest(unittest.TestCase):
    @unittest.skipUnless(
        os.getenv(HIV_PATH_ENV) and _enabled(RUN_HIV_TRAINING_ENV),
        f"需要设置 {HIV_PATH_ENV} 和 {RUN_HIV_TRAINING_ENV}=1",
    )
    def test_hiv_all_backbones_one_epoch_and_infer(self) -> None:
        bundle = _load_real_bundle(os.environ[HIV_PATH_ENV], "hiv")
        dataset_store = _BoundDatasetStore(bundle)
        with tempfile.TemporaryDirectory() as artifact_root:
            artifact_store = ArtifactStore(artifact_root)
            trainer = GraphMLTrainer(
                lambda _session_id: (_ for _ in ()).throw(AssertionError()),
                artifact_store,
                dataset_store,
            )
            inferencer = GraphMLInferencer(
                lambda _session_id: (_ for _ in ()).throw(AssertionError()),
                artifact_store,
                dataset_store,
            )
            for backbone in ("GCN", "GAT", "SAGE"):
                with self.subTest(backbone=backbone):
                    result = trainer.train(
                        "real-hiv-training",
                        _request(backbone, batch_size=256),
                    )
                    self.assertIsInstance(result["metrics"]["val_roc_auc"], float)
                    self.assertIsInstance(result["metrics"]["test_roc_auc"], float)
                    prediction = inferencer.infer(
                        "real-hiv-training",
                        InferRequest(
                            model_id=result["model_id"],
                            query={"graph_indices": [bundle.split_indices["test"][0]]},
                            infer_config={"device": "cpu", "result_limit": 1},
                        ),
                    )
                    self.assertEqual(prediction["returned_count"], 1)

    @unittest.skipUnless(
        os.getenv(PCBA_PATH_ENV) and _enabled(RUN_PCBA_TRAINING_ENV),
        f"需要设置 {PCBA_PATH_ENV} 和 {RUN_PCBA_TRAINING_ENV}=1",
    )
    def test_pcba_gcn_one_full_epoch_and_infer(self) -> None:
        bundle = _load_real_bundle(os.environ[PCBA_PATH_ENV], "pcba")
        dataset_store = _BoundDatasetStore(bundle)
        with tempfile.TemporaryDirectory() as artifact_root:
            artifact_store = ArtifactStore(artifact_root)
            trainer = GraphMLTrainer(
                lambda _session_id: (_ for _ in ()).throw(AssertionError()),
                artifact_store,
                dataset_store,
            )
            result = trainer.train(
                "real-pcba-training",
                _request("GCN", batch_size=256),
            )
            self.assertIsInstance(result["metrics"]["val_ap"], float)
            self.assertIsInstance(result["metrics"]["test_ap"], float)
            self.assertGreater(result["metrics"]["val_valid_tasks"], 0)
            self.assertGreater(result["metrics"]["test_valid_tasks"], 0)
            inferencer = GraphMLInferencer(
                lambda _session_id: (_ for _ in ()).throw(AssertionError()),
                artifact_store,
                dataset_store,
            )
            prediction = inferencer.infer(
                "real-pcba-training",
                InferRequest(
                    model_id=result["model_id"],
                    query={"graph_indices": [bundle.split_indices["test"][0]]},
                    infer_config={
                        "device": "cpu",
                        "result_limit": 1,
                        "top_k_tasks": 5,
                    },
                ),
            )
            self.assertEqual(prediction["returned_count"], 1)
            self.assertEqual(len(prediction["predictions"][0]["top_predictions"]), 5)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest

import numpy as np

from graph_data_service.graph_data import GraphData
from graph_data_service.graph_ml.artifact_store import ArtifactStore
from graph_data_service.graph_ml.infer import GraphMLInferencer
from graph_data_service.graph_ml.schemas import InferRequest, TrainRequest
from graph_data_service.graph_ml.trainer import GraphMLTrainer


def _legacy_graph() -> GraphData:
    names = [f"n{index}" for index in range(8)]
    return GraphData(
        num_nodes=8,
        edge_index=np.asarray(
            [
                [0, 1, 2, 3, 4, 5, 6, 7, 0, 2, 4, 6],
                [1, 2, 3, 4, 5, 6, 7, 0, 2, 4, 6, 0],
            ],
            dtype=np.int64,
        ),
        id2node={index: name for index, name in enumerate(names)},
        node2id={name: index for index, name in enumerate(names)},
        node_feat=np.asarray(
            [[index / 8.0, (index % 2), 1.0] for index in range(8)],
            dtype=np.float32,
        ),
        node_label=np.asarray([0, 1, 0, 1, 0, 1, 0, 1], dtype=np.int64),
        train_mask=np.asarray([1, 1, 1, 1, 0, 0, 0, 0], dtype=bool),
        val_mask=np.asarray([0, 0, 0, 0, 1, 1, 0, 0], dtype=bool),
        test_mask=np.asarray([0, 0, 0, 0, 0, 0, 1, 1], dtype=bool),
    )


def _request(task: str, *, model: str = "GCN", method: str | None = None):
    return TrainRequest.model_validate(
        {
            "task": task,
            "model": model,
            "method": method,
            "train_config": {"epochs": 1, "lr": 0.01, "seed": 42},
            "algo_config": {
                "device": "cpu",
                "hidden_dim": 8,
                "embedding_dim": 8,
                "num_layers": 2,
                "dropout": 0.0,
            },
        }
    )


class LegacyGraphMLRegressionTest(unittest.TestCase):
    def test_node_link_and_pretrain_train_infer_remain_compatible(self) -> None:
        graph = _legacy_graph()
        graph_loads: list[str] = []

        def graph_loader(session_id: str):
            graph_loads.append(session_id)
            return 7, graph

        with tempfile.TemporaryDirectory() as artifact_root:
            store = ArtifactStore(artifact_root)
            trainer = GraphMLTrainer(graph_loader, store)
            inferencer = GraphMLInferencer(graph_loader, store)

            node = trainer.train(
                "legacy-regression",
                _request("node_classification"),
            )
            node_infer = inferencer.infer(
                "legacy-regression",
                InferRequest(
                    model_id=node["model_id"],
                    graph_version=7,
                    query={"nodes": ["n0", "n1"]},
                    infer_config={"device": "cpu", "result_limit": 2},
                ),
            )
            self.assertEqual(node_infer["task"], "node_classification")
            self.assertEqual(node_infer["returned_count"], 2)

            link = trainer.train(
                "legacy-regression",
                _request("link_prediction", model="SAGE"),
            )
            link_infer = inferencer.infer(
                "legacy-regression",
                InferRequest(
                    model_id=link["model_id"],
                    graph_version=7,
                    query={"edges": [["n0", "n3"]]},
                    infer_config={"device": "cpu"},
                ),
            )
            self.assertEqual(link_infer["task"], "link_prediction")
            self.assertEqual(link_infer["count"], 1)

            pretrain = trainer.train(
                "legacy-regression",
                _request(
                    "pretrain",
                    model="GCN",
                    method="mask_autoencoding",
                ),
            )
            pretrain_infer = inferencer.infer(
                "legacy-regression",
                InferRequest(
                    model_id=pretrain["model_id"],
                    graph_version=7,
                    query={"nodes": ["n0"]},
                ),
            )
            self.assertEqual(pretrain_infer["task"], "pretrain")
            self.assertEqual(pretrain_infer["count"], 1)
            self.assertGreaterEqual(len(graph_loads), 5)


if __name__ == "__main__":
    unittest.main()

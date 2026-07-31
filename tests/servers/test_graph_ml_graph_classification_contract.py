from __future__ import annotations

import tempfile
import unittest

from pydantic import ValidationError

from graph_data_service.graph_ml.module import GraphMLModule
from graph_data_service.graph_ml.schemas import (
    LoadGraphClassificationDatasetRequest,
    TrainRequest,
)
from graph_data_service.graph_ml.tasks import validate_train_request
from graph_data_service.tool_catalog import GRAPH_ML_TOOLS
from tests.fixtures.graph_classification import (
    make_trainable_graph_classification_bundle,
)


class GraphClassificationContractTest(unittest.TestCase):
    def test_train_schema_accepts_graph_classification_and_new_defaults(self) -> None:
        req = TrainRequest.model_validate(
            {
                "task": "graph_classification",
                "model": "GCN",
                "data": {"dataset_id": "gds_ab12cd34"},
            }
        )

        self.assertEqual(req.task, "graph_classification")
        self.assertEqual(req.data.dataset_id, "gds_ab12cd34")
        self.assertEqual(req.train_config.batch_size, 64)
        self.assertEqual(req.train_config.num_workers, 0)
        self.assertNotIn("split", req.train_config.model_fields_set)
        self.assertEqual(validate_train_request(req), "gds_ab12cd34")

    def test_existing_task_contracts_remain_valid(self) -> None:
        requests = [
            TrainRequest(task="node_classification", model="GCN"),
            TrainRequest(task="link_prediction", model="SAGE"),
            TrainRequest(task="pretrain", method="node2vec"),
        ]

        for req in requests:
            with self.subTest(task=req.task):
                self.assertIsNone(validate_train_request(req))

    def test_graph_classification_requires_model(self) -> None:
        req = TrainRequest.model_validate(
            {
                "task": "graph_classification",
                "data": {"dataset_id": "gds_ab12cd34"},
            }
        )

        with self.assertRaisesRegex(ValueError, "model 必填"):
            validate_train_request(req)

    def test_graph_classification_requires_dataset_or_session_binding(self) -> None:
        req = TrainRequest(task="graph_classification", model="GAT")

        with self.assertRaisesRegex(ValueError, "load_graph_classification_dataset"):
            validate_train_request(req)

        self.assertEqual(
            validate_train_request(
                req,
                bound_graph_classification_dataset_id="gds_bound",
            ),
            "gds_bound",
        )

    def test_graph_classification_rejects_cross_task_fields(self) -> None:
        invalid_payloads = [
            ({"graph_version": None}, "graph_version"),
            ({"feature_source": "embedding"}, "feature_source=embedding"),
            ({"split": {"train": 0.8, "val": 0.2}}, "自定义 train_config.split"),
            ({"method": "node2vec"}, "不接受 method"),
            ({"upstream_model_id": "mdl_old"}, "不接受 upstream_model_id"),
            ({"target_nodes": []}, "data.target_nodes"),
            ({"target_edges": []}, "data.target_edges"),
        ]

        for extra, message in invalid_payloads:
            payload = {
                "task": "graph_classification",
                "model": "GCN",
                "data": {"dataset_id": "gds_ab12cd34"},
            }
            if "feature_source" in extra:
                payload["data"]["feature_source"] = extra["feature_source"]
            elif "target_nodes" in extra:
                payload["data"]["target_nodes"] = extra["target_nodes"]
            elif "target_edges" in extra:
                payload["data"]["target_edges"] = extra["target_edges"]
            elif "split" in extra:
                payload["train_config"] = {"split": extra["split"]}
            else:
                payload.update(extra)

            with self.subTest(field=next(iter(extra))):
                req = TrainRequest.model_validate(payload)
                with self.assertRaisesRegex(ValueError, message):
                    validate_train_request(req)

    def test_dataset_id_is_graph_classification_only(self) -> None:
        req = TrainRequest.model_validate(
            {
                "task": "node_classification",
                "model": "GCN",
                "data": {"dataset_id": "gds_wrong_task"},
            }
        )

        with self.assertRaisesRegex(ValueError, "仅用于 task=graph_classification"):
            validate_train_request(req)

    def test_train_data_rejects_loader_only_fields(self) -> None:
        with self.assertRaises(ValidationError):
            TrainRequest.model_validate(
                {
                    "task": "graph_classification",
                    "model": "GCN",
                    "data": {
                        "dataset_id": "gds_ab12cd34",
                        "filename": "hiv.pt",
                    },
                }
            )

    def test_dataset_load_schema_normalizes_filename_and_rejects_unknown_profile(self) -> None:
        req = LoadGraphClassificationDatasetRequest.model_validate(
            {"filename": "  hiv.pt  "}
        )
        self.assertEqual(req.filename, "hiv.pt")
        self.assertEqual(req.dataset_profile, "auto")

        with self.assertRaises(ValidationError):
            LoadGraphClassificationDatasetRequest.model_validate(
                {"filename": "hiv.pt", "dataset_profile": "unknown"}
            )

    def test_graph_classification_contract_does_not_load_single_graph(self) -> None:
        get_session_calls: list[str] = []
        bundle = make_trainable_graph_classification_bundle("hiv")

        class BoundDatasetStore:
            @staticmethod
            def current_dataset_id(_session_id: str) -> str:
                return bundle.dataset_id

            @staticmethod
            def resolve_for_session(_session_id: str, dataset_id: str):
                if dataset_id != bundle.dataset_id:
                    raise AssertionError(dataset_id)
                return bundle

        def get_session(session_id: str):
            get_session_calls.append(session_id)
            raise AssertionError("graph classification must not load GraphData")

        with tempfile.TemporaryDirectory() as td:
            module = GraphMLModule(
                get_session=get_session,
                artifact_root=td,
                dataset_store=BoundDatasetStore(),
            )
            result = module.train(
                "session-graph-classification",
                {
                    "task": "graph_classification",
                    "model": "SAGE",
                    "train_config": {"epochs": 1, "batch_size": 4, "lr": 0.01},
                    "algo_config": {
                        "device": "cpu",
                        "node_embedding_dim": 8,
                        "hidden_dim": 8,
                        "embedding_dim": 8,
                        "dropout": 0.0,
                    },
                },
            )
            visualization = module.visualize_model_architecture(
                "session-graph-classification",
                {"model_id": result["model_id"]},
            )

        self.assertEqual(get_session_calls, [])
        self.assertTrue(result["success"])
        self.assertEqual(visualization["task"], "graph_classification")
        self.assertEqual(visualization["architecture"]["pooling"], "global_mean")

    def test_tool_catalog_exposes_graph_classification_contract(self) -> None:
        by_name = {tool["name"]: tool for tool in GRAPH_ML_TOOLS}
        self.assertIn("load_graph_classification_dataset", by_name)

        load_schema = by_name["load_graph_classification_dataset"]["inputSchema"]
        self.assertEqual(load_schema["required"], ["filename"])
        self.assertEqual(
            load_schema["properties"]["dataset_profile"]["enum"],
            ["auto", "hiv", "pcba"],
        )

        train_props = by_name["train"]["inputSchema"]["properties"]
        self.assertIn("graph_classification", train_props["task"]["enum"])
        self.assertIn("dataset_id", train_props["data"]["properties"])
        self.assertEqual(train_props["train_config"]["properties"]["batch_size"]["default"], 64)
        self.assertEqual(train_props["train_config"]["properties"]["num_workers"]["default"], 0)
        self.assertEqual(train_props["algo_config"]["properties"]["node_embedding_dim"]["default"], 64)

        infer_props = by_name["infer"]["inputSchema"]["properties"]
        self.assertIn("graph_indices", infer_props["query"]["properties"])
        self.assertEqual(infer_props["infer_config"]["properties"]["batch_size"]["default"], 128)
        self.assertIn("top_k_tasks", infer_props["infer_config"]["properties"])
        self.assertIn("return_all_task_probs", infer_props["infer_config"]["properties"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from graph_data_service.tool_catalog import GRAPH_ML_TOOLS
from graph_data_service.graph_ml.config_catalog import model_architecture_view


class GraphMLConfigCatalogTest(unittest.TestCase):
    def test_visualize_pretrain_node2vec(self) -> None:
        payload = model_architecture_view(
            task="pretrain",
            model=None,
            method="node2vec",
            algo_config={"embedding_dim": 64},
        )
        self.assertTrue(payload["success"])
        self.assertEqual(payload["method"], "node2vec")
        self.assertIn("flowchart TD", payload["mermaid"])

    def test_visualize_gnn_architecture_includes_residual_and_norm(self) -> None:
        payload = model_architecture_view(
            task="link_prediction",
            model="SAGE",
            method=None,
            algo_config={
                "use_residual_connection": True,
                "use_normalization": True,
                "norm_pos": "pre",
                "norm_type": "rms_norm",
            },
        )
        arch = payload["architecture"]

        self.assertTrue(payload["success"])
        self.assertTrue(arch["use_residual_connection"])
        self.assertTrue(arch["use_normalization"])
        self.assertEqual(arch["norm_pos"], "pre")
        self.assertEqual(arch["norm_type"], "rms_norm")
        self.assertIn("rms_norm@pre", payload["mermaid"])

    def test_graph_ml_tool_schemas_include_new_gnn_options(self) -> None:
        by_name = {tool["name"]: tool for tool in GRAPH_ML_TOOLS}
        self.assertNotIn("get_train_config", by_name)
        self.assertNotIn("get_infer_config", by_name)
        train_algo_schema = by_name["train"]["inputSchema"]["properties"]["algo_config"]
        train_props = train_algo_schema["properties"]

        self.assertEqual(train_props["use_residual_connection"]["type"], "boolean")
        self.assertEqual(train_props["use_normalization"]["type"], "boolean")
        self.assertEqual(train_props["norm_pos"]["enum"], ["pre", "post"])
        self.assertEqual(
            train_props["norm_type"]["enum"],
            ["rms_norm", "layer_norm", "batch_norm"],
        )
        train_props_root = by_name["train"]["inputSchema"]["properties"]
        self.assertIn("feature_source", train_props_root["data"]["properties"])
        self.assertIn("epochs", train_props_root["train_config"]["properties"])

        infer_props = by_name["infer"]["inputSchema"]["properties"]
        self.assertIn("nodes", infer_props["query"]["properties"])
        self.assertIn("allow_version_mismatch", infer_props["infer_config"]["properties"])

        visualize_schema = by_name["visualize_model_architecture"]["inputSchema"]
        self.assertEqual(set(visualize_schema["properties"].keys()), {"model_id"})
        self.assertEqual(visualize_schema["required"], ["model_id"])


if __name__ == "__main__":
    unittest.main()

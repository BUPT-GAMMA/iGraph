from __future__ import annotations

import io
import unittest

import torch
from torch import nn
from torch_geometric.data import Batch, Data

from graph_data_service.graph_ml.models import (
    GraphClassifier,
    build_graph_classifier,
)
from tests.fixtures.graph_classification import make_graph_classification_payload


def _make_batch(
    profile: str,
    *,
    num_graphs: int = 4,
    edgeless_only: bool = False,
) -> Batch:
    payload = make_graph_classification_payload(
        profile,
        num_graphs=num_graphs,
        include_edgeless=True,
    )
    num_tasks = 1 if profile == "hiv" else 128
    for graph in payload["graphs"]:
        if edgeless_only:
            graph.edge_index = torch.empty((2, 0), dtype=torch.long)
            graph.xe = torch.empty((0,), dtype=torch.long)
        graph.x = graph.x.to(dtype=torch.long)
        graph.xe = graph.xe.to(dtype=torch.long)
        graph.edge_index = graph.edge_index.to(dtype=torch.long)
        graph.y = graph.y.to(dtype=torch.float32).reshape(1, num_tasks)
    return Batch.from_data_list(payload["graphs"])


class _PassThroughBackbone(nn.Module):
    def forward(
        self,
        node_features: torch.Tensor,
        _edge_index: torch.Tensor,
    ) -> torch.Tensor:
        return node_features


class GraphClassifierTest(unittest.TestCase):
    def test_all_backbones_emit_hiv_and_pcba_graph_logits(self) -> None:
        for profile, num_tasks in (("hiv", 1), ("pcba", 128)):
            batch = _make_batch(profile)
            for backbone_name in ("GCN", "GAT", "SAGE"):
                with self.subTest(profile=profile, backbone=backbone_name):
                    model = GraphClassifier(
                        backbone_name,
                        node_vocab_size=4,
                        num_tasks=num_tasks,
                        node_embedding_dim=8,
                        hidden_dim=12,
                        embedding_dim=10,
                        num_layers=2,
                        dropout=0.0,
                        gat_heads=2,
                    )
                    model.eval()
                    logits = model(batch)

                    self.assertEqual(tuple(logits.shape), (4, num_tasks))
                    self.assertTrue(torch.is_floating_point(logits))
                    self.assertTrue(logits.requires_grad)
                    self.assertEqual(model.head.in_features, 10)
                    self.assertEqual(model.head.out_features, num_tasks)

    def test_global_mean_pooling_is_fixed_and_numerically_correct(self) -> None:
        graphs = [
            Data(
                x=torch.tensor([0, 1], dtype=torch.long),
                edge_index=torch.empty((2, 0), dtype=torch.long),
                xe=torch.empty((0,), dtype=torch.long),
            ),
            Data(
                x=torch.tensor([2], dtype=torch.long),
                edge_index=torch.empty((2, 0), dtype=torch.long),
                xe=torch.empty((0,), dtype=torch.long),
            ),
        ]
        batch = Batch.from_data_list(graphs)
        model = GraphClassifier(
            "GCN",
            node_vocab_size=3,
            num_tasks=2,
            node_embedding_dim=2,
            hidden_dim=2,
            embedding_dim=2,
            dropout=0.0,
        )
        model.backbone = _PassThroughBackbone()
        with torch.no_grad():
            model.node_encoder.embedding.weight.copy_(
                torch.tensor([[1.0, 3.0], [3.0, 5.0], [10.0, 20.0]])
            )
            model.head.weight.copy_(torch.eye(2))
            model.head.bias.zero_()

        logits = model(batch)
        self.assertTrue(
            torch.equal(
                logits,
                torch.tensor([[2.0, 4.0], [10.0, 20.0]]),
            )
        )
        self.assertGreater(float(logits.detach().max()), 1.0)
        self.assertEqual(model.pooling, "global_mean")

        single_graph_logits = model(Batch.from_data_list([graphs[0]]))
        self.assertEqual(tuple(single_graph_logits.shape), (1, 2))
        self.assertTrue(
            torch.equal(single_graph_logits, torch.tensor([[2.0, 4.0]]))
        )

    def test_edgeless_graphs_work_and_edge_categories_are_not_consumed(self) -> None:
        edgeless_batch = _make_batch("hiv", edgeless_only=True)
        for backbone_name in ("GCN", "GAT", "SAGE"):
            with self.subTest(backbone=backbone_name):
                model = GraphClassifier(
                    backbone_name,
                    node_vocab_size=4,
                    num_tasks=1,
                    node_embedding_dim=8,
                    hidden_dim=8,
                    embedding_dim=8,
                    dropout=0.0,
                )
                model.eval()
                self.assertEqual(tuple(model(edgeless_batch).shape), (4, 1))

        batch = _make_batch("hiv")
        changed_edge_categories = batch.clone()
        changed_edge_categories.xe = (changed_edge_categories.xe + 1) % 3
        model = GraphClassifier(
            "GCN",
            node_vocab_size=4,
            num_tasks=1,
            node_embedding_dim=8,
            hidden_dim=8,
            embedding_dim=8,
            dropout=0.0,
        )
        model.eval()
        self.assertTrue(
            torch.equal(model(batch), model(changed_edge_categories))
        )
        self.assertFalse(model.uses_edge_features)

    def test_builder_preserves_dropout_residual_and_normalization_options(self) -> None:
        model = build_graph_classifier(
            "gat",
            node_vocab_size=4,
            num_tasks=128,
            algo_config={
                "node_embedding_dim": 7,
                "hidden_dim": 11,
                "embedding_dim": 9,
                "num_layers": 3,
                "dropout": 0.35,
                "gat_heads": 4,
                "use_residual_connection": True,
                "use_normalization": True,
                "norm_pos": "pre",
                "norm_type": "rms_norm",
            },
        )

        self.assertEqual(model.backbone_name, "GAT")
        self.assertEqual(model.node_encoder.embedding_dim, 7)
        self.assertEqual(model.backbone.dropout, 0.35)
        self.assertEqual(model.backbone.gat_heads, 4)
        self.assertTrue(model.backbone.layer_stack.use_residual_connection)
        self.assertTrue(model.backbone.layer_stack.use_normalization)
        self.assertEqual(model.backbone.layer_stack.norm_pos, "pre")
        self.assertEqual(model.backbone.layer_stack.norm_type, "rms_norm")
        self.assertEqual(len(model.backbone.layer_stack.residual_projections), 3)
        self.assertEqual(len(model.backbone.layer_stack.norms), 3)
        self.assertEqual(
            model.architecture_metadata()["pooling"],
            "global_mean",
        )
        self.assertFalse(model.architecture_metadata()["uses_edge_features"])
        self.assertEqual(tuple(model(_make_batch("pcba")).shape), (4, 128))

    def test_state_dict_strictly_reloads_on_cpu_for_every_backbone(self) -> None:
        batch = _make_batch("hiv")
        for backbone_name in ("GCN", "GAT", "SAGE"):
            with self.subTest(backbone=backbone_name):
                torch.manual_seed(123)
                model = GraphClassifier(
                    backbone_name,
                    node_vocab_size=4,
                    num_tasks=1,
                    node_embedding_dim=6,
                    hidden_dim=7,
                    embedding_dim=5,
                    dropout=0.2,
                    use_residual_connection=True,
                    use_normalization=True,
                    norm_type="layer_norm",
                ).cpu()
                model.eval()
                expected = model(batch)

                checkpoint = io.BytesIO()
                torch.save(model.state_dict(), checkpoint)
                checkpoint.seek(0)
                state_dict = torch.load(
                    checkpoint,
                    map_location="cpu",
                    weights_only=True,
                )
                rebuilt = GraphClassifier(**model.constructor_config()).cpu()
                load_result = rebuilt.load_state_dict(state_dict, strict=True)
                rebuilt.eval()

                self.assertEqual(load_result.missing_keys, [])
                self.assertEqual(load_result.unexpected_keys, [])
                self.assertTrue(torch.equal(expected, rebuilt(batch)))
                self.assertTrue(all(not parameter.is_cuda for parameter in rebuilt.parameters()))

    def test_invalid_model_parameters_and_batches_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "仅支持 GCN/GAT/SAGE"):
            GraphClassifier("GIN", node_vocab_size=4, num_tasks=1)
        with self.assertRaisesRegex(ValueError, "num_tasks 必须是正整数"):
            GraphClassifier("GCN", node_vocab_size=4, num_tasks=0)
        with self.assertRaisesRegex(ValueError, "num_layers 必须大于等于 2"):
            GraphClassifier("GCN", node_vocab_size=4, num_tasks=1, num_layers=1)
        with self.assertRaisesRegex(ValueError, "0 <= dropout < 1"):
            GraphClassifier("GCN", node_vocab_size=4, num_tasks=1, dropout=1.0)

        model = GraphClassifier("GCN", node_vocab_size=4, num_tasks=1)
        with self.assertRaisesRegex(ValueError, "必须是 PyG Batch"):
            model(Data(x=torch.tensor([0], dtype=torch.long)))
        malformed_batch = _make_batch("hiv")
        malformed_batch.edge_index = malformed_batch.edge_index[:1]
        with self.assertRaisesRegex(ValueError, r"shape \[2, E\]"):
            model(malformed_batch)


if __name__ == "__main__":
    unittest.main()

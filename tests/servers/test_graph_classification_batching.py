from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import torch
from torch_geometric.data import Batch

from graph_data_service.graph_ml.datasets import (
    GraphClassificationDatasetBundle,
    build_graph_classification_dataloaders,
    graph_classification_readiness_for_dataset,
    load_graph_classification_dataset,
    validate_graph_classification_batch,
)
from graph_data_service.graph_ml.models import NodeCategoryEncoder, build_backbone
from graph_data_service.session import GraphSession
from tests.fixtures.graph_classification import make_graph_classification_payload


def _make_bundle(
    profile: str,
    *,
    num_graphs: int = 10,
    edgeless_only: bool = False,
) -> GraphClassificationDatasetBundle:
    payload = make_graph_classification_payload(
        profile,
        num_graphs=num_graphs,
        include_edgeless=True,
    )
    num_tasks = 1 if profile == "hiv" else 128
    for graph_index, graph in enumerate(payload["graphs"]):
        if edgeless_only:
            graph.edge_index = torch.empty((2, 0), dtype=torch.long)
            graph.xe = torch.empty((0,), dtype=torch.long)
        graph.x = graph.x.to(dtype=torch.long)
        graph.xe = graph.xe.to(dtype=torch.long)
        graph.edge_index = graph.edge_index.to(dtype=torch.long)
        graph.y = graph.y.to(dtype=torch.float32).reshape(1, num_tasks)
        graph.graph_id = torch.tensor([graph_index], dtype=torch.long)

    split = payload["split"][0]
    return GraphClassificationDatasetBundle(
        dataset_id=f"gds_batch_{profile}",
        name=profile,
        source_filename=f"{profile}.pt",
        source_path=f"/registered/{profile}.pt",
        fingerprint=f"fingerprint-{profile}",
        graphs=payload["graphs"],
        split_indices={
            "train": list(split["train"]),
            "val": list(split["valid"]),
            "test": list(split["test"]),
        },
        node_texts=list(payload["texts"][0]),
        edge_texts=list(payload["texts"][1]),
        label_texts=list(payload["texts"][2]),
        num_tasks=num_tasks,
        metric="roc_auc" if profile == "hiv" else "ap",
        node_vocab_size=4,
        edge_vocab_size=3,
        texts=payload["texts"],
        metadata={"uses_edge_features": False},
    )


def _graph_order(loader) -> list[int]:
    order: list[int] = []
    for batch in loader:
        order.extend(int(value) for value in batch.graph_id.reshape(-1).tolist())
    return order


class GraphClassificationBatchingTest(unittest.TestCase):
    def test_dataset_readiness_is_separate_and_reports_missing_label_rate(self) -> None:
        bundle = _make_bundle("pcba", num_graphs=10)
        readiness = graph_classification_readiness_for_dataset(bundle)

        self.assertTrue(readiness["supported"])
        self.assertEqual(readiness["reason"], "ready")
        self.assertEqual(readiness["num_graphs"], 10)
        self.assertEqual(readiness["num_tasks"], 128)
        self.assertEqual(readiness["split_sizes"], {"train": 8, "val": 1, "test": 1})
        self.assertEqual(readiness["metric"], "ap")
        self.assertAlmostEqual(readiness["missing_label_rate"], 1 / (10 * 128))
        self.assertFalse(readiness["uses_edge_features"])

    def test_safe_loader_output_flows_directly_into_pcba_batching(self) -> None:
        old_data_root = os.environ.get("IGRAPH_DATA_ROOT")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                os.environ["IGRAPH_DATA_ROOT"] = temp_dir
                path = Path(temp_dir) / "pcba.pt"
                torch.save(make_graph_classification_payload("pcba"), path)
                session = GraphSession("batch-loader", user_id="alice")
                session.register_upload("token-pcba", "pcba.pt", str(path))

                bundle = load_graph_classification_dataset(session, "pcba.pt")
                loaders = build_graph_classification_dataloaders(
                    bundle,
                    batch_size=4,
                    seed=42,
                    pin_memory=False,
                )
                batch = next(iter(loaders.train))
                info = validate_graph_classification_batch(batch, bundle)
        finally:
            if old_data_root is None:
                os.environ.pop("IGRAPH_DATA_ROOT", None)
            else:
                os.environ["IGRAPH_DATA_ROOT"] = old_data_root

        self.assertEqual(tuple(batch.y.shape), (4, 128))
        self.assertEqual(info.num_tasks, 128)
        self.assertTrue(torch.isnan(batch.y).any().item())

    def test_hiv_and_pcba_batch_shapes_follow_builtin_splits(self) -> None:
        hiv = _make_bundle("hiv")
        hiv_loaders = build_graph_classification_dataloaders(
            hiv,
            batch_size=3,
            num_workers=0,
            seed=7,
            pin_memory=False,
        )

        self.assertEqual(hiv_loaders.num_workers, 0)
        self.assertEqual(hiv_loaders.train.dataset.indices, hiv.split_indices["train"])
        self.assertEqual(hiv_loaders.val.dataset.indices, hiv.split_indices["val"])
        self.assertEqual(hiv_loaders.test.dataset.indices, hiv.split_indices["test"])
        self.assertIs(hiv_loaders.for_split("val"), hiv_loaders.val)

        hiv_batch = next(iter(hiv_loaders.train))
        hiv_info = validate_graph_classification_batch(hiv_batch, hiv)
        self.assertEqual(tuple(hiv_batch.y.shape), (3, 1))
        self.assertEqual(hiv_info.num_graphs, 3)
        self.assertEqual(hiv_info.num_tasks, 1)
        self.assertTrue(hiv_info.has_valid_labels)
        self.assertFalse(hiv_info.uses_edge_features)
        self.assertEqual(hiv_info.valid_label_mask.shape, hiv_batch.y.shape)

        pcba = _make_bundle("pcba")
        pcba_loaders = build_graph_classification_dataloaders(
            pcba,
            batch_size=4,
            seed=7,
            pin_memory=False,
        )
        pcba_batch = next(iter(pcba_loaders.train))
        pcba_info = validate_graph_classification_batch(pcba_batch, pcba)
        self.assertEqual(tuple(pcba_batch.y.shape), (4, 128))
        self.assertEqual(pcba_info.num_tasks, 128)
        self.assertEqual(pcba_batch.x.dtype, torch.long)
        self.assertEqual(pcba_batch.xe.dtype, torch.long)
        self.assertEqual(pcba_batch.edge_index.dtype, torch.long)

    def test_fixed_seed_reproduces_train_order_and_eval_order_is_stable(self) -> None:
        bundle = _make_bundle("hiv", num_graphs=14)
        first = build_graph_classification_dataloaders(
            bundle,
            batch_size=3,
            seed=1234,
            pin_memory=False,
        )
        second = build_graph_classification_dataloaders(
            bundle,
            batch_size=3,
            seed=1234,
            pin_memory=False,
        )
        different = build_graph_classification_dataloaders(
            bundle,
            batch_size=3,
            seed=4321,
            pin_memory=False,
        )

        first_epoch_orders = [_graph_order(first.train), _graph_order(first.train)]
        second_epoch_orders = [_graph_order(second.train), _graph_order(second.train)]
        different_order = _graph_order(different.train)
        self.assertEqual(first_epoch_orders, second_epoch_orders)
        self.assertNotEqual(first_epoch_orders[0], different_order)
        self.assertEqual(_graph_order(first.val), bundle.split_indices["val"])
        self.assertEqual(_graph_order(first.test), bundle.split_indices["test"])

        torch.manual_seed(99)
        first_encoder = NodeCategoryEncoder(bundle.node_vocab_size, embedding_dim=8)
        torch.manual_seed(99)
        second_encoder = NodeCategoryEncoder(bundle.node_vocab_size, embedding_dim=8)
        node_ids = torch.tensor([0, 1, 2, 3], dtype=torch.long)
        self.assertTrue(
            torch.equal(first_encoder(node_ids), second_encoder(node_ids))
        )

    def test_node_embedding_and_existing_backbones_accept_edgeless_batches(self) -> None:
        bundle = _make_bundle("hiv", edgeless_only=True)
        loaders = build_graph_classification_dataloaders(
            bundle,
            batch_size=4,
            seed=1,
            pin_memory=False,
        )
        batch = next(iter(loaders.train))
        info = validate_graph_classification_batch(batch, bundle)
        self.assertEqual(info.num_edges, 0)
        self.assertEqual(batch.xe.numel(), 0)
        self.assertEqual(
            torch.unique(batch.batch).tolist(),
            list(range(info.num_graphs)),
        )

        encoder = NodeCategoryEncoder(bundle.node_vocab_size, embedding_dim=8)
        node_features = encoder(batch.x)
        self.assertEqual(tuple(node_features.shape), (info.num_nodes, 8))
        self.assertTrue(torch.is_floating_point(node_features))
        self.assertFalse(encoder.uses_edge_features)

        for model_name in ("GCN", "GAT", "SAGE", "GIN", "APPNP"):
            with self.subTest(model=model_name):
                backbone = build_backbone(
                    model_name,
                    in_channels=8,
                    hidden_channels=8,
                    out_channels=8,
                    num_layers=2,
                    dropout=0.0,
                )
                backbone.eval()
                output = backbone(node_features, batch.edge_index)
                self.assertEqual(tuple(output.shape), (info.num_nodes, 8))

    def test_no_valid_label_batch_is_detectable_and_can_be_rejected(self) -> None:
        bundle = _make_bundle("pcba")
        for graph_index in bundle.split_indices["train"]:
            bundle.graphs[graph_index].y.fill_(torch.nan)
        loaders = build_graph_classification_dataloaders(
            bundle,
            batch_size=4,
            seed=1,
            pin_memory=False,
        )
        batch = next(iter(loaders.train))

        info = validate_graph_classification_batch(batch, bundle)
        self.assertFalse(info.has_valid_labels)
        self.assertEqual(info.num_valid_labels, 0)
        with self.assertRaisesRegex(ValueError, "没有任何有效标签"):
            validate_graph_classification_batch(
                batch,
                bundle,
                require_valid_labels=True,
            )
        with self.assertRaisesRegex(ValueError, "batch 不能为空"):
            validate_graph_classification_batch(Batch(), bundle)

    def test_invalid_split_batch_and_node_ids_return_clear_errors(self) -> None:
        bundle = _make_bundle("hiv")
        bundle.split_indices["val"] = []
        with self.assertRaisesRegex(ValueError, "val split 不能为空"):
            build_graph_classification_dataloaders(bundle, pin_memory=False)

        bundle = _make_bundle("hiv")
        loaders = build_graph_classification_dataloaders(
            bundle,
            batch_size=2,
            pin_memory=False,
        )
        batch = next(iter(loaders.train))
        batch.x[0] = bundle.node_vocab_size
        with self.assertRaisesRegex(ValueError, "节点词表范围"):
            validate_graph_classification_batch(batch, bundle)

        encoder = NodeCategoryEncoder(node_vocab_size=4, embedding_dim=8)
        with self.assertRaisesRegex(ValueError, "torch.long"):
            encoder(torch.tensor([0.0, 1.0]))
        with self.assertRaisesRegex(ValueError, "超出 embedding 词表"):
            encoder(torch.tensor([0, 4], dtype=torch.long))
        with self.assertRaisesRegex(ValueError, "正整数"):
            NodeCategoryEncoder(node_vocab_size=0, embedding_dim=8)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import tempfile
import unittest

import numpy as np

from graph_data_service.graph_data import GraphData
from graph_data_service.session import GraphSession

try:
    import torch
    from torch_geometric.data import Data
except ImportError:  # pragma: no cover - GraphML dependency is optional in minimal installs
    torch = None
    Data = None


@unittest.skipUnless(torch is not None and Data is not None, "需要 torch 和 torch-geometric")
class GraphDataPtLoadTest(unittest.TestCase):
    def _save_and_load(self, payload, filename: str = "graph.pt") -> GraphData:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, filename)
            torch.save(payload, path)
            return GraphData.load(path)

    def test_pyg_single_graph_without_explicit_semantics_is_conservative(self) -> None:
        payload = Data(
            x=torch.tensor([[1.0], [2.0], [3.0]]),
            edge_index=torch.tensor([[0, 1], [1, 0]], dtype=torch.long),
            y=torch.tensor([0, 1, 0], dtype=torch.long),
            num_nodes=3,
        )

        graph = self._save_and_load(payload)

        self.assertEqual(graph.num_nodes, 3)
        self.assertEqual(graph.num_edges, 2)
        self.assertEqual(graph.semantic_type, "unknown")
        self.assertEqual(graph.storage_type, "bidirectional")
        self.assertEqual(graph.num_unique_edges, 2)
        self.assertEqual(graph.num_unique_undirected_edges, 1)
        np.testing.assert_array_equal(graph.node_label, np.asarray([0, 1, 0]))
        np.testing.assert_allclose(graph.node_feat, np.asarray([[1.0], [2.0], [3.0]]))

    def test_pyg_single_graph_with_explicit_semantic_metadata_preserves_undirected_counts(self) -> None:
        payload = Data(
            x=torch.tensor([[1.0], [2.0], [3.0]]),
            edge_index=torch.tensor([[0, 1], [1, 0]], dtype=torch.long),
            y=torch.tensor([0, 1, 0], dtype=torch.long),
            num_nodes=3,
            semantic_type="undirected",
            graph_metadata={"semantic_type": "undirected", "storage_type": "bidirectional"},
        )

        graph = self._save_and_load(payload)

        self.assertEqual(graph.semantic_type, "undirected")
        self.assertEqual(graph.storage_type, "bidirectional")
        self.assertEqual(graph.num_edge_entries, 2)
        self.assertEqual(graph.num_unique_edges, 1)
        self.assertEqual(graph.degree(0), 1)
        self.assertEqual(graph.degree(1), 1)

    def test_single_graph_dict_aliases_node_map_and_e_by_two_edges(self) -> None:
        payload = {
            "num_nodes": 4,
            "node_feature": torch.tensor(
                [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [0.5, 0.5]]
            ),
            "labels": torch.tensor([0, 1, 0, 1]),
            "edge_index": torch.tensor([[0, 1], [1, 2], [2, 3]], dtype=torch.long),
            "node_index": {"A": 0, "B": 1, "C": 2, "D": 3},
            "valid_mask": [1, 3],
            "directed": False,
        }

        graph = self._save_and_load(payload)

        self.assertFalse(graph.directed)
        self.assertEqual(graph.name_to_id("C"), 2)
        self.assertEqual(graph.id_to_name(3), "D")
        np.testing.assert_array_equal(
            graph.edge_index,
            np.asarray([[0, 1, 2], [1, 2, 3]], dtype=np.int64),
        )
        np.testing.assert_array_equal(graph.val_mask, [False, True, False, True])
        np.testing.assert_array_equal(graph.node_label, [0, 1, 0, 1])

    def test_label_alias_is_supported_for_complete_single_graph_dict(self) -> None:
        payload = {
            "num_nodes": 3,
            "x": torch.ones((3, 2)),
            "edge_index": torch.tensor([[0, 1], [1, 2]], dtype=torch.long),
            "label": torch.tensor([1, 0, 1]),
        }

        graph = self._save_and_load(payload)

        np.testing.assert_array_equal(graph.node_label, [1, 0, 1])

    def test_masks_accept_bool_tensor_numpy_indices_and_plain_index_list(self) -> None:
        payload = {
            "num_nodes": 5,
            "x": torch.ones((5, 1)),
            "edge_index": torch.empty((2, 0), dtype=torch.long),
            "train_mask": torch.tensor([True, False, True, False, False]),
            "val_masks": [np.asarray([1, 3], dtype=np.int64)],
            "test_mask": [0, 4],
        }

        graph = self._save_and_load(payload)

        np.testing.assert_array_equal(graph.train_mask, [True, False, True, False, False])
        np.testing.assert_array_equal(graph.val_mask, [False, True, False, True, False])
        np.testing.assert_array_equal(graph.test_mask, [True, False, False, False, True])

    def test_invalid_node_index_is_rejected(self) -> None:
        payload = {
            "num_nodes": 3,
            "x": torch.ones((3, 1)),
            "edge_index": torch.empty((2, 0), dtype=torch.long),
            "node_index": {"A": 0, "B": 2},
        }

        with self.assertRaisesRegex(ValueError, "node_index"):
            self._save_and_load(payload)

    def test_invalid_edge_index_shape_is_rejected(self) -> None:
        payload = {
            "num_nodes": 3,
            "x": torch.ones((3, 1)),
            "edge_index": torch.zeros((3, 3), dtype=torch.long),
        }

        with self.assertRaisesRegex(ValueError, "edge_index"):
            self._save_and_load(payload)

    def test_graph_level_y_is_not_coerced_to_node_label(self) -> None:
        payload = {
            "num_nodes": 3,
            "x": torch.ones((3, 1)),
            "edge_index": torch.tensor([[0, 1], [1, 2]], dtype=torch.long),
            "y": torch.tensor([1]),
        }

        with self.assertRaisesRegex(ValueError, "load_graph_classification_dataset"):
            self._save_and_load(payload)

    def test_graph_classification_container_is_rejected_without_changing_session(self) -> None:
        old_root = os.environ.get("IGRAPH_DATA_ROOT")
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ["IGRAPH_DATA_ROOT"] = td
                path = os.path.join(td, "hiv.pt")
                torch.save({"graphs": [], "texts": [], "split": []}, path)

                session = GraphSession(session_id="s-pt-multigraph", user_id="alice")
                session.set_graph(GraphData(num_nodes=1))
                version_before = session.graph_version

                with self.assertRaisesRegex(ValueError, "load_graph_classification_dataset"):
                    session.load_graph_from_upload_path(path)

                self.assertEqual(session.graph_version, version_before)
                self.assertEqual(session.get_graph().num_nodes, 1)
        finally:
            if old_root is None:
                os.environ.pop("IGRAPH_DATA_ROOT", None)
            else:
                os.environ["IGRAPH_DATA_ROOT"] = old_root

    def test_sample_mapping_is_rejected_instead_of_loading_first_graph(self) -> None:
        one_graph = {
            "num_nodes": 2,
            "x": torch.ones((2, 1)),
            "edge_index": torch.tensor([[0], [1]], dtype=torch.long),
            "y": torch.tensor([0, 1]),
        }
        payload = {"sample_b": one_graph, "sample_a": dict(one_graph)}

        with self.assertRaisesRegex(ValueError, "不会排序后只加载第一张图"):
            self._save_and_load(payload)


if __name__ == "__main__":
    unittest.main()

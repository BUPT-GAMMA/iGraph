from __future__ import annotations

import unittest

import numpy as np

try:
    import torch
except ImportError:  # pragma: no cover - optional in minimal environments
    torch = None

from graph_data_service.graph_data import GraphData
from graph_data_service.graph_structure import (
    compute_node_degrees,
    count_edge_entries,
    count_unique_directed_edges,
    count_unique_undirected_edges,
    inspect_graph_structure,
)


class GraphStructureHelpersTest(unittest.TestCase):
    def test_standard_bidirectional_undirected_graph(self) -> None:
        edge_index = np.asarray([[0, 1, 1, 2], [1, 0, 2, 1]], dtype=np.int64)

        info = inspect_graph_structure(
            edge_index, num_nodes=3, semantic_type="undirected"
        )
        degrees = compute_node_degrees(edge_index, 3, "undirected")

        self.assertEqual(count_edge_entries(edge_index), 4)
        self.assertEqual(count_unique_directed_edges(edge_index), 4)
        self.assertEqual(count_unique_undirected_edges(edge_index), 2)
        self.assertEqual(info.num_edge_entries, 4)
        self.assertEqual(info.num_unique_edges, 2)
        self.assertTrue(info.is_symmetric)
        self.assertEqual(degrees["degree"].tolist(), [1, 2, 1])
        self.assertEqual(degrees["num_neighbors"].tolist(), [1, 2, 1])

    def test_symmetric_storage_does_not_force_undirected_semantics(self) -> None:
        edge_index = np.asarray([[0, 1], [1, 0]], dtype=np.int64)

        info = inspect_graph_structure(edge_index, num_nodes=2, semantic_type="unknown")

        self.assertEqual(info.semantic_type, "unknown")
        self.assertTrue(info.is_symmetric)
        self.assertEqual(info.storage_type, "bidirectional")
        self.assertEqual(info.num_unique_edges, 2)

    def test_symmetric_storage_can_still_be_semantic_directed(self) -> None:
        edge_index = np.asarray([[0, 1], [1, 0]], dtype=np.int64)

        info = inspect_graph_structure(
            edge_index, num_nodes=2, semantic_type="directed"
        )
        degrees = compute_node_degrees(edge_index, 2, "directed")

        self.assertEqual(info.semantic_type, "directed")
        self.assertTrue(info.is_symmetric)
        self.assertEqual(info.storage_type, "bidirectional")
        self.assertEqual(info.num_unique_edges, 2)
        self.assertEqual(degrees["in_degree"].tolist(), [1, 1])
        self.assertEqual(degrees["out_degree"].tolist(), [1, 1])
        self.assertEqual(degrees["total_degree"].tolist(), [2, 2])

    def test_single_direction_storage_can_be_semantic_undirected(self) -> None:
        edge_index = np.asarray([[0, 1], [1, 2]], dtype=np.int64)

        info = inspect_graph_structure(
            edge_index, num_nodes=3, semantic_type="undirected"
        )
        degrees = compute_node_degrees(edge_index, 3, "undirected")
        graph = GraphData(
            num_nodes=3,
            edge_index=edge_index,
            semantic_type="undirected",
            directed=False,
        )
        processed = graph.to_undirected()

        self.assertEqual(info.num_edge_entries, 2)
        self.assertEqual(info.num_unique_edges, 2)
        self.assertFalse(info.is_symmetric)
        self.assertEqual(info.storage_type, "directed")
        self.assertEqual(degrees["degree"].tolist(), [1, 2, 1])
        self.assertEqual(graph.num_edge_entries, 2)
        self.assertEqual(processed.num_edge_entries, 4)
        self.assertIn(
            "Added reverse edges for undirected message passing",
            processed.transform_log,
        )

    def test_partial_reverse_storage_is_mixed(self) -> None:
        edge_index = np.asarray([[0, 1, 2], [1, 0, 3]], dtype=np.int64)

        info = inspect_graph_structure(
            edge_index, num_nodes=4, semantic_type="undirected"
        )

        self.assertFalse(info.is_symmetric)
        self.assertEqual(info.storage_type, "mixed")
        self.assertEqual(info.num_edge_entries, 3)
        self.assertEqual(info.num_unique_edges, 2)
        self.assertEqual(info.reverse_edge_missing_count, 1)

    def test_empty_graph_and_isolated_nodes_have_zero_degree(self) -> None:
        edge_index = np.empty((2, 0), dtype=np.int64)

        info = inspect_graph_structure(
            edge_index, num_nodes=3, semantic_type="undirected"
        )
        degrees = compute_node_degrees(edge_index, 3, "undirected")

        self.assertTrue(info.is_symmetric)
        self.assertEqual(info.storage_type, "bidirectional")
        self.assertEqual(info.num_edge_entries, 0)
        self.assertEqual(info.num_unique_edges, 0)
        self.assertEqual(degrees["degree"].tolist(), [0, 0, 0])
        self.assertEqual(degrees["num_neighbors"].tolist(), [0, 0, 0])

    def test_non_contiguous_but_legal_node_ids(self) -> None:
        edge_index = np.asarray([[0, 10], [10, 0]], dtype=np.int64)

        info = inspect_graph_structure(
            edge_index, num_nodes=11, semantic_type="undirected"
        )
        degrees = compute_node_degrees(edge_index, 11, "undirected")

        self.assertEqual(info.num_edge_entries, 2)
        self.assertEqual(info.num_unique_edges, 1)
        self.assertEqual(degrees["degree"][0], 1)
        self.assertEqual(degrees["degree"][10], 1)
        self.assertEqual(degrees["degree"][5], 0)

    def test_cora_style_bidirectional_storage_reports_dual_counts(self) -> None:
        edge_index = np.asarray(
            [[0, 1, 1, 2, 2, 3, 3, 4, 4, 0], [1, 0, 2, 1, 3, 2, 4, 3, 0, 4]],
            dtype=np.int64,
        )

        info = inspect_graph_structure(
            edge_index, num_nodes=5, semantic_type="undirected"
        )

        self.assertEqual(info.num_edge_entries, 10)
        self.assertEqual(info.num_unique_edges, 5)
        self.assertEqual(info.num_unique_undirected_edges, 5)
        self.assertTrue(info.is_symmetric)

    def test_duplicate_edges_and_relations_are_counted_explicitly(self) -> None:
        edge_index = np.asarray([[0, 0, 1, 1], [1, 1, 0, 0]], dtype=np.int64)
        edge_type = ["rel_a", "rel_a", "rel_b", "rel_b"]

        self.assertEqual(count_edge_entries(edge_index), 4)
        self.assertEqual(count_unique_directed_edges(edge_index, edge_type), 2)
        self.assertEqual(count_unique_undirected_edges(edge_index, edge_type), 2)

        mixed_types = ["rel_a", "rel_a", "rel_a", "rel_a"]
        self.assertEqual(count_unique_undirected_edges(edge_index, mixed_types), 1)

        degrees = compute_node_degrees(edge_index, 2, "undirected")
        self.assertEqual(degrees["degree"].tolist(), [1, 1])
        self.assertEqual(degrees["num_neighbors"].tolist(), [1, 1])

        relation_degrees = compute_node_degrees(
            edge_index, 2, "undirected", edge_type=edge_type
        )
        self.assertEqual(relation_degrees["degree"].tolist(), [2, 2])
        self.assertEqual(relation_degrees["num_neighbors"].tolist(), [1, 1])

    def test_self_loop_degree_uses_math_degree_and_neighbor_count_separately(
        self,
    ) -> None:
        edge_index = np.asarray([[0, 1, 1], [1, 0, 1]], dtype=np.int64)

        info = inspect_graph_structure(
            edge_index, num_nodes=2, semantic_type="undirected"
        )
        degrees = compute_node_degrees(edge_index, 2, "undirected")

        self.assertEqual(info.num_edge_entries, 3)
        self.assertEqual(info.num_unique_edges, 2)
        self.assertTrue(info.has_self_loops)
        self.assertEqual(degrees["degree"].tolist(), [1, 3])
        self.assertEqual(degrees["num_neighbors"].tolist(), [1, 2])

    def test_directed_graph_keeps_in_out_split(self) -> None:
        edge_index = np.asarray([[0, 0, 1], [1, 2, 2]], dtype=np.int64)

        info = inspect_graph_structure(
            edge_index, num_nodes=3, semantic_type="directed"
        )
        degrees = compute_node_degrees(edge_index, 3, "directed")

        self.assertEqual(info.num_unique_edges, 3)
        self.assertEqual(degrees["in_degree"].tolist(), [0, 1, 2])
        self.assertEqual(degrees["out_degree"].tolist(), [2, 1, 0])
        self.assertEqual(degrees["total_degree"].tolist(), [2, 2, 2])
        self.assertEqual(degrees["degree"].tolist(), [2, 2, 2])

    def test_to_undirected_preserves_edge_attribute_alignment(self) -> None:
        graph = GraphData(
            num_nodes=3,
            edge_index=np.asarray([[0, 1, 1], [1, 0, 2]], dtype=np.int64),
            edge_weight=np.asarray([0.25, 0.5, 0.75], dtype=np.float32),
            edge_feat=np.asarray([[1.0], [2.0], [3.0]], dtype=np.float32),
            edge_type=np.asarray(["link", "link", "link"], dtype=object),
            edge_text=["edge-ab", "edge-ba", "edge-bc"],
            semantic_type="undirected",
            directed=False,
        )

        processed = graph.to_undirected()

        self.assertEqual(processed.num_edge_entries, 4)
        self.assertEqual(processed.edge_weight.tolist(), [0.25, 0.5, 0.75, 0.75])
        self.assertEqual(
            processed.edge_text, ["edge-ab", "edge-ba", "edge-bc", "edge-bc"]
        )
        self.assertEqual(processed.edge_type.tolist(), ["link", "link", "link", "link"])
        np.testing.assert_array_equal(
            processed.edge_index,
            np.asarray([[0, 1, 1, 2], [1, 0, 2, 1]], dtype=np.int64),
        )

    def test_to_undirected_keeps_distinct_relation_edges(self) -> None:
        graph = GraphData(
            num_nodes=2,
            edge_index=np.asarray([[0, 1], [1, 0]], dtype=np.int64),
            edge_type=np.asarray(["rel_a", "rel_b"], dtype=object),
            semantic_type="undirected",
            directed=False,
        )

        processed = graph.to_undirected()

        self.assertEqual(processed.num_edge_entries, 4)
        self.assertEqual(processed.num_unique_undirected_edges, 2)

    @unittest.skipUnless(torch is not None, "requires torch")
    def test_cpu_tensor_inputs_round_trip_without_device_mismatch(self) -> None:
        edge_index = torch.tensor([[0, 1, 1, 2], [1, 0, 2, 1]], dtype=torch.long)

        info = inspect_graph_structure(
            edge_index, num_nodes=3, semantic_type="undirected"
        )
        degrees = compute_node_degrees(edge_index, 3, "undirected")

        self.assertEqual(info.num_unique_edges, 2)
        self.assertEqual(degrees["degree"].device.type, "cpu")
        self.assertEqual(degrees["num_neighbors"].device.type, "cpu")

    @unittest.skipUnless(torch is not None, "requires torch")
    def test_message_passing_edge_index_does_not_undirect_directed_graph(self) -> None:
        from graph_data_service.graph_ml.tasks import message_passing_edge_index

        graph = GraphData(
            num_nodes=3,
            edge_index=np.asarray([[0, 0, 1], [1, 2, 2]], dtype=np.int64),
            semantic_type="directed",
            directed=True,
        )

        edge_index = message_passing_edge_index(graph)

        self.assertEqual(edge_index.shape[1], 3)
        self.assertEqual(edge_index.tolist(), [[0, 0, 1], [1, 2, 2]])

    @unittest.skipUnless(
        torch is not None and torch.cuda.is_available(), "requires CUDA"
    )
    def test_cuda_tensor_inputs_round_trip_without_device_mismatch(self) -> None:
        edge_index = torch.tensor(
            [[0, 1, 1, 2], [1, 0, 2, 1]], dtype=torch.long, device="cuda"
        )

        info = inspect_graph_structure(
            edge_index, num_nodes=3, semantic_type="undirected"
        )
        degrees = compute_node_degrees(edge_index, 3, "undirected")

        self.assertEqual(info.num_unique_edges, 2)
        self.assertEqual(degrees["degree"].device.type, "cuda")
        self.assertEqual(degrees["num_neighbors"].device.type, "cuda")


if __name__ == "__main__":
    unittest.main()

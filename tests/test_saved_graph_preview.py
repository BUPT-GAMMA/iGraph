from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from graph_data_service.construction import ConstructionError
from graph_data_service.saved_graph_preview import saved_graph_preview


class SavedGraphPreviewTests(unittest.TestCase):
    def test_legacy_finance_snapshot_has_full_schema_and_bounded_connected_sample(self):
        root = Path(__file__).resolve().parents[1]
        path = root / "graph_engine/resources/finance_risk/finance-risk.npz"
        result = saved_graph_preview(path)
        schema, sample = result["schema"], result["graph"]
        self.assertEqual((schema["node_count"], schema["edge_count"]), (19436, 26872))
        self.assertEqual((schema["node_type_count"], schema["edge_type_count"]), (3, 3))
        self.assertEqual(sum(row["relationship_count"] for row in schema["edge_types"]), 26872)
        self.assertTrue(any(p["path"] == "data.UUID" for t in schema["node_types"] for p in t["property_paths"]))
        self.assertTrue(sample["truncated"])
        self.assertEqual({n["type"] for n in sample["nodes"]}, {t["type"] for t in schema["node_types"]})
        ids = {row["id"] for row in sample["nodes"]}
        self.assertLessEqual(len(ids), 64)
        self.assertLessEqual(len(sample["edges"]), 96)
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in sample["edges"]))

    def test_topology_only_npz_and_replaced_file(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "custom.npz"
            np.savez(path, _num_nodes=3, _directed=False, edge_index=np.array([[2], [1]]),
                     node_type=["A", "A", "B"], edge_type=["links"], map_ids=[0], map_names=["Isolated"])
            result = saved_graph_preview(path)
            self.assertFalse(result["graph"]["truncated"])
            self.assertEqual(result["schema"]["edge_types"][0]["source_type"], "A")
            self.assertEqual(result["graph"]["nodes"][0]["label"], "Isolated")
            np.savez(path, _num_nodes=0, _directed=True, edge_index=np.empty((2, 0), dtype="int64"))
            self.assertEqual(saved_graph_preview(path)["schema"]["node_count"], 0)

    def test_unreadable_format_and_missing_file(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "x.npz"
            with self.assertRaises(ConstructionError) as missing:
                saved_graph_preview(path)
            self.assertEqual(missing.exception.status_code, 404)
            np.savez(path, _num_nodes=2, _directed=True, edge_index=np.array([[0], [5]]))
            with self.assertRaises(ConstructionError) as corrupt:
                saved_graph_preview(path)
            self.assertEqual(corrupt.exception.detail["code"], "saved_graph_unreadable")

    def test_tensor_dictionary_and_standard_pyg_single_graph(self):
        import torch
        from torch_geometric.data import Data
        with TemporaryDirectory() as temp:
            for name, graph in (("dict", {"num_nodes": 2, "edge_index": torch.tensor([[1], [0]])}),
                                ("pyg", Data(num_nodes=2, edge_index=torch.tensor([[1], [0]])))):
                with self.subTest(name=name):
                    path = Path(temp) / (name + ".pt")
                    torch.save(graph, path)
                    result = saved_graph_preview(path)
                    self.assertEqual((result["schema"]["node_count"], result["schema"]["edge_count"]), (2, 1))

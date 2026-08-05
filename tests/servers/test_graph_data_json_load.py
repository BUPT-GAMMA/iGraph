from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid

import networkx as nx

from graph_data_service.compute import GraphCompute, run_nx_tool
from graph_data_service.graph_data import GraphData
from graph_data_service.session import GraphSession


class GraphDataJsonLoadTest(unittest.TestCase):
    def _sample_doc(self) -> dict:
        return {
            "schema": "graph_data.v1",
            "num_nodes": 3,
            "directed": True,
            "name": "json_graph",
            "graph_desc": "graph from json",
            "id2node": {"0": "A", "1": "B", "2": "C"},
            "edge_index": [[0, 1, 0], [1, 2, 2]],
            "edge_weight": [1.0, 2.0, 3.0],
            "edge_names": ["e0", "e1", "e2"],
            "node_feat": [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]],
            "edge_feat": [[0.1], [0.2], [0.3]],
            "node_label": [0, 1, 0],
            "node_type": ["user", "item", "item"],
            "edge_label": [1, 1, 0],
            "edge_type": ["follow", "follow", "click"],
            "graph_label": "demo",
        }

    def test_graph_data_load_from_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "graph.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self._sample_doc(), f, ensure_ascii=False)

            gd = GraphData.load(p)
            self.assertEqual(gd.num_nodes, 3)
            self.assertEqual(gd.num_edges, 3)
            self.assertTrue(gd.directed)
            self.assertEqual(gd.name, "json_graph")
            self.assertEqual(gd.graph_desc, "graph from json")
            self.assertEqual(gd.id_to_name(0), "A")
            self.assertEqual(gd.name_to_id("C"), 2)
            self.assertEqual(float(gd.get_edge_weight(0, 1)), 1.0)
            self.assertEqual(gd.get_node_type_by_id(0), "user")
            self.assertEqual(gd.get_edge_type(0, 2), "click")
            self.assertEqual(gd.graph_label, "demo")

    def test_session_load_graph_from_json_upload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "graph.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self._sample_doc(), f, ensure_ascii=False)

            session = GraphSession(session_id="s-json")
            n, e = session.load_graph_from_upload_path(p, append=False)
            self.assertEqual(n, 3)
            self.assertEqual(e, 3)
            gd = session.get_graph()
            self.assertIsNotNone(gd)
            self.assertEqual(gd.name_to_id("B"), 1)
            self.assertEqual(gd.graph_desc, "graph from json")

    def test_session_load_relative_upload_when_cwd_changes(self) -> None:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        uploads_dir = os.path.join(root, "uploads")
        os.makedirs(uploads_dir, exist_ok=True)

        filename = f"ut_{uuid.uuid4().hex[:8]}_graph.txt"
        rel_path = os.path.join("uploads", filename)
        abs_path = os.path.join(uploads_dir, filename)

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write("A B 1\nB C 2\n")

        old_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                try:
                    os.chdir(td)
                    session = GraphSession(session_id="s-relative-upload")
                    n, e = session.load_graph_from_upload_path(rel_path, append=False)
                    self.assertEqual(n, 3)
                    self.assertEqual(e, 2)
                finally:
                    os.chdir(old_cwd)
        finally:
            os.chdir(old_cwd)
            if os.path.exists(abs_path):
                os.remove(abs_path)

    def test_user_session_graph_state_uses_sessions_dir(self) -> None:
        old_root = os.environ.get("IGRAPH_DATA_ROOT")
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ["IGRAPH_DATA_ROOT"] = td
                session = GraphSession(session_id="s-user-graph", user_id="alice")
                session._parse_edgelist("A B\nB C\n", append=False)

                session_dir = os.path.join(td, "users", "alice", "sessions", "s-user-graph")
                self.assertTrue(os.path.exists(os.path.join(session_dir, "graph_current.npz")))
                self.assertTrue(os.path.exists(os.path.join(session_dir, "snapshots", "v001.npz")))
                self.assertFalse(
                    os.path.exists(
                        os.path.join(td, "users", "alice", "uploads", "s-user-graph", "graph_current.npz")
                    )
                )
        finally:
            if old_root is None:
                os.environ.pop("IGRAPH_DATA_ROOT", None)
            else:
                os.environ["IGRAPH_DATA_ROOT"] = old_root

    def test_user_session_restores_legacy_upload_graph_state(self) -> None:
        old_root = os.environ.get("IGRAPH_DATA_ROOT")
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ["IGRAPH_DATA_ROOT"] = td
                legacy_dir = os.path.join(td, "users", "alice", "uploads", "s-legacy-graph")
                os.makedirs(os.path.join(legacy_dir, "snapshots"), exist_ok=True)
                gd = GraphData(
                    num_nodes=2,
                    id2node={0: "A", 1: "B"},
                    node2id={"A": 0, "B": 1},
                )
                gd.save(os.path.join(legacy_dir, "graph_current.npz"))
                gd.save(os.path.join(legacy_dir, "snapshots", "v003.npz"))
                with open(os.path.join(legacy_dir, "session_meta.json"), "w", encoding="utf-8") as f:
                    json.dump({"session_id": "s-legacy-graph", "graph_version": 3}, f)

                session = GraphSession(session_id="s-legacy-graph", user_id="alice")
                self.assertTrue(session.load_snapshot())
                self.assertEqual(session.graph_version, 3)

                session_dir = os.path.join(td, "users", "alice", "sessions", "s-legacy-graph")
                self.assertTrue(os.path.exists(os.path.join(session_dir, "graph_current.npz")))
                self.assertTrue(os.path.exists(os.path.join(session_dir, "snapshots", "v003.npz")))
        finally:
            if old_root is None:
                os.environ.pop("IGRAPH_DATA_ROOT", None)
            else:
                os.environ["IGRAPH_DATA_ROOT"] = old_root

    def test_discard_current_graph_version_reverts_without_new_version(self) -> None:
        old_root = os.environ.get("IGRAPH_DATA_ROOT")
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ["IGRAPH_DATA_ROOT"] = td
                session = GraphSession(session_id="s-discard-version", user_id="alice")
                session._parse_edgelist("A B\nB C\n", append=False)
                self.assertEqual(session.graph_version, 1)
                session._parse_edgelist("A B\n", append=False)
                self.assertEqual(session.graph_version, 2)

                result = session.discard_version(2, fallback_graph_version=1)

                self.assertIsNotNone(result)
                self.assertEqual(session.graph_version, 1)
                self.assertEqual([r["graph_version"] for r in session.list_versions()], [1])
                session_dir = os.path.join(td, "users", "alice", "sessions", "s-discard-version")
                self.assertTrue(os.path.exists(os.path.join(session_dir, "snapshots", "v001.npz")))
                self.assertFalse(os.path.exists(os.path.join(session_dir, "snapshots", "v002.npz")))
        finally:
            if old_root is None:
                os.environ.pop("IGRAPH_DATA_ROOT", None)
            else:
                os.environ["IGRAPH_DATA_ROOT"] = old_root


class GraphComputeCutTest(unittest.TestCase):
    def test_global_minimum_cut_weighted_undirected(self) -> None:
        g = nx.Graph()
        g.add_edge("A", "B", weight=3.0)
        g.add_edge("B", "C", weight=2.0)
        g.add_edge("A", "C", weight=4.0)

        result = GraphCompute().global_minimum_cut(g)

        self.assertTrue(result["success"])
        self.assertEqual(result["algorithm"], "stoer_wagner")
        self.assertEqual(result["min_cut_value"], 5.0)
        self.assertEqual(result["partition_a_size"] + result["partition_b_size"], 3)

    def test_global_minimum_cut_disconnected_graph_is_zero(self) -> None:
        g = nx.Graph()
        g.add_edge("A", "B", weight=3.0)
        g.add_edge("C", "D", weight=2.0)

        result = run_nx_tool("global_minimum_cut", g, {})

        self.assertTrue(result["success"])
        self.assertTrue(result["disconnected"])
        self.assertEqual(result["min_cut_value"], 0.0)
        self.assertEqual(result["cut_edges_total"], 0)


if __name__ == "__main__":
    unittest.main()

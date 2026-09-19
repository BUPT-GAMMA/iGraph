from __future__ import annotations

import unittest
import warnings

import networkx as nx

from graph_data_service.compute import GraphCompute


class GraphComputeBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.compute = GraphCompute()

    def test_empty_graph_connectivity_is_reported_without_networkx_error(self) -> None:
        directed = self.compute.graph_connectivity(nx.DiGraph())
        undirected = self.compute.graph_connectivity(nx.Graph())

        self.assertTrue(directed["success"])
        self.assertFalse(directed["is_strongly_connected"])
        self.assertFalse(directed["is_weakly_connected"])
        self.assertEqual(directed["strongly_connected_components"]["count"], 0)
        self.assertEqual(directed["weakly_connected_components"]["count"], 0)
        self.assertEqual(directed["strongly_connected_components"]["largest_size"], 0)
        self.assertEqual(directed["weakly_connected_components"]["largest_size"], 0)

        self.assertTrue(undirected["success"])
        self.assertFalse(undirected["is_connected"])
        self.assertEqual(undirected["connected_components"]["count"], 0)
        self.assertEqual(undirected["connected_components"]["largest_size"], 0)

    def test_empty_graph_degree_analysis_keeps_zero_node_count(self) -> None:
        directed = self.compute.degree_analysis(nx.DiGraph())
        undirected = self.compute.degree_analysis(nx.Graph())

        self.assertEqual(directed["total_nodes"], 0)
        self.assertEqual(directed["avg_in_degree"], 0.0)
        self.assertEqual(directed["avg_out_degree"], 0.0)
        self.assertEqual(directed["top_degree_nodes"], [])

        self.assertEqual(undirected["total_nodes"], 0)
        self.assertEqual(undirected["avg_degree"], 0.0)
        self.assertEqual(undirected["top_degree_nodes"], [])

    def test_empty_graph_centralities_return_empty_rankings(self) -> None:
        for centrality_type in ("pagerank", "eigenvector"):
            with self.subTest(centrality_type=centrality_type):
                result = self.compute.node_centrality(
                    nx.DiGraph(), centrality_type=centrality_type
                )

                self.assertTrue(result["success"])
                self.assertEqual(result["total_nodes"], 0)
                self.assertEqual(result["top_nodes"], [])
                self.assertEqual(result["bottom_nodes"], [])

    def test_empty_graph_community_algorithms_return_no_communities(self) -> None:
        graph = nx.DiGraph()

        for algorithm in self.compute._COMMUNITY_ALGORITHMS:
            with self.subTest(algorithm=algorithm):
                result = self.compute.community_detection(graph, algorithm, limit=20)

                self.assertTrue(result["success"])
                self.assertEqual(result["num_communities"], 0)
                self.assertEqual(result["communities_shown"], 0)
                self.assertEqual(result["communities"], [])
                self.assertEqual(result["size_stats"]["mean"], 0)

    def test_empty_graph_path_and_global_metrics_have_stable_results(self) -> None:
        graph = nx.DiGraph()

        average_path = self.compute.path_analysis(
            graph, "average_shortest_path_length"
        )
        average_clustering = self.compute.global_metrics(
            graph, "average_clustering"
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            assortativity = self.compute.global_metrics(
                graph, "degree_assortativity"
            )

        self.assertFalse(average_path["success"])
        self.assertIn("空图", average_path["error"])
        self.assertEqual(
            average_clustering,
            {
                "success": True,
                "metric": "average_clustering",
                "value": 0.0,
                "defined": True,
            },
        )
        self.assertTrue(assortativity["success"])
        self.assertIsNone(assortativity["value"])
        self.assertFalse(assortativity["defined"])
        self.assertEqual(assortativity["undefined_reason_code"], "no_edges")
        self.assertIn("数学上未定义", assortativity["undefined_reason"])
        self.assertIn("不表示计算引擎不支持", assortativity["undefined_reason"])

    def test_singleton_is_connected_and_is_its_only_community(self) -> None:
        graph = nx.DiGraph()
        graph.add_node("solo")

        connectivity = self.compute.graph_connectivity(graph)
        self.assertTrue(connectivity["is_strongly_connected"])
        self.assertTrue(connectivity["is_weakly_connected"])
        self.assertEqual(
            connectivity["strongly_connected_components"]["components"][0][
                "nodes_preview"
            ],
            ["solo"],
        )

        for algorithm in self.compute._COMMUNITY_ALGORITHMS:
            with self.subTest(algorithm=algorithm):
                result = self.compute.community_detection(graph, algorithm, limit=20)

                self.assertTrue(result["success"])
                self.assertEqual(result["num_communities"], 1)
                self.assertEqual(result["communities"][0]["size"], 1)
                self.assertEqual(result["communities"][0]["nodes_preview"], ["solo"])

    def test_community_detection_returns_requested_focus_memberships(self) -> None:
        graph = nx.Graph()
        graph.add_edges_from([("A", "B"), ("B", "C"), ("D", "E")])

        result = self.compute.community_detection(
            graph,
            "label_propagation",
            limit=20,
            focus_nodes=["A", "D", "missing", "A"],
        )

        memberships = {
            row["node_id"]: row for row in result["focus_node_communities"]
        }
        self.assertEqual(set(memberships), {"A", "D"})
        self.assertEqual(result["missing_focus_nodes"], ["missing"])
        self.assertGreaterEqual(memberships["A"]["community_size"], 1)
        self.assertIn("A", memberships["A"]["representative_nodes"])

    def test_singleton_shortest_path_and_average_path_are_zero_length(self) -> None:
        graph = nx.DiGraph()
        graph.add_node("solo")

        shortest = self.compute.all_shortest_paths(graph, "solo", "solo")
        average = self.compute.path_analysis(
            graph, "average_shortest_path_length"
        )

        self.assertTrue(shortest["success"])
        self.assertEqual(shortest["length"], 0)
        self.assertEqual(shortest["paths"], [["solo"]])
        self.assertTrue(average["success"])
        self.assertEqual(average["value"], 0.0)

    def test_missing_nodes_are_rejected_by_path_tools(self) -> None:
        graph = nx.DiGraph()
        graph.add_edge("A", "B", weight=1.0)
        cases = (
            (
                "all_shortest_paths_source",
                lambda: self.compute.all_shortest_paths(graph, "missing", "B"),
            ),
            (
                "all_shortest_paths_target",
                lambda: self.compute.all_shortest_paths(graph, "A", "missing"),
            ),
            (
                "all_simple_paths",
                lambda: self.compute.path_analysis(
                    graph,
                    "all_simple_paths",
                    source="A",
                    target="missing",
                ),
            ),
            (
                "has_path",
                lambda: self.compute.path_analysis(
                    graph, "has_path", source="missing", target="B"
                ),
            ),
        )

        for name, call in cases:
            with self.subTest(name=name):
                result = call()
                self.assertFalse(result["success"])
                self.assertIn("missing", result["error"])
                self.assertEqual(result["available_nodes_total"], 2)

    def test_missing_nodes_are_rejected_by_pair_and_clique_tools(self) -> None:
        graph = nx.DiGraph()
        graph.add_edge("A", "B", weight=1.0)

        link_prediction = self.compute.link_prediction_score(
            graph, "jaccard", source="A", target="missing"
        )
        clique_count = self.compute.clique_analysis(
            graph, "number_of_cliques", node="missing"
        )

        self.assertFalse(link_prediction["success"])
        self.assertIn("missing", link_prediction["error"])
        self.assertEqual(link_prediction["available_nodes_total"], 2)
        self.assertFalse(clique_count["success"])
        self.assertIn("missing", clique_count["error"])

    def test_flow_and_cut_reject_identical_source_and_sink(self) -> None:
        graph = nx.DiGraph()
        graph.add_node("solo")

        maximum_flow = self.compute.maximum_flow(graph, "solo", "solo")
        minimum_cut = self.compute.minimum_cut(graph, "solo", "solo")

        self.assertEqual(
            maximum_flow, {"success": False, "error": "源点与汇点不能相同"}
        )
        self.assertEqual(
            minimum_cut, {"success": False, "error": "源点与汇点不能相同"}
        )

    def test_directed_average_path_rejects_non_strongly_connected_graph(self) -> None:
        graph = nx.DiGraph()
        graph.add_edge("A", "B", weight=1.0)

        result = self.compute.path_analysis(
            graph, "average_shortest_path_length"
        )

        self.assertFalse(result["success"])
        self.assertIn("不是强连通图", result["error"])

    def test_fluid_communities_include_every_disconnected_component(self) -> None:
        graph = nx.Graph()
        graph.add_edges_from(
            [("A", "B"), ("B", "C"), ("C", "A"), ("D", "E")]
        )
        graph.add_node("F")

        result = self.compute.community_detection(
            graph, "asyn_fluidc", limit=20
        )
        reported_nodes = {
            node
            for community in result["communities"]
            for node in community["nodes_preview"]
        }

        self.assertTrue(result["success"])
        self.assertFalse(result["communities_truncated"])
        self.assertEqual(
            sum(community["size"] for community in result["communities"]),
            graph.number_of_nodes(),
        )
        self.assertEqual(reported_nodes, set(graph.nodes()))


if __name__ == "__main__":
    unittest.main()

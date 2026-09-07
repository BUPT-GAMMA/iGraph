"""Parallel graph algorithms must not change their shared graph or attributes."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import unittest

import networkx as nx

from graph_data_service.compute import NX_TOOL_NAMES, run_nx_tool
from graph_engine.tool_execution import execution_policy


class ReadonlyGraphToolTests(unittest.TestCase):
    def test_every_networkx_tool_preserves_the_shared_graph(self):
        graph = nx.DiGraph(description={"name": "Shared graph"})
        graph.add_weighted_edges_from([
            ("A", "B", 2), ("A", "C", 1), ("B", "C", 3),
            ("B", "D", 5), ("C", "D", 4),
        ])
        for node in graph:
            graph.nodes[node]["properties"] = {"name": node, "tags": ["original"]}
        for _, _, attributes in graph.edges(data=True):
            attributes["properties"] = {"tags": ["unchanged"]}

        def state():
            return deepcopy((graph.graph, dict(graph.nodes(data=True)), list(graph.edges(data=True))))

        before = state()
        # A topology edit also fails immediately, rather than being hidden by
        # a later inverse edit. Attribute changes are checked by the snapshot.
        nx.freeze(graph)
        arguments = {
            "shortest_path": {"source": "A", "target": "D"},
            "all_shortest_paths": {"source": "A", "target": "D"},
            "maximum_flow": {"source": "A", "sink": "D"},
            "minimum_cut": {"source": "A", "sink": "D"},
            "global_minimum_cut": {}, "minimum_spanning_tree": {},
            "community_detection": {"algorithm": "greedy_modularity"},
            "node_centrality": {"centrality_type": "degree"},
            "graph_connectivity": {}, "find_cycles": {}, "maximum_cycle": {},
            "topological_sort": {}, "degree_analysis": {},
            "subgraph_extraction": {"method": "node_subset", "nodes": ["A", "B"], "create_version": False},
            "link_prediction_score": {"method": "jaccard", "source": "A", "target": "D"},
            "path_analysis": {"method": "has_path", "source": "A", "target": "D"},
            "clique_analysis": {"method": "find_cliques"},
            "reachability": {"method": "descendants", "node": "A"},
            "global_metrics": {"metric": "density"},
        }
        self.assertEqual(set(arguments), NX_TOOL_NAMES)
        for tool, args in arguments.items():
            self.assertTrue(execution_policy(tool, args).read_only)
        with ThreadPoolExecutor(max_workers=8) as pool:
            pending = {tool: pool.submit(run_nx_tool, tool, graph, args)
                       for tool, args in arguments.items()}
            for tool, future in pending.items():
                with self.subTest(tool=tool):
                    result = future.result(timeout=10)
                    self.assertTrue(result.get("success"), result)
        self.assertEqual(state(), before)


if __name__ == "__main__":
    unittest.main()

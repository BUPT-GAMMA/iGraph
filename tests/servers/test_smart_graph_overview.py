from __future__ import annotations

import unittest

import networkx as nx
import numpy as np

from graph_data_service.compute import GraphCompute
from graph_data_service.graph_data import GraphData
from graph_data_service.smart_overview import select_smart_overview


class SmartOverviewSelectorTest(unittest.TestCase):
    def test_small_graph_is_returned_in_full(self) -> None:
        graph = nx.path_graph(12)
        result = select_smart_overview(graph, graph.nodes(), max_nodes=20)
        self.assertEqual(set(result.nodes), set(graph.nodes()))
        self.assertFalse(result.connector_nodes)

    def test_selection_is_stable_and_preserves_structure(self) -> None:
        graph = nx.Graph()
        # Four dense communities joined by deterministic bridge paths.
        for community in range(4):
            nodes = [f"c{community}-{index}" for index in range(15)]
            graph.add_edges_from((nodes[0], node) for node in nodes[1:])
            graph.add_edges_from((nodes[index], nodes[index + 1]) for index in range(1, 14))
            if community:
                graph.add_edge(f"c{community - 1}-14", nodes[0])
        types = {
            node: ("minority" if index % 5 == 0 else "majority")
            for index, node in enumerate(sorted(graph.nodes()))
        }
        required = ["c0-14", "c3-14"]

        first = select_smart_overview(
            graph, graph.nodes(), max_nodes=24, node_types=types, required_nodes=required
        )
        second = select_smart_overview(
            graph, reversed(list(graph.nodes())), max_nodes=24, node_types=types, required_nodes=reversed(required)
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first.nodes), 24)
        self.assertTrue(set(required).issubset(first.nodes))
        self.assertEqual(len(first.community_representatives), len(first.communities))
        self.assertTrue(set(first.community_representatives).issubset(first.nodes))
        self.assertIn("c0-0", first.nodes)  # high-degree hub
        self.assertIn("minority", {types[node] for node in first.nodes})
        self.assertTrue(first.connector_paths)
        for path in first.connector_paths:
            self.assertTrue(set(path).issubset(first.nodes))
            self.assertTrue(all(graph.has_edge(left, right) for left, right in zip(path, path[1:])))

    def test_type_sampling_is_proportional_with_stable_ties(self) -> None:
        graph = nx.cycle_graph(100)
        types = {node: "a" if node < 80 else "b" for node in graph.nodes()}
        result = select_smart_overview(graph, graph.nodes(), max_nodes=20, node_types=types)
        counts = {
            type_name: sum(types[node] == type_name for node in result.nodes)
            for type_name in {"a", "b"}
        }
        self.assertGreaterEqual(counts["a"], 14)
        self.assertGreaterEqual(counts["b"], 3)


class GraphComputeSmartOverviewTest(unittest.TestCase):
    def test_visualize_subgraph_uses_smart_overview_and_keeps_highlights(self) -> None:
        names = [f"n-{index:03d}" for index in range(40)]
        edge_pairs = [(index, (index + 1) % len(names)) for index in range(len(names))]
        graph_data = GraphData(
            num_nodes=len(names),
            edge_index=np.asarray(edge_pairs, dtype=np.int64).T,
            id2node={index: name for index, name in enumerate(names)},
            node2id={name: index for index, name in enumerate(names)},
            node_type=np.asarray(["person" if index < 30 else "topic" for index in range(len(names))], dtype=object),
            directed=False,
        )
        graph = graph_data.to_networkx()
        highlights = {"nodes": [{"ids": [names[37]], "label": "search hit"}]}

        first = GraphCompute().visualize_subgraph(
            graph, graph_data, include_community=True, highlights=highlights, max_nodes=16
        )
        second = GraphCompute().visualize_subgraph(
            graph, graph_data, include_community=True, highlights=highlights, max_nodes=16
        )

        self.assertEqual(first["nodes"], second["nodes"])
        self.assertEqual(first["edges"], second["edges"])
        self.assertTrue(first["truncated"])
        self.assertEqual(first["truncated_to"], 16)
        self.assertEqual(first["overview"]["mode"], "smart")
        self.assertTrue(first["overview"]["deterministic"])
        self.assertIn(names[37], {node["id"] for node in first["nodes"]})
        self.assertEqual(first["community"]["num_communities"], first["overview"]["community_count"])


if __name__ == "__main__":
    unittest.main()


def test_component_proportions_do_not_create_isolated_representatives():
    graph = nx.path_graph(900)
    graph.add_edges_from((i, i + 1) for i in range(900, 1000, 2))
    result = select_smart_overview(graph, graph.nodes, max_nodes=180)
    sample = graph.subgraph(result.nodes)
    sizes = sorted(map(len, nx.connected_components(sample)), reverse=True)
    assert len(sample) == 180
    assert 158 <= sizes[0] <= 166
    assert 1 not in sizes
    assert len(sizes) > 1


def test_true_isolates_keep_proportional_types():
    graph = nx.empty_graph(1000)
    types = {i: 'A' if i < 750 else 'B' for i in graph}
    result = select_smart_overview(graph, graph.nodes, max_nodes=100, node_types=types)
    assert sum(types[n] == 'A' for n in result.nodes) == 75
    assert sum(types[n] == 'B' for n in result.nodes) == 25


def test_dense_edge_cap_preserves_bridges():
    graph = nx.Graph()
    for offset in range(0, 80, 20):
        graph.add_edges_from((a, b) for a in range(offset, offset + 20) for b in range(a + 1, offset + 20))
    graph.add_edges_from([(19, 20), (39, 40), (59, 60)])
    names = {i: str(i) for i in graph}
    gd = GraphData(num_nodes=80, edge_index=np.asarray(list(graph.edges), dtype=np.int64).T,
                   id2node=names, node2id={v: k for k, v in names.items()}, directed=False)
    compute = GraphCompute()
    compute._MAX_VIZ_EDGES = 90
    result = compute.visualize_subgraph(gd.to_networkx(), gd)
    sample = nx.Graph()
    sample.add_nodes_from(n['id'] for n in result['nodes'])
    sample.add_edges_from((e['src'], e['dst']) for e in result['edges'])
    assert len(result['edges']) == 90
    assert nx.is_connected(sample)

import networkx as nx
from graph_data_service.compute import GraphCompute
from graph_data_service.graph_data import GraphData


def test_truncated_preview_retains_full_graph_degrees():
    graph = nx.star_graph(20)
    result = GraphCompute().visualize_subgraph(graph, GraphData(num_nodes=21), max_nodes=4)
    assert result['truncated'] is True
    assert result['degree_scope'] == 'full_graph'
    assert len(result['nodes']) == 4
    hub = next(node for node in result['nodes'] if node['id'] == 0)
    assert hub['total_degree'] == 20
    assert len(result['edges']) == 3


def test_directed_preview_preserves_incoming_outgoing_and_parallel_edges():
    graph = nx.MultiDiGraph()
    graph.add_edges_from([('a', 'b'), ('a', 'b'), ('b', 'a'), ('a', 'a')])
    result = GraphCompute().visualize_subgraph(graph, GraphData(num_nodes=2), node_names=['a', 'b'])
    a = next(node for node in result['nodes'] if node['id'] == 'a')
    assert (a['in_degree'], a['out_degree'], a['total_degree']) == (2, 3, 5)
    assert result['directed'] and len(result['edges']) == 4


def test_explicit_node_visualization_returns_complete_text():
    text = "complete node text " * 40
    data = GraphData(
        num_nodes=1,
        id2node={0: "node-1"},
        node2id={"node-1": 0},
        node_text=[text],
    )
    graph = data.to_networkx()

    overview = GraphCompute().visualize_subgraph(graph, data)
    detail = GraphCompute().visualize_subgraph(graph, data, node_names=["node-1"])

    assert overview["nodes"][0]["attrs"]["text"] == text[:300] + "..."
    assert detail["nodes"][0]["attrs"]["text"] == text


def test_constructed_graph_displays_node_types_without_changing_row_ids():
    names = {0: "row_abc123", 1: "row_def456"}
    data = GraphData(
        num_nodes=2,
        id2node=names,
        node2id={name: index for index, name in names.items()},
        node_type=["products", "products"],
        graph_metadata={"construction_dataset_id": "instacart"},
    )

    result = GraphCompute().visualize_subgraph(data.to_networkx(), data)

    assert {node["id"] for node in result["nodes"]} == set(names.values())
    assert {node["label"] for node in result["nodes"]} == {"products"}

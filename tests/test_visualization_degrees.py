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


def test_constructed_graph_displays_distinct_row_labels_without_changing_ids():
    from graph_data_service.construction import to_graph_data

    graph = {"directed": True, "nodes": [
        {"id": "row_abc123", "type": "products", "properties": {
            "table": "products", "row_index": 0, "data": {"product_id": "1"}}},
        {"id": "row_def456", "type": "products", "properties": {
            "table": "products", "row_index": 1, "data": {"product_id": "2"}}},
    ], "edges": [{"id": "e1", "source": "row_abc123", "target": "row_def456",
                  "type": "related", "properties": {}}]}
    data = to_graph_data({"dataset_id": "instacart", "revision": 1, "graph": graph})
    result = GraphCompute().visualize_subgraph(data.to_networkx(), data)

    assert {node["id"]: node["label"] for node in result["nodes"]} == {
        "row_abc123": "products · #1", "row_def456": "products · #2"}
    assert (result["edges"][0]["src"], result["edges"][0]["dst"]) == (
        "row_abc123", "row_def456")


def test_constructed_graph_keeps_non_row_names_and_missing_row_metadata():
    from graph_data_service.construction import to_graph_data

    graph = {"directed": True, "nodes": [
        {"id": "Alice", "type": "Person", "properties": {}},
        {"id": "Bob", "type": "Person", "properties": {}},
        {"id": "row_legacy", "type": "products", "properties": {}},
    ], "edges": []}
    data = to_graph_data({"dataset_id": "people", "revision": 1, "graph": graph})
    result = GraphCompute().visualize_subgraph(data.to_networkx(), data)
    assert all(node["label"] == node["id"] for node in result["nodes"])


def test_regular_graph_keeps_original_node_labels():
    data = GraphData(num_nodes=1, id2node={0: "row_business_id"},
                     node2id={"row_business_id": 0}, node_type=["products"])
    result = GraphCompute().visualize_subgraph(data.to_networkx(), data)
    assert result["nodes"][0]["label"] == "row_business_id"


def test_semantic_entity_labels_keep_stable_ids_and_searchable_names():
    from graph_data_service.construction import to_graph_data

    graph = {"directed": True, "nodes": [
        {"id": "entity_a", "type": "人物", "properties": {"data": {"name": "段誉"}}},
        {"id": "entity_b", "type": "人物", "properties": {"data": {"name": "虚竹"}}},
        {"id": "entity_legacy", "type": "人物", "properties": {}},
    ], "edges": [{"id": "e1", "source": "entity_a", "target": "entity_b",
                  "type": "结义兄弟", "properties": {}}]}
    data = to_graph_data({"dataset_id": "novel", "revision": 1, "graph": graph})
    result = GraphCompute().visualize_subgraph(data.to_networkx(), data)
    assert {node["id"]: node["label"] for node in result["nodes"]} == {
        "entity_a": "段誉", "entity_b": "虚竹", "entity_legacy": "entity_legacy"}
    assert (result["edges"][0]["src"], result["edges"][0]["dst"]) == ("entity_a", "entity_b")

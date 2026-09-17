"""Allowlisted, independently computed operations; never client-selected code."""

ISOLATED_DATA_TOOLS = frozenset({
    'load_graph', 'load_graph_from_upload', 'load_graph_from_text',
    'load_graph_from_triples', 'load_dataset', 'to_undirected',
    'subgraph_extraction', 'search_node', 'search_edge', 'visualize_subgraph',
    'get_graph_info',
    'upload_document', 'retrieve_context',
    'load_graph_classification_graph',
    'undo_graph', 'load_graph_version', 'discard_graph_version', 'save_as_dataset',
})

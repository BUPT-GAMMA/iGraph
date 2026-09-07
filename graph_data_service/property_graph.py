"""Property-graph relationship identity and numeric weight compatibility."""
from graph_engine.analysis_policy import numeric_weight


def property_weights(metadata, edge_names):
    graph = metadata.get('construction_graph')
    if not graph or edge_names is None:
        return None
    rows = {row['id']: row['properties'] for row in graph['edges']}
    return [numeric_weight(rows.get(str(name), {}).get('weight', 1.0)) for name in edge_names]

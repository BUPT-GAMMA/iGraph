"""Property-graph relationship identity and numeric weight compatibility."""
from graph_engine.analysis_policy import numeric_weight


class UnsupportedPropertyGraphAppend(RuntimeError):
    """A legacy merge would discard property-graph identity and attributes."""

    def __init__(self):
        super().__init__(
            "追加接口暂不支持属性图合并，原图已保留。"
            "请使用构图工作台支持的编辑操作；覆盖或清空图需要用户明确要求。"
        )

    def to_dict(self):
        return {"success": False, "code": "UNSUPPORTED_PROPERTY_GRAPH_APPEND", "error": str(self)}


def require_lossless_append(*graphs):
    """Check both merge inputs before projecting either one to edges/triples."""
    if any(graph is not None and graph.graph_metadata.get("construction_graph") is not None
           for graph in graphs):
        raise UnsupportedPropertyGraphAppend()


def property_weights(metadata, edge_names):
    graph = metadata.get('construction_graph')
    if not graph or edge_names is None:
        return None
    rows = {row['id']: row['properties'] for row in graph['edges']}
    return [numeric_weight(rows.get(str(name), {}).get('weight', 1.0)) for name in edge_names]

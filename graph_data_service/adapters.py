"""
GraphData 适配层：对外图对象转换（如 NetworkX）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional
import copy

import numpy as np

if TYPE_CHECKING:
    from graph_data_service.graph_data import GraphData


def _label_entry(label_arr: Optional[np.ndarray], idx: int) -> Any:
    if label_arr is None:
        return None
    val = label_arr[idx]
    if label_arr.ndim == 2:
        return val.tolist()
    if label_arr.dtype.kind in ("i", "u"):
        return int(val)
    if label_arr.dtype == object:
        return str(val)
    return float(val)


def graph_to_networkx(
    graph: "GraphData",
    feat_as_attr: bool = True,
    use_node_names: bool = True,
):
    """将 GraphData 转换为 NetworkX 图对象。"""
    try:
        import networkx as nx
    except ImportError as e:
        raise ImportError("请先安装 networkx: pip install networkx") from e

    construction = graph.graph_metadata.get("construction_graph") or {}
    property_nodes = {row["id"]: row for row in construction.get("nodes", [])} if construction else {}
    property_edges = {row["id"]: row for row in construction.get("edges", [])} if construction else {}
    # Ordinary graphs retain their historical simple projection. Constructed
    # graphs preserve parallel relationship identities rather than overwriting
    # one relationship's properties with another's.
    pairs = [tuple(map(int, pair)) if graph.directed else tuple(sorted(map(int, pair)))
             for pair in graph.edge_index.T] if construction else []
    multiple = bool(construction and len(pairs) != len(set(pairs)))
    g = (nx.MultiDiGraph() if graph.directed else nx.MultiGraph()) if multiple else (nx.DiGraph() if graph.directed else nx.Graph())
    g.name = graph.name
    if graph.graph_desc:
        g.graph["description"] = graph.graph_desc
    if graph.graph_label is not None:
        g.graph["label"] = (
            graph.graph_label.tolist()
            if isinstance(graph.graph_label, np.ndarray)
            else graph.graph_label
        )
    if graph.label_names is not None:
        g.graph["label_names"] = graph.label_names

    id2node = graph.id2node

    def _nid(i: int):
        return id2node.get(i, str(i)) if use_node_names else i

    for i in range(graph.num_nodes):
        attrs: Dict[str, Any] = {}
        if use_node_names:
            attrs["node_id"] = i
        elif i in id2node:
            attrs["label"] = id2node[i]

        if feat_as_attr and graph.node_feat is not None:
            attrs["feat"] = graph.node_feat[i].tolist()
        if graph.node_label is not None:
            attrs["node_label"] = _label_entry(graph.node_label, i)
        if graph.node_type is not None:
            attrs["node_type"] = str(graph.node_type[i])
        if graph.node_text is not None:
            attrs["text"] = graph.node_text[i]

        if id2node.get(i) in property_nodes:
            properties = copy.deepcopy(property_nodes[id2node[i]]["properties"])
            for key, value in properties.items():
                if key not in {"node_for_adding"}:
                    attrs.setdefault(key, value)
            attrs["properties"] = properties
            attrs["id"] = id2node[i]
            attrs["type"] = str(graph.node_type[i]) if graph.node_type is not None else property_nodes[id2node[i]]["type"]

        g.add_node(_nid(i), **attrs)

    src_arr, dst_arr = graph.edge_index
    for j in range(graph.num_edges):
        attrs = {}
        if graph.edge_names is not None:
            attrs["name"] = str(graph.edge_names[j])
        if feat_as_attr:
            if graph.edge_weight is not None:
                attrs["weight"] = float(graph.edge_weight[j])
            if graph.edge_feat is not None:
                attrs["feat"] = graph.edge_feat[j].tolist()
        if graph.edge_label is not None:
            attrs["edge_label"] = _label_entry(graph.edge_label, j)
        if graph.edge_type is not None:
            attrs["edge_type"] = str(graph.edge_type[j])
        if graph.edge_text is not None:
            attrs["text"] = graph.edge_text[j]

        edge_id = str(graph.edge_names[j]) if graph.edge_names is not None else ""
        if edge_id in property_edges:
            properties = copy.deepcopy(property_edges[edge_id]["properties"])
            for key, value in properties.items():
                if key not in {"u_for_edge", "v_for_edge", "u_of_edge", "v_of_edge", "key"}:
                    attrs.setdefault(key, value)
            attrs["properties"] = properties
            attrs["id"] = edge_id
            attrs["type"] = str(graph.edge_type[j]) if graph.edge_type is not None else property_edges[edge_id]["type"]

        if multiple:
            # Use an integer multigraph key; business properties may legitimately
            # contain a property named 'key'. They remain inside properties.
            attrs.pop("key", None)
            g.add_edge(_nid(int(src_arr[j])), _nid(int(dst_arr[j])), key=j, **attrs)
        else:
            g.add_edge(_nid(int(src_arr[j])), _nid(int(dst_arr[j])), **attrs)

    return g

"""
GraphData 标准化编解码

用于 GraphData Server 与 MCP Server 之间的标准化数据交换。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np

from graph_data_service.graph_data import GraphData
from graph_data_service.labels import (
    aligned_label_names,
    class_mapping,
    display_label,
    label_value,
    sorted_distinct_label_values,
)

GRAPH_DATA_SCHEMA_VERSION = "1.0"


def _arr_to_list(arr: Optional[np.ndarray]) -> Optional[list]:
    if arr is None:
        return None
    return arr.tolist()


def _normalize_graph_label(label: Any) -> Any:
    if isinstance(label, np.ndarray):
        return label.tolist()
    return label


def _node_label_stats(node_label: Optional[np.ndarray]) -> Optional[Dict[str, Any]]:
    if node_label is None:
        return None

    arr = np.asarray(node_label)
    if arr.ndim == 2:
        valid = arr.sum(axis=1) > 0
        visible = int(np.sum(valid))
        total = int(arr.shape[0])
        return {
            "total": total,
            "visible": visible,
            "missing": int(total - visible),
            "partially_labeled": bool(0 < visible < total),
        }

    total = int(arr.shape[0])
    visible = 0
    for val in arr.tolist():
        if val is None:
            continue
        if isinstance(val, str) and (not val.strip()):
            continue
        if isinstance(val, (int, np.integer)) and int(val) < 0:
            continue
        if isinstance(val, (float, np.floating)) and np.isnan(float(val)):
            continue
        visible += 1

    return {
        "total": total,
        "visible": int(visible),
        "missing": int(total - visible),
        "partially_labeled": bool(0 < visible < total),
    }


def node_label_stats_for_graph(graph: GraphData) -> Optional[Dict[str, Any]]:
    return _node_label_stats(graph.node_label)


def node_label_values_for_graph(graph: GraphData, max_values: int = 50) -> List[Any]:
    if graph.node_label is None:
        return []

    arr = np.asarray(graph.node_label)
    if arr.ndim == 2:
        if arr.shape[1] <= 0:
            return []
        valid = arr.sum(axis=1) > 0
        if not np.any(valid):
            return []
        cls = np.argmax(arr[valid], axis=1).astype(np.int64).tolist()
        uniq = sorted(set(int(i) for i in cls))
        label_names = aligned_label_names(uniq, graph.label_names)
        return [display_label(uniq, label_names, i) for i in range(min(len(uniq), max_values))]

    values = sorted_distinct_label_values(arr.tolist())
    label_names = aligned_label_names(values, graph.label_names)
    return [display_label(values, label_names, i) for i in range(min(len(values), max_values))]


def node_label_class_mapping_for_graph(graph: GraphData, max_values: int = 50) -> List[dict[str, Any]]:
    if graph.node_label is None:
        return []
    arr = np.asarray(graph.node_label)
    if arr.ndim == 2:
        if arr.shape[1] <= 0:
            return []
        valid = arr.sum(axis=1) > 0
        if not np.any(valid):
            return []
        values: List[Any] = sorted(set(int(i) for i in np.argmax(arr[valid], axis=1).astype(np.int64).tolist()))
    else:
        values = sorted_distinct_label_values(arr.tolist())
    values = [label_value(v) for v in values[:max_values]]
    return class_mapping(values, aligned_label_names(values, graph.label_names))


def _distinct_str_values(values: Optional[np.ndarray], max_values: int = 50) -> List[str]:
    if values is None:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for raw in np.asarray(values).tolist():
        if raw is None:
            continue
        s = str(raw).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= max_values:
            break
    return out


def node_type_values_for_graph(graph: GraphData, max_values: int = 50) -> List[str]:
    return _distinct_str_values(graph.node_type, max_values=max_values)


def edge_type_values_for_graph(graph: GraphData, max_values: int = 50) -> List[str]:
    return _distinct_str_values(graph.edge_type, max_values=max_values)


def graph_ml_readiness_for_graph(graph: GraphData) -> Dict[str, Any]:
    stats = node_label_stats_for_graph(graph) or {
        "total": graph.num_nodes,
        "visible": 0,
        "missing": graph.num_nodes,
    }
    label_values = node_label_values_for_graph(graph, max_values=200)
    labeled_nodes = int(stats.get("visible", 0))
    label_classes = len(label_values)

    if not graph.has_node_label:
        node_cls_reason = "缺少 node_label，无法执行节点分类训练"
        node_cls_supported = False
    elif labeled_nodes < 2:
        node_cls_reason = "可见标签节点不足（至少需要 2 个）"
        node_cls_supported = False
    elif label_classes < 2:
        node_cls_reason = "标签类别不足（至少需要 2 类）"
        node_cls_supported = False
    else:
        node_cls_reason = "满足节点分类最小数据条件"
        node_cls_supported = True

    positive_edges = int(graph.num_unique_edges)
    if positive_edges < 2:
        link_reason = "边数量不足（至少需要 2 条）"
        link_supported = False
    else:
        link_reason = "满足链接预测最小数据条件"
        link_supported = True

    if graph.num_nodes < 1:
        pretrain_reason = "节点数量为 0，无法训练"
        pretrain_supported = False
    else:
        pretrain_reason = "满足预训练最小数据条件"
        pretrain_supported = True

    return {
        "node_classification": {
            "supported": node_cls_supported,
            "reason": node_cls_reason,
            "labeled_nodes": labeled_nodes,
            "label_classes": label_classes,
        },
        "link_prediction": {
            "supported": link_supported,
            "reason": link_reason,
            "positive_edges": positive_edges,
        },
        "pretrain": {
            "supported": pretrain_supported,
            "reason": pretrain_reason,
        },
    }


def graph_data_to_document(graph: GraphData) -> Dict[str, Any]:
    """将 GraphData 编码为可 JSON 传输的标准化文档。"""
    return {
        "schema_version": GRAPH_DATA_SCHEMA_VERSION,
        "num_nodes": graph.num_nodes,
        "directed": graph.directed,
        "semantic_type": graph.semantic_type,
        "storage_type": graph.storage_type,
        "num_edge_entries": graph.num_edge_entries,
        "num_unique_edges": graph.num_unique_edges,
        "num_unique_directed_edges": graph.num_unique_directed_edges,
        "num_unique_undirected_edges": graph.num_unique_undirected_edges,
        "is_symmetric": graph.is_symmetric,
        "has_self_loops": graph.has_self_loops,
        "has_duplicate_entries": graph.has_duplicate_entries,
        "name": graph.name,
        "graph_desc": graph.graph_desc,
        "graph_metadata": graph.graph_metadata,
        "transform_log": graph.transform_log,
        "id2node": {str(k): v for k, v in graph.id2node.items()} if graph.id2node else None,
        "node2id": graph.node2id if graph.node2id else None,
        "edge_index": graph.edge_index.tolist(),
        "edge_names": _arr_to_list(graph.edge_names),
        "edge_weight": _arr_to_list(graph.edge_weight),
        "node_feat": _arr_to_list(graph.node_feat),
        "edge_feat": _arr_to_list(graph.edge_feat),
        "node_label": _arr_to_list(graph.node_label),
        "node_type": _arr_to_list(graph.node_type),
        "edge_label": _arr_to_list(graph.edge_label),
        "edge_type": _arr_to_list(graph.edge_type),
        "graph_label": _normalize_graph_label(graph.graph_label),
        "node_label_stats": node_label_stats_for_graph(graph),
        # 文本图 (TAG) 字段
        "node_text": graph.node_text,
        "edge_text": graph.edge_text,
        "label_names": graph.label_names,
        "train_mask": graph.train_mask.tolist() if graph.train_mask is not None else None,
        "val_mask": graph.val_mask.tolist() if graph.val_mask is not None else None,
        "test_mask": graph.test_mask.tolist() if graph.test_mask is not None else None,
    }


def graph_data_from_document(doc: Dict[str, Any]) -> GraphData:
    """从标准化文档解码为 GraphData。"""
    id2node_doc = doc.get("id2node")
    id2node = (
        {int(k): str(v) for k, v in id2node_doc.items()}
        if isinstance(id2node_doc, dict)
        else None
    )
    node2id_doc = doc.get("node2id")
    node2id = (
        {str(k): int(v) for k, v in node2id_doc.items()}
        if isinstance(node2id_doc, dict)
        else None
    )

    return GraphData(
        num_nodes=int(doc["num_nodes"]),
        edge_index=np.asarray(doc["edge_index"], dtype=np.int64),
        id2node=id2node,
        node2id=node2id,
        edge_names=doc.get("edge_names"),
        edge_weight=(
            np.asarray(doc["edge_weight"], dtype=np.float32)
            if doc.get("edge_weight") is not None
            else None
        ),
        node_feat=(
            np.asarray(doc["node_feat"], dtype=np.float32)
            if doc.get("node_feat") is not None
            else None
        ),
        edge_feat=(
            np.asarray(doc["edge_feat"], dtype=np.float32)
            if doc.get("edge_feat") is not None
            else None
        ),
        graph_desc=doc.get("graph_desc"),
        directed=bool(doc.get("directed", True)),
        semantic_type=doc.get("semantic_type"),
        storage_type=doc.get("storage_type"),
        graph_metadata=doc.get("graph_metadata") if isinstance(doc.get("graph_metadata"), dict) else None,
        transform_log=doc.get("transform_log") if isinstance(doc.get("transform_log"), list) else None,
        name=str(doc.get("name", "")),
        node_label=doc.get("node_label"),
        node_type=doc.get("node_type"),
        edge_label=doc.get("edge_label"),
        edge_type=doc.get("edge_type"),
        graph_label=doc.get("graph_label"),
        # 文本图 (TAG) 字段
        node_text=doc.get("node_text"),
        edge_text=doc.get("edge_text"),
        label_names=doc.get("label_names"),
        train_mask=(
            np.asarray(doc["train_mask"], dtype=bool)
            if doc.get("train_mask") is not None
            else None
        ),
        val_mask=(
            np.asarray(doc["val_mask"], dtype=bool)
            if doc.get("val_mask") is not None
            else None
        ),
        test_mask=(
            np.asarray(doc["test_mask"], dtype=bool)
            if doc.get("test_mask") is not None
            else None
        ),
    )


def graph_edges_preview(graph: GraphData) -> list[dict]:
    """输出含节点名称和权重的边列表预览。"""
    id2node = graph.id2node
    src_arr, dst_arr = graph.edge_index
    edges = []
    for i in range(graph.num_edges):
        src = int(src_arr[i])
        dst = int(dst_arr[i])
        edges.append(
            {
                "src": id2node.get(src, str(src)),
                "dst": id2node.get(dst, str(dst)),
                "weight": (
                    float(graph.edge_weight[i])
                    if graph.edge_weight is not None
                    else 1.0
                ),
            }
        )
    return edges

"""
GraphData IO 层：支持 .npz 与 .json 持久化
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

import numpy as np

if TYPE_CHECKING:
    from graph_data_service.graph_data import GraphData


def save_graph_data(graph: "GraphData", path: str) -> None:
    """将 GraphData 保存为 .npz 或 .json 文件（由后缀决定）。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        save_graph_data_json(graph, path)
        return

    data: Dict[str, Any] = {
        "_num_nodes": np.array(graph.num_nodes),
        "_directed": np.array(graph.directed),
        "_name": np.array(graph.name),
        "edge_index": graph.edge_index,
    }
    if graph.graph_desc is not None:
        data["_graph_desc"] = np.array(graph.graph_desc)

    id2node = graph.id2node
    if id2node:
        data["map_ids"] = np.array(list(id2node.keys()), dtype=np.int64)
        data["map_names"] = np.array(list(id2node.values()), dtype=str)

    if graph.edge_names is not None:
        data["edge_names"] = graph.edge_names
    if graph.node_feat is not None:
        data["node_feat"] = graph.node_feat
    if graph.edge_feat is not None:
        data["edge_feat"] = graph.edge_feat
    if graph.edge_weight is not None:
        data["edge_weight"] = graph.edge_weight
    if graph.node_label is not None:
        nl = graph.node_label
        data["node_label"] = nl.astype(str) if nl.dtype == object else nl
    if graph.node_type is not None:
        data["node_type"] = graph.node_type.astype(str)
    if graph.edge_label is not None:
        el = graph.edge_label
        data["edge_label"] = el.astype(str) if el.dtype == object else el
    if graph.edge_type is not None:
        data["edge_type"] = graph.edge_type.astype(str)

    if graph.graph_label is not None:
        if isinstance(graph.graph_label, np.ndarray):
            data["graph_label"] = graph.graph_label
        elif isinstance(graph.graph_label, int):
            data["graph_label"] = np.array(graph.graph_label, dtype=np.int64)
            data["_graph_label_type"] = np.array("int")
        else:
            data["graph_label"] = np.array(graph.graph_label, dtype=str)
            data["_graph_label_type"] = np.array("str")

    # 文本图 (TAG) 字段
    if graph.node_text is not None:
        data["node_text"] = np.array(graph.node_text, dtype=object)
    if graph.edge_text is not None:
        data["edge_text"] = np.array(graph.edge_text, dtype=object)
    if graph.label_names is not None:
        data["label_names"] = np.array(graph.label_names, dtype=str)
    if graph.train_mask is not None:
        data["train_mask"] = graph.train_mask
    if graph.val_mask is not None:
        data["val_mask"] = graph.val_mask
    if graph.test_mask is not None:
        data["test_mask"] = graph.test_mask

    np.savez(path, **data)


def load_graph_data(path: str, graph_cls: type["GraphData"]) -> "GraphData":
    """从 .npz、.json 或 .pt 文件加载 GraphData（由后缀决定）。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        return load_graph_data_json(path, graph_cls)
    if ext == ".pt":
        return load_graph_data_pt(path, graph_cls)

    raw = np.load(path, allow_pickle=True)

    num_nodes = int(raw["_num_nodes"])
    directed = bool(raw["_directed"])
    name = str(raw["_name"]) if "_name" in raw else ""
    graph_desc = str(raw["_graph_desc"]) if "_graph_desc" in raw else None

    id2node: Optional[Dict[int, str]] = None
    node2id: Optional[Dict[str, int]] = None
    if "map_ids" in raw and "map_names" in raw:
        ids: List[int] = raw["map_ids"].tolist()
        names: List[str] = raw["map_names"].tolist()
        id2node = dict(zip(ids, names))
        node2id = {v: k for k, v in id2node.items()}

    graph_label: Optional[Union[str, int, np.ndarray]] = None
    if "graph_label" in raw:
        label_type = str(raw["_graph_label_type"]) if "_graph_label_type" in raw else "array"
        if label_type == "str":
            graph_label = str(raw["graph_label"])
        elif label_type == "int":
            graph_label = int(raw["graph_label"])
        else:
            graph_label = raw["graph_label"]

    node_text = raw["node_text"].tolist() if "node_text" in raw else None
    edge_text = raw["edge_text"].tolist() if "edge_text" in raw else None
    label_names = raw["label_names"].tolist() if "label_names" in raw else None
    train_mask = raw["train_mask"] if "train_mask" in raw else None
    val_mask = raw["val_mask"] if "val_mask" in raw else None
    test_mask = raw["test_mask"] if "test_mask" in raw else None

    return graph_cls(
        num_nodes=num_nodes,
        edge_index=raw["edge_index"],
        id2node=id2node,
        node2id=node2id,
        edge_names=raw["edge_names"].tolist() if "edge_names" in raw else None,
        edge_weight=raw["edge_weight"] if "edge_weight" in raw else None,
        node_feat=raw["node_feat"] if "node_feat" in raw else None,
        edge_feat=raw["edge_feat"] if "edge_feat" in raw else None,
        graph_desc=graph_desc,
        directed=directed,
        name=name,
        node_label=raw["node_label"] if "node_label" in raw else None,
        node_type=raw["node_type"].tolist() if "node_type" in raw else None,
        edge_label=raw["edge_label"] if "edge_label" in raw else None,
        edge_type=raw["edge_type"].tolist() if "edge_type" in raw else None,
        graph_label=graph_label,
        node_text=node_text,
        edge_text=edge_text,
        label_names=label_names,
        train_mask=train_mask,
        val_mask=val_mask,
        test_mask=test_mask,
    )


def save_graph_data_json(graph: "GraphData", path: str) -> None:
    """
    将 GraphData 保存为 JSON（graph_data.v1）。

    格式（字段均与 GraphData 构造参数对齐）：
    {
      "schema": "graph_data.v1",
      "num_nodes": 3,
      "directed": true,
      "name": "demo",
      "graph_desc": "...",
      "id2node": {"0":"A","1":"B","2":"C"},
      "edge_index": [[0,1],[1,2]],
      "edge_weight": [1.0, 2.0],
      "edge_names": ["e0","e1"],
      "node_feat": [[...], [...], [...]],
      "edge_feat": [[...], [...]],
      "node_label": [...],
      "node_type": [...],
      "edge_label": [...],
      "edge_type": [...],
      "graph_label": "optional"
    }
    """
    doc: Dict[str, Any] = {
        "schema": "graph_data.v1",
        "num_nodes": int(graph.num_nodes),
        "directed": bool(graph.directed),
        "name": graph.name,
        "edge_index": graph.edge_index.tolist(),
    }
    if graph.graph_desc is not None:
        doc["graph_desc"] = graph.graph_desc
    if graph.id2node:
        doc["id2node"] = {str(k): str(v) for k, v in graph.id2node.items()}
    if graph.edge_names is not None:
        doc["edge_names"] = graph.edge_names.tolist()
    if graph.edge_weight is not None:
        doc["edge_weight"] = graph.edge_weight.tolist()
    if graph.node_feat is not None:
        doc["node_feat"] = graph.node_feat.tolist()
    if graph.edge_feat is not None:
        doc["edge_feat"] = graph.edge_feat.tolist()
    if graph.node_label is not None:
        doc["node_label"] = graph.node_label.tolist()
    if graph.node_type is not None:
        doc["node_type"] = graph.node_type.tolist()
    if graph.edge_label is not None:
        doc["edge_label"] = graph.edge_label.tolist()
    if graph.edge_type is not None:
        doc["edge_type"] = graph.edge_type.tolist()
    if graph.graph_label is not None:
        if isinstance(graph.graph_label, np.ndarray):
            doc["graph_label"] = graph.graph_label.tolist()
        else:
            doc["graph_label"] = graph.graph_label

    # 文本图 (TAG) 字段
    if graph.node_text is not None:
        doc["node_text"] = graph.node_text
    if graph.edge_text is not None:
        doc["edge_text"] = graph.edge_text
    if graph.label_names is not None:
        doc["label_names"] = graph.label_names
    if graph.train_mask is not None:
        doc["train_mask"] = graph.train_mask.tolist()
    if graph.val_mask is not None:
        doc["val_mask"] = graph.val_mask.tolist()
    if graph.test_mask is not None:
        doc["test_mask"] = graph.test_mask.tolist()

    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


def load_graph_data_json(path: str, graph_cls: type["GraphData"]) -> "GraphData":
    """从 graph_data.v1 JSON 文件加载 GraphData。"""
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise ValueError("JSON 根对象必须为 object")

    schema = str(doc.get("schema", "graph_data.v1"))
    if schema != "graph_data.v1":
        raise ValueError(f"不支持的 JSON schema: {schema}")

    if "num_nodes" not in doc:
        raise ValueError("JSON 缺少必填字段: num_nodes")
    if "edge_index" not in doc:
        raise ValueError("JSON 缺少必填字段: edge_index")

    num_nodes = int(doc["num_nodes"])
    directed = bool(doc.get("directed", True))
    name = str(doc.get("name", ""))
    graph_desc = doc.get("graph_desc")
    if graph_desc is not None:
        graph_desc = str(graph_desc)

    edge_index = np.asarray(doc["edge_index"], dtype=np.int64)
    if not (edge_index.ndim == 2 and edge_index.shape[0] == 2):
        raise ValueError("edge_index 必须是 shape [2, E] 的二维数组")

    id2node: Optional[Dict[int, str]] = None
    node2id: Optional[Dict[str, int]] = None
    if "id2node" in doc and doc["id2node"] is not None:
        raw_map = doc["id2node"]
        if not isinstance(raw_map, dict):
            raise ValueError("id2node 必须是 object")
        id2node = {int(k): str(v) for k, v in raw_map.items()}
        node2id = {v: k for k, v in id2node.items()}

    edge_names = _as_str_list(doc.get("edge_names"))
    edge_weight = _as_float_array(doc.get("edge_weight"))
    node_feat = _as_float_array(doc.get("node_feat"))
    edge_feat = _as_float_array(doc.get("edge_feat"))
    node_label = _as_label_array(doc.get("node_label"))
    edge_label = _as_label_array(doc.get("edge_label"))
    node_type = _as_str_list(doc.get("node_type"))
    edge_type = _as_str_list(doc.get("edge_type"))
    graph_label = doc.get("graph_label")

    # 文本图 (TAG) 字段
    node_text = _as_str_list(doc.get("node_text"))
    edge_text = _as_str_list(doc.get("edge_text"))
    label_names = _as_str_list(doc.get("label_names"))
    train_mask = (
        np.asarray(doc["train_mask"], dtype=bool)
        if doc.get("train_mask") is not None
        else None
    )
    val_mask = (
        np.asarray(doc["val_mask"], dtype=bool)
        if doc.get("val_mask") is not None
        else None
    )
    test_mask = (
        np.asarray(doc["test_mask"], dtype=bool)
        if doc.get("test_mask") is not None
        else None
    )

    return graph_cls(
        num_nodes=num_nodes,
        edge_index=edge_index,
        id2node=id2node,
        node2id=node2id,
        edge_names=edge_names,
        edge_weight=edge_weight,
        node_feat=node_feat,
        edge_feat=edge_feat,
        graph_desc=graph_desc,
        directed=directed,
        name=name,
        node_label=node_label,
        node_type=node_type,
        edge_label=edge_label,
        edge_type=edge_type,
        graph_label=graph_label,
        node_text=node_text,
        edge_text=edge_text,
        label_names=label_names,
        train_mask=train_mask,
        val_mask=val_mask,
        test_mask=test_mask,
    )


def _as_str_list(v: Any) -> Optional[List[str]]:
    if v is None:
        return None
    if not isinstance(v, list):
        raise ValueError("字符串列表字段必须是 list")
    return [str(x) for x in v]


def _as_float_array(v: Any) -> Optional[np.ndarray]:
    if v is None:
        return None
    return np.asarray(v, dtype=np.float32)


def _as_label_array(v: Any) -> Optional[np.ndarray]:
    if v is None:
        return None
    arr = np.asarray(v)
    if arr.ndim == 2:
        return arr.astype(np.float32)
    if arr.dtype.kind in ("U", "S", "O"):
        return arr.astype(object)
    if arr.dtype.kind in ("i", "u"):
        return arr.astype(np.int64)
    return arr.astype(np.float32)


def load_graph_data_pt(path: str, graph_cls: type["GraphData"]) -> "GraphData":
    """
    从 PyTorch Geometric .pt 文件（TAGLAS 格式）加载 GraphData。

    支持以下字段（自动适配多种命名变体）：
        edge_index, x (数值 Tensor), y, raw_texts/raw_text, label_name/label_names,
        train_mask/train_masks, val_mask/val_masks, test_mask/test_masks
    """
    try:
        import torch
    except ImportError as e:
        raise ImportError(
            "加载 .pt 文件需要安装 PyTorch：pip install torch"
        ) from e

    data = torch.load(path, map_location="cpu", weights_only=False)

    # Some datasets store multiple graph snapshots as {sample_id: graph_dict}.
    # GraphData represents one graph, so load the first sample deterministically.
    if isinstance(data, dict):
        graph_keys = {
            "num_nodes",
            "edge_index",
            "x",
            "x_original",
            "y",
            "labels",
        }
        if not graph_keys.intersection(data) and data:
            sample_keys = list(data)
            try:
                sample_keys.sort()
            except TypeError:
                sample_keys.sort(key=lambda key: (type(key).__name__, repr(key)))
            first_sample = data[sample_keys[0]]
            if isinstance(first_sample, dict):
                data = first_sample

        if isinstance(data, dict):
            normalized = dict(data)
            if "y" not in normalized and "labels" in normalized:
                normalized["y"] = normalized["labels"]
            if "y" not in normalized and "label" in normalized:
                normalized["y"] = normalized["label"]
            if "x" not in normalized and "node_feature" in normalized:
                normalized["x"] = normalized["node_feature"]
            data = SimpleNamespace(**normalized)

    # ── num_nodes ────────────────────────────────────────────────────
    if hasattr(data, "num_nodes") and isinstance(getattr(data, "num_nodes"), int):
        num_nodes: int = data.num_nodes
    elif hasattr(data, "x") and isinstance(data.x, torch.Tensor):
        num_nodes = data.x.shape[0]
    elif hasattr(data, "y") and isinstance(data.y, torch.Tensor):
        num_nodes = data.y.shape[0]
    else:
        raise ValueError(f"无法从 .pt 文件确定节点数: {path}")

    # ── node name maps ──────────────────────────────────────────────
    id2node: Optional[Dict[int, str]] = None
    node2id: Optional[Dict[str, int]] = None
    node_index = getattr(data, "node_index", None)
    if isinstance(node_index, dict):
        candidate_node2id = {
            str(node_name): int(node_id)
            for node_name, node_id in node_index.items()
        }
        if (
            len(candidate_node2id) == num_nodes
            and set(candidate_node2id.values()) == set(range(num_nodes))
        ):
            node2id = candidate_node2id
            id2node = {node_id: node_name for node_name, node_id in node2id.items()}

    # ── edge_index ───────────────────────────────────────────────────
    if hasattr(data, "edge_index") and data.edge_index is not None:
        edge_index = data.edge_index.cpu().numpy().astype(np.int64)
        if edge_index.ndim != 2:
            raise ValueError(
                f".pt 文件中的 edge_index 必须是二维数组，实际 shape={edge_index.shape}: {path}"
            )
        if edge_index.shape[0] == 2:
            pass
        elif edge_index.shape[1] == 2:
            edge_index = np.ascontiguousarray(edge_index.T)
        else:
            raise ValueError(
                ".pt 文件中的 edge_index 必须是 [2, E] 或 [E, 2]，"
                f"实际 shape={edge_index.shape}: {path}"
            )
    else:
        edge_index = np.empty((2, 0), dtype=np.int64)

    # ── node_feat ────────────────────────────────────────────────────
    node_feat: Optional[np.ndarray] = None
    x_val = getattr(data, "x", None)
    if x_val is not None and isinstance(x_val, torch.Tensor):
        node_feat = x_val.cpu().numpy().astype(np.float32)
    # 若 x 已被重命名（TAGLAS 处理后格式：x_original 存数值特征）
    x_orig = getattr(data, "x_original", None)
    if x_orig is not None and isinstance(x_orig, torch.Tensor):
        node_feat = x_orig.cpu().numpy().astype(np.float32)

    # ── node_label ───────────────────────────────────────────────────
    node_label: Optional[np.ndarray] = None
    y_val = getattr(data, "y", None)
    if y_val is not None and isinstance(y_val, torch.Tensor):
        node_label = y_val.cpu().numpy().astype(np.int64)

    # ── node_text ────────────────────────────────────────────────────
    node_text: Optional[List[str]] = None
    for attr in ("raw_texts", "raw_text"):
        val = getattr(data, attr, None)
        if val is not None and isinstance(val, list) and len(val) > 0 and isinstance(val[0], str):
            node_text = val
            break
    # 若 x 为字符串列表（TAGLAS 处理后格式）
    if node_text is None and x_val is not None and isinstance(x_val, list):
        if len(x_val) > 0 and isinstance(x_val[0], str):
            node_text = x_val

    # ── edge_text / edge_feat ────────────────────────────────────────
    edge_text: Optional[List[str]] = None
    edge_feat: Optional[np.ndarray] = None
    ea = getattr(data, "edge_attr", None)
    if ea is not None:
        if isinstance(ea, list) and len(ea) > 0 and isinstance(ea[0], str):
            emap = getattr(data, "edge_map", None)
            if emap is not None:
                if isinstance(emap, torch.Tensor):
                    emap = emap.cpu().numpy()
                edge_text = [ea[int(i)] for i in emap]
            else:
                # 若 edge_attr 长度 == num_edges，直接用；否则广播第一个
                num_edges = edge_index.shape[1]
                if len(ea) == num_edges:
                    edge_text = ea
                elif len(ea) > 0:
                    edge_text = [ea[0]] * num_edges
        elif isinstance(ea, torch.Tensor):
            edge_feat = ea.cpu().numpy().astype(np.float32)

    # ── label_names ──────────────────────────────────────────────────
    label_names: Optional[List[str]] = None
    for attr in ("label_name", "label_names"):
        val = getattr(data, attr, None)
        if val is not None and isinstance(val, list) and len(val) > 0 and isinstance(val[0], str):
            label_names = val
            break

    # ── 划分掩码 ─────────────────────────────────────────────────────
    def _extract_mask(*attrs: str) -> Optional[np.ndarray]:
        value = None
        for attr in attrs:
            candidate = getattr(data, attr, None)
            if candidate is None:
                continue
            if isinstance(candidate, list):
                if not candidate:
                    continue
                candidate = candidate[0]
            if isinstance(candidate, torch.Tensor):
                value = candidate.cpu().numpy()
                break

        if value is None:
            return None

        value = np.asarray(value)
        if value.ndim == 2 and 1 in value.shape:
            value = value.reshape(-1)
        if value.ndim != 1:
            raise ValueError(
                f"{attrs[0]} 必须是一维布尔掩码或节点索引，实际 shape={value.shape}: {path}"
            )

        if value.shape[0] == num_nodes and (
            np.issubdtype(value.dtype, np.bool_)
            or np.all((value == 0) | (value == 1))
        ):
            return value.astype(bool)

        if not np.issubdtype(value.dtype, np.integer):
            raise ValueError(
                f"{attrs[0]} 长度与节点数不一致时必须是整数节点索引，"
                f"实际 dtype={value.dtype}, shape={value.shape}: {path}"
            )

        indices = value.astype(np.int64, copy=False)
        if indices.size and (indices.min() < 0 or indices.max() >= num_nodes):
            raise ValueError(
                f"{attrs[0]} 包含越界节点索引，有效范围为 [0, {num_nodes - 1}]: {path}"
            )
        mask = np.zeros(num_nodes, dtype=bool)
        mask[indices] = True
        return mask

    train_mask = _extract_mask("train_mask", "train_masks")
    val_mask = _extract_mask("val_mask", "valid_mask", "val_masks", "valid_masks")
    test_mask = _extract_mask("test_mask", "test_masks")

    # ── name ─────────────────────────────────────────────────────────
    name = os.path.splitext(os.path.basename(path))[0]

    directed = True
    if hasattr(data, "is_directed"):
        if callable(data.is_directed):
            directed = data.is_directed()
        else:
            directed = bool(data.is_directed)
    elif hasattr(data, "directed"):
        directed = bool(data.directed)

    return graph_cls(
        num_nodes=num_nodes,
        edge_index=edge_index,
        id2node=id2node,
        node2id=node2id,
        node_feat=node_feat,
        edge_feat=edge_feat,
        directed=directed,
        name=name,
        node_label=node_label,
        node_text=node_text,
        edge_text=edge_text,
        label_names=label_names,
        train_mask=train_mask,
        val_mask=val_mask,
        test_mask=test_mask,
    )

"""
GraphData IO 层：支持 .npz 与 .json 持久化
"""

from __future__ import annotations

import json
import os
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
    从 PyTorch Geometric 单图 .pt 文件（TAGLAS 格式）加载 GraphData。

    支持以下字段（自动适配多种命名变体）：
        edge_index, x (数值 Tensor), y, raw_texts/raw_text, label_name/label_names,
        train_mask/train_masks, val_mask/valid_mask/val_masks/valid_masks,
        test_mask/test_masks

    多图图分类容器必须由专用 Loader 处理；本函数不会静默截取第一张图。
    """
    try:
        import torch
    except ImportError as e:
        raise ImportError(
            "加载 .pt 文件需要安装 PyTorch：pip install torch"
        ) from e

    data = torch.load(path, map_location="cpu", weights_only=False)

    if isinstance(data, dict):
        keys = set(data)
        if {"graphs", "texts", "split"}.issubset(keys):
            raise ValueError(
                "检测到多图图分类数据集（包含 graphs/texts/split）；"
                "请调用 load_graph_classification_dataset，单图 Loader 不会只加载第一张图。"
            )

        single_graph_structure = {
            "num_nodes",
            "edge_index",
            "x",
            "x_original",
            "node_feature",
        }
        if not single_graph_structure.intersection(keys):
            looks_like_sample_mapping = bool(data) and all(
                isinstance(value, dict) or hasattr(value, "edge_index")
                for value in data.values()
            )
            if looks_like_sample_mapping:
                raise ValueError(
                    "检测到包含多个图样本的字典；单图 Loader 不会排序后只加载第一张图。"
                    "如用于图分类，请调用 load_graph_classification_dataset。"
                )
            raise ValueError(
                ".pt 字典不符合单图数据契约：必须包含 num_nodes、edge_index、x、"
                "x_original 或 node_feature 之一。"
            )

        normalized = dict(data)
        if "y" not in normalized and "labels" in normalized:
            normalized["y"] = normalized["labels"]
        if "y" not in normalized and "label" in normalized:
            normalized["y"] = normalized["label"]
        if "x" not in normalized and "node_feature" in normalized:
            normalized["x"] = normalized["node_feature"]
        data = normalized

    missing = object()

    def _field(name: str, default: Any = None) -> Any:
        if isinstance(data, dict):
            return data.get(name, default)
        return getattr(data, name, default)

    def _to_numpy(value: Any) -> Optional[np.ndarray]:
        if isinstance(value, torch.Tensor):
            return value.detach().cpu().numpy()
        if isinstance(value, np.ndarray):
            return value
        if isinstance(value, (list, tuple)):
            return np.asarray(value)
        return None

    # ── num_nodes ────────────────────────────────────────────────────
    raw_num_nodes = _field("num_nodes")
    x_val = _field("x")
    x_array = _to_numpy(x_val)
    if isinstance(raw_num_nodes, (int, np.integer)) and not isinstance(raw_num_nodes, bool):
        num_nodes = int(raw_num_nodes)
        if num_nodes < 0:
            raise ValueError(f".pt 文件中的 num_nodes 不能为负数: {path}")
    elif x_array is not None and x_array.ndim >= 1:
        num_nodes = int(x_array.shape[0])
    else:
        raise ValueError(
            f"无法从 .pt 单图文件确定节点数（需要 num_nodes 或数组形式的 x）: {path}"
        )

    # ── node name maps ──────────────────────────────────────────────
    id2node: Optional[Dict[int, str]] = None
    node2id: Optional[Dict[str, int]] = None
    node_index = _field("node_index")
    if node_index is not None:
        if not isinstance(node_index, dict):
            raise ValueError(f".pt 文件中的 node_index 必须是 dict: {path}")
        try:
            candidate_node2id = {
                str(node_name): int(node_id)
                for node_name, node_id in node_index.items()
                if not isinstance(node_id, bool)
            }
        except (TypeError, ValueError) as exc:
            raise ValueError(f".pt 文件中的 node_index 含非法节点 ID: {path}") from exc
        if (
            len(candidate_node2id) != num_nodes
            or set(candidate_node2id.values()) != set(range(num_nodes))
        ):
            raise ValueError(
                ".pt 文件中的 node_index 必须完整且恰好覆盖节点 ID "
                f"[0, {max(0, num_nodes - 1)}]: {path}"
            )
        node2id = candidate_node2id
        id2node = {node_id: node_name for node_name, node_id in node2id.items()}

    # ── edge_index ───────────────────────────────────────────────────
    edge_value = _field("edge_index")
    if edge_value is not None:
        edge_array = _to_numpy(edge_value)
        if edge_array is None:
            raise ValueError(f".pt 文件中的 edge_index 必须是 Tensor 或数组: {path}")
        if edge_array.ndim != 2:
            raise ValueError(
                f".pt 文件中的 edge_index 必须是二维数组，实际 shape={edge_array.shape}: {path}"
            )
        if edge_array.shape[0] == 2:
            edge_index = edge_array
        elif edge_array.shape[1] == 2:
            edge_index = edge_array.T
        else:
            raise ValueError(
                ".pt 文件中的 edge_index 必须是 [2, E] 或 [E, 2]，"
                f"实际 shape={edge_array.shape}: {path}"
            )
        edge_index = np.ascontiguousarray(edge_index, dtype=np.int64)
        if edge_index.size and int(edge_index.min()) < 0:
            raise ValueError(f".pt 文件中的 edge_index 包含负节点 ID: {path}")
    else:
        edge_index = np.empty((2, 0), dtype=np.int64)

    # ── node_feat ────────────────────────────────────────────────────
    node_feat: Optional[np.ndarray] = None
    if x_array is not None and x_array.dtype.kind not in ("U", "S", "O"):
        node_feat = x_array.astype(np.float32)
    # 若 x 已被重命名（TAGLAS 处理后格式：x_original 存数值特征）
    x_orig = _field("x_original")
    x_orig_array = _to_numpy(x_orig)
    if x_orig_array is not None:
        if x_orig_array.ndim < 1 or x_orig_array.shape[0] != num_nodes:
            raise ValueError(f".pt 文件中的 x_original 与 num_nodes 不一致: {path}")
        if x_orig_array.dtype.kind in ("U", "S", "O"):
            raise ValueError(f".pt 文件中的 x_original 必须是数值数组: {path}")
        node_feat = x_orig_array.astype(np.float32)

    # ── node_label ───────────────────────────────────────────────────
    node_label: Optional[np.ndarray] = None
    y_val = _field("y")
    if y_val is not None:
        y_array = _to_numpy(y_val)
        if y_array is None or y_array.ndim == 0:
            raise ValueError(f".pt 文件中的 y 必须是节点标签数组: {path}")
        if y_array.shape[0] != num_nodes:
            raise ValueError(
                f".pt 文件中的 y 第一维({y_array.shape[0]})与节点数({num_nodes})不一致；"
                "单图入口只接受 node-level 标签，整图/多图分类请调用 "
                "load_graph_classification_dataset。"
            )
        node_label = _as_label_array(y_array)

    # ── node_text ────────────────────────────────────────────────────
    node_text: Optional[List[str]] = None
    for attr in ("raw_texts", "raw_text"):
        val = _field(attr)
        if isinstance(val, (list, tuple)) and val and all(isinstance(item, str) for item in val):
            node_text = list(val)
            break
    # 若 x 为字符串列表（TAGLAS 处理后格式）
    if node_text is None and isinstance(x_val, (list, tuple)):
        if x_val and all(isinstance(item, str) for item in x_val):
            node_text = list(x_val)

    # ── edge_text / edge_feat ────────────────────────────────────────
    edge_text: Optional[List[str]] = None
    edge_feat: Optional[np.ndarray] = None
    ea = _field("edge_attr")
    if ea is not None:
        if isinstance(ea, (list, tuple)) and ea and all(isinstance(item, str) for item in ea):
            emap = _field("edge_map")
            if emap is not None:
                emap_array = _to_numpy(emap)
                if emap_array is None:
                    raise ValueError(f".pt 文件中的 edge_map 必须是 Tensor 或数组: {path}")
                edge_text = [str(ea[int(i)]) for i in emap_array.reshape(-1)]
            else:
                # 若 edge_attr 长度 == num_edges，直接用；否则广播第一个
                num_edges = edge_index.shape[1]
                if len(ea) == num_edges:
                    edge_text = list(ea)
                else:
                    edge_text = [str(ea[0])] * num_edges
        else:
            ea_array = _to_numpy(ea)
            if ea_array is not None:
                edge_feat = ea_array.astype(np.float32)

    # ── label_names ──────────────────────────────────────────────────
    label_names: Optional[List[str]] = None
    for attr in ("label_name", "label_names"):
        val = _field(attr)
        if isinstance(val, (list, tuple)) and val and all(isinstance(item, str) for item in val):
            label_names = list(val)
            break

    # ── 划分掩码 ─────────────────────────────────────────────────────
    def _extract_mask(*attrs: str) -> Optional[np.ndarray]:
        for attr in attrs:
            candidate = _field(attr, missing)
            if candidate is missing or candidate is None:
                continue

            is_plural = attr.endswith("s")
            if is_plural and isinstance(candidate, (list, tuple)):
                if not candidate:
                    continue
                if isinstance(candidate[0], (torch.Tensor, np.ndarray, list, tuple)):
                    candidate = candidate[0]

            value = _to_numpy(candidate)
            if value is None:
                raise ValueError(f".pt 文件中的 {attr} 必须是 Tensor、数组或列表: {path}")
            value = np.asarray(value)
            if value.size == 0:
                return np.zeros(num_nodes, dtype=bool)
            if value.ndim == 2 and 1 in value.shape:
                value = value.reshape(-1)
            elif is_plural and value.ndim == 2:
                value = value[0]
            if value.ndim != 1:
                raise ValueError(
                    f"{attr} 必须是一维布尔掩码或节点索引，实际 shape={value.shape}: {path}"
                )

            if np.issubdtype(value.dtype, np.bool_):
                if value.shape[0] != num_nodes:
                    raise ValueError(
                        f"{attr} 布尔掩码长度({value.shape[0]})与节点数({num_nodes})不一致: {path}"
                    )
                return value.astype(bool)

            if not np.issubdtype(value.dtype, np.integer):
                raise ValueError(f"{attr} 必须是布尔掩码或整数节点索引: {path}")
            if value.shape[0] == num_nodes and np.all((value == 0) | (value == 1)):
                return value.astype(bool)

            indices = value.astype(np.int64, copy=False)
            if indices.size and (indices.min() < 0 or indices.max() >= num_nodes):
                raise ValueError(
                    f"{attr} 包含越界节点索引，有效范围为 [0, {num_nodes - 1}]: {path}"
                )
            mask = np.zeros(num_nodes, dtype=bool)
            mask[indices] = True
            return mask
        return None

    train_mask = _extract_mask("train_mask", "train_masks")
    val_mask = _extract_mask("val_mask", "valid_mask", "val_masks", "valid_masks")
    test_mask = _extract_mask("test_mask", "test_masks")

    # ── name ─────────────────────────────────────────────────────────
    name = os.path.splitext(os.path.basename(path))[0]

    directed = True
    is_directed = _field("is_directed")
    if callable(is_directed):
        directed = bool(is_directed())
    elif is_directed is not None:
        directed = bool(is_directed)
    else:
        raw_directed = _field("directed")
        if raw_directed is not None:
            directed = bool(raw_directed)

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

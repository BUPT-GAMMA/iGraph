"""
Node Classification 任务。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import numpy as np
import torch

from graph_data_service.graph_data import GraphData
from graph_data_service.labels import (
    aligned_label_names,
    class_mapping,
    is_missing_label,
    label_key,
    label_value,
    sorted_distinct_label_values,
)
from ..schemas import TrainRequest


def parse_node_labels(gd: GraphData) -> Tuple[torch.Tensor, List[Any], List[str | None]]:
    if gd.node_label is None:
        raise ValueError("当前图缺少 node_label，无法执行节点分类任务")

    raw = np.asarray(gd.node_label)
    n = gd.num_nodes
    y = np.full((n,), -1, dtype=np.int64)

    if raw.ndim == 2:
        if raw.shape[0] != n:
            raise ValueError("node_label 维度与节点数不一致")
        y = np.argmax(raw, axis=1).astype(np.int64)
        valid = raw.sum(axis=1) > 0
        y[~valid] = -1
        labels = [int(i) for i in range(raw.shape[1])]
        return torch.as_tensor(y, dtype=torch.long), labels, aligned_label_names(labels, gd.label_names)

    if raw.shape[0] != n:
        raise ValueError("node_label 维度与节点数不一致")

    label_values = sorted_distinct_label_values(raw.tolist())
    mapping: Dict[str, int] = {
        label_key(value): idx for idx, value in enumerate(label_values)
    }
    for i, val in enumerate(raw.tolist()):
        if is_missing_label(val):
            continue
        y[i] = mapping[label_key(label_value(val))]

    if len(label_values) < 2:
        raise ValueError("node_label 类别不足（至少需要 2 类）")
    return torch.as_tensor(y, dtype=torch.long), label_values, aligned_label_names(label_values, gd.label_names)


def node_label_class_mapping(label_values: List[Any], label_names: List[str | None]) -> List[dict[str, Any]]:
    return class_mapping(label_values, label_names)


def split_indices(indices: np.ndarray, req: TrainRequest) -> Tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(req.train_config.seed)
    idx = np.asarray(indices, dtype=np.int64).copy()
    rng.shuffle(idx)

    if idx.size <= 1:
        return idx, np.array([], dtype=np.int64)

    split = req.train_config.split
    total = float(split.train + split.val)
    train_ratio = split.train / total

    n = idx.size
    n_train = max(1, int(round(n * train_ratio)))
    n_train = min(n_train, n - 1)
    n_val = n - n_train
    return idx[:n_train], idx[n_train: n_train + n_val]


@dataclass
class NodeClassificationBatch:
    y: torch.Tensor
    train_idx: torch.Tensor
    val_idx: torch.Tensor
    label_values: List[Any]
    label_names: List[str | None]


class NodeClassificationTask:
    def prepare(self, gd: GraphData, req: TrainRequest) -> NodeClassificationBatch:
        y, label_values, label_names = parse_node_labels(gd)
        labeled_idx = np.where(y.cpu().numpy() >= 0)[0]
        if req.data.target_nodes:
            target_ids = []
            for name in req.data.target_nodes:
                if gd.has_node_name(name):
                    nid = gd.name_to_id(name)
                    if int(y[nid]) >= 0:
                        target_ids.append(nid)
            labeled_idx = np.array(sorted(set(target_ids)), dtype=np.int64)

        if labeled_idx.size < 2:
            raise ValueError("可用于训练的有标签节点不足（至少需要 2 个）")

        labeled_set = set(labeled_idx.tolist())

        # 优先使用数据集自带的 train/val/test mask
        if gd.train_mask is not None:
            train_idx = np.where(gd.train_mask)[0]
            train_idx = np.array([i for i in train_idx if i in labeled_set], dtype=np.int64)
            if gd.val_mask is not None:
                val_idx = np.where(gd.val_mask)[0]
            elif gd.test_mask is not None:
                val_idx = np.where(gd.test_mask)[0]
            else:
                val_idx = np.array([i for i in labeled_idx if i not in set(train_idx.tolist())], dtype=np.int64)
            val_idx = np.array([i for i in val_idx if i in labeled_set], dtype=np.int64)
        else:
            train_idx, val_idx = split_indices(labeled_idx, req)

        return NodeClassificationBatch(
            y=y,
            train_idx=torch.as_tensor(train_idx, dtype=torch.long),
            val_idx=torch.as_tensor(val_idx, dtype=torch.long),
            label_values=label_values,
            label_names=label_names,
        )

    def infer_node_ids(self, gd: GraphData, query: Dict[str, Any]) -> List[int]:
        # 支持按 split mask 筛选推理节点
        split = query.get("split")
        if split:
            mask_map = {"train": gd.train_mask, "val": gd.val_mask, "test": gd.test_mask}
            mask = mask_map.get(split)
            if mask is not None:
                return np.where(mask)[0].tolist()
            raise ValueError(f"query.split='{split}' 但图数据中不存在对应的 {split}_mask")

        nodes = query.get("nodes")
        if not nodes:
            return list(range(gd.num_nodes))
        out: List[int] = []
        for n in nodes:
            if isinstance(n, int) and n < gd.num_nodes:
                out.append(n)
            elif isinstance(n, str) and gd.has_node_name(n):
                out.append(gd.name_to_id(n))
        if not out:
            raise ValueError("query.nodes 中没有可识别节点")
        return out

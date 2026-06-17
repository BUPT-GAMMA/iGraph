"""
任务层导出。
"""

from __future__ import annotations

from typing import List

import numpy as np
import torch

from graph_data_service.graph_data import GraphData
from ..schemas import TrainRequest
from .link_prediction import (
    LinkPredictionBatch,
    LinkPredictionTask,
    best_threshold,
    binary_auc,
    sample_negative_edges,
)
from .node_classification import (
    NodeClassificationBatch,
    NodeClassificationTask,
    node_label_class_mapping,
)
from .pre_train import PRETRAIN_METHODS_REQUIRE_MODEL, PreTrainTask


def validate_train_request(req: TrainRequest) -> None:
    task = req.task
    model = (req.model or "").strip()
    method = (req.method or "").strip().lower()

    if task in {"node_classification", "link_prediction"} and not model:
        raise ValueError(f"task={task} 时 model 必填")
    if task == "pretrain" and not method:
        raise ValueError("task=pretrain 时 method 必填")
    if task == "pretrain" and method in PRETRAIN_METHODS_REQUIRE_MODEL and not model:
        raise ValueError(f"pretrain method={method} 需要提供 model")

    split = req.train_config.split
    total = float(split.train + split.val)
    if total <= 0:
        raise ValueError("split 比例和必须 > 0")
    if int(req.train_config.early_stop_patience) < 0:
        raise ValueError("early_stop_patience 不能为负数")
    if float(req.train_config.early_stop_min_delta) < 0:
        raise ValueError("early_stop_min_delta 不能为负数")


def node_names(gd: GraphData) -> List[str]:
    id2node = gd.id2node
    return [str(id2node.get(i, str(i))) for i in range(gd.num_nodes)]


def edge_index_tensor(gd: GraphData) -> torch.Tensor:
    return torch.as_tensor(gd.edge_index, dtype=torch.long)


def base_node_features(gd: GraphData) -> torch.Tensor:
    if gd.node_feat is not None:
        x = np.asarray(gd.node_feat, dtype=np.float32)
        if x.ndim > 2:
            x = x.reshape((x.shape[0], -1))
        if x.ndim == 1:
            x = x[:, None]
        return torch.as_tensor(x, dtype=torch.float32)

    n = gd.num_nodes
    src, dst = gd.edge_index
    in_deg = np.zeros(n, dtype=np.float32)
    out_deg = np.zeros(n, dtype=np.float32)
    for s, d in zip(src, dst):
        out_deg[int(s)] += 1.0
        in_deg[int(d)] += 1.0
    total = in_deg + out_deg
    x = np.stack([in_deg, out_deg, total, np.ones(n, dtype=np.float32)], axis=1)
    maxv = np.maximum(x.max(axis=0, keepdims=True), 1.0)
    return torch.as_tensor(x / maxv, dtype=torch.float32)


__all__ = [
    "validate_train_request",
    "node_names",
    "edge_index_tensor",
    "base_node_features",
    "NodeClassificationTask",
    "NodeClassificationBatch",
    "node_label_class_mapping",
    "LinkPredictionTask",
    "LinkPredictionBatch",
    "PreTrainTask",
    "best_threshold",
    "binary_auc",
    "sample_negative_edges",
]

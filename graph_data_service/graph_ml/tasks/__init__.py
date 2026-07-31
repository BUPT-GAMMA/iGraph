"""
任务层导出。
"""

from __future__ import annotations

from typing import List, Optional

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


GRAPH_CLASSIFICATION_MODELS = {"GCN", "GAT", "SAGE"}


def validate_train_request(
    req: TrainRequest,
    *,
    bound_graph_classification_dataset_id: str | None = None,
) -> Optional[str]:
    """
    校验训练请求，并返回图分类最终使用的 dataset_id。

    ``bound_graph_classification_dataset_id`` 是为独立的多图 DatasetStore
    预留的会话解析结果；不得传入现有单图 ``dataset_binding.json`` 的绑定。
    """
    task = req.task
    model = (req.model or "").strip()
    method = (req.method or "").strip().lower()

    if task in {"node_classification", "link_prediction", "graph_classification"} and not model:
        raise ValueError(f"task={task} 时 model 必填")
    if task == "pretrain" and not method:
        raise ValueError("task=pretrain 时 method 必填")
    if task == "pretrain" and method in PRETRAIN_METHODS_REQUIRE_MODEL and not model:
        raise ValueError(f"pretrain method={method} 需要提供 model")

    resolved_dataset_id: Optional[str] = None
    request_dataset_id = str(req.data.dataset_id or "").strip()
    if task == "graph_classification":
        normalized_model = model.upper()
        if normalized_model not in GRAPH_CLASSIFICATION_MODELS:
            supported = "/".join(sorted(GRAPH_CLASSIFICATION_MODELS))
            raise ValueError(f"graph_classification 当前仅支持 {supported}，收到: {model}")
        if "graph_version" in req.model_fields_set:
            raise ValueError("graph_classification 不接受 graph_version；请使用 dataset_id")
        if req.data.feature_source == "embedding":
            raise ValueError(
                "graph_classification 不支持 feature_source=embedding；"
                "第一版只使用数据集内的节点类别 ID"
            )
        if "split" in req.train_config.model_fields_set:
            raise ValueError(
                "graph_classification 不接受自定义 train_config.split；"
                "必须使用数据集自带的 train/val/test 划分"
            )
        if req.method is not None:
            raise ValueError("graph_classification 不接受 method")
        if req.upstream_model_id is not None:
            raise ValueError("graph_classification 不接受 upstream_model_id")
        if req.data.target_nodes is not None:
            raise ValueError("graph_classification 不接受 data.target_nodes")
        if req.data.target_edges is not None:
            raise ValueError("graph_classification 不接受 data.target_edges")

        bound_dataset_id = str(bound_graph_classification_dataset_id or "").strip()
        resolved_dataset_id = request_dataset_id or bound_dataset_id
        if not resolved_dataset_id:
            raise ValueError(
                "graph_classification 尚未指定可用数据集：请先调用 "
                "load_graph_classification_dataset，或传入 data.dataset_id"
            )
    elif request_dataset_id:
        raise ValueError("data.dataset_id 仅用于 task=graph_classification")

    split = req.train_config.split
    total = float(split.train + split.val)
    if total <= 0:
        raise ValueError("split 比例和必须 > 0")
    if int(req.train_config.early_stop_patience) < 0:
        raise ValueError("early_stop_patience 不能为负数")
    if float(req.train_config.early_stop_min_delta) < 0:
        raise ValueError("early_stop_min_delta 不能为负数")
    return resolved_dataset_id


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
    "GRAPH_CLASSIFICATION_MODELS",
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

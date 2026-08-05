"""
Link Prediction 任务。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np
import torch

from graph_data_service.graph_data import GraphData
from ..schemas import TrainRequest


def collect_positive_edges(gd: GraphData, req: TrainRequest) -> List[Tuple[int, int]]:
    if req.data.target_edges:
        out: List[Tuple[int, int]] = []
        for pair in req.data.target_edges:
            if not isinstance(pair, Sequence) or len(pair) != 2:
                continue
            u_name, v_name = pair[0], pair[1]
            if not (isinstance(u_name, str) and isinstance(v_name, str)):
                continue
            if gd.has_node_name(u_name) and gd.has_node_name(v_name):
                out.append((gd.name_to_id(u_name), gd.name_to_id(v_name)))
        return out
    return [(int(s), int(d)) for s, d in gd.logical_edges()]


def sample_negative_edges(
    num_samples: int,
    existing_edges: set[Tuple[int, int]],
    num_nodes: int,
    directed: bool,
    seed: int,
) -> List[Tuple[int, int]]:
    rng = np.random.default_rng(seed)
    neg: List[Tuple[int, int]] = []
    tried = 0
    max_try = max(1000, num_samples * 30)
    while len(neg) < num_samples and tried < max_try:
        tried += 1
        u = int(rng.integers(0, num_nodes))
        v = int(rng.integers(0, num_nodes))
        if u == v:
            continue
        if (u, v) in existing_edges:
            continue
        if (not directed) and ((v, u) in existing_edges):
            continue
        pair = (u, v)
        if pair in neg:
            continue
        neg.append(pair)
    return neg


def best_threshold(scores: np.ndarray, labels: np.ndarray) -> Tuple[float, float]:
    if scores.size == 0:
        return 0.5, 0.0
    uniq = np.unique(scores)
    if uniq.size > 200:
        cands = np.quantile(scores, np.linspace(0.05, 0.95, 50))
    else:
        cands = uniq
    best_t = float(cands[0])
    best_acc = -1.0
    for t in cands:
        pred = (scores >= t).astype(np.int64)
        acc = float(np.mean((pred == labels).astype(np.float32)))
        if acc > best_acc:
            best_acc = acc
            best_t = float(t)
    return best_t, best_acc


def binary_auc(scores: np.ndarray, labels: np.ndarray) -> float:
    pos_mask = labels == 1
    neg_mask = labels == 0
    n_pos = int(np.sum(pos_mask))
    n_neg = int(np.sum(neg_mask))
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = np.argsort(scores)
    ranks = np.empty_like(order, dtype=np.float64)
    ranks[order] = np.arange(1, len(scores) + 1, dtype=np.float64)
    pos_rank_sum = float(np.sum(ranks[pos_mask]))
    u = pos_rank_sum - n_pos * (n_pos + 1) / 2.0
    return float(u / (n_pos * n_neg))


@dataclass
class LinkPredictionBatch:
    train_pos_edges: torch.Tensor
    val_pos_edges: torch.Tensor
    val_neg_edges: torch.Tensor


class LinkPredictionTask:
    def prepare(self, gd: GraphData, req: TrainRequest) -> LinkPredictionBatch:
        pos_edges = collect_positive_edges(gd, req)
        if not pos_edges:
            raise ValueError("正样本边为空，无法训练链接预测任务")
        if len(pos_edges) < 2:
            raise ValueError("正样本边不足（至少需要 2 条）")

        rng = np.random.default_rng(req.train_config.seed)
        pos = np.asarray(pos_edges, dtype=np.int64)
        rng.shuffle(pos)

        total = float(req.train_config.split.train + req.train_config.split.val)
        train_ratio = req.train_config.split.train / total

        n = pos.shape[0]
        n_train = max(1, int(round(n * train_ratio)))
        n_train = min(n_train, n - 1)
        n_val = n - n_train

        train_pos = pos[:n_train]
        val_pos = pos[n_train: n_train + n_val]

        existing = set((int(u), int(v)) for u, v in gd.logical_edges())
        val_neg = sample_negative_edges(
            num_samples=max(1, len(val_pos)),
            existing_edges=existing,
            num_nodes=gd.num_nodes,
            directed=gd.semantic_type != "undirected",
            seed=req.train_config.seed + 11,
        )
        def _to_tensor(arr: np.ndarray) -> torch.Tensor:
            if arr.size == 0:
                return torch.empty((2, 0), dtype=torch.long)
            return torch.as_tensor(arr.T, dtype=torch.long)

        return LinkPredictionBatch(
            train_pos_edges=_to_tensor(train_pos),
            val_pos_edges=_to_tensor(val_pos),
            val_neg_edges=_to_tensor(np.asarray(val_neg, dtype=np.int64)),
        )

    def parse_infer_edges(self, gd: GraphData, query: dict) -> Optional[List[Tuple[int, int]]]:
        edges = query.get("edges")
        if not edges:
            return None
        out: List[Tuple[int, int]] = []
        for pair in edges:
            if not isinstance(pair, Sequence) or len(pair) != 2:
                continue
            u_name, v_name = pair[0], pair[1]
            if isinstance(u_name, str) and isinstance(v_name, str):
                if gd.has_node_name(u_name) and gd.has_node_name(v_name):
                    out.append((gd.name_to_id(u_name), gd.name_to_id(v_name)))
        return out

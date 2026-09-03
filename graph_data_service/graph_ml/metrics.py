"""Loss and benchmark metrics dedicated to graph classification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
import torch
import torch.nn.functional as F


ArrayLike = Sequence[float] | np.ndarray | torch.Tensor


@dataclass(frozen=True)
class MaskedBinaryCrossEntropyResult:
    loss: torch.Tensor | None
    num_valid_labels: int
    num_total_labels: int

    @property
    def num_masked_labels(self) -> int:
        return self.num_total_labels - self.num_valid_labels

    @property
    def skipped(self) -> bool:
        return self.loss is None


def masked_binary_cross_entropy_with_logits(
    logits: torch.Tensor,
    targets: torch.Tensor,
) -> MaskedBinaryCrossEntropyResult:
    """Compute unweighted BCE over finite graph labels only."""
    if not isinstance(logits, torch.Tensor) or not isinstance(targets, torch.Tensor):
        raise ValueError("logits 和 targets 必须是 Tensor")
    if logits.ndim != 2 or targets.ndim != 2:
        raise ValueError("图分类 logits 和 targets 必须是二维 [B, num_tasks]")
    if logits.shape != targets.shape:
        raise ValueError(
            f"logits 与 targets shape 不一致: {list(logits.shape)} != {list(targets.shape)}"
        )
    if not torch.is_floating_point(logits) or torch.is_complex(logits):
        raise ValueError("logits 必须是浮点 Tensor")
    if not torch.is_floating_point(targets) or torch.is_complex(targets):
        raise ValueError("targets 必须是浮点 Tensor")
    if logits.device != targets.device:
        raise ValueError("logits 与 targets 必须位于同一设备")
    valid_mask = torch.isfinite(targets)
    invalid_targets = valid_mask & (targets != 0) & (targets != 1)
    has_invalid_input = (
        (~torch.isfinite(logits)).any()
        | torch.isinf(targets).any()
        | invalid_targets.any()
    ).item()
    if has_invalid_input:
        if not torch.isfinite(logits).all().item():
            raise ValueError("logits 不能包含 NaN 或无穷值")
        if torch.isinf(targets).any().item():
            raise ValueError("targets 不能包含无穷值")
        raise ValueError("targets 只能包含 0、1 或 NaN")
    num_total = int(targets.numel())
    num_valid = int(valid_mask.sum().item())
    if num_valid == 0:
        return MaskedBinaryCrossEntropyResult(
            loss=None,
            num_valid_labels=0,
            num_total_labels=num_total,
        )

    safe_targets = torch.nan_to_num(targets, nan=0.0)
    loss_matrix = F.binary_cross_entropy_with_logits(
        logits,
        safe_targets,
        reduction="none",
    )
    return MaskedBinaryCrossEntropyResult(
        loss=loss_matrix[valid_mask].mean(),
        num_valid_labels=num_valid,
        num_total_labels=num_total,
    )


def binary_roc_auc(y_true: ArrayLike, y_score: ArrayLike) -> float | None:
    """Tie-aware ROC-AUC via average ranks; NaN labels are ignored."""
    labels, scores = _prepare_binary_inputs(y_true, y_score)
    n_pos = int(np.sum(labels == 1.0))
    n_neg = int(np.sum(labels == 0.0))
    if n_pos == 0 or n_neg == 0:
        return None

    order = np.argsort(scores, kind="mergesort")
    sorted_scores = scores[order]
    group_starts = np.concatenate(
        ([0], np.flatnonzero(np.diff(sorted_scores) != 0) + 1)
    )
    group_ends = np.concatenate((group_starts[1:], [sorted_scores.size]))
    average_ranks = ((group_starts + 1) + group_ends) / 2.0
    sorted_ranks = np.repeat(average_ranks, group_ends - group_starts)
    ranks = np.empty(scores.shape[0], dtype=np.float64)
    ranks[order] = sorted_ranks

    positive_rank_sum = float(np.sum(ranks[labels == 1.0]))
    mann_whitney_u = positive_rank_sum - n_pos * (n_pos + 1) / 2.0
    return float(mann_whitney_u / (n_pos * n_neg))


def binary_average_precision(
    y_true: ArrayLike,
    y_score: ArrayLike,
) -> float | None:
    """Non-interpolated, tie-aware binary AP; NaN labels are ignored."""
    labels, scores = _prepare_binary_inputs(y_true, y_score)
    n_pos = int(np.sum(labels == 1.0))
    n_neg = int(np.sum(labels == 0.0))
    if n_pos == 0 or n_neg == 0:
        return None

    order = np.argsort(-scores, kind="mergesort")
    sorted_scores = scores[order]
    sorted_labels = labels[order]
    group_ends = np.concatenate(
        (np.flatnonzero(np.diff(sorted_scores) != 0) + 1, [sorted_scores.size])
    )
    cumulative_positives = np.cumsum(sorted_labels == 1.0, dtype=np.int64)
    positives_at_threshold = cumulative_positives[group_ends - 1]
    positives_per_group = np.diff(
        np.concatenate(([0], positives_at_threshold))
    )
    precision_at_threshold = positives_at_threshold / group_ends
    return float(
        np.sum((positives_per_group / n_pos) * precision_at_threshold)
    )


def multitask_average_precision(
    y_true: ArrayLike,
    y_score: ArrayLike,
) -> dict[str, Any]:
    """Compute AP per task, then macro-average only valid binary tasks."""
    labels = _to_float_array(y_true, "y_true")
    scores = _to_float_array(y_score, "y_score")
    if labels.ndim != 2 or scores.ndim != 2:
        raise ValueError("多任务 AP 的 y_true 和 y_score 必须是二维 [N, T]")
    if labels.shape != scores.shape:
        raise ValueError(
            f"y_true 与 y_score shape 不一致: {list(labels.shape)} != {list(scores.shape)}"
        )
    if labels.shape[1] < 1:
        raise ValueError("多任务 AP 至少需要一个任务")

    per_task_ap: list[float | None] = []
    skipped_tasks: list[dict[str, Any]] = []
    valid_values: list[float] = []
    total_valid_labels = 0
    for task_index in range(labels.shape[1]):
        task_labels = labels[:, task_index]
        task_scores = scores[:, task_index]
        valid_mask = ~np.isnan(task_labels)
        valid_labels = task_labels[valid_mask]
        valid_scores = task_scores[valid_mask]
        total_valid_labels += int(valid_labels.size)
        _validate_binary_values(valid_labels, f"y_true[:, {task_index}]")
        if not np.isfinite(valid_scores).all():
            raise ValueError(f"y_score[:, {task_index}] 的有效标签位置包含 NaN 或无穷值")

        reason = _binary_skip_reason(valid_labels)
        if reason is not None:
            per_task_ap.append(None)
            skipped_tasks.append({"task_index": task_index, "reason": reason})
            continue
        value = binary_average_precision(valid_labels, valid_scores)
        assert value is not None
        per_task_ap.append(value)
        valid_values.append(value)

    mean_ap = float(np.mean(valid_values)) if valid_values else None
    return {
        "metric": "ap",
        "ap": mean_ap,
        "valid": mean_ap is not None,
        "reason": None if mean_ap is not None else "no_valid_tasks",
        "num_tasks": int(labels.shape[1]),
        "num_valid_tasks": len(valid_values),
        "num_skipped_tasks": len(skipped_tasks),
        "num_valid_labels": total_valid_labels,
        "per_task_ap": per_task_ap,
        "skipped_tasks": skipped_tasks,
    }


def evaluate_graph_classification(
    metric: str,
    y_true: ArrayLike,
    y_score: ArrayLike,
) -> dict[str, Any]:
    normalized_metric = str(metric or "").strip().lower()
    if normalized_metric == "ap":
        return multitask_average_precision(y_true, y_score)
    if normalized_metric != "roc_auc":
        raise ValueError(f"图分类 metric 仅支持 roc_auc 或 ap，收到: {metric}")

    labels = _to_float_array(y_true, "y_true")
    scores = _to_float_array(y_score, "y_score")
    if labels.ndim == 2:
        if labels.shape[1] != 1:
            raise ValueError("ROC-AUC 只支持单任务标签 [N] 或 [N, 1]")
        labels = labels[:, 0]
    if scores.ndim == 2:
        if scores.shape[1] != 1:
            raise ValueError("ROC-AUC 只支持单任务分数 [N] 或 [N, 1]")
        scores = scores[:, 0]
    prepared_labels, prepared_scores = _prepare_binary_inputs(labels, scores)
    reason = _binary_skip_reason(prepared_labels)
    value = None if reason is not None else binary_roc_auc(
        prepared_labels,
        prepared_scores,
    )
    return {
        "metric": "roc_auc",
        "roc_auc": value,
        "valid": value is not None,
        "reason": reason,
        "num_valid_labels": int(prepared_labels.size),
        "num_positive_labels": int(np.sum(prepared_labels == 1.0)),
        "num_negative_labels": int(np.sum(prepared_labels == 0.0)),
    }


def graph_classification_selection_metric_name(metric: str) -> str:
    normalized_metric = str(metric or "").strip().lower()
    if normalized_metric == "roc_auc":
        return "val_roc_auc"
    if normalized_metric == "ap":
        return "val_ap"
    raise ValueError(f"图分类 metric 仅支持 roc_auc 或 ap，收到: {metric}")


def _prepare_binary_inputs(
    y_true: ArrayLike,
    y_score: ArrayLike,
) -> tuple[np.ndarray, np.ndarray]:
    labels = _to_float_array(y_true, "y_true")
    scores = _to_float_array(y_score, "y_score")
    if labels.ndim != 1 or scores.ndim != 1:
        raise ValueError("二分类指标的 y_true 和 y_score 必须是一维")
    if labels.shape != scores.shape:
        raise ValueError(
            f"y_true 与 y_score shape 不一致: {list(labels.shape)} != {list(scores.shape)}"
        )
    valid_mask = ~np.isnan(labels)
    labels = labels[valid_mask]
    scores = scores[valid_mask]
    _validate_binary_values(labels, "y_true")
    if not np.isfinite(scores).all():
        raise ValueError("y_score 的有效标签位置不能包含 NaN 或无穷值")
    return labels, scores


def _to_float_array(value: ArrayLike, name: str) -> np.ndarray:
    if isinstance(value, torch.Tensor):
        if torch.is_complex(value):
            raise ValueError(f"{name} 不能是复数 Tensor")
        return value.detach().cpu().to(dtype=torch.float64).numpy()
    raw = np.asarray(value)
    if np.iscomplexobj(raw):
        raise ValueError(f"{name} 不能包含复数")
    try:
        return raw.astype(np.float64, copy=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数值数组") from exc


def _validate_binary_values(labels: np.ndarray, name: str) -> None:
    if np.isinf(labels).any():
        raise ValueError(f"{name} 不能包含无穷值")
    if labels.size and not np.all((labels == 0.0) | (labels == 1.0)):
        raise ValueError(f"{name} 只能包含 0、1 或 NaN")


def _binary_skip_reason(labels: np.ndarray) -> str | None:
    if labels.size == 0:
        return "all_labels_missing"
    if not np.any(labels == 1.0):
        return "no_positive_labels"
    if not np.any(labels == 0.0):
        return "no_negative_labels"
    return None


__all__ = [
    "MaskedBinaryCrossEntropyResult",
    "binary_average_precision",
    "binary_roc_auc",
    "evaluate_graph_classification",
    "graph_classification_selection_metric_name",
    "masked_binary_cross_entropy_with_logits",
    "multitask_average_precision",
]

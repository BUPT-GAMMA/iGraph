"""Inference for session-bound HIV/PCBA graph-classification artifacts."""

from __future__ import annotations
from graph_data_service.worker_signals import check_cancelled

import math
from numbers import Integral, Real
from typing import Any, Dict, Sequence

import numpy as np
import torch
from torch.utils.data import Subset
from torch_geometric.loader import DataLoader

from ..artifact_store import ArtifactRef, ArtifactStore
from ..datasets import (
    GraphClassificationDatasetBundle,
    GraphMLDatasetStore,
    build_graph_classification_dataloaders,
    validate_graph_classification_batch,
)
from ..metrics import evaluate_graph_classification
from ..models import GraphClassifier
from ..schemas import InferRequest


class GraphClassificationInfer:
    def __init__(
        self,
        store: ArtifactStore,
        dataset_store: GraphMLDatasetStore | None,
    ) -> None:
        self._store = store
        self._dataset_store = dataset_store

    def infer(
        self,
        ref: ArtifactRef,
        meta: Dict[str, Any],
        req: InferRequest,
        device: torch.device,
    ) -> Dict[str, Any]:
        if self._dataset_store is None:
            raise ValueError("图分类 DatasetStore 未初始化")
        model = self._build_model_from_meta(meta)
        dataset_id = str(meta["dataset_id"])
        try:
            bundle = self._dataset_store.resolve_for_session(
                ref.session_id,
                dataset_id,
            )
        except ValueError as exc:
            raise ValueError(
                f"请重新加载该图分类数据集（dataset_id={dataset_id}）后再推理: {exc}"
            ) from exc
        self._validate_dataset_identity(bundle, meta)

        state_dict = self._store.load_graph_classification_checkpoint(
            ref.model_dir,
            map_location="cpu",
            expected_state_dict=model.state_dict(),
        )
        model.load_state_dict(state_dict, strict=True)
        model = model.to(device)
        model.eval()
        try:
            indices, loader, query_result, mode = self._resolve_query(
                bundle,
                req,
                device,
            )
            labels, probabilities = self._predict(
                model,
                loader,
                bundle,
                device,
            )
            if probabilities.shape[0] != len(indices):
                raise ValueError("图分类推理结果数量与请求图索引数量不一致")
            evaluation = evaluate_graph_classification(
                bundle.metric,
                labels,
                probabilities,
            )

            result_limit = self._require_int_config(
                req.infer_config.get("result_limit", 20),
                "result_limit",
                minimum=0,
            )
            result_offset = self._require_int_config(
                req.infer_config.get("result_offset", 0),
                "result_offset",
                minimum=0,
            )
            page_end = result_offset + result_limit
            page_indices = indices[result_offset:page_end]
            page_labels = labels[result_offset:page_end]
            page_probabilities = probabilities[result_offset:page_end]
            truncated = len(page_indices) < max(0, len(indices) - result_offset)

            if bundle.metric == "roc_auc":
                predictions = self._hiv_predictions(
                    page_indices,
                    page_labels,
                    page_probabilities,
                    threshold=self._threshold(req),
                )
            else:
                predictions = self._pcba_predictions(
                    bundle,
                    page_indices,
                    page_labels,
                    page_probabilities,
                    req,
                )

            return {
                "success": True,
                "task": "graph_classification",
                "model": str(meta["model"]),
                "model_id": ref.model_id,
                "dataset_id": bundle.dataset_id,
                "dataset_name": bundle.name,
                "mode": mode,
                "query": query_result,
                "metric": bundle.metric,
                "evaluation": evaluation,
                "training_metrics": self._store.load_json(
                    ref.model_dir,
                    "metrics.json",
                ),
                "count": len(indices),
                "returned_count": len(page_indices),
                "result_limit": result_limit,
                "result_offset": result_offset,
                "truncated": truncated,
                "message": (
                    "图分类预测明细已截断；可使用 infer_config.result_offset/"
                    "result_limit 继续分页，完整 split 指标不受分页影响。"
                    if truncated
                    else ""
                ),
                "predictions": predictions,
            }
        finally:
            model = model.cpu()
            if device.type == "cuda" and torch.cuda.is_available():
                torch.cuda.empty_cache()

    @staticmethod
    def _build_model_from_meta(meta: Dict[str, Any]) -> GraphClassifier:
        required = {
            "task",
            "model",
            "dataset_id",
            "dataset_name",
            "dataset_fingerprint",
            "source_filename",
            "metric",
            "num_tasks",
            "node_vocab_size",
            "edge_vocab_size",
            "model_config",
            "split_sizes",
            "trained_at",
        }
        missing = sorted(required - set(meta))
        if missing:
            raise ValueError("图分类 artifact metadata 缺少字段: " + ", ".join(missing))
        if meta.get("task") != "graph_classification":
            raise ValueError("artifact 不是 graph_classification 模型")
        if meta.get("metric") not in {"roc_auc", "ap"}:
            raise ValueError("图分类 artifact metric 必须是 roc_auc 或 ap")
        model_config = meta.get("model_config")
        if not isinstance(model_config, dict):
            raise ValueError("图分类 artifact model_config 必须是对象")
        try:
            model = GraphClassifier(**model_config)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"无法按图分类 artifact metadata 重建模型: {exc}") from exc
        comparisons = {
            "model": model.backbone_name,
            "num_tasks": model.num_tasks,
            "node_vocab_size": model.node_vocab_size,
        }
        for field_name, actual in comparisons.items():
            expected = meta.get(field_name)
            if field_name == "model":
                expected = str(expected or "").upper()
            else:
                if isinstance(expected, bool) or not isinstance(expected, Integral):
                    raise ValueError(f"图分类 artifact {field_name} 必须是整数")
                expected = int(expected)
            if actual != expected:
                raise ValueError(
                    f"图分类 artifact {field_name} 与 model_config 不一致: "
                    f"{expected} != {actual}"
                )
        if isinstance(meta.get("edge_vocab_size"), bool) or not isinstance(
            meta.get("edge_vocab_size"), Integral
        ):
            raise ValueError("图分类 artifact edge_vocab_size 必须是整数")
        return model

    @staticmethod
    def _validate_dataset_identity(
        bundle: GraphClassificationDatasetBundle,
        meta: Dict[str, Any],
    ) -> None:
        if bundle.fingerprint != str(meta["dataset_fingerprint"]):
            raise ValueError(
                "图分类数据集 fingerprint 与训练 artifact 不一致，已拒绝推理"
            )
        expected = {
            "dataset_id": bundle.dataset_id,
            "dataset_name": bundle.name,
            "num_tasks": bundle.num_tasks,
            "node_vocab_size": bundle.node_vocab_size,
            "edge_vocab_size": bundle.edge_vocab_size,
            "metric": bundle.metric,
        }
        for field_name, actual in expected.items():
            if meta.get(field_name) != actual:
                raise ValueError(
                    f"图分类数据集与 artifact 的 {field_name} 不一致，已拒绝推理"
                )
        split_sizes = meta.get("split_sizes")
        current_sizes = {
            split: len(indices) for split, indices in bundle.split_indices.items()
        }
        if split_sizes != current_sizes:
            raise ValueError("图分类数据集 split_sizes 与训练 artifact 不一致")

    @classmethod
    def _resolve_query(
        cls,
        bundle: GraphClassificationDatasetBundle,
        req: InferRequest,
        device: torch.device,
    ) -> tuple[list[int], Any, dict[str, Any], str]:
        query = req.query
        unknown_fields = sorted(set(query) - {"split", "graph_indices"})
        if unknown_fields:
            raise ValueError(
                "graph_classification query 不支持字段: " + ", ".join(unknown_fields)
            )
        has_split = "split" in query
        has_indices = "graph_indices" in query
        if has_split and has_indices:
            raise ValueError("图分类推理不能同时指定 split 和 graph_indices")
        batch_size = cls._require_int_config(
            req.infer_config.get("batch_size", 128),
            "batch_size",
            minimum=1,
        )

        if has_indices:
            indices = cls._normalize_graph_indices(
                query.get("graph_indices"),
                len(bundle.graphs),
            )
            loader = DataLoader(
                Subset(bundle.graphs, indices),
                batch_size=batch_size,
                shuffle=False,
                num_workers=0,
                pin_memory=device.type == "cuda",
                drop_last=False,
            )
            return indices, loader, {"graph_indices": indices}, "specified_graphs"

        split = str(query.get("split", "test") or "").strip().lower()
        if split not in {"train", "val", "test"}:
            raise ValueError(f"图分类 split 仅支持 train/val/test，收到: {split}")
        loaders = build_graph_classification_dataloaders(
            bundle,
            batch_size=batch_size,
            num_workers=0,
            seed=0,
            pin_memory=device.type == "cuda",
        )
        indices = list(bundle.split_indices[split])
        mode = "evaluation_summary" if not query else "specified_split"
        return indices, loaders.for_split(split), {"split": split}, mode

    @staticmethod
    def _predict(
        model: GraphClassifier,
        loader: Any,
        bundle: GraphClassificationDatasetBundle,
        device: torch.device,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        labels: list[torch.Tensor] = []
        probabilities: list[torch.Tensor] = []
        with torch.no_grad():
            for cpu_batch in loader:
                check_cancelled()
                validate_graph_classification_batch(cpu_batch, bundle)
                batch = cpu_batch.to(device)
                logits = model(batch)
                labels.append(batch.y.detach().cpu())
                probabilities.append(torch.sigmoid(logits).detach().cpu())
        if not labels:
            raise ValueError("图分类推理请求不包含任何图")
        return torch.cat(labels, dim=0), torch.cat(probabilities, dim=0)

    @staticmethod
    def _hiv_predictions(
        graph_indices: Sequence[int],
        labels: torch.Tensor,
        probabilities: torch.Tensor,
        *,
        threshold: float,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row_index, graph_index in enumerate(graph_indices):
            probability = float(probabilities[row_index, 0].item())
            true_value = float(labels[row_index, 0].item())
            items.append(
                {
                    "graph_index": int(graph_index),
                    "probability": probability,
                    "predicted_label": int(probability >= threshold),
                    "true_label": None if math.isnan(true_value) else int(true_value),
                }
            )
        return items

    @classmethod
    def _pcba_predictions(
        cls,
        bundle: GraphClassificationDatasetBundle,
        graph_indices: Sequence[int],
        labels: torch.Tensor,
        probabilities: torch.Tensor,
        req: InferRequest,
    ) -> list[dict[str, Any]]:
        raw_topk = req.infer_config.get(
            "top_k_tasks",
            req.infer_config.get("topk", 5),
        )
        topk = min(
            cls._require_int_config(raw_topk, "top_k_tasks", minimum=1),
            bundle.num_tasks,
        )
        return_all = req.infer_config.get("return_all_task_probs", False)
        if not isinstance(return_all, bool):
            raise ValueError("return_all_task_probs 必须是布尔值")
        threshold = cls._threshold(req)
        items: list[dict[str, Any]] = []
        for row_index, graph_index in enumerate(graph_indices):
            row_probs = probabilities[row_index].numpy()
            row_labels = labels[row_index].numpy()
            task_indices = np.argsort(-row_probs, kind="mergesort")[:topk]
            top_predictions = []
            for task_index_value in task_indices:
                task_index = int(task_index_value)
                item = {
                    "task_index": task_index,
                    "task_name": cls._task_name(bundle, task_index),
                    "probability": float(row_probs[task_index]),
                    "predicted_label": int(row_probs[task_index] >= threshold),
                }
                if not math.isnan(float(row_labels[task_index])):
                    item["true_label"] = int(row_labels[task_index])
                top_predictions.append(item)
            graph_item: dict[str, Any] = {
                "graph_index": int(graph_index),
                "top_predictions": top_predictions,
                "num_available_true_labels": int(np.isfinite(row_labels).sum()),
            }
            if return_all:
                graph_item.update(
                    {
                        "probabilities": row_probs.astype(float).tolist(),
                        "predicted_labels": (row_probs >= threshold).astype(int).tolist(),
                        "true_labels": [
                            None if math.isnan(float(value)) else int(value)
                            for value in row_labels
                        ],
                    }
                )
            items.append(graph_item)
        return items

    @staticmethod
    def _task_name(bundle: GraphClassificationDatasetBundle, task_index: int) -> str:
        if task_index < len(bundle.label_texts):
            return bundle.label_texts[task_index]
        return f"task_{task_index}"

    @staticmethod
    def _normalize_graph_indices(value: Any, num_graphs: int) -> list[int]:
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("graph_indices 必须是非空整数列表")
        indices: list[int] = []
        seen: set[int] = set()
        for raw_index in value:
            if isinstance(raw_index, bool) or not isinstance(raw_index, Integral):
                raise ValueError("graph_indices 只能包含整数")
            index = int(raw_index)
            if index < 0 or index >= num_graphs:
                raise ValueError(f"graph_indices 包含越界索引: {index}")
            if index in seen:
                raise ValueError(f"graph_indices 包含重复索引: {index}")
            seen.add(index)
            indices.append(index)
        return indices

    @staticmethod
    def _require_int_config(value: Any, name: str, *, minimum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, Integral):
            raise ValueError(f"{name} 必须是整数")
        normalized = int(value)
        if normalized < minimum:
            raise ValueError(f"{name} 必须大于等于 {minimum}")
        return normalized

    @staticmethod
    def _threshold(req: InferRequest) -> float:
        raw = req.infer_config.get("threshold", 0.5)
        if isinstance(raw, bool) or not isinstance(raw, Real):
            raise ValueError("threshold 必须是数值")
        threshold = float(raw)
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold 必须位于 [0, 1]")
        return threshold


__all__ = ["GraphClassificationInfer"]

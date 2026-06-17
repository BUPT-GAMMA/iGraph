"""
节点分类推理器。
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import torch
from torch import nn

from graph_data_service.graph_data import GraphData
from graph_data_service.labels import class_mapping, display_label, label_key
from ..artifact_store import ArtifactRef, ArtifactStore
from ..models import build_backbone, parse_gnn_options
from ..schemas import InferRequest
from ..tasks import edge_index_tensor, node_names


class NodeClassificationInfer:
    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    def infer(
        self,
        ref: ArtifactRef,
        meta: Dict[str, Any],
        gd: GraphData,
        graph_version: int,
        req: InferRequest,
        x: torch.Tensor,
        device: torch.device,
    ) -> Dict[str, Any]:
        x = x.to(device)
        eidx = edge_index_tensor(gd).to(device)

        cfg = meta.get("model_config", {}) or {}
        emb_dim = int(cfg.get("embedding_dim", 64))
        backbone = build_backbone(
            meta.get("model", "GCN"),
            in_channels=int(x.shape[1]),
            hidden_channels=int(cfg.get("hidden_dim", emb_dim)),
            out_channels=emb_dim,
            num_layers=int(cfg.get("num_layers", 2)),
            dropout=float(cfg.get("dropout", 0.2)),
            gat_heads=int(cfg.get("gat_heads", 2)),
            **parse_gnn_options(cfg),
        ).to(device)
        head = nn.Linear(emb_dim, len(meta.get("label_values", []))).to(device)

        ckpt = self._store.load_torch(ref.model_dir, "checkpoint.pt")
        backbone.load_state_dict(ckpt["backbone"])
        head.load_state_dict(ckpt["head"])
        backbone.eval()
        head.eval()

        with torch.no_grad():
            logits = head(backbone(x, eidx))
            probs = torch.softmax(logits, dim=1)
            pred = torch.argmax(probs, dim=1)

        names = node_names(gd)
        label_values = list(meta.get("label_values", []) or [])
        label_names = self._label_names_from_meta(meta, label_values)
        class_map = class_mapping(label_values, label_names)
        return_prob = bool(req.infer_config.get("return_prob", True))
        y_true = self._true_label_indices(gd, label_values)

        explicit_nodes = bool(req.query.get("nodes"))
        explicit_split = bool(req.query.get("split"))

        if explicit_nodes or explicit_split:
            ids = self._resolve_query_node_ids(gd, req.query)
            result_limit = int(req.infer_config.get("result_limit", 20))
            result_offset = int(req.infer_config.get("result_offset", 0))
            result_ids = self._slice_prediction_ids(
                ids,
                limit=max(0, result_limit),
                offset=max(0, result_offset),
            )
            truncated = len(result_ids) < max(0, len(ids) - max(0, result_offset))
            return {
                "success": True,
                "task": "node_classification",
                "model_id": req.model_id,
                "graph_version": graph_version,
                "mode": "specified_nodes" if explicit_nodes else "specified_split",
                "query": req.query,
                "class_mapping": class_map,
                "evaluation": self._metrics_for_node_ids(ids, pred, y_true, label_values, label_names),
                "count": len(ids),
                "returned_count": len(result_ids),
                "result_limit": max(0, result_limit),
                "result_offset": max(0, result_offset),
                "truncated": truncated,
                "message": (
                    "需要推理的节点过多，工具返回结果已截断；"
                    "如需剩余节点结果，可继续使用 infer_config.result_offset/result_limit 分页查询。"
                    if truncated
                    else ""
                ),
                "predictions": self._prediction_items(
                    result_ids,
                    names,
                    pred,
                    probs,
                    label_values,
                    label_names,
                    y_true,
                    return_prob=return_prob,
                ),
            }

        sample_size = int(req.infer_config.get("sample_size", 20))
        sample_seed = int(req.infer_config.get("sample_seed", 42))
        sample_ids, sample_source = self._sample_default_nodes(
            gd,
            y_true,
            sample_size=max(0, sample_size),
            seed=sample_seed,
        )
        result = {
            "success": True,
            "task": "node_classification",
            "model_id": req.model_id,
            "graph_version": graph_version,
            "mode": "evaluation_summary",
            "class_mapping": class_map,
            "evaluation": self._split_metrics(gd, pred, y_true, label_values, label_names),
            "training_metrics": self._load_training_metrics(ref),
            "sample_source": sample_source,
            "sample_count": len(sample_ids),
            "sample_predictions": self._prediction_items(
                sample_ids,
                names,
                pred,
                probs,
                label_values,
                label_names,
                y_true,
                return_prob=return_prob,
            ),
        }
        return result

    @staticmethod
    def _prediction_items(
        ids: List[int],
        names: List[str],
        pred: torch.Tensor,
        probs: torch.Tensor,
        label_values: List[Any],
        label_names: List[str | None],
        y_true: np.ndarray | None,
        *,
        return_prob: bool,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for nid in ids:
            cls = int(pred[nid].item())
            raw_label = label_values[cls] if cls < len(label_values) else cls
            name = label_names[cls] if cls < len(label_names) else None
            item = {
                "node": names[nid],
                "pred_class_idx": cls,
                "pred_label": display_label(label_values, label_names, cls),
                "pred_label_value": raw_label,
            }
            if name:
                item["pred_label_name"] = name
            if isinstance(raw_label, int) and raw_label >= 0:
                item["pred_original_class_idx"] = int(raw_label)
            if y_true is not None and 0 <= int(y_true[nid]):
                true_cls = int(y_true[nid])
                true_raw = label_values[true_cls] if true_cls < len(label_values) else true_cls
                true_name = label_names[true_cls] if true_cls < len(label_names) else None
                item["true_class_idx"] = true_cls
                item["true_label"] = display_label(label_values, label_names, true_cls)
                item["true_label_value"] = true_raw
                if true_name:
                    item["true_label_name"] = true_name
                if isinstance(true_raw, int) and true_raw >= 0:
                    item["true_original_class_idx"] = int(true_raw)
                item["correct"] = bool(true_cls == cls)
            if return_prob:
                item["probabilities"] = probs[nid].detach().cpu().numpy().astype(float).tolist()
            out.append(item)
        return out

    @staticmethod
    def _resolve_query_node_ids(gd: GraphData, query: Dict[str, Any]) -> List[int]:
        nodes = query.get("nodes")
        if nodes:
            out: List[int] = []
            for n in nodes:
                if isinstance(n, int) and n < gd.num_nodes:
                    out.append(n)
                elif isinstance(n, str) and gd.has_node_name(n):
                    out.append(gd.name_to_id(n))
            if not out:
                raise ValueError("query.nodes 中没有可识别节点")
            return out

        split = query.get("split")
        if split:
            mask_map = {"train": gd.train_mask, "val": gd.val_mask, "test": gd.test_mask}
            mask = mask_map.get(split)
            if mask is not None:
                return np.where(mask)[0].astype(int).tolist()
            raise ValueError(f"query.split='{split}' 但图数据中不存在对应的 {split}_mask")

        return list(range(gd.num_nodes))

    @staticmethod
    def _slice_prediction_ids(ids: List[int], *, limit: int, offset: int) -> List[int]:
        if limit <= 0:
            return []
        if offset >= len(ids):
            return []
        return ids[offset: offset + limit]

    @staticmethod
    def _true_label_indices(gd: GraphData, label_values: List[Any]) -> np.ndarray | None:
        if gd.node_label is None:
            return None
        raw = np.asarray(gd.node_label)
        y = np.full((gd.num_nodes,), -1, dtype=np.int64)
        if raw.ndim == 2:
            if raw.shape[0] != gd.num_nodes:
                return None
            valid = raw.sum(axis=1) > 0
            y[valid] = np.argmax(raw[valid], axis=1).astype(np.int64)
            return y
        if raw.shape[0] != gd.num_nodes:
            return None
        label_to_idx = {label_key(v): i for i, v in enumerate(label_values)}
        for idx, val in enumerate(raw.tolist()):
            key = label_key(val)
            if key in label_to_idx:
                y[idx] = int(label_to_idx[key])
        return y

    @staticmethod
    def _label_names_from_meta(meta: Dict[str, Any], label_values: List[Any]) -> List[str | None]:
        raw_names = meta.get("label_names")
        if isinstance(raw_names, list) and len(raw_names) == len(label_values):
            return [str(v) if v is not None else None for v in raw_names]
        raw_classes = meta.get("label_classes")
        if isinstance(raw_classes, list):
            names: List[str | None] = [None] * len(label_values)
            for row in raw_classes:
                if not isinstance(row, dict):
                    continue
                idx = row.get("class_idx")
                if not isinstance(idx, int) or not (0 <= idx < len(names)):
                    continue
                name = row.get("label_name")
                if name is not None:
                    names[idx] = str(name)
            return names
        return [None] * len(label_values)

    @staticmethod
    def _split_metrics(
        gd: GraphData,
        pred: torch.Tensor,
        y_true: np.ndarray | None,
        label_values: List[Any],
        label_names: List[str | None],
    ) -> Dict[str, Any]:
        if y_true is None:
            return {"available": False, "reason": "current_graph_has_no_node_label"}
        pred_np = pred.detach().cpu().numpy().astype(np.int64)
        splits: Dict[str, Any] = {}
        for name, mask in (
            ("train", gd.train_mask),
            ("val", gd.val_mask),
            ("test", gd.test_mask),
        ):
            if mask is None:
                continue
            idx = np.where(mask)[0]
            idx = idx[y_true[idx] >= 0]
            if idx.size == 0:
                splits[name] = {"available": False, "reason": "no_labeled_nodes", "count": 0}
                continue
            splits[name] = NodeClassificationInfer._classification_metrics(
                y_true[idx],
                pred_np[idx],
                label_values,
                label_names,
            )
        if not splits:
            idx = np.where(y_true >= 0)[0]
            splits["all_labeled"] = (
                NodeClassificationInfer._classification_metrics(y_true[idx], pred_np[idx], label_values, label_names)
                if idx.size
                else {"available": False, "reason": "no_labeled_nodes", "count": 0}
            )
        return {"available": True, "splits": splits}

    @staticmethod
    def _metrics_for_node_ids(
        ids: List[int],
        pred: torch.Tensor,
        y_true: np.ndarray | None,
        label_values: List[Any],
        label_names: List[str | None],
    ) -> Dict[str, Any]:
        if y_true is None:
            return {
                "available": False,
                "reason": "current_graph_has_no_node_label",
                "requested_count": len(ids),
                "labeled_count": 0,
            }
        idx = np.asarray(ids, dtype=np.int64)
        if idx.size == 0:
            return {
                "available": False,
                "reason": "empty_node_selection",
                "requested_count": 0,
                "labeled_count": 0,
            }
        labeled_idx = idx[y_true[idx] >= 0]
        if labeled_idx.size == 0:
            return {
                "available": False,
                "reason": "selected_nodes_have_no_labels",
                "requested_count": int(idx.size),
                "labeled_count": 0,
            }
        pred_np = pred.detach().cpu().numpy().astype(np.int64)
        metrics = NodeClassificationInfer._classification_metrics(
            y_true[labeled_idx],
            pred_np[labeled_idx],
            label_values,
            label_names,
        )
        metrics["requested_count"] = int(idx.size)
        metrics["labeled_count"] = int(labeled_idx.size)
        return metrics

    @staticmethod
    def _classification_metrics(
        y_true: np.ndarray,
        y_pred: np.ndarray,
        label_values: List[Any],
        label_names: List[str | None],
    ) -> Dict[str, Any]:
        classes = list(range(len(label_values))) or sorted(set(y_true.tolist()) | set(y_pred.tolist()))
        count = int(y_true.size)
        accuracy = float(np.mean(y_true == y_pred)) if count else 0.0
        per_class: List[Dict[str, Any]] = []
        f1s: List[float] = []
        weighted_sum = 0.0
        for cls in classes:
            tp = int(np.sum((y_true == cls) & (y_pred == cls)))
            fp = int(np.sum((y_true != cls) & (y_pred == cls)))
            fn = int(np.sum((y_true == cls) & (y_pred != cls)))
            support = int(np.sum(y_true == cls))
            precision = tp / (tp + fp) if (tp + fp) else 0.0
            recall = tp / (tp + fn) if (tp + fn) else 0.0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
            f1s.append(float(f1))
            weighted_sum += f1 * support
            raw_label = label_values[cls] if cls < len(label_values) else cls
            name = label_names[cls] if cls < len(label_names) else None
            row = {
                "class_idx": int(cls),
                "label": display_label(label_values, label_names, cls),
                "label_value": raw_label,
                "precision": float(precision),
                "recall": float(recall),
                "f1": float(f1),
                "support": support,
            }
            if name:
                row["label_name"] = name
            if isinstance(raw_label, int) and raw_label >= 0:
                row["original_class_idx"] = int(raw_label)
            per_class.append(
                row
            )
        return {
            "available": True,
            "count": count,
            "accuracy": accuracy,
            "micro_f1": accuracy,
            "macro_f1": float(np.mean(f1s)) if f1s else 0.0,
            "weighted_f1": float(weighted_sum / count) if count else 0.0,
            "per_class": per_class,
        }

    @staticmethod
    def _sample_default_nodes(
        gd: GraphData,
        y_true: np.ndarray | None,
        *,
        sample_size: int,
        seed: int,
    ) -> tuple[List[int], str]:
        if sample_size <= 0:
            return [], "disabled"
        candidates: np.ndarray | None = None
        source = "all_nodes"
        for name, mask in (("test", gd.test_mask), ("val", gd.val_mask), ("train", gd.train_mask)):
            if mask is None:
                continue
            idx = np.where(mask)[0]
            if y_true is not None:
                idx = idx[y_true[idx] >= 0]
            if idx.size:
                candidates = idx
                source = name
                break
        if candidates is None:
            candidates = np.where(y_true >= 0)[0] if y_true is not None else np.arange(gd.num_nodes)
            source = "all_labeled" if y_true is not None else "all_nodes"
        if candidates.size <= sample_size:
            return candidates.astype(int).tolist(), source
        rng = np.random.default_rng(seed)
        return rng.choice(candidates, size=sample_size, replace=False).astype(int).tolist(), source

    def _load_training_metrics(self, ref: ArtifactRef) -> Dict[str, Any] | None:
        try:
            metrics = self._store.load_json(ref.model_dir, "metrics.json")
            return metrics if isinstance(metrics, dict) else None
        except Exception:
            return None

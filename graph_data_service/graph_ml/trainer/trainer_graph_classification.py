"""Training loop for HIV/PCBA whole-graph classification baselines."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict

import torch
from torch.optim import Adam

from backend.logging.logger import get_service_logger

from ..artifact_store import ArtifactStore
from ..datasets import (
    GraphClassificationDatasetBundle,
    GraphMLDatasetStore,
    build_graph_classification_dataloaders,
    validate_graph_classification_batch,
)
from ..metrics import (
    evaluate_graph_classification,
    graph_classification_selection_metric_name,
    masked_binary_cross_entropy_with_logits,
)
from ..models import GraphClassifier, build_graph_classifier
from ..schemas import TrainRequest


@dataclass
class _SplitEvaluation:
    loss: float | None
    metric_details: dict[str, Any]
    skipped_batches: int
    num_valid_labels: int


class GraphClassificationTrainer:
    def __init__(
        self,
        store: ArtifactStore,
        dataset_store: GraphMLDatasetStore | None,
    ) -> None:
        self._store = store
        self._dataset_store = dataset_store

    def train(
        self,
        *,
        session_id: str,
        dataset_id: str,
        req: TrainRequest,
        device: torch.device,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Dict[str, Any]:
        if self._dataset_store is None:
            raise ValueError("图分类 DatasetStore 未初始化")
        bundle = self._dataset_store.resolve_for_session(session_id, dataset_id)
        epochs, lr, weight_decay = self._validate_hyperparameters(req)
        patience = int(req.train_config.early_stop_patience)
        min_delta = float(req.train_config.early_stop_min_delta)
        if patience < 0:
            raise ValueError("graph_classification 的 early_stop_patience 不能为负数")
        if not math.isfinite(min_delta) or min_delta < 0:
            raise ValueError(
                "graph_classification 的 early_stop_min_delta 必须是有限非负数"
            )
        selection_metric = graph_classification_selection_metric_name(bundle.metric)
        metric_key = selection_metric.removeprefix("val_")

        loaders = None
        model: GraphClassifier | None = None
        optimizer: Adam | None = None
        try:
            loaders = build_graph_classification_dataloaders(
                bundle,
                batch_size=req.train_config.batch_size,
                num_workers=req.train_config.num_workers,
                seed=req.train_config.seed,
                pin_memory=device.type == "cuda",
            )
            model = build_graph_classifier(
                req.model or "GCN",
                node_vocab_size=bundle.node_vocab_size,
                num_tasks=bundle.num_tasks,
                algo_config=req.algo_config,
            ).to(device)
            optimizer = Adam(
                model.parameters(),
                lr=lr,
                weight_decay=weight_decay,
            )

            best_state: dict[str, torch.Tensor] | None = None
            best_val_details: dict[str, Any] | None = None
            best_train_loss = math.inf
            best_score = -math.inf
            best_epoch = 0
            trained_epochs = 0
            bad_epochs = 0
            stopped_early = False
            total_skipped_train_batches = 0
            ml_log = get_service_logger(session_id, "graph_ml")

            for epoch_index in range(epochs):
                trained_epochs = epoch_index + 1
                train_loss, skipped_train_batches = self._train_epoch(
                    model,
                    loaders.train,
                    bundle,
                    optimizer,
                    device,
                )
                total_skipped_train_batches += skipped_train_batches
                val_evaluation = self._evaluate_split(
                    model,
                    loaders.val,
                    bundle,
                    device,
                )
                val_score = val_evaluation.metric_details.get(metric_key)
                if val_score is None or not math.isfinite(float(val_score)):
                    reason = val_evaluation.metric_details.get("reason") or "unknown"
                    raise ValueError(
                        f"验证集无法计算 {selection_metric}，不能选择最佳模型: {reason}"
                    )
                normalized_score = float(val_score)
                is_best = normalized_score > best_score + min_delta
                if is_best:
                    best_score = normalized_score
                    best_epoch = trained_epochs
                    best_train_loss = train_loss
                    best_state = self._snapshot_state_dict(model)
                    best_val_details = val_evaluation.metric_details
                    bad_epochs = 0
                else:
                    bad_epochs += 1

                progress_event = {
                    "kind": "train_epoch",
                    "task": "graph_classification",
                    "epoch": trained_epochs,
                    "epochs": epochs,
                    "progress": trained_epochs / epochs,
                    "message": f"epoch {trained_epochs}/{epochs}",
                    "loss": round(train_loss, 6),
                    selection_metric: round(normalized_score, 6),
                    "selection_metric": selection_metric,
                    "is_best": is_best,
                    "best_epoch": best_epoch,
                    "bad_epochs": bad_epochs,
                    "early_stop_patience": patience,
                    "stopped_early": False,
                }
                ml_log.event(
                    "TRAIN_EPOCH_GRAPH_CLASSIFICATION",
                    model=req.model,
                    dataset_id=bundle.dataset_id,
                    **progress_event,
                )
                if progress_callback is not None:
                    progress_callback(progress_event)

                if patience > 0 and bad_epochs >= patience:
                    stopped_early = True
                    if progress_callback is not None:
                        progress_callback(
                            {
                                **progress_event,
                                "kind": "train_early_stop",
                                "progress": 1.0,
                                "message": f"early stop at epoch {trained_epochs}/{epochs}",
                                "stopped_early": True,
                            }
                        )
                    break

            if best_state is None or best_val_details is None:
                raise ValueError("图分类训练未产生可用的最佳模型")
            model.load_state_dict(best_state, strict=True)
            model.eval()

            # The test split is intentionally untouched until best-state selection ends.
            test_evaluation = self._evaluate_split(
                model,
                loaders.test,
                bundle,
                device,
            )
            metrics = self._build_metrics(
                bundle,
                best_train_loss=best_train_loss,
                best_val_details=best_val_details,
                test_evaluation=test_evaluation,
                best_epoch=best_epoch,
                trained_epochs=trained_epochs,
                stopped_early=stopped_early,
                selection_metric=selection_metric,
                skipped_train_batches=total_skipped_train_batches,
            )
            per_task_metrics = {
                "metric": bundle.metric,
                "val": best_val_details,
                "test": test_evaluation.metric_details,
            }
            model_config = model.constructor_config()
            ref = self._store.create_model_dir(session_id, req.task)
            self._store.save_graph_classification_checkpoint(
                ref.model_dir,
                best_state,
            )
            self._store.save_json(ref.model_dir, "metrics.json", metrics)
            self._store.save_json(
                ref.model_dir,
                "per_task_metrics.json",
                per_task_metrics,
            )
            self._store.save_json(
                ref.model_dir,
                "meta.json",
                {
                    "task": "graph_classification",
                    "model": model.backbone_name,
                    "dataset_id": bundle.dataset_id,
                    "dataset_name": bundle.name,
                    "dataset_fingerprint": bundle.fingerprint,
                    "source_filename": bundle.source_filename,
                    "metric": bundle.metric,
                    "num_tasks": bundle.num_tasks,
                    "node_vocab_size": bundle.node_vocab_size,
                    "edge_vocab_size": bundle.edge_vocab_size,
                    "model_config": model_config,
                    "split_sizes": {
                        split: len(indices)
                        for split, indices in bundle.split_indices.items()
                    },
                    "pooling": model.pooling,
                    "uses_edge_features": model.uses_edge_features,
                    "trained_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            return {
                "success": True,
                "session_id": session_id,
                "task": "graph_classification",
                "model": model.backbone_name,
                "model_id": ref.model_id,
                "dataset_id": bundle.dataset_id,
                "dataset_name": bundle.name,
                "name": bundle.name,
                "metrics": metrics,
            }
        finally:
            optimizer = None
            model = None
            loaders = None
            if device.type == "cuda" and torch.cuda.is_available():
                torch.cuda.empty_cache()

    @staticmethod
    def _train_epoch(
        model: GraphClassifier,
        loader: Any,
        bundle: GraphClassificationDatasetBundle,
        optimizer: Adam,
        device: torch.device,
    ) -> tuple[float, int]:
        model.train()
        weighted_loss = 0.0
        num_valid_labels = 0
        skipped_batches = 0
        for cpu_batch in loader:
            batch_info = validate_graph_classification_batch(cpu_batch, bundle)
            if not batch_info.has_valid_labels:
                skipped_batches += 1
                continue
            batch = cpu_batch.to(device)
            optimizer.zero_grad(set_to_none=True)
            logits = model(batch)
            loss_result = masked_binary_cross_entropy_with_logits(logits, batch.y)
            if loss_result.loss is None:
                skipped_batches += 1
                continue
            loss_result.loss.backward()
            optimizer.step()
            weighted_loss += (
                float(loss_result.loss.detach().cpu().item())
                * loss_result.num_valid_labels
            )
            num_valid_labels += loss_result.num_valid_labels
        if num_valid_labels == 0:
            raise ValueError("训练集没有任何有效图分类标签")
        return weighted_loss / num_valid_labels, skipped_batches

    @staticmethod
    def _evaluate_split(
        model: GraphClassifier,
        loader: Any,
        bundle: GraphClassificationDatasetBundle,
        device: torch.device,
    ) -> _SplitEvaluation:
        model.eval()
        labels: list[torch.Tensor] = []
        scores: list[torch.Tensor] = []
        weighted_loss = 0.0
        num_valid_labels = 0
        skipped_batches = 0
        with torch.no_grad():
            for cpu_batch in loader:
                validate_graph_classification_batch(cpu_batch, bundle)
                batch = cpu_batch.to(device)
                logits = model(batch)
                probabilities = torch.sigmoid(logits)
                labels.append(cpu_batch.y.detach().cpu())
                scores.append(probabilities.detach().cpu())
                loss_result = masked_binary_cross_entropy_with_logits(logits, batch.y)
                if loss_result.loss is None:
                    skipped_batches += 1
                    continue
                weighted_loss += (
                    float(loss_result.loss.detach().cpu().item())
                    * loss_result.num_valid_labels
                )
                num_valid_labels += loss_result.num_valid_labels
        if not labels:
            raise ValueError("图分类 split 为空，无法评估")
        metric_details = evaluate_graph_classification(
            bundle.metric,
            torch.cat(labels, dim=0),
            torch.cat(scores, dim=0),
        )
        loss = weighted_loss / num_valid_labels if num_valid_labels else None
        return _SplitEvaluation(
            loss=loss,
            metric_details=metric_details,
            skipped_batches=skipped_batches,
            num_valid_labels=num_valid_labels,
        )

    @staticmethod
    def _build_metrics(
        bundle: GraphClassificationDatasetBundle,
        *,
        best_train_loss: float,
        best_val_details: dict[str, Any],
        test_evaluation: _SplitEvaluation,
        best_epoch: int,
        trained_epochs: int,
        stopped_early: bool,
        selection_metric: str,
        skipped_train_batches: int,
    ) -> dict[str, Any]:
        metric_key = bundle.metric
        metrics: dict[str, Any] = {
            "train_loss": float(best_train_loss),
            f"val_{metric_key}": best_val_details.get(metric_key),
            f"test_{metric_key}": test_evaluation.metric_details.get(metric_key),
            "test_loss": test_evaluation.loss,
            "best_epoch": best_epoch,
            "selection_metric": selection_metric,
            "trained_epochs": trained_epochs,
            "stopped_early": stopped_early,
            "skipped_train_batches": skipped_train_batches,
            "test_skipped_batches": test_evaluation.skipped_batches,
        }
        if bundle.metric == "ap":
            metrics.update(
                {
                    "val_valid_tasks": int(best_val_details["num_valid_tasks"]),
                    "test_valid_tasks": int(
                        test_evaluation.metric_details["num_valid_tasks"]
                    ),
                }
            )
        return metrics

    @staticmethod
    def _snapshot_state_dict(model: GraphClassifier) -> dict[str, torch.Tensor]:
        return {
            key: value.detach().cpu().clone()
            for key, value in model.state_dict().items()
        }

    @staticmethod
    def _validate_hyperparameters(req: TrainRequest) -> tuple[int, float, float]:
        epochs = int(req.train_config.epochs)
        lr = float(req.train_config.lr)
        weight_decay = float(req.train_config.weight_decay)
        if epochs < 1:
            raise ValueError("graph_classification 的 epochs 必须大于等于 1")
        if not math.isfinite(lr) or lr <= 0:
            raise ValueError("graph_classification 的 lr 必须是有限正数")
        if not math.isfinite(weight_decay) or weight_decay < 0:
            raise ValueError("graph_classification 的 weight_decay 必须是有限非负数")
        return epochs, lr, weight_decay


__all__ = ["GraphClassificationTrainer"]

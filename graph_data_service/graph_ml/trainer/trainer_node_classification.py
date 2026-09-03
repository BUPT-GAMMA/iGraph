"""
节点分类训练器。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict

import torch
import torch.nn.functional as F
from torch import nn
from torch.optim import Adam

from graph_engine.observability import get_service_logger
from graph_data_service.graph_data import GraphData
from ..artifact_store import ArtifactStore
from ..models import build_backbone, parse_gnn_options
from ..schemas import TrainRequest
from ..tasks import (
    NodeClassificationTask,
    message_passing_edge_index,
    node_label_class_mapping,
    node_names,
)


class NodeClassificationTrainer:
    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    def train(
        self,
        session_id: str,
        graph_version: int,
        gd: GraphData,
        req: TrainRequest,
        x: torch.Tensor,
        feat_meta: Dict[str, Any],
        device: torch.device,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Dict[str, Any]:
        x = x.to(device)
        eidx = message_passing_edge_index(gd).to(device)

        task = NodeClassificationTask()
        batch = task.prepare(gd, req)
        y = batch.y.to(device)
        tr_idx = batch.train_idx.to(device)
        val_idx = batch.val_idx.to(device)

        hidden = int(req.algo_config.get("hidden_dim", 64))
        emb_dim = int(req.algo_config.get("embedding_dim", hidden))
        num_layers = int(req.algo_config.get("num_layers", 2))
        dropout = float(req.algo_config.get("dropout", 0.2))
        gat_heads = int(req.algo_config.get("gat_heads", 2))
        gnn_options = parse_gnn_options(req.algo_config)

        backbone = build_backbone(
            req.model or "GCN",
            in_channels=x.shape[1],
            hidden_channels=hidden,
            out_channels=emb_dim,
            num_layers=num_layers,
            dropout=dropout,
            gat_heads=gat_heads,
            **gnn_options,
        ).to(device)
        head = nn.Linear(emb_dim, len(batch.label_values)).to(device)

        opt = Adam(
            list(backbone.parameters()) + list(head.parameters()),
            lr=float(req.train_config.lr),
            weight_decay=float(req.train_config.weight_decay),
        )
        ml_log = get_service_logger(session_id, "graph_ml")
        patience = max(0, int(req.train_config.early_stop_patience))
        min_delta = max(0.0, float(req.train_config.early_stop_min_delta))
        bad_epochs = 0
        trained_epochs = 0
        stopped_early = False

        epochs = max(1, int(req.train_config.epochs))
        last_loss = 0.0
        best_epoch = 0
        best_val_acc = -1.0
        best_loss = float("inf")
        best_backbone_state: Dict[str, torch.Tensor] | None = None
        best_head_state: Dict[str, torch.Tensor] | None = None
        for epoch in range(epochs):
            trained_epochs = epoch + 1
            backbone.train()
            head.train()
            opt.zero_grad()
            z = backbone(x, eidx)
            logits = head(z)
            loss = F.cross_entropy(logits[tr_idx], y[tr_idx])
            loss.backward()
            opt.step()
            last_loss = float(loss.detach().cpu().item())

            backbone.eval()
            head.eval()
            with torch.no_grad():
                eval_logits = head(backbone(x, eidx))
                eval_pred = torch.argmax(eval_logits, dim=1)
                epoch_train_acc = self._accuracy(eval_pred, y, tr_idx)
                epoch_val_acc = self._accuracy(eval_pred, y, val_idx)
            is_best = epoch_val_acc > (best_val_acc + min_delta)
            if is_best:
                best_epoch = epoch + 1
                best_val_acc = epoch_val_acc
                best_loss = last_loss
                best_backbone_state = self._snapshot_state_dict(backbone)
                best_head_state = self._snapshot_state_dict(head)
                bad_epochs = 0
            else:
                bad_epochs += 1
            ml_log.event(
                "TRAIN_EPOCH_NODE_CLASSIFICATION",
                epoch=epoch + 1,
                epochs=epochs,
                loss=round(last_loss, 6),
                train_acc=round(epoch_train_acc, 6),
                val_acc=round(epoch_val_acc, 6),
                is_best=is_best,
                bad_epochs=bad_epochs,
                early_stop_patience=patience,
            )
            if progress_callback is not None:
                progress_callback(
                    {
                        "kind": "train_epoch",
                        "task": req.task,
                        "epoch": epoch + 1,
                        "epochs": epochs,
                        "progress": (epoch + 1) / epochs,
                        "message": f"epoch {epoch + 1}/{epochs}",
                        "loss": round(last_loss, 6),
                        "train_acc": round(epoch_train_acc, 6),
                        "val_acc": round(epoch_val_acc, 6),
                        "selection_metric": "val_acc",
                        "is_best": bool(is_best),
                        "best_epoch": int(best_epoch),
                        "bad_epochs": int(bad_epochs),
                        "early_stop_patience": int(patience),
                        "stopped_early": False,
                    }
                )
            if patience > 0 and bad_epochs >= patience:
                stopped_early = True
                ml_log.event(
                    "TRAIN_EARLY_STOP_NODE_CLASSIFICATION",
                    stop_epoch=epoch + 1,
                    best_epoch=best_epoch,
                    best_val_acc=round(float(best_val_acc), 6),
                    patience=patience,
                    min_delta=round(float(min_delta), 6),
                )
                if progress_callback is not None:
                    progress_callback(
                        {
                            "kind": "train_early_stop",
                            "task": req.task,
                            "epoch": epoch + 1,
                            "epochs": epochs,
                            "progress": 1.0,
                            "message": f"early stop at epoch {epoch + 1}/{epochs}",
                            "loss": round(last_loss, 6),
                            "val_acc": round(float(best_val_acc), 6),
                            "selection_metric": "val_acc",
                            "best_epoch": int(best_epoch),
                            "bad_epochs": int(bad_epochs),
                            "early_stop_patience": int(patience),
                            "stopped_early": True,
                        }
                    )
                break

        if best_backbone_state is None or best_head_state is None:
            best_epoch = epochs
            best_backbone_state = self._snapshot_state_dict(backbone)
            best_head_state = self._snapshot_state_dict(head)
            best_loss = last_loss if math.isfinite(last_loss) else 0.0

        backbone.load_state_dict(best_backbone_state)
        head.load_state_dict(best_head_state)
        backbone.eval()
        head.eval()
        with torch.no_grad():
            z = backbone(x, eidx)
            logits = head(z)
            pred = torch.argmax(logits, dim=1)

        metrics = {
            "train_loss": float(best_loss),
            "train_acc": self._accuracy(pred, y, tr_idx),
            "val_acc": self._accuracy(pred, y, val_idx),
            "num_classes": len(batch.label_values),
            "num_train": int(tr_idx.numel()),
            "num_val": int(val_idx.numel()),
            "best_epoch": int(best_epoch),
            "selection_metric": "val_acc",
            "trained_epochs": int(trained_epochs),
            "stopped_early": bool(stopped_early),
        }

        ref = self._store.create_model_dir(session_id, req.task)
        self._store.save_torch(
            ref.model_dir,
            "checkpoint.pt",
            {"backbone": best_backbone_state, "head": best_head_state},
        )
        self._store.save_json(ref.model_dir, "metrics.json", metrics)
        self._store.save_json(ref.model_dir, "node_names.json", node_names(gd))
        self._store.save_json(
            ref.model_dir,
            "meta.json",
            {
                "task": req.task,
                "model": req.model,
                "method": req.method,
                "graph_version": graph_version,
                "feature_source": feat_meta["feature_source"],
                "upstream_model_id": feat_meta.get("upstream_model_id"),
                "model_config": {
                    "hidden_dim": hidden,
                    "embedding_dim": emb_dim,
                    "num_layers": num_layers,
                    "dropout": dropout,
                    "gat_heads": gat_heads,
                    **gnn_options,
                },
                "label_values": batch.label_values,
                "label_names": batch.label_names,
                "label_classes": node_label_class_mapping(batch.label_values, batch.label_names),
                "num_nodes": gd.num_nodes,
                "num_features": int(x.shape[1]),
            },
        )

        return {
            "success": True,
            "session_id": session_id,
            "task": req.task,
            "model_id": ref.model_id,
            "graph_version": graph_version,
            "metrics": metrics,
            "meta": {
                "model": req.model,
                "feature_source": feat_meta["feature_source"],
                "upstream_model_id": feat_meta.get("upstream_model_id"),
            },
        }

    @staticmethod
    def _accuracy(pred: torch.Tensor, y: torch.Tensor, idx: torch.Tensor) -> float:
        if idx.numel() == 0:
            return 0.0
        return float((pred[idx] == y[idx]).float().mean().item())

    @staticmethod
    def _snapshot_state_dict(module: nn.Module) -> Dict[str, torch.Tensor]:
        return {k: v.detach().cpu().clone() for k, v in module.state_dict().items()}

"""
链接预测训练器。
"""

from __future__ import annotations

from typing import Any, Callable, Dict

import numpy as np
import torch
import torch.nn.functional as F
from torch.optim import Adam
from torch_geometric.utils import negative_sampling

from graph_engine.observability import get_service_logger
from graph_data_service.graph_data import GraphData
from ..artifact_store import ArtifactStore
from ..models import build_backbone, parse_gnn_options
from ..schemas import TrainRequest
from ..tasks import (
    LinkPredictionTask,
    best_threshold,
    binary_auc,
    message_passing_edge_index,
    node_names,
)


class LinkPredictionTrainer:
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

        task = LinkPredictionTask()
        batch = task.prepare(gd, req)
        train_pos = batch.train_pos_edges.to(device)
        val_pos = batch.val_pos_edges.to(device)
        val_neg = batch.val_neg_edges.to(device)

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
        opt = Adam(
            backbone.parameters(),
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
        best_val_auc = -1.0
        best_val_acc = -1.0
        best_threshold_value = 0.5
        best_loss = float("inf")
        best_backbone_state: Dict[str, torch.Tensor] | None = None
        for epoch in range(epochs):
            trained_epochs = epoch + 1
            backbone.train()
            opt.zero_grad()
            z = backbone(x, eidx)
            if train_pos.numel() == 0:
                raise ValueError("训练正边为空，无法训练 link_prediction")
            neg = negative_sampling(
                edge_index=eidx,
                num_nodes=gd.num_nodes,
                num_neg_samples=train_pos.shape[1],
            ).to(device)
            pos_logits = self._edge_logits(z, train_pos)
            neg_logits = self._edge_logits(z, neg)
            loss = F.binary_cross_entropy_with_logits(pos_logits, torch.ones_like(pos_logits))
            loss = loss + F.binary_cross_entropy_with_logits(neg_logits, torch.zeros_like(neg_logits))
            loss.backward()
            opt.step()
            last_loss = float(loss.detach().cpu().item())

            backbone.eval()
            with torch.no_grad():
                z_eval = backbone(x, eidx)
                val_pos_scores_ep = torch.sigmoid(
                    self._edge_logits(z_eval, val_pos)
                ).detach().cpu().numpy()
                val_neg_scores_ep = torch.sigmoid(
                    self._edge_logits(z_eval, val_neg)
                ).detach().cpu().numpy()
                val_scores_ep = np.concatenate([val_pos_scores_ep, val_neg_scores_ep], axis=0)
                val_labels_ep = np.concatenate(
                    [
                        np.ones_like(val_pos_scores_ep, dtype=np.int64),
                        np.zeros_like(val_neg_scores_ep, dtype=np.int64),
                    ],
                    axis=0,
                )
                threshold_ep, val_acc_ep = best_threshold(val_scores_ep, val_labels_ep)
                val_auc_ep = binary_auc(val_scores_ep, val_labels_ep)
            is_best = (val_auc_ep > (best_val_auc + min_delta)) or (
                abs(float(val_auc_ep) - float(best_val_auc)) <= min_delta
                and val_acc_ep > best_val_acc
            )
            if is_best:
                best_epoch = epoch + 1
                best_val_auc = float(val_auc_ep)
                best_val_acc = float(val_acc_ep)
                best_threshold_value = float(threshold_ep)
                best_loss = last_loss
                best_backbone_state = self._snapshot_state_dict(backbone)
                bad_epochs = 0
            else:
                bad_epochs += 1
            ml_log.event(
                "TRAIN_EPOCH_LINK_PREDICTION",
                epoch=epoch + 1,
                epochs=epochs,
                loss=round(last_loss, 6),
                val_acc=round(float(val_acc_ep), 6),
                val_auc=round(float(val_auc_ep), 6),
                threshold=round(float(threshold_ep), 6),
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
                        "val_acc": round(float(val_acc_ep), 6),
                        "val_auc": round(float(val_auc_ep), 6),
                        "threshold": round(float(threshold_ep), 6),
                        "selection_metric": "val_auc",
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
                    "TRAIN_EARLY_STOP_LINK_PREDICTION",
                    stop_epoch=epoch + 1,
                    best_epoch=best_epoch,
                    best_val_auc=round(float(best_val_auc), 6),
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
                            "val_auc": round(float(best_val_auc), 6),
                            "selection_metric": "val_auc",
                            "best_epoch": int(best_epoch),
                            "bad_epochs": int(bad_epochs),
                            "early_stop_patience": int(patience),
                            "stopped_early": True,
                        }
                    )
                break

        if best_backbone_state is None:
            best_epoch = epochs
            best_backbone_state = self._snapshot_state_dict(backbone)
            best_loss = last_loss

        backbone.load_state_dict(best_backbone_state)
        backbone.eval()
        with torch.no_grad():
            z = backbone(x, eidx)
            val_pos_scores = torch.sigmoid(self._edge_logits(z, val_pos)).detach().cpu().numpy()
            val_neg_scores = torch.sigmoid(self._edge_logits(z, val_neg)).detach().cpu().numpy()

        val_scores = np.concatenate([val_pos_scores, val_neg_scores], axis=0)
        val_labels = np.concatenate(
            [np.ones_like(val_pos_scores, dtype=np.int64), np.zeros_like(val_neg_scores, dtype=np.int64)],
            axis=0,
        )
        threshold, val_acc = best_threshold(val_scores, val_labels)
        val_auc = float(binary_auc(val_scores, val_labels))
        if best_epoch <= 0:
            best_epoch = epochs
        if best_val_auc < 0:
            best_val_auc = val_auc
        if best_val_acc < 0:
            best_val_acc = float(val_acc)
        if best_threshold_value == 0.5 and best_epoch == epochs:
            best_threshold_value = float(threshold)

        metrics = {
            "train_loss": float(best_loss),
            "val_acc": float(best_val_acc),
            "val_auc": float(best_val_auc),
            "threshold": float(best_threshold_value),
            "best_epoch": int(best_epoch),
            "selection_metric": "val_auc",
            "trained_epochs": int(trained_epochs),
            "stopped_early": bool(stopped_early),
        }

        ref = self._store.create_model_dir(session_id, req.task)
        self._store.save_torch(ref.model_dir, "checkpoint.pt", {"backbone": best_backbone_state})
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
                "threshold": float(best_threshold_value),
                "model_config": {
                    "hidden_dim": hidden,
                    "embedding_dim": emb_dim,
                    "num_layers": num_layers,
                    "dropout": dropout,
                    "gat_heads": gat_heads,
                    **gnn_options,
                },
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
    def _edge_logits(z: torch.Tensor, edges: torch.Tensor) -> torch.Tensor:
        if edges.numel() == 0:
            return torch.empty((0,), dtype=torch.float32, device=z.device)
        return (z[edges[0]] * z[edges[1]]).sum(dim=-1)

    @staticmethod
    def _snapshot_state_dict(module: torch.nn.Module) -> Dict[str, torch.Tensor]:
        return {k: v.detach().cpu().clone() for k, v in module.state_dict().items()}

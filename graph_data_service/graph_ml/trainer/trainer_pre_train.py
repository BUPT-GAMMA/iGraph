"""
预训练任务训练器。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import numpy as np
import torch
import torch.nn.functional as F
from torch.optim import Adam, SparseAdam
from torch_geometric.nn import Node2Vec
from torch_geometric.utils import negative_sampling

from backend.logging.logger import get_service_logger
from graph_data_service.graph_data import GraphData
from ..artifact_store import ArtifactStore
from ..models import build_backbone, parse_gnn_options
from ..schemas import TrainRequest
from ..tasks import PreTrainTask, message_passing_edge_index, node_names


class PreTrainTrainer:
    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    def train(
        self,
        session_id: str,
        graph_version: int,
        gd: GraphData,
        req: TrainRequest,
        device: torch.device,
        x: Optional[torch.Tensor] = None,
        feat_meta: Optional[Dict[str, Any]] = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Dict[str, Any]:
        method = PreTrainTask().method(req)
        ml_log = get_service_logger(session_id, "graph_ml")

        if method == "node2vec":
            eidx = message_passing_edge_index(gd)
            emb, metrics = self._train_node2vec(
                session_id,
                gd,
                eidx,
                req,
                progress_callback=progress_callback,
            )
            ref = self._store.create_model_dir(session_id, req.task)
            self._store.save_npy(ref.model_dir, "embedding.npy", emb.astype(np.float32))
            self._store.save_json(ref.model_dir, "node_names.json", node_names(gd))
            self._store.save_json(ref.model_dir, "metrics.json", metrics)
            self._store.save_json(
                ref.model_dir,
                "meta.json",
                {
                    "task": req.task,
                    "method": method,
                    "model": req.model,
                    "graph_version": graph_version,
                    "embedding_dim": int(emb.shape[1]),
                    "num_nodes": gd.num_nodes,
                },
            )
            return {
                "success": True,
                "session_id": session_id,
                "task": req.task,
                "model_id": ref.model_id,
                "graph_version": graph_version,
                "metrics": metrics,
                "meta": {"method": method, "embedding_dim": int(emb.shape[1])},
            }

        if x is None or feat_meta is None:
            raise ValueError("backbone 预训练缺少特征输入")
        if not req.model:
            raise ValueError(f"pretrain method={method} 需要 model")

        x = x.to(device)
        eidx = message_passing_edge_index(gd).to(device)

        hidden = int(req.algo_config.get("hidden_dim", 64))
        emb_dim = int(req.algo_config.get("embedding_dim", hidden))
        num_layers = int(req.algo_config.get("num_layers", 2))
        dropout = float(req.algo_config.get("dropout", 0.2))
        gat_heads = int(req.algo_config.get("gat_heads", 2))
        gnn_options = parse_gnn_options(req.algo_config)

        backbone = build_backbone(
            req.model,
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

        epochs = max(1, int(req.train_config.epochs))
        last_loss = 0.0
        for epoch in range(epochs):
            backbone.train()
            opt.zero_grad()
            z = backbone(x, eidx)
            pos_logits = self._edge_logits(z, eidx)
            neg = negative_sampling(
                edge_index=eidx,
                num_nodes=gd.num_nodes,
                num_neg_samples=eidx.shape[1],
            ).to(device)
            neg_logits = self._edge_logits(z, neg)
            loss = F.binary_cross_entropy_with_logits(pos_logits, torch.ones_like(pos_logits))
            loss = loss + F.binary_cross_entropy_with_logits(neg_logits, torch.zeros_like(neg_logits))
            loss.backward()
            opt.step()
            last_loss = float(loss.detach().cpu().item())
            ml_log.event(
                "TRAIN_EPOCH_PRETRAIN",
                method=method,
                epoch=epoch + 1,
                epochs=epochs,
                loss=round(last_loss, 6),
            )
            if progress_callback is not None:
                progress_callback(
                    {
                        "kind": "train_epoch",
                        "task": req.task,
                        "method": method,
                        "epoch": epoch + 1,
                        "epochs": epochs,
                        "progress": (epoch + 1) / epochs,
                        "message": f"epoch {epoch + 1}/{epochs}",
                        "loss": round(last_loss, 6),
                        "selection_metric": "loss",
                        "stopped_early": False,
                    }
                )

        backbone.eval()
        with torch.no_grad():
            emb = backbone(x, eidx).detach().cpu().numpy().astype(np.float32)

        metrics = {
            "method": method,
            "backbone_model": req.model,
            "pretrain_loss": last_loss,
            "embedding_dim": int(emb.shape[1]),
        }

        ref = self._store.create_model_dir(session_id, req.task)
        self._store.save_torch(ref.model_dir, "checkpoint.pt", {"backbone": backbone.state_dict()})
        self._store.save_npy(ref.model_dir, "embedding.npy", emb)
        self._store.save_json(ref.model_dir, "node_names.json", node_names(gd))
        self._store.save_json(ref.model_dir, "metrics.json", metrics)
        self._store.save_json(
            ref.model_dir,
            "meta.json",
            {
                "task": req.task,
                "method": method,
                "model": req.model,
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
                "method": method,
                "model": req.model,
                "embedding_dim": int(emb.shape[1]),
            },
        }

    @staticmethod
    def _edge_logits(z: torch.Tensor, edges: torch.Tensor) -> torch.Tensor:
        if edges.numel() == 0:
            return torch.empty((0,), dtype=torch.float32, device=z.device)
        return (z[edges[0]] * z[edges[1]]).sum(dim=-1)

    def _train_node2vec(
        self,
        session_id: str,
        gd: GraphData,
        eidx: torch.Tensor,
        req: TrainRequest,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[np.ndarray, Dict[str, Any]]:
        emb_dim = int(req.algo_config.get("embedding_dim", 128))
        walk_length = int(req.algo_config.get("walk_length", 20))
        context_size = int(req.algo_config.get("context_size", 10))
        walks_per_node = int(req.algo_config.get("walks_per_node", 10))
        num_negative_samples = int(req.algo_config.get("num_negative_samples", 1))
        batch_size = int(req.algo_config.get("batch_size", 128))
        epochs = max(1, int(req.train_config.epochs))

        # Node2Vec 随机游走在部分环境仅支持 CPU，默认走 CPU。
        n2v = Node2Vec(
            edge_index=eidx.cpu(),
            embedding_dim=emb_dim,
            walk_length=walk_length,
            context_size=context_size,
            walks_per_node=walks_per_node,
            num_negative_samples=num_negative_samples,
            p=float(req.algo_config.get("p", 1.0)),
            q=float(req.algo_config.get("q", 1.0)),
            sparse=True,
        ).to("cpu")

        loader = n2v.loader(batch_size=batch_size, shuffle=True, num_workers=0)
        opt = SparseAdam(list(n2v.parameters()), lr=float(req.train_config.lr))
        ml_log = get_service_logger(session_id, "graph_ml")
        last_loss = 0.0
        for epoch in range(epochs):
            epoch_losses = []
            for pos_rw, neg_rw in loader:
                opt.zero_grad()
                loss = n2v.loss(pos_rw, neg_rw)
                loss.backward()
                opt.step()
                epoch_losses.append(float(loss.detach().cpu().item()))
            if epoch_losses:
                last_loss = float(np.mean(epoch_losses))
            else:
                last_loss = 0.0
            ml_log.event(
                "TRAIN_EPOCH_PRETRAIN_NODE2VEC",
                epoch=epoch + 1,
                epochs=epochs,
                loss=round(last_loss, 6),
            )
            if progress_callback is not None:
                progress_callback(
                    {
                        "kind": "train_epoch",
                        "task": req.task,
                        "method": "node2vec",
                        "epoch": epoch + 1,
                        "epochs": epochs,
                        "progress": (epoch + 1) / epochs,
                        "message": f"epoch {epoch + 1}/{epochs}",
                        "loss": round(last_loss, 6),
                        "selection_metric": "loss",
                        "stopped_early": False,
                    }
                )

        n2v.eval()
        with torch.no_grad():
            emb = n2v().detach().cpu().numpy().astype(np.float32)
        metrics = {
            "method": "node2vec",
            "num_nodes": gd.num_nodes,
            "embedding_dim": emb_dim,
            "walk_length": walk_length,
            "context_size": context_size,
            "walks_per_node": walks_per_node,
            "num_negative_samples": num_negative_samples,
            "loss": last_loss,
        }
        return emb, metrics

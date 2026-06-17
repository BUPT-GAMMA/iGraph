"""
链接预测推理器。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
import torch

from graph_data_service.graph_data import GraphData
from ..artifact_store import ArtifactRef, ArtifactStore
from ..models import build_backbone, parse_gnn_options
from ..schemas import InferRequest
from ..tasks import LinkPredictionTask, binary_auc, edge_index_tensor, node_names, sample_negative_edges


class LinkPredictionInfer:
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
        ckpt = self._store.load_torch(ref.model_dir, "checkpoint.pt")
        backbone.load_state_dict(ckpt["backbone"])
        backbone.eval()

        with torch.no_grad():
            z = backbone(x, eidx)

        threshold = float(meta.get("threshold", 0.5))
        return_prob = bool(req.infer_config.get("return_prob", True))
        names = node_names(gd)
        lp_task = LinkPredictionTask()

        parsed_edges = lp_task.parse_infer_edges(gd, req.query)
        if parsed_edges:
            return {
                "success": True,
                "task": "link_prediction",
                "model_id": req.model_id,
                "graph_version": graph_version,
                "mode": "specified_edges",
                "query": req.query,
                "threshold": threshold,
                "count": len(parsed_edges),
                "predictions": self._prediction_items(
                    z,
                    parsed_edges,
                    names,
                    threshold,
                    return_prob=return_prob,
                    gd=gd,
                ),
            }

        source = req.query.get("source_node")
        if isinstance(source, str) and gd.has_node_name(source):
            src = gd.name_to_id(source)
            cands = req.query.get("candidate_nodes")
            if not cands:
                cand_ids = [i for i in range(gd.num_nodes) if i != src]
            else:
                cand_ids = [gd.name_to_id(n) for n in cands if isinstance(n, str) and gd.has_node_name(n)]

            topk = int(req.query.get("topk", req.infer_config.get("topk", 10)))
            candidate_edges = [(src, nid) for nid in cand_ids]
            scored = self._prediction_items(
                z,
                candidate_edges,
                names,
                threshold,
                return_prob=return_prob,
                gd=gd,
            )
            scored.sort(key=lambda x: x["score"], reverse=True)
            return {
                "success": True,
                "task": "link_prediction",
                "model_id": req.model_id,
                "graph_version": graph_version,
                "mode": "source_node_topk",
                "query": req.query,
                "threshold": threshold,
                "count": min(len(scored), topk),
                "predictions": scored[:topk],
            }

        if req.query:
            raise ValueError("link_prediction infer 需提供 query.edges 或合法的 query.source_node")

        topk = int(req.infer_config.get("topk", 20))
        max_candidate_pairs = int(req.infer_config.get("max_candidate_pairs", 200000))
        seed = int(req.infer_config.get("sample_seed", 42))
        candidate_edges, sampled = self._default_candidate_edges(
            gd,
            max_candidate_pairs=max(1, max_candidate_pairs),
            seed=seed,
        )
        scored = self._prediction_items(
            z,
            candidate_edges,
            names,
            threshold,
            return_prob=return_prob,
            gd=gd,
        )
        scored.sort(key=lambda x: x["score"], reverse=True)
        return {
            "success": True,
            "task": "link_prediction",
            "model_id": req.model_id,
            "graph_version": graph_version,
            "mode": "evaluation_summary",
            "threshold": threshold,
            "count": min(len(scored), topk),
            "evaluation": self._evaluation_summary(gd, z, threshold, seed=seed),
            "training_metrics": self._load_training_metrics(ref),
            "candidate_pairs_evaluated": len(candidate_edges),
            "candidate_pairs_sampled": sampled,
            "top_predictions": scored[:topk],
        }

    @staticmethod
    def _prediction_items(
        z: torch.Tensor,
        edges: Iterable[Tuple[int, int]],
        names: List[str],
        threshold: float,
        *,
        return_prob: bool,
        gd: GraphData,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for u, v in edges:
            logit = float(torch.sum(z[u] * z[v]).item())
            prob = float(torch.sigmoid(torch.tensor(logit)).item())
            item = {
                "src": names[u],
                "dst": names[v],
                "score": logit,
                "pred": int(prob >= threshold),
                "exists_in_graph": bool(gd.has_edge(u, v)),
            }
            if return_prob:
                item["prob"] = prob
            out.append(item)
        return out

    @staticmethod
    def _default_candidate_edges(
        gd: GraphData,
        *,
        max_candidate_pairs: int,
        seed: int,
    ) -> tuple[List[Tuple[int, int]], bool]:
        rng = np.random.default_rng(seed)
        candidates: List[Tuple[int, int]] = []
        seen = 0
        sampled = False
        for u in range(gd.num_nodes):
            v_range = range(gd.num_nodes) if gd.directed else range(u + 1, gd.num_nodes)
            for v in v_range:
                if u == v:
                    continue
                if gd.has_edge(u, v):
                    continue
                if not gd.directed and gd.has_edge(v, u):
                    continue
                seen += 1
                pair = (u, v)
                if len(candidates) < max_candidate_pairs:
                    candidates.append(pair)
                    continue
                sampled = True
                replace_idx = int(rng.integers(0, seen))
                if replace_idx < max_candidate_pairs:
                    candidates[replace_idx] = pair
        return candidates, sampled

    @staticmethod
    def _evaluation_summary(
        gd: GraphData,
        z: torch.Tensor,
        threshold: float,
        *,
        seed: int,
    ) -> Dict[str, Any]:
        pos_edges = [(int(u), int(v)) for u, v in gd.all_edges()]
        if not pos_edges:
            return {"available": False, "reason": "current_graph_has_no_positive_edges"}
        existing = set(pos_edges)
        neg_edges = sample_negative_edges(
            num_samples=len(pos_edges),
            existing_edges=existing,
            num_nodes=gd.num_nodes,
            directed=gd.directed,
            seed=seed + 17,
        )
        if not neg_edges:
            return {"available": False, "reason": "negative_sampling_failed"}

        scores: List[float] = []
        labels: List[int] = []
        for label, edges in ((1, pos_edges), (0, neg_edges)):
            for u, v in edges:
                logit = float(torch.sum(z[u] * z[v]).item())
                prob = float(torch.sigmoid(torch.tensor(logit)).item())
                scores.append(prob)
                labels.append(label)
        score_np = np.asarray(scores, dtype=np.float64)
        label_np = np.asarray(labels, dtype=np.int64)
        pred_np = (score_np >= threshold).astype(np.int64)
        tp = int(np.sum((pred_np == 1) & (label_np == 1)))
        fp = int(np.sum((pred_np == 1) & (label_np == 0)))
        fn = int(np.sum((pred_np == 0) & (label_np == 1)))
        accuracy = float(np.mean(pred_np == label_np)) if label_np.size else 0.0
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        return {
            "available": True,
            "scope": "current_graph_positive_edges_vs_sampled_negative_edges",
            "positive_count": len(pos_edges),
            "negative_count": len(neg_edges),
            "accuracy": accuracy,
            "precision": float(precision),
            "recall": float(recall),
            "f1": float(f1),
            "auc": float(binary_auc(score_np, label_np)),
            "threshold": float(threshold),
        }

    def _load_training_metrics(self, ref: ArtifactRef) -> Dict[str, Any] | None:
        try:
            metrics = self._store.load_json(ref.model_dir, "metrics.json")
            return metrics if isinstance(metrics, dict) else None
        except Exception:
            return None

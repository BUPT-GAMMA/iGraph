"""
训练编排入口。
"""

from __future__ import annotations

import random
from typing import Any, Callable, Dict, Tuple

import numpy as np
import torch

from graph_data_service.graph_data import GraphData

from ..artifact_store import ArtifactStore
from ..device_utils import select_best_gpu
from ..schemas import TrainRequest
from ..tasks import PreTrainTask, base_node_features, node_names, validate_train_request
from .trainer_link_prediction import LinkPredictionTrainer
from .trainer_node_classification import NodeClassificationTrainer
from .trainer_pre_train import PreTrainTrainer


class GraphMLTrainer:
    def __init__(
        self,
        graph_loader: Callable[[str], Tuple[int, GraphData]],
        store: ArtifactStore,
    ) -> None:
        self._graph_loader = graph_loader
        self._store = store
        self._node_trainer = NodeClassificationTrainer(store)
        self._link_trainer = LinkPredictionTrainer(store)
        self._pre_trainer = PreTrainTrainer(store)

    def train(
        self,
        session_id: str,
        req: TrainRequest,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Dict[str, Any]:
        validate_train_request(req)
        self._set_seed(req.train_config.seed)
        device = self._resolve_device(req)

        graph_version, gd = self._load_graph(session_id)
        if req.graph_version is not None and int(req.graph_version) != graph_version:
            raise ValueError(
                f"graph_version 不一致: 请求={req.graph_version}, 当前={graph_version}"
            )

        if req.task == "node_classification":
            x, feat_meta = self._resolve_features(session_id, gd, req)
            return self._node_trainer.train(
                session_id=session_id,
                graph_version=graph_version,
                gd=gd,
                req=req,
                x=x,
                feat_meta=feat_meta,
                device=device,
                progress_callback=progress_callback,
            )

        if req.task == "link_prediction":
            x, feat_meta = self._resolve_features(session_id, gd, req)
            return self._link_trainer.train(
                session_id=session_id,
                graph_version=graph_version,
                gd=gd,
                req=req,
                x=x,
                feat_meta=feat_meta,
                device=device,
                progress_callback=progress_callback,
            )

        if req.task == "pretrain":
            method = PreTrainTask().method(req)
            if method == "node2vec":
                return self._pre_trainer.train(
                    session_id=session_id,
                    graph_version=graph_version,
                    gd=gd,
                    req=req,
                    device=device,
                    progress_callback=progress_callback,
                )
            x, feat_meta = self._resolve_features(session_id, gd, req)
            return self._pre_trainer.train(
                session_id=session_id,
                graph_version=graph_version,
                gd=gd,
                req=req,
                device=device,
                x=x,
                feat_meta=feat_meta,
                progress_callback=progress_callback,
            )

        raise ValueError(f"不支持的 task: {req.task}")

    def _load_graph(self, session_id: str) -> Tuple[int, GraphData]:
        return self._graph_loader(session_id)

    def _resolve_features(
        self,
        session_id: str,
        gd: GraphData,
        req: TrainRequest,
    ) -> Tuple[torch.Tensor, Dict[str, Any]]:
        source = req.data.feature_source
        if source == "embedding":
            upstream = (req.upstream_model_id or "").strip()
            if not upstream:
                raise ValueError("feature_source=embedding 时 upstream_model_id 必填")
            emb = self._load_embedding_for_graph(session_id, upstream, gd)
            return emb, {"feature_source": "embedding", "upstream_model_id": upstream}
        return base_node_features(gd), {"feature_source": "graph"}

    def _load_embedding_for_graph(self, session_id: str, model_id: str, gd: GraphData) -> torch.Tensor:
        ref = self._store.resolve_model(session_id, model_id)
        if ref is None:
            raise ValueError(f"上游 embedding 模型不存在: {model_id}")
        emb = self._store.load_npy(ref.model_dir, "embedding.npy")
        names = self._store.load_json(ref.model_dir, "node_names.json")
        if emb.shape[0] != len(names):
            raise ValueError("embedding 与 node_names 尺寸不一致")

        name_to_idx = {str(n): i for i, n in enumerate(names)}
        cur_names = node_names(gd)
        aligned = np.zeros((gd.num_nodes, emb.shape[1]), dtype=np.float32)
        for i, n in enumerate(cur_names):
            j = name_to_idx.get(n)
            if j is not None:
                aligned[i] = emb[j]
        return torch.as_tensor(aligned, dtype=torch.float32)

    @staticmethod
    def _resolve_device(req: TrainRequest) -> torch.device:
        dev = str(req.algo_config.get("device", "auto")).lower()
        if dev == "cpu":
            return torch.device("cpu")
        if dev.startswith("cuda"):
            if torch.cuda.is_available():
                return torch.device(dev)
            raise ValueError(f"请求 device={dev}，但当前环境无可用 GPU")
        return select_best_gpu()

    @staticmethod
    def _set_seed(seed: int) -> None:
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)


__all__ = ["GraphMLTrainer"]

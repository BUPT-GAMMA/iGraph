"""
推理编排入口。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

import numpy as np
import torch

from graph_data_service.graph_data import GraphData

from ..artifact_store import ArtifactStore
from ..datasets import GraphMLDatasetStore
from ..device_utils import select_best_gpu
from ..schemas import InferRequest
from ..tasks import base_node_features, node_names
from .infer_link_prediction import LinkPredictionInfer
from .infer_node_classification import NodeClassificationInfer
from .infer_pre_train import PreTrainInfer


class GraphMLInferencer:
    def __init__(
        self,
        graph_loader: Callable[[str], Tuple[int, GraphData]],
        store: ArtifactStore,
        dataset_store: GraphMLDatasetStore | None = None,
    ) -> None:
        self._graph_loader = graph_loader
        self._store = store
        self._dataset_store = dataset_store
        self._node_infer = NodeClassificationInfer(store)
        self._link_infer = LinkPredictionInfer(store)
        self._pre_infer = PreTrainInfer(store)

    def infer(self, session_id: str, req: InferRequest) -> Dict[str, Any]:
        ref = self._store.resolve_model(session_id, req.model_id)
        if ref is None:
            raise ValueError(f"模型不存在: {req.model_id}")
        meta = self._store.load_json(ref.model_dir, "meta.json")
        task = str(meta.get("task", ""))

        if task == "pretrain":
            return self._pre_infer.infer(ref, meta, req)

        graph_version, gd = self._load_graph(ref.session_id)
        if req.graph_version is not None and int(req.graph_version) != graph_version:
            raise ValueError(
                f"graph_version 不一致: 请求={req.graph_version}, 当前={graph_version}"
            )
        trained_ver = int(meta.get("graph_version", -1))
        allow_mismatch = bool(req.infer_config.get("allow_version_mismatch", False))
        if (not allow_mismatch) and trained_ver >= 0 and trained_ver != graph_version:
            raise ValueError(
                f"模型训练图版本={trained_ver}，当前图版本={graph_version}。"
                "如需强制推理，请设置 infer_config.allow_version_mismatch=true。"
            )

        device = select_best_gpu()
        x = self._resolve_features_for_infer(ref.session_id, meta, gd)
        if task == "node_classification":
            return self._node_infer.infer(ref, meta, gd, graph_version, req, x, device)
        if task == "link_prediction":
            return self._link_infer.infer(ref, meta, gd, graph_version, req, x, device)
        raise ValueError(f"模型 task 不支持推理: {task}")

    def _load_graph(self, session_id: str) -> Tuple[int, GraphData]:
        return self._graph_loader(session_id)

    def _resolve_features_for_infer(
        self,
        session_id: str,
        meta: Dict[str, Any],
        gd: GraphData,
    ) -> torch.Tensor:
        source = str(meta.get("feature_source", "graph"))
        if source == "embedding":
            upstream = str(meta.get("upstream_model_id", "")).strip()
            if not upstream:
                raise ValueError("模型缺少 upstream_model_id")
            return self._load_embedding_for_graph(session_id, upstream, gd)
        return base_node_features(gd)

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


__all__ = ["GraphMLInferencer"]

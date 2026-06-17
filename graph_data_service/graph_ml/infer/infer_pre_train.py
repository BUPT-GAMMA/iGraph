"""
预训练产物推理（embedding 查询）。
"""

from __future__ import annotations

from typing import Any, Dict

from ..artifact_store import ArtifactRef, ArtifactStore
from ..schemas import InferRequest


class PreTrainInfer:
    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    def infer(self, ref: ArtifactRef, meta: Dict[str, Any], req: InferRequest) -> Dict[str, Any]:
        model_ver = int(meta.get("graph_version", -1))
        if req.graph_version is not None and int(req.graph_version) != model_ver:
            raise ValueError(
                f"graph_version 不一致: 请求={req.graph_version}, 模型={model_ver}"
            )

        emb = self._store.load_npy(ref.model_dir, "embedding.npy")
        names = self._store.load_json(ref.model_dir, "node_names.json")
        name_to_idx = {str(n): i for i, n in enumerate(names)}
        limit = int(req.infer_config.get("limit", 200))
        nodes = req.query.get("nodes")
        if nodes:
            indices = [name_to_idx[n] for n in nodes if isinstance(n, str) and n in name_to_idx]
        else:
            indices = list(range(min(len(names), limit)))

        vectors = [{"node": names[i], "embedding": emb[i].astype(float).tolist()} for i in indices]
        return {
            "success": True,
            "task": "pretrain",
            "model_id": req.model_id,
            "graph_version": model_ver,
            "method": meta.get("method"),
            "count": len(vectors),
            "embeddings": vectors,
        }


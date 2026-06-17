"""
GraphML in-process 模块。

与 GraphDataService 同进程运行，直接读取会话图数据，
避免通过 graph/document 做跨服务序列化传输。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Tuple

from graph_data_service.graph_data import GraphData
from graph_data_service.session import GraphSession

from .artifact_store import ArtifactStore
from .config_catalog import model_architecture_view
from .schemas import (
    InferRequest,
    TrainRequest,
    VisualizeArchitectureRequest,
)


class GraphMLModule:
    """
    GraphDataService 的图机器学习子模块。

    get_session: session_id -> GraphSession（不存在时抛异常）
    """

    def __init__(
        self,
        get_session: Callable[[str], GraphSession],
        artifact_root: str | None = None,
    ) -> None:
        self._get_session = get_session
        store = ArtifactStore(artifact_root or os.getenv("GRAPH_ML_ARTIFACT_ROOT", "artifacts/graph_ml"))
        self._store = store
        self._trainer = None
        self._inferencer = None

    def _ensure_runtime(self) -> None:
        if self._trainer is not None and self._inferencer is not None:
            return
        from .infer import GraphMLInferencer
        from .trainer import GraphMLTrainer

        self._trainer = GraphMLTrainer(graph_loader=self._load_graph_snapshot, store=self._store)
        self._inferencer = GraphMLInferencer(graph_loader=self._load_graph_snapshot, store=self._store)

    def _load_graph_snapshot(self, session_id: str) -> Tuple[int, GraphData]:
        session = self._get_session(session_id)
        gd = session.get_graph()
        if gd is None:
            raise ValueError("尚未加载任何图数据")
        return session.graph_version, gd

    def train(
        self,
        session_id: str,
        payload: Dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict:
        self._ensure_runtime()
        assert self._trainer is not None
        req = TrainRequest.model_validate(payload)
        return self._trainer.train(session_id, req, progress_callback=progress_callback)

    def infer(self, session_id: str, payload: Dict[str, Any]) -> dict:
        self._ensure_runtime()
        assert self._inferencer is not None
        req = InferRequest.model_validate(payload)
        return self._inferencer.infer(session_id, req)

    def visualize_model_architecture(self, session_id: str, payload: Dict[str, Any]) -> dict:
        req = VisualizeArchitectureRequest.model_validate(payload)
        ref = self._store.resolve_model(session_id, req.model_id)
        if ref is None:
            raise ValueError(f"模型不存在: {req.model_id}")
        meta = self._store.load_json(ref.model_dir, "meta.json")
        task = str(meta.get("task", "")).strip()
        if task not in {"node_classification", "link_prediction", "pretrain"}:
            raise ValueError(f"模型 task 不支持结构可视化: {task}")
        return model_architecture_view(
            task=task,
            model=str(meta.get("model") or ""),
            method=str(meta.get("method") or ""),
            algo_config={},
            model_id=req.model_id,
            meta=meta,
        )

"""
GraphML Server 请求/响应 schema。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


TaskType = Literal["node_classification", "link_prediction", "pretrain"]
FeatureSource = Literal["graph", "embedding"]


class TrainDataConfig(BaseModel):
    feature_source: FeatureSource = "graph"
    target_nodes: Optional[List[str]] = None
    target_edges: Optional[List[List[str]]] = None


class TrainSplitConfig(BaseModel):
    train: float = 0.8
    val: float = 0.2
    # 兼容旧请求字段；当前训练流程不再使用 test 划分。
    test: Optional[float] = None


class TrainConfig(BaseModel):
    split: TrainSplitConfig = Field(default_factory=TrainSplitConfig)
    epochs: int = 200
    lr: float = 0.05
    weight_decay: float = 0.0
    seed: int = 42
    # <=0 表示关闭 early stop；>0 表示连续 N 个 epoch 无提升则提前停止。
    early_stop_patience: int = 0
    # 指标提升阈值（严格大于 best + min_delta 才视为提升）。
    early_stop_min_delta: float = 0.0


class TrainRequest(BaseModel):
    task: TaskType
    model: Optional[str] = None
    method: Optional[str] = None
    graph_version: Optional[int] = None
    upstream_model_id: Optional[str] = None
    data: TrainDataConfig = Field(default_factory=TrainDataConfig)
    train_config: TrainConfig = Field(default_factory=TrainConfig)
    algo_config: Dict[str, Any] = Field(default_factory=dict)


class InferRequest(BaseModel):
    model_id: str
    graph_version: Optional[int] = None
    query: Dict[str, Any] = Field(default_factory=dict)
    infer_config: Dict[str, Any] = Field(default_factory=dict)


class VisualizeArchitectureRequest(BaseModel):
    model_id: str

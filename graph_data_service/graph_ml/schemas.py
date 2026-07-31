"""
GraphML Server 请求/响应 schema。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


TaskType = Literal[
    "node_classification",
    "link_prediction",
    "graph_classification",
    "pretrain",
]
FeatureSource = Literal["graph", "embedding"]
GraphClassificationDatasetProfile = Literal["auto", "hiv", "pcba"]


class TrainDataConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature_source: FeatureSource = "graph"
    target_nodes: Optional[List[str]] = None
    target_edges: Optional[List[List[str]]] = None
    dataset_id: Optional[str] = None


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
    # 多图 mini-batch 参数；现有单图任务保持原有行为并忽略这两个字段。
    batch_size: int = Field(default=64, ge=1)
    num_workers: int = Field(default=0, ge=0)


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


class LoadGraphClassificationDatasetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1)
    dataset_profile: GraphClassificationDatasetProfile = "auto"

    @field_validator("filename")
    @classmethod
    def _strip_filename(cls, value: str) -> str:
        filename = value.strip()
        if not filename:
            raise ValueError("filename 不能为空")
        return filename

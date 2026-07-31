"""Mini-batch construction and validation for graph classification."""

from __future__ import annotations

import random
from dataclasses import dataclass
from numbers import Integral
from typing import Literal

import numpy as np
import torch
from torch.utils.data import Subset
from torch_geometric.data import Batch
from torch_geometric.loader import DataLoader

from .graph_classification import GraphClassificationDatasetBundle


GraphClassificationSplit = Literal["train", "val", "test"]


@dataclass(frozen=True)
class GraphClassificationBatchInfo:
    num_graphs: int
    num_nodes: int
    num_edges: int
    num_tasks: int
    num_valid_labels: int
    valid_label_mask: torch.Tensor
    uses_edge_features: bool = False

    @property
    def has_valid_labels(self) -> bool:
        return self.num_valid_labels > 0


@dataclass
class GraphClassificationDataLoaders:
    train: DataLoader
    val: DataLoader
    test: DataLoader
    batch_size: int
    num_workers: int
    seed: int
    pin_memory: bool

    def for_split(self, split: GraphClassificationSplit) -> DataLoader:
        if split == "train":
            return self.train
        if split == "val":
            return self.val
        if split == "test":
            return self.test
        raise ValueError(f"图分类 split 仅支持 train/val/test，收到: {split}")


def build_graph_classification_dataloaders(
    bundle: GraphClassificationDatasetBundle,
    *,
    batch_size: int = 64,
    num_workers: int = 0,
    seed: int = 42,
    pin_memory: bool | None = None,
) -> GraphClassificationDataLoaders:
    """Build deterministic loaders over the benchmark-provided split only."""
    normalized_batch_size = _require_int(batch_size, "batch_size", minimum=1)
    normalized_num_workers = _require_int(num_workers, "num_workers", minimum=0)
    normalized_seed = _require_int(seed, "seed", minimum=0)
    if normalized_seed > torch.iinfo(torch.int64).max:
        raise ValueError("seed 超出 int64 范围")
    resolved_pin_memory = torch.cuda.is_available() if pin_memory is None else pin_memory
    if not isinstance(resolved_pin_memory, bool):
        raise ValueError("pin_memory 必须是布尔值")

    normalized_splits = _validate_bundle_splits(bundle)
    common_kwargs = {
        "batch_size": normalized_batch_size,
        "num_workers": normalized_num_workers,
        "pin_memory": resolved_pin_memory,
        "drop_last": False,
        "worker_init_fn": _seed_worker,
    }
    train_loader = DataLoader(
        Subset(bundle.graphs, normalized_splits["train"]),
        shuffle=True,
        generator=_seeded_generator(normalized_seed),
        **common_kwargs,
    )
    val_loader = DataLoader(
        Subset(bundle.graphs, normalized_splits["val"]),
        shuffle=False,
        generator=_seeded_generator(normalized_seed + 1),
        **common_kwargs,
    )
    test_loader = DataLoader(
        Subset(bundle.graphs, normalized_splits["test"]),
        shuffle=False,
        generator=_seeded_generator(normalized_seed + 2),
        **common_kwargs,
    )
    return GraphClassificationDataLoaders(
        train=train_loader,
        val=val_loader,
        test=test_loader,
        batch_size=normalized_batch_size,
        num_workers=normalized_num_workers,
        seed=normalized_seed,
        pin_memory=resolved_pin_memory,
    )


def validate_graph_classification_batch(
    batch: Batch,
    bundle: GraphClassificationDatasetBundle,
    *,
    require_valid_labels: bool = False,
) -> GraphClassificationBatchInfo:
    """Validate the collated PyG contract and return its reusable label mask."""
    if not isinstance(batch, Batch):
        raise ValueError("图分类 batch 必须是 torch_geometric.data.Batch")
    x = getattr(batch, "x", None)
    batch_vector = getattr(batch, "batch", None)
    if not isinstance(x, torch.Tensor) or x.numel() == 0:
        raise ValueError("图分类 batch 不能为空且必须包含节点类别 x")
    if not isinstance(batch_vector, torch.Tensor):
        raise ValueError("图分类 batch 缺少 batch 向量，无法执行 graph pooling")

    edge_index = getattr(batch, "edge_index", None)
    xe = getattr(batch, "xe", None)
    y = getattr(batch, "y", None)
    if not isinstance(edge_index, torch.Tensor):
        raise ValueError("图分类 batch 缺少 edge_index")
    if not isinstance(xe, torch.Tensor):
        raise ValueError("图分类 batch 缺少边类别 xe")
    if not isinstance(y, torch.Tensor):
        raise ValueError("图分类 batch 缺少 graph-level 标签 y")

    _require_long_tensor(x, "batch.x")
    _require_long_tensor(xe, "batch.xe")
    _require_long_tensor(edge_index, "batch.edge_index")
    _require_long_tensor(batch_vector, "batch.batch")
    if x.ndim != 1:
        raise ValueError("batch.x 必须是一维节点类别 ID")
    if xe.ndim != 1:
        raise ValueError("batch.xe 必须是一维边类别 ID")
    if edge_index.ndim != 2 or edge_index.shape[0] != 2:
        raise ValueError("batch.edge_index 必须是 shape [2, E]")
    if batch_vector.ndim != 1 or batch_vector.shape[0] != x.shape[0]:
        raise ValueError("batch.batch 必须与节点数量一致")

    num_nodes = int(x.shape[0])
    num_edges = int(edge_index.shape[1])
    if int(xe.numel()) != num_edges:
        raise ValueError("batch.xe 长度必须与边数一致")
    if int(x.min().item()) < 0 or int(x.max().item()) >= bundle.node_vocab_size:
        raise ValueError("batch.x 包含超出节点词表范围的类别 ID")
    if num_edges:
        if int(xe.min().item()) < 0 or int(xe.max().item()) >= bundle.edge_vocab_size:
            raise ValueError("batch.xe 包含超出边词表范围的类别 ID")
        if int(edge_index.min().item()) < 0 or int(edge_index.max().item()) >= num_nodes:
            raise ValueError("batch.edge_index 包含越界节点索引")

    try:
        num_graphs = int(batch.num_graphs)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("图分类 batch 无法确定图数量") from exc
    if num_graphs < 1:
        raise ValueError("图分类 batch 不能为空")
    graph_ids = torch.unique(batch_vector)
    expected_graph_ids = torch.arange(
        num_graphs,
        dtype=batch_vector.dtype,
        device=batch_vector.device,
    )
    if not torch.equal(graph_ids, expected_graph_ids):
        raise ValueError("batch.batch 未完整覆盖每张图的 graph id")

    if y.ndim != 2 or tuple(y.shape) != (num_graphs, bundle.num_tasks):
        raise ValueError(
            "batch.y shape 错误: "
            f"期望 [{num_graphs}, {bundle.num_tasks}]，实际 {list(y.shape)}"
        )
    if not torch.is_floating_point(y) or torch.is_complex(y):
        raise ValueError("batch.y 必须是浮点 graph-level 标签")
    if torch.isinf(y).any().item():
        raise ValueError("batch.y 不能包含无穷值")
    valid_label_mask = ~torch.isnan(y)
    invalid_labels = valid_label_mask & (y != 0) & (y != 1)
    if invalid_labels.any().item():
        raise ValueError("batch.y 只能包含 0、1 或 NaN")
    num_valid_labels = int(valid_label_mask.sum().item())
    if require_valid_labels and num_valid_labels == 0:
        raise ValueError("当前图分类 batch 没有任何有效标签，应跳过该 batch")

    return GraphClassificationBatchInfo(
        num_graphs=num_graphs,
        num_nodes=num_nodes,
        num_edges=num_edges,
        num_tasks=bundle.num_tasks,
        num_valid_labels=num_valid_labels,
        valid_label_mask=valid_label_mask,
    )


def _validate_bundle_splits(
    bundle: GraphClassificationDatasetBundle,
) -> dict[GraphClassificationSplit, list[int]]:
    num_graphs = len(bundle.graphs)
    if num_graphs < 1:
        raise ValueError("图分类数据集不能为空")
    normalized: dict[GraphClassificationSplit, list[int]] = {}
    occupied: set[int] = set()
    for split in ("train", "val", "test"):
        raw_indices = bundle.split_indices.get(split)
        if not isinstance(raw_indices, (list, tuple)) or not raw_indices:
            raise ValueError(f"图分类数据集的 {split} split 不能为空")
        indices: list[int] = []
        local_seen: set[int] = set()
        for raw_index in raw_indices:
            if isinstance(raw_index, bool) or not isinstance(raw_index, Integral):
                raise ValueError(f"图分类数据集的 {split} split 必须是整数索引")
            index = int(raw_index)
            if index < 0 or index >= num_graphs:
                raise ValueError(f"图分类数据集的 {split} split 包含越界索引: {index}")
            if index in local_seen:
                raise ValueError(f"图分类数据集的 {split} split 包含重复索引: {index}")
            if index in occupied:
                raise ValueError(f"图分类数据集 split 存在重叠索引: {index}")
            local_seen.add(index)
            occupied.add(index)
            indices.append(index)
        normalized[split] = indices
    if len(occupied) != num_graphs:
        raise ValueError("图分类数据集的 train/val/test 未完整覆盖全部图")
    return normalized


def _seed_worker(worker_id: int) -> None:
    del worker_id
    worker_seed = torch.initial_seed() % (2**32)
    random.seed(worker_seed)
    np.random.seed(worker_seed)


def _seeded_generator(seed: int) -> torch.Generator:
    generator = torch.Generator()
    generator.manual_seed(seed)
    return generator


def _require_int(value: int, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} 必须是整数")
    normalized = int(value)
    if normalized < minimum:
        raise ValueError(f"{name} 必须大于等于 {minimum}")
    return normalized


def _require_long_tensor(value: torch.Tensor, name: str) -> None:
    if value.dtype != torch.long:
        raise ValueError(f"{name} 必须是 torch.long Tensor")


__all__ = [
    "GraphClassificationBatchInfo",
    "GraphClassificationDataLoaders",
    "GraphClassificationSplit",
    "build_graph_classification_dataloaders",
    "validate_graph_classification_batch",
]

"""Categorical node encoder shared by graph-classification models."""

from __future__ import annotations

from numbers import Integral

import torch
from torch import nn


class NodeCategoryEncoder(nn.Module):
    """Map one-dimensional categorical node IDs to dense float features."""

    uses_edge_features = False

    def __init__(
        self,
        node_vocab_size: int,
        embedding_dim: int,
        *,
        validate_ids: bool = True,
    ) -> None:
        super().__init__()
        self.node_vocab_size = _require_positive_int(
            node_vocab_size,
            "node_vocab_size",
        )
        self.embedding_dim = _require_positive_int(embedding_dim, "embedding_dim")
        if not isinstance(validate_ids, bool):
            raise ValueError("validate_ids 必须是布尔值")
        self.validate_ids = validate_ids
        self.embedding = nn.Embedding(self.node_vocab_size, self.embedding_dim)

    def forward(self, node_category_ids: torch.Tensor) -> torch.Tensor:
        if not isinstance(node_category_ids, torch.Tensor):
            raise ValueError("节点类别 ID 必须是 Tensor")
        if node_category_ids.dtype != torch.long:
            raise ValueError("节点类别 ID 必须是 torch.long Tensor")
        if node_category_ids.ndim != 1 or node_category_ids.numel() == 0:
            raise ValueError("节点类别 ID 必须是非空一维 Tensor")
        if self.validate_ids:
            min_id = int(node_category_ids.min().item())
            max_id = int(node_category_ids.max().item())
            if min_id < 0 or max_id >= self.node_vocab_size:
                raise ValueError(
                    "节点类别 ID 超出 embedding 词表范围: "
                    f"min={min_id}, max={max_id}, vocab_size={self.node_vocab_size}"
                )
        return self.embedding(node_category_ids)


def _require_positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 1:
        raise ValueError(f"{name} 必须是正整数")
    return int(value)


__all__ = ["NodeCategoryEncoder"]

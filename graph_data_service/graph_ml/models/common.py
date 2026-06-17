"""
Shared building blocks for GNN backbones.
"""

from __future__ import annotations

import torch
from torch import nn


NORM_POSITIONS = {"pre", "post"}
NORM_TYPES = {"rms_norm", "layer_norm", "batch_norm"}


class RMSNorm(nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6) -> None:
        super().__init__()
        self.weight = nn.Parameter(torch.ones(dim))
        self.eps = float(eps)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        scale = torch.rsqrt(x.pow(2).mean(dim=-1, keepdim=True) + self.eps)
        return x * scale * self.weight


def build_norm(norm_type: str, channels: int) -> nn.Module:
    normalized = str(norm_type or "layer_norm").strip().lower()
    if normalized == "rms_norm":
        return RMSNorm(channels)
    if normalized == "layer_norm":
        return nn.LayerNorm(channels)
    if normalized == "batch_norm":
        return nn.BatchNorm1d(channels)
    raise ValueError(f"不支持的 norm_type: {norm_type}")


def normalize_norm_pos(norm_pos: str) -> str:
    normalized = str(norm_pos or "post").strip().lower()
    if normalized not in NORM_POSITIONS:
        raise ValueError(f"norm_pos 仅支持 pre/post，收到: {norm_pos}")
    return normalized


def normalize_norm_type(norm_type: str) -> str:
    normalized = str(norm_type or "layer_norm").strip().lower()
    if normalized not in NORM_TYPES:
        raise ValueError(
            f"norm_type 仅支持 rms_norm/layer_norm/batch_norm，收到: {norm_type}"
        )
    return normalized


class GNNLayerStack(nn.Module):
    def __init__(
        self,
        dims: list[int],
        *,
        use_residual_connection: bool = False,
        use_normalization: bool = False,
        norm_pos: str = "post",
        norm_type: str = "layer_norm",
    ) -> None:
        super().__init__()
        self.use_residual_connection = bool(use_residual_connection)
        self.use_normalization = bool(use_normalization)
        self.norm_pos = normalize_norm_pos(norm_pos)
        self.norm_type = normalize_norm_type(norm_type)
        self.residual_projections = nn.ModuleList()
        self.norms = nn.ModuleList()

        for in_dim, out_dim in zip(dims[:-1], dims[1:]):
            if self.use_residual_connection:
                self.residual_projections.append(
                    nn.Identity()
                    if in_dim == out_dim
                    else nn.Linear(in_dim, out_dim, bias=False)
                )
            if self.use_normalization:
                norm_dim = in_dim if self.norm_pos == "pre" else out_dim
                self.norms.append(build_norm(self.norm_type, norm_dim))

    def norm_input(self, h: torch.Tensor, layer_idx: int) -> torch.Tensor:
        if self.use_normalization and self.norm_pos == "pre":
            return self.norms[layer_idx](h)
        return h

    def finish_layer(
        self,
        h: torch.Tensor,
        residual: torch.Tensor,
        layer_idx: int,
    ) -> torch.Tensor:
        if self.use_residual_connection:
            h = h + self.residual_projections[layer_idx](residual)
        if self.use_normalization and self.norm_pos == "post":
            h = self.norms[layer_idx](h)
        return h

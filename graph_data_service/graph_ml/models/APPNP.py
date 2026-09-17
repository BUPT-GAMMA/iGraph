"""APPNP backbone: neural prediction followed by personalized propagation."""

from __future__ import annotations

import math

import torch
import torch.nn.functional as F
from torch import nn
from torch_geometric.nn import APPNP

from .common import GNNLayerStack


class APPNPBackbone(nn.Module):
    """MLP predictor with approximate personalized PageRank propagation."""

    def __init__(
        self,
        in_channels: int,
        hidden_channels: int,
        out_channels: int,
        num_layers: int = 2,
        dropout: float = 0.2,
        appnp_k: int = 10,
        appnp_alpha: float = 0.1,
        use_residual_connection: bool = False,
        use_normalization: bool = False,
        norm_pos: str = "post",
        norm_type: str = "layer_norm",
    ) -> None:
        super().__init__()
        self.dropout = float(dropout)
        self.num_layers = max(2, int(num_layers))
        self.appnp_k = int(appnp_k)
        self.appnp_alpha = float(appnp_alpha)
        if self.appnp_k < 1:
            raise ValueError("appnp_k 必须是正整数")
        if not math.isfinite(self.appnp_alpha) or not 0.0 < self.appnp_alpha <= 1.0:
            raise ValueError("appnp_alpha 必须满足 0 < appnp_alpha <= 1")

        dims = [in_channels] + [hidden_channels] * (self.num_layers - 1) + [out_channels]
        self.linears = nn.ModuleList(
            nn.Linear(dims[i], dims[i + 1]) for i in range(self.num_layers)
        )
        self.layer_stack = GNNLayerStack(
            dims,
            use_residual_connection=use_residual_connection,
            use_normalization=use_normalization,
            norm_pos=norm_pos,
            norm_type=norm_type,
        )
        self.propagation = APPNP(
            K=self.appnp_k,
            alpha=self.appnp_alpha,
            dropout=self.dropout,
            cached=False,
        )

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor) -> torch.Tensor:
        h = x
        for i, linear in enumerate(self.linears):
            residual = h
            h = self.layer_stack.norm_input(h, i)
            h = linear(h)
            h = self.layer_stack.finish_layer(h, residual, i)
            if i < len(self.linears) - 1:
                h = F.relu(h)
                h = F.dropout(h, p=self.dropout, training=self.training)
        return self.propagation(h, edge_index)

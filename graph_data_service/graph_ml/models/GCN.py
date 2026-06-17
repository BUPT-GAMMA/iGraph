"""
GCN Backbone (PyG)。
"""

from __future__ import annotations

import torch
import torch.nn.functional as F
from torch import nn
from torch_geometric.nn import GCNConv

from .common import GNNLayerStack


class GCNBackbone(nn.Module):
    def __init__(
        self,
        in_channels: int,
        hidden_channels: int,
        out_channels: int,
        num_layers: int = 2,
        dropout: float = 0.2,
        use_residual_connection: bool = False,
        use_normalization: bool = False,
        norm_pos: str = "post",
        norm_type: str = "layer_norm",
    ) -> None:
        super().__init__()
        self.dropout = float(dropout)
        self.num_layers = max(2, int(num_layers))
        self.convs = nn.ModuleList()

        dims = [in_channels] + [hidden_channels] * (self.num_layers - 1) + [out_channels]
        for i in range(self.num_layers):
            self.convs.append(GCNConv(dims[i], dims[i + 1]))
        self.layer_stack = GNNLayerStack(
            dims,
            use_residual_connection=use_residual_connection,
            use_normalization=use_normalization,
            norm_pos=norm_pos,
            norm_type=norm_type,
        )

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor) -> torch.Tensor:
        h = x
        for i, conv in enumerate(self.convs):
            residual = h
            h = self.layer_stack.norm_input(h, i)
            h = conv(h, edge_index)
            h = self.layer_stack.finish_layer(h, residual, i)
            if i < len(self.convs) - 1:
                h = F.relu(h)
                h = F.dropout(h, p=self.dropout, training=self.training)
        return h

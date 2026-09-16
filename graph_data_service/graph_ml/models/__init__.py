"""
模型工厂：按名称创建 PyG Backbone。
"""

from __future__ import annotations

from typing import Any, Dict, Type

from torch import nn

from .APPNP import APPNPBackbone
from .GAT import GATBackbone
from .GATV2 import GATV2Backbone
from .GCN import GCNBackbone
from .GEN import GENBackbone
from .GIN import GINBackbone
from .SAGE import SAGEBackbone
from .TRANSFORMER import TransformerBackbone
from .graph_classifier import GraphClassifier, build_graph_classifier
from .node_encoder import NodeCategoryEncoder

_BACKBONE_REGISTRY: Dict[str, Type[nn.Module]] = {
    "APPNP": APPNPBackbone,
    "GCN": GCNBackbone,
    "GAT": GATBackbone,
    "GATV2": GATV2Backbone,
    "SAGE": SAGEBackbone,
    "TRANSFORMER": TransformerBackbone,
    "GEN": GENBackbone,
    "GIN": GINBackbone,
}
SUPPORTED_BACKBONES = frozenset(_BACKBONE_REGISTRY)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


def parse_gnn_options(config: Dict[str, Any] | None) -> Dict[str, Any]:
    cfg = config or {}
    return {
        "use_residual_connection": _as_bool(cfg.get("use_residual_connection"), False),
        "use_normalization": _as_bool(cfg.get("use_normalization"), False),
        "norm_pos": str(cfg.get("norm_pos", "post") or "post"),
        "norm_type": str(cfg.get("norm_type", "layer_norm") or "layer_norm"),
        "appnp_k": int(cfg.get("appnp_k", 10)),
        "appnp_alpha": float(cfg.get("appnp_alpha", 0.1)),
    }


def build_backbone(
    model_name: str,
    in_channels: int,
    hidden_channels: int,
    out_channels: int,
    num_layers: int = 2,
    dropout: float = 0.2,
    gat_heads: int = 2,
    appnp_k: int = 10,
    appnp_alpha: float = 0.1,
    use_residual_connection: bool = False,
    use_normalization: bool = False,
    norm_pos: str = "post",
    norm_type: str = "layer_norm",
) -> nn.Module:
    name = (model_name or "").strip().upper()
    cls = _BACKBONE_REGISTRY.get(name)
    if cls is None:
        supported = "/".join(sorted(SUPPORTED_BACKBONES))
        raise ValueError(f"当前仅支持 {supported}，收到: {model_name}")

    if name in {"GAT", "GATV2", "TRANSFORMER"}:
        return cls(
            in_channels=in_channels,
            hidden_channels=hidden_channels,
            out_channels=out_channels,
            num_layers=num_layers,
            dropout=dropout,
            gat_heads=gat_heads,
            use_residual_connection=use_residual_connection,
            use_normalization=use_normalization,
            norm_pos=norm_pos,
            norm_type=norm_type,
        )
    if name == "APPNP":
        return cls(
            in_channels=in_channels,
            hidden_channels=hidden_channels,
            out_channels=out_channels,
            num_layers=num_layers,
            dropout=dropout,
            appnp_k=appnp_k,
            appnp_alpha=appnp_alpha,
            use_residual_connection=use_residual_connection,
            use_normalization=use_normalization,
            norm_pos=norm_pos,
            norm_type=norm_type,
        )
    return cls(
        in_channels=in_channels,
        hidden_channels=hidden_channels,
        out_channels=out_channels,
        num_layers=num_layers,
        dropout=dropout,
        use_residual_connection=use_residual_connection,
        use_normalization=use_normalization,
        norm_pos=norm_pos,
        norm_type=norm_type,
    )


__all__ = [
    "build_backbone",
    "build_graph_classifier",
    "parse_gnn_options",
    "GCNBackbone",
    "GINBackbone",
    "APPNPBackbone",
    "GATBackbone",
    "GATV2Backbone",
    "SAGEBackbone",
    "TransformerBackbone",
    "GENBackbone",
    "SUPPORTED_BACKBONES",
    "NodeCategoryEncoder",
    "GraphClassifier",
]

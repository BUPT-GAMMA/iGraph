"""Unified whole-graph classifier over the existing GNN backbones."""

from __future__ import annotations

from numbers import Integral, Real
from typing import Any, Mapping

import torch
from torch import nn
from torch_geometric.data import Batch
from torch_geometric.nn import global_mean_pool

from .node_encoder import NodeCategoryEncoder


class GraphClassifier(nn.Module):
    """Categorical node embedding -> GNN -> mean pooling -> graph logits."""

    pooling = "global_mean"
    uses_edge_features = False

    def __init__(
        self,
        backbone_name: str,
        node_vocab_size: int,
        num_tasks: int,
        *,
        node_embedding_dim: int = 64,
        hidden_dim: int = 64,
        embedding_dim: int = 64,
        num_layers: int = 2,
        dropout: float = 0.2,
        gat_heads: int = 2,
        appnp_k: int = 10,
        appnp_alpha: float = 0.1,
        use_residual_connection: bool = False,
        use_normalization: bool = False,
        norm_pos: str = "post",
        norm_type: str = "layer_norm",
    ) -> None:
        super().__init__()
        self.backbone_name = _normalize_backbone_name(backbone_name)
        self.node_vocab_size = _require_positive_int(
            node_vocab_size,
            "node_vocab_size",
        )
        self.num_tasks = _require_positive_int(num_tasks, "num_tasks")
        self.node_embedding_dim = _require_positive_int(
            node_embedding_dim,
            "node_embedding_dim",
        )
        self.hidden_dim = _require_positive_int(hidden_dim, "hidden_dim")
        self.embedding_dim = _require_positive_int(embedding_dim, "embedding_dim")
        self.num_layers = _require_positive_int(num_layers, "num_layers")
        if self.num_layers < 2:
            raise ValueError("num_layers 必须大于等于 2")
        self.dropout = _require_dropout(dropout)
        self.gat_heads = _require_positive_int(gat_heads, "gat_heads")
        self.appnp_k = _require_positive_int(appnp_k, "appnp_k")
        self.appnp_alpha = _require_appnp_alpha(appnp_alpha)
        if not isinstance(use_residual_connection, bool):
            raise ValueError("use_residual_connection 必须是布尔值")
        if not isinstance(use_normalization, bool):
            raise ValueError("use_normalization 必须是布尔值")
        self.use_residual_connection = use_residual_connection
        self.use_normalization = use_normalization
        self.norm_pos = str(norm_pos or "post").strip().lower()
        self.norm_type = str(norm_type or "layer_norm").strip().lower()

        # Local import avoids a package-initialization cycle with the existing
        # models factory while keeping one canonical backbone registry.
        from . import build_backbone

        self.node_encoder = NodeCategoryEncoder(
            self.node_vocab_size,
            self.node_embedding_dim,
            # Step 4 validates category bounds on CPU before device transfer.
            # Avoid a min/max GPU synchronization inside every model forward.
            validate_ids=False,
        )
        self.backbone = build_backbone(
            self.backbone_name,
            in_channels=self.node_embedding_dim,
            hidden_channels=self.hidden_dim,
            out_channels=self.embedding_dim,
            num_layers=self.num_layers,
            dropout=self.dropout,
            gat_heads=self.gat_heads,
            appnp_k=self.appnp_k,
            appnp_alpha=self.appnp_alpha,
            use_residual_connection=self.use_residual_connection,
            use_normalization=self.use_normalization,
            norm_pos=self.norm_pos,
            norm_type=self.norm_type,
        )
        self.head = nn.Linear(self.embedding_dim, self.num_tasks)

    def forward(self, batch: Batch) -> torch.Tensor:
        if not isinstance(batch, Batch):
            raise ValueError("GraphClassifier 输入必须是 PyG Batch")
        node_ids = getattr(batch, "x", None)
        edge_index = getattr(batch, "edge_index", None)
        batch_vector = getattr(batch, "batch", None)
        if not isinstance(node_ids, torch.Tensor):
            raise ValueError("GraphClassifier batch 缺少节点类别 x")
        if not isinstance(edge_index, torch.Tensor):
            raise ValueError("GraphClassifier batch 缺少 edge_index")
        if not isinstance(batch_vector, torch.Tensor):
            raise ValueError("GraphClassifier batch 缺少 batch 向量")
        node_features = self.node_encoder(node_ids)
        if edge_index.dtype != torch.long:
            raise ValueError("GraphClassifier edge_index 必须是 torch.long Tensor")
        if edge_index.ndim != 2 or edge_index.shape[0] != 2:
            raise ValueError("GraphClassifier edge_index 必须是 shape [2, E]")
        if batch_vector.dtype != torch.long or batch_vector.ndim != 1:
            raise ValueError("GraphClassifier batch 向量必须是一维 torch.long Tensor")
        if batch_vector.shape[0] != node_ids.shape[0]:
            raise ValueError("GraphClassifier batch 向量与节点数量不一致")
        if edge_index.device != node_ids.device or batch_vector.device != node_ids.device:
            raise ValueError("GraphClassifier 的 x、edge_index 和 batch 必须位于同一设备")
        try:
            num_graphs = int(batch.num_graphs)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("GraphClassifier 无法确定 batch 中的图数量") from exc
        if num_graphs < 1 or batch_vector.numel() == 0:
            raise ValueError("GraphClassifier 不接受空 batch")
        node_embeddings = self.backbone(node_features, edge_index)
        if tuple(node_embeddings.shape) != (
            int(node_ids.shape[0]),
            self.embedding_dim,
        ):
            raise ValueError(
                "GNN backbone 输出 shape 错误: "
                f"期望 [{int(node_ids.shape[0])}, {self.embedding_dim}]，"
                f"实际 {list(node_embeddings.shape)}"
            )
        graph_embeddings = global_mean_pool(
            node_embeddings,
            batch_vector,
            size=num_graphs,
        )
        logits = self.head(graph_embeddings)
        if tuple(logits.shape) != (num_graphs, self.num_tasks):
            raise ValueError(
                "图分类 logits shape 错误: "
                f"期望 [{num_graphs}, {self.num_tasks}]，实际 {list(logits.shape)}"
            )
        return logits

    def constructor_config(self) -> dict[str, Any]:
        """Return the exact JSON-safe arguments needed to rebuild the model."""
        return {
            "backbone_name": self.backbone_name,
            "node_vocab_size": self.node_vocab_size,
            "num_tasks": self.num_tasks,
            "node_embedding_dim": self.node_embedding_dim,
            "hidden_dim": self.hidden_dim,
            "embedding_dim": self.embedding_dim,
            "num_layers": self.num_layers,
            "dropout": self.dropout,
            "gat_heads": self.gat_heads,
            "appnp_k": self.appnp_k,
            "appnp_alpha": self.appnp_alpha,
            "use_residual_connection": self.use_residual_connection,
            "use_normalization": self.use_normalization,
            "norm_pos": self.norm_pos,
            "norm_type": self.norm_type,
        }

    def architecture_metadata(self) -> dict[str, Any]:
        return {
            **self.constructor_config(),
            "pooling": self.pooling,
            "uses_edge_features": self.uses_edge_features,
        }


def build_graph_classifier(
    backbone_name: str,
    *,
    node_vocab_size: int,
    num_tasks: int,
    algo_config: Mapping[str, Any] | None = None,
) -> GraphClassifier:
    """Build a graph classifier using the public GraphML algo-config names."""
    config = dict(algo_config or {})
    hidden_dim = config.get("hidden_dim", 64)
    embedding_dim = config.get("embedding_dim", hidden_dim)

    from . import parse_gnn_options

    return GraphClassifier(
        backbone_name=backbone_name,
        node_vocab_size=node_vocab_size,
        num_tasks=num_tasks,
        node_embedding_dim=config.get("node_embedding_dim", 64),
        hidden_dim=hidden_dim,
        embedding_dim=embedding_dim,
        num_layers=config.get("num_layers", 2),
        dropout=config.get("dropout", 0.2),
        gat_heads=config.get("gat_heads", 2),
        **parse_gnn_options(config),
    )


def _normalize_backbone_name(value: str) -> str:
    name = str(value or "").strip().upper()
    supported = {
        "APPNP",
        "GCN",
        "GAT",
        "GATV2",
        "GIN",
        "SAGE",
        "TRANSFORMER",
        "GEN",
    }
    if name not in supported:
        raise ValueError(
            "图分类 backbone 仅支持 "
            + "/".join(sorted(supported))
            + f"，收到: {value}"
        )
    return name


def _require_positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 1:
        raise ValueError(f"{name} 必须是正整数")
    return int(value)


def _require_dropout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("dropout 必须是数值")
    normalized = float(value)
    if normalized < 0.0 or normalized >= 1.0:
        raise ValueError("dropout 必须满足 0 <= dropout < 1")
    return normalized


def _require_appnp_alpha(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("appnp_alpha 必须是数值")
    normalized = float(value)
    if not 0.0 < normalized <= 1.0:
        raise ValueError("appnp_alpha 必须满足 0 < appnp_alpha <= 1")
    return normalized


__all__ = ["GraphClassifier", "build_graph_classifier"]

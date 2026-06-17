"""
GraphML architecture visualization helpers.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

TaskType = Literal["node_classification", "link_prediction", "pretrain"]


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


def model_architecture_view(
    *,
    task: TaskType,
    model: Optional[str],
    method: Optional[str],
    algo_config: Optional[Dict[str, Any]] = None,
    model_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = dict(algo_config or {})
    model_name = (model or "").strip().upper()
    if not model_name:
        model_name = str((meta or {}).get("model") or "GCN").upper()
    method_name = (method or "").strip().lower() or str((meta or {}).get("method") or "").strip().lower()

    if task == "pretrain" and method_name == "node2vec":
        emb_dim = int(cfg.get("embedding_dim", (meta or {}).get("embedding_dim", 128)))
        walk_length = int(cfg.get("walk_length", 20))
        context_size = int(cfg.get("context_size", 10))
        mermaid = (
            "flowchart TD\n"
            "  A[\"Graph Edges\"] --> B[\"Node2Vec Random Walk Sampler\"]\n"
            "  B --> C[\"Skip-Gram Objective\"]\n"
            f"  C --> D[\"Node Embedding (dim={emb_dim})\"]"
        )
        return {
            "success": True,
            "task": task,
            "model_id": model_id,
            "model": None,
            "method": "node2vec",
            "architecture": {
                "type": "node2vec",
                "embedding_dim": emb_dim,
                "walk_length": walk_length,
                "context_size": context_size,
            },
            "layers": [
                {"name": "node2vec_sampler", "walk_length": walk_length, "context_size": context_size},
                {"name": "embedding_table", "embedding_dim": emb_dim},
            ],
            "mermaid": mermaid,
        }

    meta_cfg = (meta or {}).get("model_config", {}) or {}
    hidden = int(cfg.get("hidden_dim", meta_cfg.get("hidden_dim", 64)))
    emb_dim = int(cfg.get("embedding_dim", meta_cfg.get("embedding_dim", hidden)))
    layers = int(cfg.get("num_layers", meta_cfg.get("num_layers", 2)))
    dropout = float(cfg.get("dropout", meta_cfg.get("dropout", 0.2)))
    heads = int(cfg.get("gat_heads", meta_cfg.get("gat_heads", 2)))
    use_residual = _as_bool(
        cfg.get("use_residual_connection", meta_cfg.get("use_residual_connection")),
        False,
    )
    use_norm = _as_bool(cfg.get("use_normalization", meta_cfg.get("use_normalization")), False)
    norm_pos = str(cfg.get("norm_pos", meta_cfg.get("norm_pos", "post")) or "post")
    norm_type = str(cfg.get("norm_type", meta_cfg.get("norm_type", "layer_norm")) or "layer_norm")
    head_desc = "Linear Classifier Head" if task == "node_classification" else "DotProduct Decoder"

    conv_label = model_name
    if model_name == "GAT":
        conv_label = f"GAT (heads={heads})"
    layer_annotations = []
    if use_residual:
        layer_annotations.append("residual")
    if use_norm:
        layer_annotations.append(f"{norm_type}@{norm_pos}")
    if layer_annotations:
        conv_label = f"{conv_label}, {' + '.join(layer_annotations)}"
    mermaid = (
        "flowchart TD\n"
        "  I[\"Node Features\"] --> C1\n"
        f"  C1[\"{conv_label} x {layers}\"] --> E[\"Node Embedding (dim={emb_dim})\"]\n"
        f"  E --> O[\"{head_desc}\"]"
    )
    if task == "node_classification":
        num_classes = len((meta or {}).get("label_values", []))
        if num_classes > 0:
            mermaid += f"\n  O --> P[\"Class Probabilities (classes={num_classes})\"]"
        else:
            mermaid += "\n  O --> P[\"Class Probabilities\"]"

    return {
        "success": True,
        "task": task,
        "model_id": model_id,
        "model": model_name,
        "method": method_name or None,
        "architecture": {
            "type": "gnn_backbone",
            "model": model_name,
            "hidden_dim": hidden,
            "embedding_dim": emb_dim,
            "num_layers": layers,
            "dropout": dropout,
            "gat_heads": heads,
            "use_residual_connection": use_residual,
            "use_normalization": use_norm,
            "norm_pos": norm_pos,
            "norm_type": norm_type,
        },
        "layers": [
            {
                "name": "gnn_backbone",
                "model": model_name,
                "num_layers": layers,
                "hidden_dim": hidden,
                "use_residual_connection": use_residual,
                "use_normalization": use_norm,
                "norm_pos": norm_pos,
                "norm_type": norm_type,
            },
            {"name": "embedding_output", "embedding_dim": emb_dim},
            {"name": "task_head", "head": head_desc},
        ],
        "mermaid": mermaid,
    }

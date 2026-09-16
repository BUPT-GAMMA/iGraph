from __future__ import annotations

import torch
from torch_geometric.data import Batch, Data

from graph_data_service.graph_ml.config_catalog import model_architecture_view
from graph_data_service.graph_ml.models import SUPPORTED_BACKBONES, GraphClassifier, build_backbone
from graph_data_service.graph_ml.tasks import GRAPH_CLASSIFICATION_MODELS


MODERN_BACKBONES = {"APPNP", "GATV2", "GIN", "TRANSFORMER", "GEN"}


def _edge_index() -> torch.Tensor:
    return torch.tensor(
        [[0, 1, 1, 2, 2, 3, 3, 0], [1, 0, 2, 1, 3, 2, 0, 3]],
        dtype=torch.long,
    )


def test_modern_backbones_are_registered_and_preserve_output_shape() -> None:
    assert MODERN_BACKBONES <= SUPPORTED_BACKBONES
    assert MODERN_BACKBONES <= GRAPH_CLASSIFICATION_MODELS
    features = torch.randn(4, 6)

    for name in sorted(MODERN_BACKBONES):
        model = build_backbone(
            name,
            in_channels=6,
            hidden_channels=8,
            out_channels=5,
            num_layers=3,
            dropout=0.0,
            gat_heads=2,
            use_residual_connection=True,
            use_normalization=True,
        )
        model.eval()
        output = model(features, _edge_index())
        assert tuple(output.shape) == (4, 5)
        assert torch.isfinite(output).all()


def test_modern_backbones_work_in_graph_classifier_and_architecture_view() -> None:
    batch = Batch.from_data_list(
        [
            Data(x=torch.tensor([0, 1, 2]), edge_index=_edge_index()[:, :4]),
            Data(x=torch.tensor([1, 2, 3, 0]), edge_index=_edge_index()),
        ]
    )

    for name in sorted(MODERN_BACKBONES):
        classifier = GraphClassifier(
            name,
            node_vocab_size=4,
            num_tasks=3,
            node_embedding_dim=6,
            hidden_dim=8,
            embedding_dim=5,
            num_layers=2,
            dropout=0.0,
            gat_heads=2,
        )
        classifier.eval()
        assert tuple(classifier(batch).shape) == (2, 3)

        view = model_architecture_view(
            task="graph_classification",
            model=name,
            method=None,
            algo_config={"gat_heads": 2},
            meta={"num_tasks": 3},
        )
        assert view["success"] is True
        assert view["architecture"]["model"] == name
        if name in {"GATV2", "TRANSFORMER"}:
            assert "heads=2" in view["mermaid"]


def test_appnp_specific_options_are_preserved() -> None:
    classifier = GraphClassifier(
        "APPNP",
        node_vocab_size=4,
        num_tasks=2,
        appnp_k=6,
        appnp_alpha=0.2,
    )
    assert classifier.backbone.appnp_k == 6
    assert classifier.backbone.appnp_alpha == 0.2
    assert classifier.constructor_config()["appnp_k"] == 6
    assert classifier.constructor_config()["appnp_alpha"] == 0.2

    view = model_architecture_view(
        task="node_classification",
        model="APPNP",
        method=None,
        algo_config={"appnp_k": 6, "appnp_alpha": 0.2},
    )
    assert "K=6" in view["mermaid"]
    assert "alpha=0.2" in view["mermaid"]

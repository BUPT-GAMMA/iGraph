"""Small HIV/PCBA-style payloads; binary fixtures are generated only in temp dirs."""

from __future__ import annotations

import torch
from torch_geometric.data import Data


def make_graph_classification_payload(
    profile: str = "hiv",
    *,
    num_graphs: int = 6,
    include_edgeless: bool = True,
) -> dict:
    if profile not in {"hiv", "pcba"}:
        raise ValueError(profile)
    if num_graphs < 3:
        raise ValueError("num_graphs must be at least 3")

    num_tasks = 1 if profile == "hiv" else 128
    graphs = []
    for graph_index in range(num_graphs):
        num_nodes = 2 + graph_index % 3
        x = torch.tensor(
            [(graph_index + node_index) % 4 for node_index in range(num_nodes)],
            dtype=torch.long,
        )
        if include_edgeless and graph_index == 0:
            edge_index = torch.empty((2, 0), dtype=torch.long)
            xe = torch.empty((0,), dtype=torch.long)
        else:
            source = torch.arange(0, num_nodes - 1, dtype=torch.long)
            target = torch.arange(1, num_nodes, dtype=torch.long)
            edge_index = torch.stack([source, target], dim=0)
            xe = torch.tensor(
                [edge_index_position % 3 for edge_index_position in range(source.numel())],
                dtype=torch.long,
            )

        if profile == "hiv":
            y = torch.tensor([graph_index % 2], dtype=torch.long)
        else:
            y = torch.zeros(num_tasks, dtype=torch.float32)
            y[graph_index % num_tasks] = 1.0
            if graph_index == 0:
                y[1] = torch.nan
        graphs.append(Data(x=x, xe=xe, edge_index=edge_index, y=y))

    train_end = num_graphs - 2
    benchmark_split = {
        "train": list(range(train_end)),
        "valid": [num_graphs - 2],
        "test": [num_graphs - 1],
    }
    texts = [
        [f"node category {index}" for index in range(4)],
        [f"edge category {index}" for index in range(3)],
        [f"label text {index}" for index in range(2 if profile == "hiv" else 256)],
        ["prompt edge"],
        ["graph classification prompt"],
    ]
    return {
        "graphs": graphs,
        "texts": texts,
        "split": [benchmark_split, {"e2e_graph": {}, "lr_graph": {}}],
    }

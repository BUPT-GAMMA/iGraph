"""Immutable, validated worker inputs and tensor-only dataset results."""
from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from collections.abc import Sequence


class LazyGraphs(Sequence):
    """Read one small canonical graph at a time; never unpickle user objects."""
    def __init__(self, directory, count):
        self.directory, self.count = Path(directory), count

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self.count))]
        if index < 0:
            index += self.count
        if index < 0 or index >= self.count:
            raise IndexError(index)
        import torch
        from torch_geometric.data import Data
        payload = torch.load(self.directory / f'{index}.pt', map_location='cpu', weights_only=True)
        return Data(**payload)


def write_bundle(bundle, directory, check):
    import torch
    directory = Path(directory)
    directory.mkdir()
    manifest = {field.name: getattr(bundle, field.name) for field in fields(bundle) if field.name != 'graphs'}
    manifest['graph_count'] = len(bundle.graphs)
    for index, graph in enumerate(bundle.graphs):
        check()
        torch.save(graph.to_dict(), directory / f'{index}.pt')
    (directory / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')


def read_bundle(directory):
    from graph_data_service.graph_ml.datasets.graph_classification import GraphClassificationDatasetBundle
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    count = manifest.pop('graph_count')
    return GraphClassificationDatasetBundle(**manifest, graphs=LazyGraphs(directory, count))

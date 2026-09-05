"""Validate complete model outputs inside the cancellable worker."""
import json
import os
from pathlib import Path

from graph_data_service.worker_signals import check_cancelled


def validate_model(directory, task):
    import numpy as np
    import torch
    directory = Path(directory)
    check_cancelled()
    meta = json.loads((directory / 'meta.json').read_text())
    metrics = json.loads((directory / 'metrics.json').read_text())
    if meta.get('task') != task or not isinstance(metrics, dict):
        raise ValueError('INVALID_WORKER_ARTIFACT_METADATA')
    if task == 'graph_classification':
        from graph_data_service.graph_ml.artifact_store import ArtifactStore
        ArtifactStore(str(directory.parent)).load_graph_classification_checkpoint(str(directory))
    else:
        checkpoint = torch.load(directory / 'checkpoint.pt', map_location='cpu', weights_only=True)
        required = {'backbone', 'head'} if task == 'node_classification' else {'backbone'}
        if not isinstance(checkpoint, dict) or set(checkpoint) != required:
            raise ValueError('INVALID_WORKER_CHECKPOINT')
        for state in checkpoint.values():
            if not isinstance(state, dict) or not state or any(not isinstance(value, torch.Tensor) for value in state.values()):
                raise ValueError('INVALID_WORKER_CHECKPOINT')
        names = json.loads((directory / 'node_names.json').read_text())
        if not isinstance(names, list):
            raise ValueError('INVALID_WORKER_NODE_NAMES')
    for path in directory.glob('*.npy'):
        check_cancelled()
        value = np.load(path, allow_pickle=False, mmap_mode='r')
        if value.ndim == 0:
            raise ValueError('INVALID_WORKER_ARRAY')
    for path in directory.iterdir():
        check_cancelled()
        if path.is_file():
            with path.open('rb') as handle:
                os.fsync(handle.fileno())
    check_cancelled()

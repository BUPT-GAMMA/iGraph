"""Immutable, validated NPZ reference with cheap session metadata.

Controlled computations read the NPZ in their worker. Legacy callers can still
materialize GraphData on demand, retaining its existing public data protocol.
"""
from pathlib import Path
import shutil
from graph_data_service.graph_data import GraphData

SUMMARY_FIELDS = ('num_nodes', 'num_edges', 'num_edge_entries', 'num_unique_edges',
    'num_unique_directed_edges', 'num_unique_undirected_edges', 'semantic_type',
    'storage_type', 'is_symmetric', 'has_self_loops', 'has_duplicate_entries',
    'directed', 'has_node_names', 'has_edge_weight', 'has_node_text',
    'has_split_masks', 'graph_name', 'graph_desc')


def snapshot_summary(graph):
    return {name: getattr(graph, name, '' if name in {'graph_name', 'graph_desc'} else None) for name in SUMMARY_FIELDS}


class GraphSnapshot:
    def __init__(self, path, summary):
        self._path, self._summary, self._materialized = Path(path), dict(summary), None

    def __getattr__(self, name):
        if name in self._summary and self._materialized is None:
            return self._summary[name]
        if self._materialized is None:
            self._materialized = GraphData.load(str(self._path))
        return getattr(self._materialized, name)

    def save(self, path):
        if self._materialized is not None:
            return self._materialized.save(str(path))
        if Path(path).resolve() != self._path.resolve():
            shutil.copyfile(self._path, path)

    def ensure_node_names(self):
        if self._materialized is not None:
            self._materialized.ensure_node_names()

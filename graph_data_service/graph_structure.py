"""Graph structure inspection and semantic-aware degree helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Sequence

import numpy as np

try:
    import torch
except Exception:  # pragma: no cover - optional dependency.
    torch = None  # type: ignore[assignment]


SemanticType = Literal["directed", "undirected", "unknown"]
StorageType = Literal["directed", "bidirectional", "mixed"]

PREDEFINED_DATASET_SEMANTICS: dict[str, SemanticType] = {
    "cora": "undirected",
    "citeseer": "undirected",
    "pubmed": "undirected",
}


@dataclass(frozen=True)
class GraphStructureInfo:
    semantic_type: SemanticType
    storage_type: StorageType
    is_symmetric: bool
    num_nodes: int
    num_edge_entries: int
    num_unique_edges: int
    num_unique_directed_edges: int
    num_unique_undirected_edges: int
    has_self_loops: bool
    has_duplicate_entries: bool
    self_loop_entry_count: int = 0
    duplicate_entry_count: int = 0
    reverse_edge_missing_count: int = 0
    semantic_source: str = "unknown"
    storage_source: str = "inspection"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_semantic_type(
    value: Any, *, default: SemanticType = "unknown"
) -> SemanticType:
    """Normalize a semantic type value."""
    if value is None:
        return default
    if isinstance(value, bool):
        return "directed" if value else "undirected"
    text = str(value).strip().lower().replace("-", "_")
    if text in {"directed", "digraph", "true", "1"}:
        return "directed"
    if text in {"undirected", "graph", "false", "0"}:
        return "undirected"
    if text in {"unknown", "unspecified", "none", ""}:
        return "unknown"
    return default


def normalize_storage_type(
    value: Any, *, default: StorageType | None = None
) -> StorageType | None:
    """Normalize a storage type value."""
    if value is None:
        return default
    text = str(value).strip().lower().replace("-", "_")
    if text in {"directed", "single_direction", "single_directional"}:
        return "directed"
    if text in {"bidirectional", "bi_directional", "undirected"}:
        return "bidirectional"
    if text in {"mixed", "partial", "partially_bidirectional"}:
        return "mixed"
    return default


def semantic_type_from_directed(directed: bool | None) -> SemanticType:
    if directed is None:
        return "unknown"
    return "directed" if bool(directed) else "undirected"


def resolve_semantic_type(
    *,
    explicit_semantic_type: Any = None,
    directed: bool | None = None,
    metadata: dict[str, Any] | None = None,
    dataset_name: str | None = None,
    structure_is_symmetric: bool | None = None,
) -> tuple[SemanticType, str]:
    """Resolve graph semantics using explicit metadata before structure."""
    semantic = normalize_semantic_type(explicit_semantic_type)
    if semantic != "unknown":
        return semantic, "explicit"

    meta = metadata or {}
    for key in ("semantic_type", "graph_semantic_type", "graph_type"):
        semantic = normalize_semantic_type(meta.get(key))
        if semantic != "unknown":
            return semantic, f"metadata.{key}"

    for key in ("is_directed", "directed"):
        if isinstance(meta.get(key), bool):
            return semantic_type_from_directed(bool(meta[key])), f"metadata.{key}"

    if dataset_name:
        name_key = str(dataset_name).strip().lower()
        if name_key in PREDEFINED_DATASET_SEMANTICS:
            return PREDEFINED_DATASET_SEMANTICS[name_key], "predefined_dataset"

    if directed is False:
        return "undirected", "directed_flag_false"

    if structure_is_symmetric is False:
        return "directed", "structure_asymmetric"
    if structure_is_symmetric is True:
        return "unknown", "structure_symmetric_ambiguous"
    return "unknown", "structure_insufficient"


def count_edge_entries(edge_index: Any) -> int:
    """Return the raw edge record count."""
    return int(_edge_index_to_numpy(edge_index).shape[1])


def count_unique_directed_edges(
    edge_index: Any,
    edge_type: Sequence[Any] | np.ndarray | None = None,
) -> int:
    """Count unique directed edge records after removing exact duplicates."""
    return len(
        {
            _directed_key(src, dst, rel)
            for src, dst, rel in _edge_records(edge_index, edge_type)
        }
    )


def count_unique_undirected_edges(
    edge_index: Any,
    edge_type: Sequence[Any] | np.ndarray | None = None,
) -> int:
    """Count unique undirected node pairs after normalizing each pair."""
    return len(
        {
            _undirected_key(src, dst, rel)
            for src, dst, rel in _edge_records(edge_index, edge_type)
        }
    )


def inspect_graph_structure(
    edge_index: Any,
    *,
    num_nodes: int,
    semantic_type: SemanticType | str = "unknown",
    storage_type: StorageType | str | None = None,
    edge_type: Sequence[Any] | np.ndarray | None = None,
    semantic_source: str = "unknown",
) -> GraphStructureInfo:
    """Inspect edge storage and compute semantic-aware counts."""
    records = _edge_records(edge_index, edge_type)
    directed_keys = [_directed_key(src, dst, rel) for src, dst, rel in records]
    unique_directed = set(directed_keys)
    unique_undirected = {_undirected_key(src, dst, rel) for src, dst, rel in records}
    non_self = {key for key in unique_directed if key[0] != key[1]}
    paired = {
        key
        for key in non_self
        if _directed_key(key[1], key[0], key[2]) in unique_directed
    }
    missing_reverse_count = len(non_self) - len(paired)
    is_symmetric = missing_reverse_count == 0

    resolved_semantic = normalize_semantic_type(semantic_type)
    resolved_storage = normalize_storage_type(storage_type)
    if resolved_storage is None:
        resolved_storage = _storage_type_from_pairs(
            semantic_type=resolved_semantic,
            non_self_count=len(non_self),
            paired_count=len(paired),
        )

    num_unique_directed = len(unique_directed)
    num_unique_undirected = len(unique_undirected)
    num_unique_edges = (
        num_unique_undirected
        if resolved_semantic == "undirected"
        else num_unique_directed
    )
    self_loop_entry_count = sum(1 for src, dst, _rel in records if src == dst)
    duplicate_entry_count = max(0, len(records) - num_unique_directed)

    return GraphStructureInfo(
        semantic_type=resolved_semantic,
        storage_type=resolved_storage,
        is_symmetric=is_symmetric,
        num_nodes=int(num_nodes),
        num_edge_entries=len(records),
        num_unique_edges=num_unique_edges,
        num_unique_directed_edges=num_unique_directed,
        num_unique_undirected_edges=num_unique_undirected,
        has_self_loops=self_loop_entry_count > 0,
        has_duplicate_entries=duplicate_entry_count > 0,
        self_loop_entry_count=self_loop_entry_count,
        duplicate_entry_count=duplicate_entry_count,
        reverse_edge_missing_count=missing_reverse_count,
        semantic_source=semantic_source,
        storage_source="provided" if storage_type is not None else "inspection",
    )


def compute_node_degrees(
    edge_index: Any,
    num_nodes: int,
    semantic_type: SemanticType | str,
    *,
    edge_type: Sequence[Any] | np.ndarray | None = None,
    count_unique_neighbors: bool = True,
) -> dict[str, Any]:
    """Compute semantic-aware node degrees.

    For directed graphs this returns ``in_degree``, ``out_degree``, ``degree``
    and ``total_degree``. For undirected graphs, ``degree`` is the logical
    undirected degree where a self-loop contributes 2. ``num_neighbors`` counts
    unique adjacent nodes.
    """
    semantic = normalize_semantic_type(semantic_type)
    records = _edge_records(edge_index, edge_type)
    in_degree = np.zeros(int(num_nodes), dtype=np.int64)
    out_degree = np.zeros(int(num_nodes), dtype=np.int64)
    degree = np.zeros(int(num_nodes), dtype=np.int64)
    neighbor_sets: list[set[int]] = [set() for _ in range(int(num_nodes))]

    directed_seen: set[tuple[int, int, Any]] = set()
    for src, dst, rel in records:
        key = _directed_key(src, dst, rel)
        if key in directed_seen:
            continue
        directed_seen.add(key)
        if 0 <= src < num_nodes:
            out_degree[src] += 1
            neighbor_sets[src].add(dst)
        if 0 <= dst < num_nodes:
            in_degree[dst] += 1
            neighbor_sets[dst].add(src)

    if semantic == "undirected":
        undirected_seen: set[tuple[int, int, Any]] = set()
        degree = np.zeros(int(num_nodes), dtype=np.int64)
        neighbor_sets = [set() for _ in range(int(num_nodes))]
        for src, dst, rel in records:
            key = _undirected_key(src, dst, rel)
            if key in undirected_seen:
                continue
            undirected_seen.add(key)
            u, v, _ = key
            if u == v:
                if 0 <= u < num_nodes:
                    degree[u] += 2
                    neighbor_sets[u].add(u)
                continue
            if 0 <= u < num_nodes:
                degree[u] += 1
                neighbor_sets[u].add(v)
            if 0 <= v < num_nodes:
                degree[v] += 1
                neighbor_sets[v].add(u)
        total_degree = degree.copy()
    else:
        total_degree = in_degree + out_degree
        degree = total_degree.copy()

    num_neighbors = np.asarray(
        [len(values) for values in neighbor_sets], dtype=np.int64
    )
    return _degree_output(
        edge_index,
        {
            "semantic_type": semantic,
            "degree": degree,
            "num_neighbors": num_neighbors if count_unique_neighbors else degree.copy(),
            "in_degree": in_degree,
            "out_degree": out_degree,
            "total_degree": total_degree,
        },
    )


def logical_edge_pairs(
    edge_index: Any,
    semantic_type: SemanticType | str,
    *,
    edge_type: Sequence[Any] | np.ndarray | None = None,
) -> list[tuple[int, int]]:
    """Return deduplicated logical edge pairs for statistics and sampling."""
    semantic = normalize_semantic_type(semantic_type)
    if semantic == "undirected":
        seen_undir: set[tuple[int, int, Any]] = set()
        out: list[tuple[int, int]] = []
        for src, dst, rel in _edge_records(edge_index, edge_type):
            key = _undirected_key(src, dst, rel)
            if key in seen_undir:
                continue
            seen_undir.add(key)
            out.append((key[0], key[1]))
        return out

    seen_dir: set[tuple[int, int, Any]] = set()
    out = []
    for src, dst, rel in _edge_records(edge_index, edge_type):
        key = _directed_key(src, dst, rel)
        if key in seen_dir:
            continue
        seen_dir.add(key)
        out.append((src, dst))
    return out


def structure_metadata_dict(info: GraphStructureInfo) -> dict[str, Any]:
    """Return a JSON-safe metadata dictionary."""
    return info.to_dict()


def _edge_records(
    edge_index: Any,
    edge_type: Sequence[Any] | np.ndarray | None = None,
) -> list[tuple[int, int, Any]]:
    arr = _edge_index_to_numpy(edge_index)
    edge_count = int(arr.shape[1])
    relations = _relation_values(edge_type, edge_count)
    return [
        (int(arr[0, idx]), int(arr[1, idx]), relations[idx])
        for idx in range(edge_count)
    ]


def _edge_index_to_numpy(edge_index: Any) -> np.ndarray:
    if torch is not None and isinstance(edge_index, torch.Tensor):
        arr = edge_index.detach().cpu().to(dtype=torch.long).numpy()
    else:
        arr = np.asarray(edge_index, dtype=np.int64)
    if arr.ndim != 2 or arr.shape[0] != 2:
        raise ValueError("edge_index must have shape [2, E]")
    return np.ascontiguousarray(arr, dtype=np.int64)


def _relation_values(
    edge_type: Sequence[Any] | np.ndarray | None, edge_count: int
) -> list[Any]:
    if edge_type is None:
        return [None] * edge_count
    if torch is not None and isinstance(edge_type, torch.Tensor):
        values = edge_type.detach().cpu().reshape(-1).tolist()
    else:
        values = np.asarray(edge_type, dtype=object).reshape(-1).tolist()
    if len(values) != edge_count:
        return [None] * edge_count
    return [_hashable(value) for value in values]


def _hashable(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (list, tuple, np.ndarray)):
        return tuple(np.asarray(value, dtype=object).reshape(-1).tolist())
    return value


def _directed_key(src: int, dst: int, rel: Any) -> tuple[int, int, Any]:
    return int(src), int(dst), rel


def _undirected_key(src: int, dst: int, rel: Any) -> tuple[int, int, Any]:
    u = int(src)
    v = int(dst)
    if v < u:
        u, v = v, u
    return u, v, rel


def _storage_type_from_pairs(
    *,
    semantic_type: SemanticType,
    non_self_count: int,
    paired_count: int,
) -> StorageType:
    if non_self_count == 0:
        return "bidirectional" if semantic_type == "undirected" else "directed"
    if paired_count == non_self_count:
        return "bidirectional"
    if paired_count == 0:
        return "directed"
    return "mixed"


def _degree_output(source_edge_index: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if torch is None or not isinstance(source_edge_index, torch.Tensor):
        return payload

    device = source_edge_index.device
    converted: dict[str, Any] = {"semantic_type": payload["semantic_type"]}
    for field, value in payload.items():
        if field == "semantic_type":
            continue
        converted[field] = torch.as_tensor(value, dtype=torch.long, device=device)
    return converted


__all__ = [
    "GraphStructureInfo",
    "PREDEFINED_DATASET_SEMANTICS",
    "SemanticType",
    "StorageType",
    "compute_node_degrees",
    "count_edge_entries",
    "count_unique_directed_edges",
    "count_unique_undirected_edges",
    "inspect_graph_structure",
    "logical_edge_pairs",
    "normalize_semantic_type",
    "normalize_storage_type",
    "resolve_semantic_type",
    "semantic_type_from_directed",
    "structure_metadata_dict",
]

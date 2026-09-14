"""Per-question materialization for masked graph-ML benchmark datasets.

The ML and prediction corpora do not contain standalone graph datasets.  Each
category-local checkpoint is an integer-indexed mapping whose values contain
the labels and edge index for one question.  This module combines the selected
value with the trusted base PyG dataset in an attempt-private ``.pt`` file.

Masked checkpoints can be hundreds of gigabytes.  They are therefore loaded
with ``mmap=True`` and ``weights_only=True`` and cached once per process/path.
There is deliberately no eager-loading fallback: an incompatible PyTorch
version or legacy checkpoint produces an actionable error instead of silently
allocating the whole checkpoint for every question or attempt.
"""

from __future__ import annotations

import inspect
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from .tool_names import expected_tool_path


_MASKED_FILENAMES = {
    "ml": "pure_ml_bench_masked_data.pt",
    "prediction": "intermediate_ground_truth_masked_data.pt",
}
_SAFE_DATASET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_GRAPH_REFERENCE = re.compile(r"\bgraph\s*#?\s*(\d+)\b", re.IGNORECASE)
_ENTRY_KEYS = frozenset(("labels", "edge_index"))
_TASK_KIND_TO_FIELD = {
    "NC": "y",
    "LP": "edge_index",
    "GC": "y",
}
_DIRECT_TASK_KINDS = {
    "NC": "NC",
    "NODE_CLASSIFICATION": "NC",
    "LP": "LP",
    "LINK_PREDICTION": "LP",
    "GC": "GC",
    "GRAPH_CLASSIFICATION": "GC",
}
_TOOL_PREFIX_TO_KIND = {
    "structural_node_embedding_classification_": "NC",
    "structural_node_embedding_link_prediction_": "LP",
    "structural_graph_embedding_classification_": "GC",
}
_GROUND_TRUTH_KEY_TO_KIND = {
    "structural_node_embedding_classification": "NC",
    "structural_node_embedding_link_prediction": "LP",
    "structural_graph_embedding_classification": "GC",
}


class MaskedDatasetError(ValueError):
    """Raised when masked graph data cannot be selected or materialized."""


@dataclass(frozen=True, slots=True)
class DatasetBinding:
    """Resolved source paths for one benchmark dataset.

    ``base_path`` is always the trusted graph checkpoint. ``masked_path`` is
    set only for category-local ML/prediction inputs.  Paths are normalized but
    are not loaded by construction, so bindings are safe to create in
    preflight code.
    """

    base_path: Path
    masked_path: Path | None = None
    category: str | None = None
    dataset: str | None = None

    def __post_init__(self) -> None:
        base_path = Path(self.base_path).expanduser().resolve()
        masked_path = (
            None
            if self.masked_path is None
            else Path(self.masked_path).expanduser().resolve()
        )
        category = None if self.category is None else str(self.category).strip()
        dataset = None if self.dataset is None else str(self.dataset).strip()
        if category == "":
            raise MaskedDatasetError("dataset category cannot be empty")
        if dataset == "":
            raise MaskedDatasetError("dataset name cannot be empty")
        if dataset is not None and (
            not _SAFE_DATASET.fullmatch(dataset) or dataset in {".", ".."}
        ):
            raise MaskedDatasetError(f"unsafe dataset name: {dataset!r}")
        if masked_path is not None and masked_path == base_path:
            raise MaskedDatasetError("base and masked dataset paths must differ")
        object.__setattr__(self, "base_path", base_path)
        object.__setattr__(self, "masked_path", masked_path)
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "dataset", dataset)

    @property
    def requires_materialization(self) -> bool:
        return self.masked_path is not None

    def validate_files(self) -> None:
        if not self.base_path.is_file():
            raise FileNotFoundError(f"base dataset file not found: {self.base_path}")
        if self.masked_path is not None and not self.masked_path.is_file():
            raise FileNotFoundError(
                f"masked dataset file not found: {self.masked_path}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_path": str(self.base_path),
            "masked_path": (
                None if self.masked_path is None else str(self.masked_path)
            ),
            "category": self.category,
            "dataset": self.dataset,
            "requires_materialization": self.requires_materialization,
        }


@dataclass(frozen=True, slots=True)
class MaterializedDataset:
    """Provenance for one attempt-private materialized checkpoint."""

    path: Path
    base_path: Path
    masked_path: Path
    masked_index: int
    task_kind: str
    applied_field: str
    masked_cache_hit: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "base_path": str(self.base_path),
            "masked_path": str(self.masked_path),
            "masked_index": self.masked_index,
            "task_kind": self.task_kind,
            "applied_field": self.applied_field,
            "masked_cache_hit": self.masked_cache_hit,
        }


@dataclass(frozen=True, slots=True)
class _CachedMaskedDataset:
    payload: Mapping[int, Any]
    stat_signature: tuple[int, int, int, int]


_MASKED_CACHE: dict[Path, _CachedMaskedDataset] = {}
_MASKED_CACHE_LOCK = threading.RLock()


def _task_mapping(task: Any) -> dict[str, Any]:
    if isinstance(task, Mapping):
        return dict(task)
    if hasattr(task, "to_dict") and callable(task.to_dict):
        value = task.to_dict()
    elif is_dataclass(task):
        value = asdict(task)
    else:
        raise TypeError("task must be a mapping or dataclass")
    if not isinstance(value, Mapping):
        raise TypeError("task serialization must return a mapping")
    return dict(value)


def resolve_dataset_binding(
    task: Any,
    *,
    routed_path: str | Path,
    dataset_root: str | Path,
) -> DatasetBinding:
    """Turn the runner's routed path into base/masked source bindings.

    For ML and prediction questions, ``routed_path`` must be the respective
    category-local masked checkpoint and the base graph is resolved below
    ``dataset_root``.  Other categories bind directly to ``routed_path``.
    Dataset spelling (notably ``NBA``) is preserved exactly.
    """

    value = _task_mapping(task)
    category = str(value.get("category") or "").strip()
    dataset = str(value.get("dataset") or "").strip()
    if not category:
        raise MaskedDatasetError("task has no category")
    if not dataset:
        raise MaskedDatasetError("task has no dataset")
    if not _SAFE_DATASET.fullmatch(dataset) or dataset in {".", ".."}:
        raise MaskedDatasetError(f"unsafe dataset name: {dataset!r}")

    routed = Path(routed_path).expanduser().resolve()
    masked_filename = _MASKED_FILENAMES.get(category)
    if masked_filename is None:
        binding = DatasetBinding(
            base_path=routed,
            category=category,
            dataset=dataset,
        )
        binding.validate_files()
        return binding

    if routed.name != masked_filename:
        raise MaskedDatasetError(
            f"{category} tasks require masked file {masked_filename!r}, got "
            f"{routed.name!r}"
        )
    root = Path(dataset_root).expanduser().resolve()
    base_path = (root / f"{dataset}.pt").resolve()
    if not base_path.is_relative_to(root):
        raise MaskedDatasetError(f"base dataset path escapes dataset root: {base_path}")
    binding = DatasetBinding(
        base_path=base_path,
        masked_path=routed,
        category=category,
        dataset=dataset,
    )
    binding.validate_files()
    return binding


def _strict_nonnegative_index(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MaskedDatasetError(f"{field} must be a non-negative integer")
    return value


def resolve_masked_index(task: Any) -> int:
    """Resolve a stable masked-entry index from source provenance.

    Selection order is never used.  Canonically loaded questions retain their
    original ``source_index`` across filtering and shuffling; legacy ML tasks
    may instead supply ``context.task_index``.  If both are present they must
    agree.
    """

    value = _task_mapping(task)
    source_raw = value.get("source_index")
    context = value.get("context", {})
    if context is None:
        context = {}
    if not isinstance(context, Mapping):
        raise MaskedDatasetError("task context must be a mapping")
    context_raw = context.get("task_index")

    source_index = (
        None
        if source_raw is None
        else _strict_nonnegative_index(source_raw, field="source_index")
    )
    context_index = (
        None
        if context_raw is None
        else _strict_nonnegative_index(context_raw, field="context.task_index")
    )
    if source_index is None and context_index is None:
        raise MaskedDatasetError(
            "masked dataset lookup requires stable source_index or "
            "context.task_index provenance"
        )
    if (
        source_index is not None
        and context_index is not None
        and source_index != context_index
    ):
        raise MaskedDatasetError(
            "source_index and context.task_index disagree: "
            f"{source_index} != {context_index}"
        )
    return source_index if source_index is not None else int(context_index)


def infer_ml_task_kind(task: Any) -> str:
    """Infer ``NC``, ``LP``, or ``GC`` without trusting selection position.

    Prediction records commonly use ``task_type='prediction'``.  Their subtype
    is inferred from the canonical required-tool path.  A uniquely named
    structural ground-truth member is accepted as a final compatibility hint,
    but conflicting hints are rejected.
    """

    value = _task_mapping(task)
    hints: set[str] = set()
    task_type = str(value.get("task_type") or "").strip().upper()
    direct_kind = _DIRECT_TASK_KINDS.get(task_type)
    if direct_kind is not None:
        hints.add(direct_kind)

    try:
        canonical_path = expected_tool_path(value, strict=True)
    except ValueError as exc:
        raise MaskedDatasetError(f"invalid required tool path: {exc}") from exc
    for tool_name in canonical_path:
        for prefix, kind in _TOOL_PREFIX_TO_KIND.items():
            if tool_name.startswith(prefix):
                hints.add(kind)
                break

    ground_truth = value.get("ground_truth")
    if isinstance(ground_truth, Mapping):
        for key, kind in _GROUND_TRUTH_KEY_TO_KIND.items():
            if key in ground_truth:
                hints.add(kind)

    if not hints:
        raise MaskedDatasetError(
            "cannot infer masked task kind; expected task_type NC/LP/GC or one "
            "canonical structural ML tool family"
        )
    if len(hints) != 1:
        raise MaskedDatasetError(
            "conflicting masked task-kind hints: " + ", ".join(sorted(hints))
        )
    return next(iter(hints))


def _stat_signature(path: Path) -> tuple[int, int, int, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise MaskedDatasetError(f"cannot stat checkpoint {path}: {exc}") from exc
    if not path.is_file():
        raise MaskedDatasetError(f"checkpoint is not a regular file: {path}")
    return (
        int(getattr(stat, "st_dev", 0)),
        int(getattr(stat, "st_ino", 0)),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def _import_torch() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise MaskedDatasetError(
            "masked dataset materialization requires PyTorch"
        ) from exc
    return torch


def _require_mmap_load(torch: Any, *, path: Path, weights_only: bool) -> Any:
    try:
        parameters = inspect.signature(torch.load).parameters
    except (TypeError, ValueError):
        parameters = {}
    missing = [
        name for name in ("mmap", "weights_only") if name not in parameters
    ]
    if missing:
        raise MaskedDatasetError(
            "this PyTorch build lacks safe mmap checkpoint loading "
            f"({', '.join(missing)}); refusing an eager load of {path}. "
            "Upgrade to the benchmark's supported PyTorch version."
        )
    try:
        return torch.load(
            path,
            map_location="cpu",
            mmap=True,
            weights_only=weights_only,
        )
    except Exception as exc:
        trust_description = "trusted base" if not weights_only else "masked"
        raise MaskedDatasetError(
            f"cannot mmap-load {trust_description} checkpoint {path}: {exc}. "
            "No eager fallback was attempted; use a current PyTorch build and "
            "a checkpoint saved with the zipfile serialization format."
        ) from exc


def _validate_index_mapping(payload: Any, *, path: Path) -> Mapping[int, Any]:
    if not isinstance(payload, Mapping):
        raise MaskedDatasetError(
            f"masked checkpoint must contain an integer-indexed mapping: {path}"
        )
    if not payload:
        raise MaskedDatasetError(f"masked checkpoint is empty: {path}")
    keys = list(payload.keys())
    for key in keys:
        if isinstance(key, bool) or not isinstance(key, int) or key < 0:
            raise MaskedDatasetError(
                f"masked checkpoint key must be a non-negative integer, got {key!r}"
            )
    expected_keys = set(range(len(keys)))
    actual_keys = set(keys)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)[:5]
        extra = sorted(actual_keys - expected_keys)[:5]
        raise MaskedDatasetError(
            "masked checkpoint keys must be contiguous 0..N-1; "
            f"missing={missing}, extra={extra}"
        )
    return payload


def _load_masked_cached(path: Path) -> tuple[Mapping[int, Any], bool]:
    """Return one read-only process-wide mmap mapping and a cache-hit flag."""

    resolved = path.expanduser().resolve()
    signature = _stat_signature(resolved)
    with _MASKED_CACHE_LOCK:
        cached = _MASKED_CACHE.get(resolved)
        if cached is not None:
            if cached.stat_signature != signature:
                _MASKED_CACHE.pop(resolved, None)
                raise MaskedDatasetError(
                    f"masked checkpoint changed while cached: {resolved}"
                )
            return cached.payload, True

        torch = _import_torch()
        raw_payload = _require_mmap_load(torch, path=resolved, weights_only=True)
        after_signature = _stat_signature(resolved)
        if after_signature != signature:
            raise MaskedDatasetError(
                f"masked checkpoint changed while loading: {resolved}"
            )
        payload = _validate_index_mapping(raw_payload, path=resolved)
        _MASKED_CACHE[resolved] = _CachedMaskedDataset(
            payload=payload,
            stat_signature=after_signature,
        )
        return payload, False


def clear_masked_cache() -> int:
    """Release process-level references to all mmap-backed masked payloads."""

    with _MASKED_CACHE_LOCK:
        count = len(_MASKED_CACHE)
        _MASKED_CACHE.clear()
    return count


def _selected_entry(
    masked_path: Path,
    index: int,
) -> tuple[Mapping[str, Any], bool]:
    payload, cache_hit = _load_masked_cached(masked_path)
    if index not in payload:
        raise MaskedDatasetError(
            f"masked index {index} is outside 0..{len(payload) - 1}: {masked_path}"
        )
    entry = payload[index]
    if not isinstance(entry, Mapping):
        raise MaskedDatasetError(
            f"masked entry {index} must be a mapping, got {type(entry).__name__}"
        )
    keys = set(entry.keys())
    if keys != _ENTRY_KEYS:
        raise MaskedDatasetError(
            f"masked entry {index} must contain exactly labels and edge_index; "
            f"got {sorted(str(key) for key in keys)}"
        )
    return entry, cache_hit


def _base_field(base: Any, name: str) -> Any:
    if isinstance(base, Mapping):
        aliases = {
            "x": ("x", "node_feature", "node_features"),
            "y": ("y", "label", "labels"),
        }.get(name, (name,))
        for alias in aliases:
            if alias in base:
                return base.get(alias)
        return None
    aliases = {
        "x": ("x", "node_feature", "node_features"),
        "y": ("y", "label", "labels"),
    }.get(name, (name,))
    for alias in aliases:
        value = getattr(base, alias, None)
        if value is not None:
            return value
    return None


def _base_num_nodes(base: Any, torch: Any) -> int | None:
    raw = _base_field(base, "num_nodes")
    if isinstance(raw, torch.Tensor):
        if raw.numel() != 1:
            raise MaskedDatasetError("base num_nodes tensor must contain one value")
        raw = raw.item()
    if raw is not None:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise MaskedDatasetError("base num_nodes must be a non-negative integer")
        return int(raw)
    x = _base_field(base, "x")
    if isinstance(x, torch.Tensor) and x.ndim >= 1:
        return int(x.shape[0])
    return None


def _require_dense_tensor(value: Any, *, field: str, torch: Any) -> Any:
    if not isinstance(value, torch.Tensor):
        raise MaskedDatasetError(
            f"{field} must be a torch.Tensor, got {type(value).__name__}"
        )
    if value.layout != torch.strided:
        raise MaskedDatasetError(f"{field} must be a dense strided tensor")
    if value.device.type != "cpu":
        raise MaskedDatasetError(f"{field} must be loaded on CPU")
    return value


def _validate_edge_index(
    value: Any,
    *,
    field: str,
    num_nodes: int | None,
    torch: Any,
    allow_transposed: bool = False,
) -> Any:
    edge_index = _require_dense_tensor(value, field=field, torch=torch)
    integral_dtypes = {
        torch.uint8,
        torch.int8,
        torch.int16,
        torch.int32,
        torch.int64,
    }
    if edge_index.dtype not in integral_dtypes:
        raise MaskedDatasetError(f"{field} must use an integer dtype")
    normal_shape = edge_index.ndim == 2 and int(edge_index.shape[0]) == 2
    transposed_shape = (
        allow_transposed
        and edge_index.ndim == 2
        and int(edge_index.shape[1]) == 2
    )
    if not normal_shape and not transposed_shape:
        raise MaskedDatasetError(
            f"{field} must have shape [2, E]"
            + (" or [E, 2]" if allow_transposed else "")
            + f", got {list(edge_index.shape)}"
        )
    if transposed_shape and not normal_shape:
        edge_index = edge_index.transpose(0, 1).contiguous()
    if edge_index.numel():
        minimum = int(edge_index.min().item())
        maximum = int(edge_index.max().item())
        if minimum < 0:
            raise MaskedDatasetError(f"{field} contains a negative node index")
        if num_nodes is not None and maximum >= num_nodes:
            raise MaskedDatasetError(
                f"{field} node index {maximum} exceeds num_nodes={num_nodes}"
            )
    return edge_index


def _validate_finite_tensor(value: Any, *, field: str, torch: Any) -> None:
    if value.is_complex():
        raise MaskedDatasetError(f"{field} cannot use a complex dtype")
    if value.is_floating_point() and value.numel():
        if not bool(torch.isfinite(value).all().item()):
            raise MaskedDatasetError(f"{field} contains a non-finite value")


def _validate_labels(
    value: Any,
    *,
    task_kind: str,
    base: Any,
    num_nodes: int | None,
    torch: Any,
) -> Any:
    labels = _require_dense_tensor(value, field="masked labels", torch=torch)
    _validate_finite_tensor(labels, field="masked labels", torch=torch)
    if task_kind == "NC":
        if labels.ndim not in {1, 2}:
            raise MaskedDatasetError(
                "NC masked labels must have shape [N] or [N, C]"
            )
        if num_nodes is None:
            raise MaskedDatasetError(
                "cannot validate NC labels because base num_nodes is unavailable"
            )
        if int(labels.shape[0]) != num_nodes:
            raise MaskedDatasetError(
                "NC label count does not match base graph: "
                f"{int(labels.shape[0])} != {num_nodes}"
            )
        return labels

    if task_kind != "GC":
        raise MaskedDatasetError(f"labels are not valid for task kind {task_kind}")
    if labels.ndim > 2:
        raise MaskedDatasetError("GC masked labels must be scalar, 1D, or 2D")
    if labels.numel() == 0:
        raise MaskedDatasetError("GC masked labels cannot be empty")

    base_y = _base_field(base, "y")
    if isinstance(base_y, torch.Tensor) and tuple(labels.shape) != tuple(base_y.shape):
        raise MaskedDatasetError(
            "GC masked label shape does not match base y: "
            f"{list(labels.shape)} != {list(base_y.shape)}"
        )
    batch = _base_field(base, "batch")
    if base_y is None and isinstance(batch, torch.Tensor) and batch.numel():
        graph_count = int(batch.max().item()) + 1
        label_count = 1 if labels.ndim == 0 else int(labels.shape[0])
        if label_count != graph_count:
            raise MaskedDatasetError(
                "GC label count does not match base batch graph count: "
                f"{label_count} != {graph_count}"
            )
    return labels


def _validate_base(
    base: Any,
    *,
    task_kind: str,
    torch: Any,
) -> tuple[Any, int | None]:
    if not isinstance(base, Mapping):
        try:
            from torch_geometric.data import Data
        except ImportError as exc:
            raise MaskedDatasetError(
                "loading a PyG base checkpoint requires torch-geometric"
            ) from exc
        if not isinstance(base, Data):
            raise MaskedDatasetError(
                "trusted base checkpoint must contain a PyG Data object or mapping, "
                f"got {type(base).__name__}"
            )

    num_nodes = _base_num_nodes(base, torch)
    x = _base_field(base, "x")
    if task_kind in {"NC", "LP"}:
        x = _require_dense_tensor(x, field="base x", torch=torch)
        if x.ndim != 2:
            raise MaskedDatasetError(
                f"base x must have shape [N, D], got {list(x.shape)}"
            )
        if num_nodes is None or int(x.shape[0]) != num_nodes:
            raise MaskedDatasetError("base x row count does not match num_nodes")

    if task_kind == "NC":
        base_edge_index = _base_field(base, "edge_index")
        normalized_edges = _validate_edge_index(
            base_edge_index,
            field="base edge_index",
            num_nodes=num_nodes,
            torch=torch,
            allow_transposed=True,
        )
        if normalized_edges is not base_edge_index:
            base = _set_base_field(base, "edge_index", normalized_edges)
    elif task_kind == "GC":
        # Molecular graph-classification bases (HIV/PCBA) are top-level
        # mappings that contain graph lists rather than one flat edge_index.
        # Validate a flat edge tensor when present, but accept the container;
        # its selected graph is validated after graph_idx has been resolved.
        base_edge_index = _base_field(base, "edge_index")
        if base_edge_index is not None:
            normalized_edges = _validate_edge_index(
                base_edge_index,
                field="base edge_index",
                num_nodes=num_nodes,
                torch=torch,
                allow_transposed=True,
            )
            if normalized_edges is not base_edge_index:
                base = _set_base_field(base, "edge_index", normalized_edges)
    return base, num_nodes


def _set_base_field(base: Any, name: str, value: Any) -> Any:
    if isinstance(base, Mapping):
        merged = dict(base)
        merged[name] = value
        # Some trusted TAGLAS mappings (notably NBA/pokec) expose node labels
        # only through ``label``.  Keep any existing aliases synchronized with
        # the canonical y field so direct consumers cannot observe stale GT.
        if name == "y":
            for alias in ("label", "labels"):
                if alias in merged:
                    merged[alias] = value
        return merged
    setattr(base, name, value)
    if name == "y":
        for alias in ("label", "labels"):
            if getattr(base, alias, None) is not None:
                setattr(base, alias, value)
    return base


def _graph_reference_indices(value: Any) -> set[int]:
    if not isinstance(value, str):
        return set()
    return {int(match) for match in _GRAPH_REFERENCE.findall(value)}


def _graph_target_index(task: Any, graph_count: int) -> int:
    value = _task_mapping(task)
    context = value.get("context", {})
    if not isinstance(context, Mapping):
        raise MaskedDatasetError("GC task context must be a mapping")

    context_index: int | None = None
    if "graph_idx" in context:
        context_index = _strict_nonnegative_index(
            context["graph_idx"], field="context.graph_idx"
        )
    elif "graph_index" in context:
        context_index = _strict_nonnegative_index(
            context["graph_index"], field="context.graph_index"
        )

    ground_truth_index: int | None = None
    ground_truth = value.get("ground_truth")
    classification_truth = (
        ground_truth.get("structural_graph_embedding_classification")
        if isinstance(ground_truth, Mapping)
        else None
    )
    if isinstance(classification_truth, Mapping):
        if "graph_idx" in classification_truth:
            ground_truth_index = _strict_nonnegative_index(
                classification_truth["graph_idx"],
                field=(
                    "ground_truth.structural_graph_embedding_classification."
                    "graph_idx"
                ),
            )
        elif "graph_index" in classification_truth:
            ground_truth_index = _strict_nonnegative_index(
                classification_truth["graph_index"],
                field=(
                    "ground_truth.structural_graph_embedding_classification."
                    "graph_index"
                ),
            )
    if (
        context_index is not None
        and ground_truth_index is not None
        and context_index != ground_truth_index
    ):
        raise MaskedDatasetError(
            "context graph index and structural graph-classification ground-truth "
            f"index disagree: {context_index} != {ground_truth_index}"
        )

    raw_index: Any = (
        context_index if context_index is not None else ground_truth_index
    )
    if raw_index is None:
        anchor_nodes = context.get("anchor_nodes")
        if anchor_nodes is not None and not isinstance(anchor_nodes, Mapping):
            raise MaskedDatasetError("context.anchor_nodes must be a mapping")
        anchor_indices: set[int] = set()
        if isinstance(anchor_nodes, Mapping):
            for field in ("node1_desc", "node2_desc"):
                anchor_indices.update(
                    _graph_reference_indices(anchor_nodes.get(field))
                )
        if len(anchor_indices) > 1:
            raise MaskedDatasetError(
                "context.anchor_nodes descriptions contain conflicting graph "
                "indices: " + ", ".join(str(item) for item in sorted(anchor_indices))
            )
        if anchor_indices:
            raw_index = next(iter(anchor_indices))
        else:
            question_indices = _graph_reference_indices(value.get("question"))
            if len(question_indices) > 1:
                raise MaskedDatasetError(
                    "question contains conflicting graph indices: "
                    + ", ".join(str(item) for item in sorted(question_indices))
                )
            if question_indices:
                raw_index = next(iter(question_indices))

    index = _strict_nonnegative_index(raw_index, field="context.graph_idx")
    if index >= graph_count:
        raise MaskedDatasetError(
            f"context.graph_idx {index} is outside 0..{graph_count - 1}"
        )
    return index


def _set_graph_classification_data(
    base: Any,
    task: Any,
    labels: Any,
    *,
    torch: Any,
) -> Any:
    """Apply GC labels to the selected HIV/PCBA graph.

    The reference evaluator uses the masked ``labels`` for NC/GC and uses the
    masked ``edge_index`` only for LP.  HIV/PCBA require correcting the
    reference implementation's top-level-dict bug by targeting graph_idx, but
    the selected graph's edge structure remains unchanged.
    """

    if not isinstance(base, Mapping) or "graphs" not in base:
        return _set_base_field(base, "y", labels)
    graphs = base.get("graphs")
    if not isinstance(graphs, list) or not graphs:
        raise MaskedDatasetError(
            "GC base graphs must be a non-empty list when present"
        )
    graph_index = _graph_target_index(task, len(graphs))
    target_graph = graphs[graph_index]
    target_y = _base_field(target_graph, "y")
    if isinstance(target_y, torch.Tensor) and tuple(target_y.shape) != tuple(labels.shape):
        raise MaskedDatasetError(
            "GC masked label shape does not match target graph y: "
            f"{list(labels.shape)} != {list(target_y.shape)}"
        )
    merged_graphs = list(graphs)
    merged_graph = _set_base_field(target_graph, "y", labels)
    merged_graphs[graph_index] = merged_graph
    merged_base = dict(base)
    merged_base["graphs"] = merged_graphs
    return merged_base


def _atomic_torch_save(torch: Any, payload: Any, destination: Path) -> Path:
    destination = Path(destination).expanduser()
    parent = destination.parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / destination.name
    if destination.suffix.lower() != ".pt":
        raise MaskedDatasetError(
            f"materialized dataset must use a .pt filename: {destination}"
        )
    if destination.is_symlink():
        raise MaskedDatasetError(
            f"refusing to replace a symlink destination: {destination}"
        )

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
        torch.save(payload, temporary_name)
        with open(temporary_name, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return destination


def materialize_task_dataset(
    binding: DatasetBinding,
    task: Any,
    destination: str | Path,
) -> MaterializedDataset:
    """Create one task-private base graph with its masked field applied.

    The selected masked tensors are never copied or mutated. NC entries replace
    the node labels, LP entries replace ``base.edge_index``, and GC entries
    replace the selected graph's label. The trusted base
    checkpoint is loaded separately for each materialization while the very
    large masked mapping is process-cached.
    """

    if not isinstance(binding, DatasetBinding):
        raise TypeError("binding must be a DatasetBinding")
    binding.validate_files()
    if binding.masked_path is None:
        raise MaskedDatasetError(
            "dataset binding has no masked checkpoint to materialize"
        )

    destination_path = Path(destination).expanduser()
    resolved_destination = destination_path.resolve()
    if resolved_destination in {binding.base_path, binding.masked_path}:
        raise MaskedDatasetError("destination must not overwrite a source checkpoint")

    masked_index = resolve_masked_index(task)
    task_kind = infer_ml_task_kind(task)
    entry, cache_hit = _selected_entry(binding.masked_path, masked_index)
    torch = _import_torch()
    base_signature = _stat_signature(binding.base_path)
    base = _require_mmap_load(torch, path=binding.base_path, weights_only=False)
    if _stat_signature(binding.base_path) != base_signature:
        raise MaskedDatasetError(
            f"base checkpoint changed while loading: {binding.base_path}"
        )
    base, num_nodes = _validate_base(base, task_kind=task_kind, torch=torch)

    applied_field = _TASK_KIND_TO_FIELD[task_kind]
    if task_kind == "LP":
        replacement = _validate_edge_index(
            entry["edge_index"],
            field="masked edge_index",
            num_nodes=num_nodes,
            torch=torch,
        )
    else:
        replacement = _validate_labels(
            entry["labels"],
            task_kind=task_kind,
            base=base,
            num_nodes=num_nodes,
            torch=torch,
        )
    if task_kind == "GC":
        if isinstance(base, Mapping) and isinstance(base.get("graphs"), list):
            applied_field = (
                f"graphs[{_graph_target_index(task, len(base['graphs']))}]"
                ".y"
            )
        merged = _set_graph_classification_data(
            base,
            task,
            replacement,
            torch=torch,
        )
    else:
        merged = _set_base_field(base, applied_field, replacement)
    written_path = _atomic_torch_save(torch, merged, destination_path)
    return MaterializedDataset(
        path=written_path,
        base_path=binding.base_path,
        masked_path=binding.masked_path,
        masked_index=masked_index,
        task_kind=task_kind,
        applied_field=applied_field,
        masked_cache_hit=cache_hit,
    )


__all__ = [
    "DatasetBinding",
    "MaskedDatasetError",
    "MaterializedDataset",
    "clear_masked_cache",
    "infer_ml_task_kind",
    "materialize_task_dataset",
    "resolve_dataset_binding",
    "resolve_masked_index",
]

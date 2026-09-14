"""Attempt-scoped ground-truth context for deterministic virtual graph ML.

The preferred execution path binds this context inside a local
``VirtualMLBroker`` request.  A :class:`~contextvars.ContextVar` keeps labels
request-local even when attempts execute concurrently; the agent process only
receives an opaque broker endpoint and token.  The file helpers remain for
standalone/backwards-compatible use, but benchmark runners must not use them
for untrusted agent workspaces.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .tool_names import expected_tool_path


VIRTUAL_ML_CONTEXT_ENV = "IGRAPH_VIRTUAL_ML_CONTEXT"
VIRTUAL_ML_BROKER_ENDPOINT_ENV = "IGRAPH_VIRTUAL_ML_BROKER_ENDPOINT"
VIRTUAL_ML_BROKER_TOKEN_ENV = "IGRAPH_VIRTUAL_ML_BROKER_TOKEN"
VIRTUAL_ML_BROKER_TOKEN_HEADER = "X-Igraph-Virtual-ML-Token"
VIRTUAL_ML_CONTEXT_SCHEMA = "igraph.virtual_ml.context"
VIRTUAL_ML_CONTEXT_VERSION = "1.0"

NODE_CLASSIFICATION = "structural_node_embedding_classification"
LINK_PREDICTION = "structural_node_embedding_link_prediction"
GRAPH_CLASSIFICATION = "structural_graph_embedding_classification"

VIRTUAL_ML_FAMILIES = (
    NODE_CLASSIFICATION,
    LINK_PREDICTION,
    GRAPH_CLASSIFICATION,
)

_TASK_TYPE_TO_FAMILY = {
    "NC": NODE_CLASSIFICATION,
    "LP": LINK_PREDICTION,
    "GC": GRAPH_CLASSIFICATION,
}
_VALUE_KEYS = {
    NODE_CLASSIFICATION: ("label", "node_label"),
    LINK_PREDICTION: ("label", "has_edge"),
    GRAPH_CLASSIFICATION: ("label", "graph_label"),
}
_MISSING = object()
_UNBOUND_CONTEXT = object()
_BOUND_VIRTUAL_ML_CONTEXT: ContextVar[Any] = ContextVar(
    "igraph_bound_virtual_ml_context", default=_UNBOUND_CONTEXT
)


class VirtualMLContextError(ValueError):
    """Raised when an attempt-scoped virtual-ML context is invalid."""


def _validated_context_copy(context: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached strict-JSON context suitable for request binding."""

    if not isinstance(context, Mapping):
        raise TypeError("virtual-ML context must be a mapping")
    try:
        serialized = json.dumps(
            dict(context), ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        copied = json.loads(serialized)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VirtualMLContextError("virtual-ML context is not strict JSON") from exc
    if not isinstance(copied, dict):
        raise VirtualMLContextError("virtual-ML context must be a JSON object")
    labels = copied.get("labels", {})
    if not isinstance(labels, dict):
        raise VirtualMLContextError("virtual-ML context labels must be an object")
    for family, value in labels.items():
        if family in VIRTUAL_ML_FAMILIES:
            _finite_json_scalar(value, field=f"labels.{family}")
    return copied


@contextmanager
def bind_virtual_ml_context(context: Mapping[str, Any]) -> Iterator[None]:
    """Bind a virtual-ML context to the current request/thread context.

    The token is always reset, including after tool-handler failures.  This is
    intentionally independent of ``os.environ`` so concurrent broker requests
    cannot observe one another's ground truth.
    """

    token = _BOUND_VIRTUAL_ML_CONTEXT.set(_validated_context_copy(context))
    try:
        yield
    finally:
        _BOUND_VIRTUAL_ML_CONTEXT.reset(token)


def bound_virtual_ml_context() -> Mapping[str, Any] | None:
    """Return the request-bound context, or ``None`` outside a broker call."""

    value = _BOUND_VIRTUAL_ML_CONTEXT.get()
    if value is _UNBOUND_CONTEXT:
        return None
    return value


def _finite_json_scalar(value: Any, *, field: str) -> Any:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        raise VirtualMLContextError(f"{field} must be a non-null JSON scalar")
    if isinstance(value, float) and not math.isfinite(value):
        raise VirtualMLContextError(f"{field} must be finite")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise VirtualMLContextError(f"{field} is not a strict JSON scalar") from exc
    return value


def _value_from_payload(payload: Any, family: str) -> Any:
    if payload is None:
        return _MISSING
    if not isinstance(payload, Mapping):
        return payload
    for key in _VALUE_KEYS[family]:
        value = payload.get(key, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return _MISSING


def _inferred_family(task: Mapping[str, Any]) -> str | None:
    task_type = str(task.get("task_type", "")).upper()
    direct = _TASK_TYPE_TO_FAMILY.get(task_type)
    if direct is not None:
        return direct

    prefixes = {
        family
        for action in expected_tool_path(task)
        for family in VIRTUAL_ML_FAMILIES
        if action == family or action.startswith(f"{family}_")
    }
    if len(prefixes) == 1:
        return next(iter(prefixes))

    ground_truth = task.get("ground_truth")
    if isinstance(ground_truth, Mapping):
        candidate = ground_truth.get("answer", ground_truth)
        if isinstance(candidate, Mapping):
            if "has_edge" in candidate:
                return LINK_PREDICTION
            if "node_label" in candidate:
                return NODE_CLASSIFICATION
            if "graph_label" in candidate:
                return GRAPH_CLASSIFICATION
    return None


def _extract_labels(task: Mapping[str, Any]) -> dict[str, Any]:
    labels: dict[str, Any] = {}
    ground_truth = task.get("ground_truth", _MISSING)
    scalar_family = _inferred_family(task)

    if scalar_family is not None and ground_truth is not _MISSING:
        direct_payload = (
            ground_truth.get("answer", ground_truth)
            if isinstance(ground_truth, Mapping)
            else ground_truth
        )
        value = _value_from_payload(direct_payload, scalar_family)
        if value is not _MISSING:
            labels[scalar_family] = _finite_json_scalar(
                value, field=f"ground_truth for {scalar_family}"
            )

    if isinstance(ground_truth, Mapping):
        for key, nested in ground_truth.items():
            normalized_key = str(key)
            for suffix in ("_train", "_infer"):
                if normalized_key.endswith(suffix):
                    normalized_key = normalized_key[: -len(suffix)]
                    break
            if normalized_key not in VIRTUAL_ML_FAMILIES:
                continue
            value = _value_from_payload(nested, normalized_key)
            if value is _MISSING:
                continue
            labels[normalized_key] = _finite_json_scalar(
                value, field=f"ground_truth.{key}"
            )

    return labels


def _task_mapping(task: Any) -> dict[str, Any]:
    if isinstance(task, Mapping):
        return dict(task)
    if is_dataclass(task):
        value = asdict(task)
    elif hasattr(task, "to_dict") and callable(task.to_dict):
        value = task.to_dict()
    else:
        raise TypeError("task must be a mapping or dataclass")
    if not isinstance(value, Mapping):
        raise TypeError("task serialization must return a mapping")
    return dict(value)


def build_virtual_ml_context(task: Any) -> dict[str, Any]:
    """Build the minimal strict-JSON context for one benchmark attempt."""

    task_value = _task_mapping(task)
    attempt_id = task_value.get("attempt_id")
    if attempt_id is not None:
        attempt_id = str(attempt_id).strip() or None
    resolved_task_id = task_value.get("composite_id", task_value.get("id"))
    if resolved_task_id is not None:
        resolved_task_id = str(resolved_task_id)

    context = {
        "schema": VIRTUAL_ML_CONTEXT_SCHEMA,
        "schema_version": VIRTUAL_ML_CONTEXT_VERSION,
        "attempt_id": attempt_id,
        "task_id": resolved_task_id,
        "task_type": task_value.get("task_type"),
        "labels": _extract_labels(task_value),
    }
    try:
        json.dumps(context, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise VirtualMLContextError("virtual-ML context is not strict JSON") from exc
    return context


def write_virtual_ml_context(task: Any, path: Path) -> Path:
    """Atomically write one attempt's context without touching shared source."""

    context = build_virtual_ml_context(task)
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        serialized = json.dumps(
            dict(context), ensure_ascii=False, sort_keys=True, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise VirtualMLContextError("virtual-ML context is not strict JSON") from exc

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(serialized)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)
    return destination


def build_virtual_ml_environment(
    context_path: str | Path,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a process environment copy bound to one attempt context."""

    path = Path(context_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"virtual-ML context not found: {path}")
    environment = dict(os.environ if base_environment is None else base_environment)
    environment[VIRTUAL_ML_CONTEXT_ENV] = str(path)
    return environment


def prepare_virtual_ml_attempt(
    task: Any,
    attempt_dir: str | Path,
    attempt_id: str,
    *,
    task_id: str | None = None,
    base_environment: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], Path, dict[str, str]]:
    """Build, persist, and bind an isolated context for one attempt process."""

    task_value = _task_mapping(task)
    task_value["attempt_id"] = attempt_id
    if task_id is not None:
        task_value["composite_id"] = task_id
    context = build_virtual_ml_context(task_value)
    path = write_virtual_ml_context(task_value, Path(attempt_dir) / "virtual_ml_context.json")
    environment = build_virtual_ml_environment(path, base_environment)
    return context, path, environment


def load_virtual_ml_context(source: str | Path | None = None) -> dict[str, Any]:
    """Load a context path (or inline JSON) from the attempt environment."""

    raw_source: str
    if source is None:
        raw_source = os.environ.get(VIRTUAL_ML_CONTEXT_ENV, "").strip()
    else:
        raw_source = str(source).strip()
    if not raw_source:
        return {}

    try:
        if raw_source.startswith("{"):
            loaded = json.loads(raw_source)
        else:
            context_path = Path(raw_source).expanduser()
            if not context_path.is_file():
                raise VirtualMLContextError(
                    f"virtual-ML context not found: {context_path}"
                )
            loaded = json.loads(context_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VirtualMLContextError("virtual-ML context is invalid JSON") from exc
    except OSError as exc:
        raise VirtualMLContextError("virtual-ML context cannot be read") from exc

    if not isinstance(loaded, dict):
        raise VirtualMLContextError("virtual-ML context must be a JSON object")
    labels = loaded.get("labels", {})
    if not isinstance(labels, dict):
        raise VirtualMLContextError("virtual-ML context labels must be an object")
    for family, value in labels.items():
        if family not in VIRTUAL_ML_FAMILIES:
            continue
        _finite_json_scalar(value, field=f"labels.{family}")
    return loaded


def virtual_label_for(
    family: str, context: Mapping[str, Any] | None = None
) -> Any | None:
    """Return the attempt's injected scalar label for a canonical ML family.

    An explicitly supplied context wins, followed by the broker's request-local
    binding.  The legacy file/environment loader is consulted only when no
    request is bound, which preserves standalone CLI behaviour without an
    environment mutation in broker worker threads.
    """

    normalized = str(family)
    for suffix in ("_train", "_infer"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    if normalized not in VIRTUAL_ML_FAMILIES:
        raise VirtualMLContextError(f"unknown virtual-ML family: {family}")
    if context is not None:
        resolved_context: Mapping[str, Any] = dict(context)
    else:
        bound_context = bound_virtual_ml_context()
        resolved_context = (
            load_virtual_ml_context()
            if bound_context is None
            else bound_context
        )
    labels = resolved_context.get("labels", {})
    if not isinstance(labels, Mapping):
        raise VirtualMLContextError("virtual-ML context labels must be an object")
    value = labels.get(normalized)
    if value is None:
        return None
    return _finite_json_scalar(value, field=f"labels.{normalized}")

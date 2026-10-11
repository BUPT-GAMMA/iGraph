"""Stable contracts shared by the large-graph control and worker layers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal


JobStatus = Literal[
    "queued", "running", "cancelling", "cancelled", "done", "failed"
]
GraphVersionStatus = Literal["preparing", "published", "failed", "cancelled"]
ComputePath = Literal["small_graph", "large_graph", "reject"]
LargeGraphBackend = Literal["auto", "graphscope", "legacy"]


@dataclass(frozen=True)
class BackendPlan:
    """Immutable execution choice persisted with a durable compute job."""

    requested_backend: LargeGraphBackend
    selected_backend: Literal["graphscope", "legacy"]
    native_app: str | None = None
    semantic_profile: str = "legacy_v1"
    adapter_url: str | None = None
    protocol_version: str = "igraph-graphscope-v1"
    fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "BackendPlan":
        values = dict(raw or {})
        if not values:
            return cls(requested_backend="legacy", selected_backend="legacy")
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError(f"未知大图后端计划字段: {', '.join(unknown)}")
        requested = str(values.get("requested_backend") or "legacy").strip().lower()
        selected = str(values.get("selected_backend") or "legacy").strip().lower()
        if requested not in {"auto", "graphscope", "legacy"}:
            raise ValueError("requested_backend 必须是 auto、graphscope 或 legacy")
        if selected not in {"graphscope", "legacy"}:
            raise ValueError("selected_backend 必须是 graphscope 或 legacy")
        values["requested_backend"] = requested
        values["selected_backend"] = selected
        plan = cls(**values)
        if plan.selected_backend == "graphscope" and (
            not plan.native_app or not plan.adapter_url
        ):
            raise ValueError("GraphScope 后端计划缺少 native_app 或 adapter_url")
        return plan


@dataclass(frozen=True)
class DataQualityPolicy:
    """Explicit validation/deduplication semantics stored with a snapshot.

    The defaults match the existing text graph builder where practical:
    node identifiers are normalised, exact logical edges are merged, and the
    strongest weight wins.  Every policy receives a content-derived id so a
    result can always be traced back to the exact semantics that produced it.
    """

    node_normalization: Literal["nfkc_casefold", "exact"] = "nfkc_casefold"
    # The computation catalog operates on the same simple-graph semantics as
    # GraphData -> NetworkX. Relation-aware parallel edges remain opt-in for
    # callers that need a multirelation snapshot for non-algorithm consumers.
    edge_key: Literal["endpoints", "endpoints_relation"] = "endpoints"
    weight_conflict: Literal["first", "last", "max", "min", "reject"] = "max"
    self_loops: Literal["allow", "skip", "reject"] = "allow"
    invalid_records: Literal["skip", "reject"] = "skip"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "DataQualityPolicy":
        values = dict(raw or {})
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError(f"未知数据质量策略字段: {', '.join(unknown)}")
        policy = cls(**values)
        policy.validate()
        return policy

    def validate(self) -> None:
        choices = {
            "node_normalization": {"nfkc_casefold", "exact"},
            "edge_key": {"endpoints", "endpoints_relation"},
            "weight_conflict": {"first", "last", "max", "min", "reject"},
            "self_loops": {"allow", "skip", "reject"},
            "invalid_records": {"skip", "reject"},
        }
        for name, valid in choices.items():
            value = getattr(self, name)
            if value not in valid:
                raise ValueError(f"{name} 必须是 {sorted(valid)} 之一")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def policy_id(self) -> str:
        encoded = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return f"dqp_{hashlib.sha256(encoded).hexdigest()[:16]}"


@dataclass(frozen=True)
class LargeGraphCapability:
    """One explicitly supported algorithm and its execution semantics."""

    algorithm: str
    engine: str
    engine_version: str
    supports_directed: bool = True
    supports_undirected: bool = True
    supports_weighted: bool = False
    result_kind: str = "node_score"
    # Added for auditable method-level mappings. "approximate_modes" prevents
    # scalable substitutions from being mistaken for an exact small-path run.
    modes: tuple[str, ...] = ()
    approximate_modes: tuple[str, ...] = ()
    # Added: make the execution contract explicit before a job is admitted.
    # This keeps an approximation, a bounded enumeration, and an exact result
    # distinguishable in the catalog instead of relying on implementation
    # details buried in the worker.
    execution_mode: str = "exact"
    complexity_class: str = "O(V+E)"
    parallel_strategy: str = "partition_map_reduce"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RouteDecision:
    """Auditable compute-router result returned before work is admitted."""

    path: ComputePath
    accepted: bool
    reason: str
    code: str
    capability: LargeGraphCapability | None = None
    resource_estimate: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.capability is not None:
            payload["capability"] = self.capability.to_dict()
        return payload

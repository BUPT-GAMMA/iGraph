"""Durable large-graph backend dispatcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import BackendPlan
from .engine import LocalParallelGraphEngine, ProgressCallback
from .graphscope_backend import GraphScopeBackend


def execute_compute_request(
    request: dict[str, Any],
    *,
    snapshot_root: str | Path,
    artifact_root: str | Path,
    progress: ProgressCallback | None,
) -> dict[str, Any]:
    """Execute a persisted plan; old jobs intentionally default to legacy."""

    plan = BackendPlan.from_dict(
        dict(request["backend_plan"])
        if isinstance(request.get("backend_plan"), dict)
        else None
    )
    common = {
        "snapshot": dict(request["snapshot"]),
        "algorithm": str(request["algorithm"]),
        "parameters": dict(request.get("parameters") or {}),
        "user_scope": request["user_scope"],
        "graph_id": request["graph_id"],
        "graph_version": int(request["graph_version"]),
        "workers": int(request["workers"]),
        "progress": progress,
        "expected_user_id": str(
            request["snapshot"].get("user_id") or request["user_id"]
        ),
        "expected_session_id": str(
            request["snapshot"].get("session_id") or request["session_id"]
        ),
        "snapshot_scope": str(request.get("snapshot_scope") or ""),
        "derived_target": (
            dict(request["derived_target"])
            if isinstance(request.get("derived_target"), dict)
            else None
        ),
    }
    if plan.selected_backend == "legacy":
        return LocalParallelGraphEngine(
            artifact_root, snapshot_root=snapshot_root
        ).execute(**common)
    return GraphScopeBackend(
        artifact_root,
        snapshot_root=snapshot_root,
        plan=plan,
        job_id=str(request["job_id"]),
        lease_id=(str(request["lease_id"]) if request.get("lease_id") else None),
    ).execute(**common)

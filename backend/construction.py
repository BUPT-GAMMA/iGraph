"""Public, dataset-authorized adapter to Graph Engine construction storage."""
from __future__ import annotations

import math
import os
from typing import Any

import httpx
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.service_client import forwarded_headers
from execution_protocol import service_headers
from construction_access import scope_headers
from backend.dataset_api import legacy_dataset_api_notice


class BuildRequest(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)


class EditRequest(BaseModel):
    base_revision: int = Field(ge=1)
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=100)


def construction_request_timeout(options: dict[str, Any]) -> float:
    """Allow a bounded construction run to finish without extending ordinary RPCs."""
    if "max_seconds" in options:
        duration = options["max_seconds"]
        if type(duration) not in (int, float) or not 1 <= duration <= 3600 or not math.isfinite(duration):
            raise HTTPException(422, {"code": "invalid_construction_options",
                                      "message": "max_seconds 必须是 1 到 3600 之间的有限数值。"})
    else:
        try:
            duration = float(os.getenv("GRAPH_CONSTRUCTION_MAX_SECONDS", "900"))
            if not math.isfinite(duration) or not 1 <= duration <= 3600:
                raise ValueError("out of bounds")
        except ValueError:
            raise HTTPException(500, {"code": "construction_configuration_invalid",
                                      "message": "服务器的构图时间预算配置无效。"}) from None
    return min(float(duration) + 30.0, 3630.0)


def sync_record(directory, dataset, state):
    """The engine is authoritative; metadata is a directory/search projection."""
    if not isinstance(state, dict) or not state.get("status"):
        return dataset
    with directory._lock:
        return _sync_record_locked(directory, dataset, state)


def _sync_record_locked(directory, dataset, state):
    latest = directory.get(dataset.dataset_id, dataset.user_id, dataset.project_id)
    if latest is None:
        raise HTTPException(404, "数据集已删除")
    if latest.metadata.get("construction", {}).get("revision", 0) > state.get("revision", 0):
        return latest
    construction = {key: state[key] for key in (
        "status", "revision", "node_count", "edge_count", "source_type",
        "source_filename", "algorithm_available", "message", "directed", "build_id", "failure_code"
    ) if key in state}
    ready = state["status"] == "ready"
    metadata = {**latest.metadata, "construction": construction,
                "dataset_kind": "single_graph" if ready else "construction_source"}
    if ready:
        metadata.update(num_nodes=state.get("node_count"), num_edges=state.get("edge_count"),
                        directed=state.get("directed"), format_detected="constructed_graph")
    return directory.update(dataset.dataset_id, dataset.user_id, project_id=dataset.project_id,
                            status="ready" if ready else "source_ready", metadata=metadata) or latest


def install_construction_routes(app, *, graph, directory, user_for, project_for, dataset_for):
    def owned(dataset_id, request, project_id):
        user = user_for(request)
        if project_id:
            project_for(project_id, user.user_id)
        dataset = dataset_for(dataset_id, project_id, user.user_id)
        if dataset.file_type not in {"sql", "csv", "txt"}:
            raise HTTPException(422, "构图支持 SQL、CSV 和 TXT 数据源")
        return user, dataset

    def headers(request, user_id, project_id):
        return {**forwarded_headers(request, user_id), **service_headers(user_id), **scope_headers(project_id)}

    async def state_for(dataset, request, user_id):
        result = await graph.request("GET", f"/construction/{dataset.dataset_id}",
                                     headers=headers(request, user_id, dataset.project_id))
        if not result.get("source_type"):
            capabilities = await graph.request("GET", "/construction/capabilities", headers=headers(request, user_id, dataset.project_id))
            available = next((item.get("algorithm_available", False) for item in capabilities.get("formats", [])
                              if item.get("source_type") == dataset.file_type), False)
            result = {**result, "algorithm_available": available}
        return {**result, "source_filename": dataset.original_filename, "source_type": dataset.file_type}

    @app.get("/api/personal-datasets/{dataset_id}/construction")
    @app.get("/api/datasets/{dataset_id}/construction", deprecated=True, dependencies=[Depends(legacy_dataset_api_notice)])
    @app.get("/api/projects/{project_id}/datasets/{dataset_id}/construction")
    async def status(dataset_id: str, request: Request, project_id: str | None = None):
        user, dataset = owned(dataset_id, request, project_id)
        state = await state_for(dataset, request, user.user_id)
        # Merely inspecting a legacy source does not change its load semantics.
        if state.get("status") != "not_built" or "construction" in dataset.metadata:
            sync_record(directory, dataset, state)
        return state

    @app.post("/api/personal-datasets/{dataset_id}/construction/build")
    @app.post("/api/datasets/{dataset_id}/construction/build", deprecated=True, dependencies=[Depends(legacy_dataset_api_notice)])
    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/construction/build")
    async def build(dataset_id: str, req: BuildRequest, request: Request, project_id: str | None = None):
        user, dataset = owned(dataset_id, request, project_id)
        # Browser input never chooses a server-side path or another owner.
        try:
            result = await graph.request("POST", f"/construction/{dataset_id}/build",
                headers=headers(request, user.user_id, project_id), json_body={"source_path": dataset.storage_path,
                    "source_filename": dataset.original_filename, "options": req.options},
                timeout=construction_request_timeout(req.options))
        except httpx.TimeoutException:
            # Disconnecting this HTTP request does not cancel the engine-owned
            # build. Never write a failed dataset projection for an unknown result.
            raise HTTPException(504, {"code": "construction_running_unconfirmed",
                "message": "等待构图响应超时，服务端任务可能仍在运行。请重新查询构图状态后再决定是否重试。",
                "dataset_id": dataset_id, "retryable": True}) from None
        sync_record(directory, dataset, result)
        return result

    @app.get("/api/personal-datasets/{dataset_id}/construction/graph")
    @app.get("/api/datasets/{dataset_id}/construction/graph", deprecated=True, dependencies=[Depends(legacy_dataset_api_notice)])
    @app.get("/api/projects/{project_id}/datasets/{dataset_id}/construction/graph")
    async def read_graph(dataset_id: str, request: Request, project_id: str | None = None):
        user, _ = owned(dataset_id, request, project_id)
        return await graph.request("GET", f"/construction/{dataset_id}/graph", headers=headers(request, user.user_id, project_id))

    @app.post("/api/personal-datasets/{dataset_id}/construction/preview")
    @app.post("/api/datasets/{dataset_id}/construction/preview", deprecated=True, dependencies=[Depends(legacy_dataset_api_notice)])
    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/construction/preview")
    async def preview(dataset_id: str, req: EditRequest, request: Request, project_id: str | None = None):
        user, _ = owned(dataset_id, request, project_id)
        return await graph.request("POST", f"/construction/{dataset_id}/preview",
            headers=headers(request, user.user_id, project_id), json_body=req.model_dump())

    @app.post("/api/personal-datasets/{dataset_id}/construction/commit")
    @app.post("/api/datasets/{dataset_id}/construction/commit", deprecated=True, dependencies=[Depends(legacy_dataset_api_notice)])
    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/construction/commit")
    async def commit(dataset_id: str, req: EditRequest, request: Request, project_id: str | None = None):
        user, dataset = owned(dataset_id, request, project_id)
        result = await graph.request("POST", f"/construction/{dataset_id}/commit",
            headers=headers(request, user.user_id, project_id), json_body=req.model_dump())
        saved_graph = result.get("graph", {})
        sync_record(directory, dataset, {**dataset.metadata.get("construction", {}),
            "status": "ready", "revision": result["revision"],
            "node_count": len(saved_graph.get("nodes", [])), "edge_count": len(saved_graph.get("edges", [])),
            "directed": saved_graph.get("directed", True)})
        return result

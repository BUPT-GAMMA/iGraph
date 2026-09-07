"""Public, dataset-authorized adapter to Graph Engine construction storage."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from backend.service_client import forwarded_headers
from execution_protocol import service_headers


class BuildRequest(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)


class EditRequest(BaseModel):
    base_revision: int = Field(ge=1)
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=100)


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
        "source_filename", "algorithm_available", "message", "directed"
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

    def headers(request, user_id):
        return {**forwarded_headers(request, user_id), **service_headers(user_id)}

    async def state_for(dataset, request, user_id):
        try:
            result = await graph.request("GET", f"/construction/{dataset.dataset_id}",
                                         headers=headers(request, user_id))
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            result = {"dataset_id": dataset.dataset_id, "status": "not_built", "revision": 0,
                      "node_count": 0, "edge_count": 0}
        if not result.get("source_type"):
            capabilities = await graph.request("GET", "/construction/capabilities", headers=headers(request, user_id))
            available = next((item.get("algorithm_available", False) for item in capabilities.get("formats", [])
                              if item.get("source_type") == dataset.file_type), False)
            result = {**result, "algorithm_available": available}
        return {**result, "source_filename": dataset.original_filename, "source_type": dataset.file_type}

    @app.get("/api/personal-datasets/{dataset_id}/construction")
    @app.get("/api/datasets/{dataset_id}/construction")
    @app.get("/api/projects/{project_id}/datasets/{dataset_id}/construction")
    async def status(dataset_id: str, request: Request, project_id: str | None = None):
        user, dataset = owned(dataset_id, request, project_id)
        state = await state_for(dataset, request, user.user_id)
        # Merely inspecting a legacy source does not change its load semantics.
        if state.get("status") != "not_built" or "construction" in dataset.metadata:
            sync_record(directory, dataset, state)
        return state

    @app.post("/api/personal-datasets/{dataset_id}/construction/build")
    @app.post("/api/datasets/{dataset_id}/construction/build")
    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/construction/build")
    async def build(dataset_id: str, req: BuildRequest, request: Request, project_id: str | None = None):
        user, dataset = owned(dataset_id, request, project_id)
        # Browser input never chooses a server-side path or another owner.
        result = await graph.request("POST", f"/construction/{dataset_id}/build",
            headers=headers(request, user.user_id), json_body={"source_path": dataset.storage_path,
                "source_filename": dataset.original_filename, "options": req.options})
        sync_record(directory, dataset, result)
        return result

    @app.get("/api/personal-datasets/{dataset_id}/construction/graph")
    @app.get("/api/datasets/{dataset_id}/construction/graph")
    @app.get("/api/projects/{project_id}/datasets/{dataset_id}/construction/graph")
    async def read_graph(dataset_id: str, request: Request, project_id: str | None = None):
        user, _ = owned(dataset_id, request, project_id)
        return await graph.request("GET", f"/construction/{dataset_id}/graph", headers=headers(request, user.user_id))

    @app.post("/api/personal-datasets/{dataset_id}/construction/preview")
    @app.post("/api/datasets/{dataset_id}/construction/preview")
    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/construction/preview")
    async def preview(dataset_id: str, req: EditRequest, request: Request, project_id: str | None = None):
        user, _ = owned(dataset_id, request, project_id)
        return await graph.request("POST", f"/construction/{dataset_id}/preview",
            headers=headers(request, user.user_id), json_body=req.model_dump())

    @app.post("/api/personal-datasets/{dataset_id}/construction/commit")
    @app.post("/api/datasets/{dataset_id}/construction/commit")
    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/construction/commit")
    async def commit(dataset_id: str, req: EditRequest, request: Request, project_id: str | None = None):
        user, dataset = owned(dataset_id, request, project_id)
        result = await graph.request("POST", f"/construction/{dataset_id}/commit",
            headers=headers(request, user.user_id), json_body=req.model_dump())
        saved_graph = result.get("graph", {})
        sync_record(directory, dataset, {**dataset.metadata.get("construction", {}),
            "status": "ready", "revision": result["revision"],
            "node_count": len(saved_graph.get("nodes", [])), "edge_count": len(saved_graph.get("edges", [])),
            "directed": saved_graph.get("directed", True)})
        return result

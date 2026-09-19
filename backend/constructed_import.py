"""Copy a user's personal constructed dataset into an independently owned project."""
from __future__ import annotations

import copy
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from backend.construction import sync_record
from construction_access import scope_headers
from execution_protocol import service_headers


class ImportConstructedRequest(BaseModel):
    source_dataset_id: str = Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")


def install_constructed_import_routes(app, *, graph, directory, projects, user_for, project_for, dataset_row):
    @app.get("/api/projects/{project_id}/constructed-datasets")
    async def available(project_id: str, request: Request):
        user = user_for(request)
        project_for(project_id, user.user_id)
        rows = [item for item in directory.list(None, user.user_id)
                if item.status == "ready" and item.metadata.get("construction", {}).get("status") == "ready"]
        return {"success": True, "datasets": [dataset_row(item) for item in rows]}

    @app.post("/api/projects/{project_id}/datasets/import-constructed")
    async def import_dataset(project_id: str, req: ImportConstructedRequest, request: Request):
        user = user_for(request)
        project_for(project_id, user.user_id)
        source = directory.get(req.source_dataset_id, user.user_id, None)
        if source is None:
            raise HTTPException(404, "个人构图数据集不存在")
        if source.status != "ready" or source.metadata.get("construction", {}).get("status") != "ready":
            raise HTTPException(409, "请先完成构图再导入项目")
        path = Path(source.storage_path).resolve()
        if not path.is_relative_to(directory.storage_root.resolve()) or not path.is_file():
            raise HTTPException(409, "构图源文件不可用")
        # The new dataset starts as source_ready, so no session can load a
        # partially imported graph. It owns its source as well as its graph.
        target = directory.create(project_id=project_id, user_id=user.user_id,
            name=source.name, original_filename=source.original_filename,
            content=path.read_bytes(), description=source.description)
        target_id = target.dataset_id
        headers = {**service_headers(user.user_id), **scope_headers(project_id)}
        try:
            state = await graph.request("POST", f"/construction/{target.dataset_id}/import",
                headers=headers, json_body={"source_dataset_id": source.dataset_id})
            if not isinstance(state, dict) or state.get("status") != "ready" or not state.get("success"):
                raise HTTPException(502, "构图副本保存失败")
            project = project_for(project_id, user.user_id)
            if directory.get(source.dataset_id, user.user_id, None) is None:
                raise HTTPException(409, "源数据集已删除，请刷新后重试")
            metadata = copy.deepcopy(source.metadata)
            metadata.pop("construction", None)
            metadata["imported_from"] = {"dataset_id": source.dataset_id, "revision": state.get("source_revision")}
            target = directory.update(target.dataset_id, user.user_id, project_id=project_id, metadata=metadata)
            if target is None:
                raise HTTPException(409, "目标数据集已删除")
            target = sync_record(directory, target, state)
            if not project.default_dataset_id:
                project = projects.set_default_dataset(project_id, user.user_id, target.dataset_id) or project
            return {"success": True, "dataset": dataset_row(target, project.default_dataset_id),
                    "default_dataset_id": project.default_dataset_id}
        except BaseException:
            # Cleanup is limited to the new, unpublished dataset. Preserve the
            # metadata if engine cleanup fails so that deletion can be retried.
            try:
                await graph.request("DELETE", f"/construction/{target_id}", headers=headers)
                directory.delete(target_id, user.user_id, project_id)
            except Exception:
                directory.update(target_id, user.user_id, project_id=project_id, status="failed")
            raise

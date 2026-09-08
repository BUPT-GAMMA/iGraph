"""Migration notices for retained dataset HTTP clients.

Registry datasets and project source records have different IDs and lifecycle
semantics. Keep existing responses intact until callers migrate explicitly.
"""
from fastapi import Response


MIGRATION_GUIDE_PATH = "/api/dataset-api-migration"


def legacy_dataset_api_notice(response: Response) -> None:
    response.headers["X-iGraph-API-Deprecated"] = "true"
    response.headers["X-iGraph-Migration-Guide"] = MIGRATION_GUIDE_PATH


DATASET_API_MIGRATION = {
    "version": 1,
    "status": "compatibility",
    "removal_scheduled": False,
    "message": "旧 HTTP 接口继续保留现有响应和数据语义。新页面使用个人／项目数据集接口；迁移前需确认数据集归属及 ID 映射。",
    "legacy_registry_routes": [
        {"method": "GET", "path": "/api/datasets"},
        {"method": "DELETE", "path": "/api/datasets/{dataset_id}"},
        {"method": "POST", "path": "/api/sessions/{session_id}/load-dataset"},
        {"method": "POST", "path": "/api/sessions/{session_id}/save-dataset"},
    ],
    "browser_routes": {
        "personal": "/api/personal-datasets",
        "project": "/api/projects/{project_id}/datasets",
        "select_dataset": {"method": "PATCH", "path": "/api/sessions/{session_id}/dataset",
                           "body": {"active_dataset_id": "<个人或当前项目的数据集 ID>"}},
    },
    "construction_alias": {
        "from": "/api/datasets/{dataset_id}/construction",
        "to": "/api/personal-datasets/{dataset_id}/construction",
        "suffixes": ["", "/build", "/graph", "/preview", "/commit"],
        "same_dataset_id": True,
    },
    "agent_tools": {
        "status": "supported",
        "tools": ["list_datasets", "load_dataset", "save_as_dataset", "load_constructed_graph"],
        "message": "Agent 工具继续维护，通过 MCP 调用 Graph Engine；HTTP 弃用标记不适用于这些工具。",
    },
    "registry_migration": {
        "automatic_redirect": False,
        "message": "公共数据集、Registry 版本和个人／项目上传记录分别保存。保留现有 ID、版本和权限；完整迁移后再切换客户端。",
        "steps": ["盘点调用方和 Registry 数据集（包括公共数据、历史版本及 Nebula 引用）。",
                  "确定个人／项目归属并建立新旧 ID 映射，保留源数据与版本。",
                  "验证列表、加载、保存和删除行为，再切换调用方并决定旧入口下线时间。"],
    },
}

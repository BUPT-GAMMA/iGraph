"""Path-free, validated routing from construction MCP tools to engine HTTP."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from urllib.parse import quote

from graph_engine.construction_catalog import CONSTRUCTION_TOOL_NAMES


CONSTRUCTION_SOURCE_TYPES = frozenset({"sql", "csv", "txt"})


def construction_source_type(filename: object) -> str:
    """Recognize raw source uploads without reading or executing their content."""
    suffix = PurePosixPath(str(filename or "").replace("\\", "/")).suffix.lower().lstrip(".")
    return suffix if suffix in CONSTRUCTION_SOURCE_TYPES else ""


def construction_request(tool_name: str, session_id: str, arguments: dict) -> tuple[str, str, dict | None]:
    """Resolve only catalogued routes, forbidding user-selected source paths."""
    if tool_name not in CONSTRUCTION_TOOL_NAMES:
        raise ValueError("Unknown graph construction tool")
    allowed = set() if tool_name == "get_graph_construction_capabilities" else {"dataset_id"}
    if tool_name in {"preview_graph_optimization", "commit_graph_optimization"}:
        allowed.update({"base_revision", "operations"})
    if set(arguments) - allowed:
        raise ValueError("构图工具只接受数据集 ID、版本和优化操作，不能指定源文件路径或身份。")
    if not allowed:
        return "GET", "/construction/capabilities", None
    dataset_id = arguments.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id.strip() or dataset_id in {".", ".."}:
        raise ValueError("dataset_id 必须是可访问数据集的非空 ID。")
    # Quote each identifier independently; it cannot change the selected route.
    root = f"/construction/{quote(dataset_id, safe='')}"
    if tool_name == "get_graph_construction_status":
        return "GET", root, None
    if tool_name == "get_constructed_graph":
        return "GET", f"{root}/graph", None
    if tool_name == "load_constructed_graph":
        if not session_id:
            raise ValueError("构图数据集加载需要当前会话。")
        return "POST", f"/sessions/{quote(session_id, safe='')}/graph/load_construction", {"dataset_id": dataset_id}
    revision = arguments.get("base_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("base_revision 必须是读取或预览时的正整数版本。")
    operations = arguments.get("operations")
    if isinstance(operations, str):
        try:
            operations = json.loads(operations)
        except ValueError as exc:
            raise ValueError("operations 必须是优化操作数组。") from exc
    if not isinstance(operations, list) or not operations or not all(isinstance(op, dict) for op in operations):
        raise ValueError("operations 必须是非空优化操作对象数组。")
    action = "preview" if tool_name == "preview_graph_optimization" else "commit"
    return "POST", f"{root}/{action}", {"base_revision": revision, "operations": operations}

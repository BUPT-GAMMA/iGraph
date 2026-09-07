"""Keep approved dataset scope and native Agno state aligned with graph loads."""

from __future__ import annotations

import json


_SCOPED_TOOLS = frozenset({"load_constructed_graph", "preview_graph_optimization", "commit_graph_optimization"})


def _result_payload(value):
    """Read both MCP text content and a direct Graph Engine result."""
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if isinstance(value, dict):
        if "success" in value:
            return value
        if isinstance(value.get("structuredContent"), dict):
            structured = _result_payload(value["structuredContent"])
            if structured is not None:
                return structured
            # FastMCP string-returning handlers expose {"result": "..."}
            # alongside the canonical text blocks. Fall back to those blocks.
        # Agno's actual MCP entrypoint wraps the wire response in ToolResult,
        # whose model_dump() contains a JSON *string* in content.
        if isinstance(value.get("content"), (str, list, dict)):
            return _result_payload(value["content"])
        if value.get("type") == "text":
            return _result_payload(value.get("text"))
    if isinstance(value, list):
        for block in value:
            payload = _result_payload(block)
            if payload is not None:
                return payload
    return None


def _allowed_datasets(state):
    if not isinstance(state, dict) or not isinstance(state.get("data_state"), dict):
        return {}
    data = state["data_state"]
    rows = data.get("available_constructed_datasets")
    available = {
        row["dataset_id"]: row for row in rows
        if isinstance(row, dict) and isinstance(row.get("dataset_id"), str)
    } if isinstance(rows, list) else {}
    active_id = data.get("active_dataset_id")
    if isinstance(active_id, str) and active_id:
        available.setdefault(active_id, data)
    return available


async def construction_tool_hook(function_name, function_call, arguments, run_context):
    """Guard writes/loads by BFF scope, and let Agno persist a successful load.

    The BFF refreshes this list for the current user and project on every run.
    An LLM-supplied ID cannot choose another project merely by sharing an owner.
    """
    if function_name not in _SCOPED_TOOLS:
        return await function_call(**arguments)
    state = getattr(run_context, "session_state", None)
    allowed = _allowed_datasets(state)
    dataset_id = arguments.get("dataset_id")
    if not isinstance(dataset_id, str) or dataset_id not in allowed:
        return json.dumps({
            "success": False, "code": "DATASET_SCOPE_REQUIRED",
            "error": "此数据集不在当前会话范围的可用构图数据集中。请先在数据集面板选择当前项目/个人范围内的已构图数据集。",
        }, ensure_ascii=False)
    result = await function_call(**arguments)
    payload = _result_payload(result)
    if function_name == "load_constructed_graph" and isinstance(payload, dict) and payload.get("success") is True:
        returned_id = payload.get("construction_dataset_id", payload.get("dataset_id", dataset_id))
        if returned_id != dataset_id:
            return result
        row = allowed[dataset_id]
        data = dict(state["data_state"])
        data.update(
            active_dataset_id=dataset_id,
            construction_status="ready",
            construction_revision=payload.get("construction_revision", payload.get("revision", row.get("revision", row.get("construction_revision")))),
            source_format=row.get("source_format", row.get("source_type", data.get("source_format"))),
        )
        state.update(active_dataset_id=dataset_id, data_state=data)
    return result

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.agent.memory.memory_engine import MemoryEngine


VIRTUAL_MEMORY_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "server": "igraph",
        "name": "memory_read",
        "description": "查询当前会话的长期记忆、题型经验或用户档案。返回按相关度排序的条目列表。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["long_term", "playbook", "user_profile"],
                    "description": "查询范围",
                },
                "query": {
                    "type": "string",
                    "description": "搜索词（可选，留空则返回所有条目）",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回条数，默认 10",
                    "default": 10,
                },
            },
            "required": ["scope"],
        },
    },
    {
        "server": "igraph",
        "name": "memory_delete",
        "description": "软删除一条长期记忆或题型经验（将其标记为 archived，不再出现在下次会话快照中）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["long_term", "playbook"],
                    "description": "操作范围",
                },
                "memory_id": {
                    "type": "string",
                    "description": "要删除的记忆/题型 ID（memory_id 或 playbook_id）",
                },
                "reason": {
                    "type": "string",
                    "description": "删除原因（可选，记录在事件日志中）",
                },
            },
            "required": ["scope", "memory_id"],
        },
    },
    {
        "server": "igraph",
        "name": "memory_write",
        "description": "手动写入长期记忆或用户档案。写入会经过安全扫描，并在下次会话快照生效。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["long_term", "user_profile", "project", "dataset"],
                    "description": "写入范围；project/dataset 当前映射到长期记忆并保留 category。",
                },
                "category": {"type": "string"},
                "key": {"type": "string"},
                "value": {"type": "string"},
                "reason": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["scope", "key", "value"],
        },
    },
    {
        "server": "igraph",
        "name": "memory_update",
        "description": "编辑已有长期记忆、用户档案或题型经验。写入会经过安全扫描。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["long_term", "user_profile", "project", "dataset", "playbook"],
                },
                "memory_id": {"type": "string"},
                "key": {"type": "string"},
                "value": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["scope"],
        },
    },
]


def handle_memory_tool_call(
    name: str,
    args: dict[str, Any],
    engine: "MemoryEngine | None",
) -> str | None:
    """Handle virtual memory tool calls.  Returns result string or None if not handled."""
    if engine is None:
        if name in {"memory_read", "memory_delete", "memory_write", "memory_update"}:
            return "错误：内存引擎未初始化"
        return None

    if name == "memory_read":
        scope = str((args or {}).get("scope") or "").strip()
        query = str((args or {}).get("query") or "").strip()
        try:
            limit = max(1, int((args or {}).get("limit") or 10))
        except (TypeError, ValueError):
            limit = 10
        results = engine.query_memories(scope=scope, query=query, limit=limit)
        return json.dumps({"scope": scope, "query": query, "results": results}, ensure_ascii=False, indent=2)

    if name == "memory_delete":
        scope = str((args or {}).get("scope") or "").strip()
        memory_id = str((args or {}).get("memory_id") or "").strip()
        reason = str((args or {}).get("reason") or "").strip()
        if not memory_id:
            return "错误：memory_id 不能为空"
        result = engine.delete_memory(scope=scope, memory_id=memory_id, reason=reason, session_id="agent")
        if result is None:
            return json.dumps({"deleted": False, "memory_id": memory_id, "reason": "not_found"}, ensure_ascii=False)
        return json.dumps({"deleted": True, **result}, ensure_ascii=False)

    if name == "memory_write":
        scope = str((args or {}).get("scope") or "long_term").strip()
        key = str((args or {}).get("key") or "").strip()
        value = str((args or {}).get("value") or "").strip()
        category = str((args or {}).get("category") or scope or "fact").strip()
        reason = str((args or {}).get("reason") or "").strip()
        try:
            confidence = float((args or {}).get("confidence") or 0.8)
        except (TypeError, ValueError):
            confidence = 0.8
        if not key or not value:
            return "错误：memory_write 需要 key 和 value"
        result = engine.write_memory_manual(
            scope=scope,
            key=key,
            value=value,
            category=category,
            confidence=confidence,
            reason=reason,
            session_id="agent",
        )
        return json.dumps({"written": bool(result), "result": result}, ensure_ascii=False, indent=2)

    if name == "memory_update":
        scope = str((args or {}).get("scope") or "long_term").strip()
        memory_id = str((args or {}).get("memory_id") or "").strip()
        key = str((args or {}).get("key") or "").strip()
        value = str((args or {}).get("value") or "").strip()
        reason = str((args or {}).get("reason") or "").strip()
        result = engine.update_memory_manual(
            scope=scope,
            memory_id=memory_id,
            key=key,
            value=value,
            reason=reason,
            session_id="agent",
        )
        return json.dumps({"updated": bool(result), "result": result}, ensure_ascii=False, indent=2)

    return None

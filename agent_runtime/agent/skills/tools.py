from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from agent_runtime.agent.skills.guard import validate_supporting_file
from agent_runtime.agent.skills.registry import SkillRegistry


VIRTUAL_SKILL_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "server": "igraph",
        "name": "skills_list",
        "description": "列出可用 iGraph skills 的 compact index，可按 domain 过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {"domain": {"type": "string"}},
        },
    },
    {
        "server": "igraph",
        "name": "skill_view",
        "description": "按名称加载某个 skill 的完整指引，或读取其 supporting file。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "file_path": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "server": "igraph",
        "name": "skill_manage",
        "description": "创建、维护、审批用户或候选 iGraph skill。不能修改 bundled skills。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "write_file", "remove_file", "approve", "reject"],
                },
                "destination": {"type": "string", "enum": ["user", "review_queue"]},
                "name": {"type": "string"},
                "target_name": {"type": "string"},
                "playbook_id": {"type": "string"},
                "body": {"type": "string"},
                "metadata": {"type": "object"},
                "file_path": {"type": "string"},
                "content": {"type": "string"},
                "reason": {"type": "string"},
                "remove_files": {"type": "boolean"},
            },
            "required": ["action", "name"],
        },
    },
]


def handle_skill_tool_call(
    name: str,
    args: dict[str, Any],
    registry: SkillRegistry | None = None,
    *,
    user_id: str = "",
    register_tools: Callable[[Iterable[str]], list[str]] | None = None,
) -> str | None:
    registry = registry or SkillRegistry()
    if name == "skills_list":
        domain = str((args or {}).get("domain") or "").strip()
        specs = registry.discover()
        if domain:
            specs = [spec for spec in specs if spec.domain == domain]
        rows = [
            {
                "name": spec.name,
                "description": spec.description,
                "domain": spec.domain,
                "version": spec.version,
                "risk_level": spec.risk_level,
                "source": spec.source,
            }
            for spec in specs
        ]
        return json.dumps({"skills": rows}, ensure_ascii=False, indent=2)

    if name == "skill_view":
        skill_name = str((args or {}).get("name") or "").strip()
        file_path = str((args or {}).get("file_path") or "").strip()
        spec = registry.get(skill_name)
        if spec is None:
            return f"错误：未知 skill {skill_name}"
        if file_path:
            target = validate_supporting_file(Path(spec.path).parent, file_path)
            text = target.read_text(encoding="utf-8")
            return text[:12000]
        body_text = spec.body
        if spec.config_keys:
            from agent_runtime.agent.skills.config_resolver import resolve_skill_config, substitute_vars
            cfg = resolve_skill_config(spec.config_keys)
            body_text = substitute_vars(body_text, cfg)
        # Pre-load tools declared by the skill so the model does not need to
        # call tool_select / tool_expand again before invoking them.
        loaded_tools: list[str] = []
        if register_tools is not None:
            declared: list[str] = []
            seen: set[str] = set()
            for source in (spec.condition.requires_tools, spec.condition.any_tools):
                for tool_name in source:
                    name_str = str(tool_name or "").strip()
                    if name_str and name_str not in seen:
                        seen.add(name_str)
                        declared.append(name_str)
            if declared:
                try:
                    loaded_tools = list(register_tools(declared))
                except Exception:
                    loaded_tools = []
        loaded_line = (
            f"已加载工具 schema 到当前上下文（无需再调用 tool_select）：{', '.join(loaded_tools)}\n\n"
            if loaded_tools
            else ""
        )
        return (
            f"# Skill: {spec.name}\n"
            f"Description: {spec.description}\n"
            f"Domain: {spec.domain}\n"
            f"Version: {spec.version}\n\n"
            f"{loaded_line}"
            f"{body_text[:12000]}"
        )

    if name == "skill_manage":
        from agent_runtime.agent.skills.manager import SkillManager

        try:
            result = SkillManager(user_id=user_id).manage(str((args or {}).get("action") or ""), args or {})
        except Exception as exc:
            result = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        return json.dumps(result, ensure_ascii=False, indent=2)

    return None

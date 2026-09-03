from __future__ import annotations

from collections import defaultdict
from typing import Iterable, List

from agent_runtime.agent.skills.models import SkillSpec
from agent_runtime.agent.skills.registry import SkillRegistry

def _tool_names(tool_specs: Iterable[dict]) -> set[str]:
    names: set[str] = set()
    for spec in tool_specs:
        name = str(spec.get("name", "")).strip()
        if name:
            names.add(name)
    return names


def _graph_state_matches(required: str, graph_state: dict | None) -> bool:
    required = (required or "none").strip().lower()
    if required in {"", "none", "any"}:
        return True
    state = graph_state or {}
    if required == "loaded":
        return bool(state.get("has_graph") or state.get("loaded"))
    if required == "weighted":
        return bool(state.get("has_weights"))
    if required == "directed":
        return bool(state.get("is_directed"))
    return True


def _is_active(
    spec: SkillSpec,
    names: set[str],
    graph_state: dict | None,
    intent: str | None,
    protocol: str | None = None,
) -> bool:
    cond = spec.condition
    if protocol:
        supported = {str(v).strip().lower() for v in cond.supports_protocols if str(v).strip()}
        if supported and protocol.strip().lower() not in supported:
            return False
    if cond.requires_tools and not set(cond.requires_tools).issubset(names):
        return False
    if cond.any_tools and not set(cond.any_tools).intersection(names):
        return False
    if cond.fallback_for_tools and set(cond.fallback_for_tools).issubset(names):
        return False
    if cond.activates_on_intents and intent and intent not in cond.activates_on_intents:
        return False
    return _graph_state_matches(cond.requires_graph_state, graph_state)


def build_skills_system_prompt(
    tool_specs: List[dict],
    *,
    graph_state: dict | None = None,
    intent: str | None = None,
    protocol: str | None = None,
    registry: SkillRegistry | None = None,
    char_limit: int = 2000,
) -> str:
    """Build a compact skill index; full bodies are loaded with skill_view."""
    names = _tool_names(tool_specs)
    registry = registry or SkillRegistry()
    active = [
        spec
        for spec in registry.discover()
        if _is_active(spec, names, graph_state=graph_state, intent=intent, protocol=protocol)
    ]
    if not active:
        return ""

    grouped: dict[str, list[SkillSpec]] = defaultdict(list)
    for spec in active:
        grouped[spec.domain].append(spec)

    lines = [
        "## Available Skills",
        "以下是当前可用技能索引。需要完整流程时调用 `skill_view(name)`；",
        "需要查看全部技能或按领域筛选时调用 `skills_list(domain=...)`。",
    ]
    for domain in sorted(grouped):
        lines.append("")
        lines.append(f"### {domain}")
        for spec in sorted(grouped[domain], key=lambda item: item.name):
            row = f"- **{spec.name}**: {spec.description}"
            tentative = "\n".join([*lines, row])
            if len(tentative) > char_limit:
                lines.append(f"- ...等更多技能，调用 `skills_list(domain=\"{domain}\")` 查看")
                return "\n".join(lines)
            lines.append(row)
    return "\n".join(lines)

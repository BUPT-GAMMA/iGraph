from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SkillCondition:
    requires_tools: tuple[str, ...] = ()
    any_tools: tuple[str, ...] = ()
    fallback_for_tools: tuple[str, ...] = ()
    requires_graph_state: str = "none"
    activates_on_intents: tuple[str, ...] = ()
    supports_protocols: tuple[str, ...] = ("text", "native")
    tags: tuple[str, ...] = ()
    related_skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    domain: str
    path: str
    body: str
    version: str = "1.0.0"
    risk_level: str = "medium"
    source: str = "bundled"
    condition: SkillCondition = field(default_factory=SkillCondition)
    config_keys: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)

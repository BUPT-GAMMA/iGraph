from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_FIELD_ORDER = [
    "language",
    "expertise_level",
    "preferred_output_format",
    "communication_style",
    "domain_knowledge",
    "known_constraints",
    "custom_facts",
]


@dataclass
class UserProfile:
    """Structured user-profile facts stored in ``user_profile.json``.

    Fields are ordered by priority for character-budget truncation:
    ``language`` and ``expertise_level`` are highest priority.
    """

    language: str = ""
    expertise_level: str = ""              # beginner / intermediate / expert
    preferred_output_format: str = ""      # markdown / plain / structured
    communication_style: str = ""          # concise / detailed / step-by-step
    domain_knowledge: list[str] = field(default_factory=list)
    known_constraints: list[str] = field(default_factory=list)
    custom_facts: list[dict] = field(default_factory=list)
    updated_at: str = ""


def load_from_items(items: list[dict]) -> UserProfile:
    """Build a UserProfile from the ``items`` list in ``user_profile.json``."""
    profile = UserProfile()
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or item.get("name") or "").strip().lower()
        value = item.get("value")
        if not key or value is None:
            continue
        _apply_field(profile, key, value)
    return profile


def _apply_field(profile: UserProfile, key: str, value: Any) -> None:
    if key == "language" and isinstance(value, str):
        profile.language = value.strip()
    elif key == "expertise_level" and isinstance(value, str):
        profile.expertise_level = value.strip()
    elif key == "preferred_output_format" and isinstance(value, str):
        profile.preferred_output_format = value.strip()
    elif key == "communication_style" and isinstance(value, str):
        profile.communication_style = value.strip()
    elif key == "domain_knowledge":
        if isinstance(value, list):
            profile.domain_knowledge = [str(v) for v in value if str(v).strip()]
        elif isinstance(value, str) and value.strip():
            profile.domain_knowledge = [value.strip()]
    elif key == "known_constraints":
        if isinstance(value, list):
            profile.known_constraints = [str(v) for v in value if str(v).strip()]
        elif isinstance(value, str) and value.strip():
            profile.known_constraints = [value.strip()]
    elif key.startswith("custom_") or key == "custom_facts":
        if isinstance(value, list):
            profile.custom_facts.extend(v for v in value if isinstance(v, dict))
        elif isinstance(value, dict):
            profile.custom_facts.append(value)


def to_snapshot_lines(profile: UserProfile, char_limit: int = 1500) -> list[str]:
    """Render UserProfile as lines for the frozen memory snapshot.

    Fields are emitted in priority order; lines are truncated when the
    cumulative character count would exceed *char_limit*.
    """
    candidates: list[str] = []
    if profile.language:
        candidates.append(f"- language: {profile.language}")
    if profile.expertise_level:
        candidates.append(f"- expertise_level: {profile.expertise_level}")
    if profile.preferred_output_format:
        candidates.append(f"- preferred_output_format: {profile.preferred_output_format}")
    if profile.communication_style:
        candidates.append(f"- communication_style: {profile.communication_style}")
    if profile.domain_knowledge:
        candidates.append(f"- domain_knowledge: {', '.join(profile.domain_knowledge)}")
    if profile.known_constraints:
        candidates.append(f"- known_constraints: {'; '.join(profile.known_constraints)}")
    for fact in profile.custom_facts:
        if isinstance(fact, dict):
            parts = ", ".join(f"{k}={v}" for k, v in fact.items())
            candidates.append(f"- custom: {parts}")

    kept: list[str] = []
    used = 0
    for line in candidates:
        size = len(line) + 1
        if used + size > char_limit:
            break
        kept.append(line)
        used += size
    return kept

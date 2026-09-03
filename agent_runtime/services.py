"""Public Runtime services used by the application layer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any


def stream_skill_generation_from_env(
    playbook_item: dict[str, Any],
    model: str | None = None,
) -> AsyncIterator[str]:
    """Create the legacy skill-generation stream from environment credentials."""

    from .agent.sub_agents.skill_generator import SkillGenerator

    return SkillGenerator.from_env(model).stream(playbook_item)


def extract_generated_skill_name(content: str) -> str:
    """Extract the generated skill name with the existing parser."""

    from .agent.sub_agents.skill_generator import extract_name

    return extract_name(content)

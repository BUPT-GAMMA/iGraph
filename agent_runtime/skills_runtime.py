"""Agno LocalSkills layout and loader construction."""

from __future__ import annotations

import re
import shutil
from functools import lru_cache
from pathlib import Path

from agno.skills import LocalSkills, Skills

import backend.data_paths as _dp
from agent_runtime.config import AgentRuntimeConfig


def bundled_skills_source_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / "skills"


def _canonical_name(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", value.strip().lower())).strip("-")


@lru_cache(maxsize=4)
def bundled_skills_path(config: AgentRuntimeConfig | None = None) -> Path:
    """Return AgentOS-ready bundled Skills.

    The legacy repository used underscores in Skill names.  AgentOS follows
    the portable ``lowercase-hyphen`` convention, so the runtime materializes
    a normalized, read-only copy under its own data root.  Source assets stay
    unchanged for compatibility with older imports and tests.
    """

    source = bundled_skills_source_path()
    if config is None:
        return source
    target = config.skills_root / "bundled"
    target.mkdir(parents=True, exist_ok=True)
    for source_dir in source.iterdir() if source.exists() else ():
        source_file = source_dir / "SKILL.md"
        if not source_dir.is_dir() or not source_file.is_file():
            continue
        canonical = _canonical_name(source_dir.name)
        target_dir = target / canonical
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)
        target_file = target_dir / "SKILL.md"
        body = target_file.read_text(encoding="utf-8")
        body = re.sub(r"(?m)^name:\s*[^\r\n]+", f"name: {canonical}", body, count=1)
        target_file.write_text(body, encoding="utf-8")
    return target


def user_skills_path(config: AgentRuntimeConfig, user_id: str) -> Path:
    """Return the canonical approved-Skill directory for a user."""
    del config
    return _dp.user_skills_dir(user_id)


def candidate_skills_path(config: AgentRuntimeConfig, user_id: str) -> Path:
    """Return the canonical review-queue directory for a user."""
    del config
    return _dp.user_review_queue_dir(user_id)


def build_skills(config: AgentRuntimeConfig, user_id: str) -> Skills:
    active_user_dir = user_skills_path(config, user_id)
    candidate_skills_path(config, user_id).mkdir(parents=True, exist_ok=True)
    active_user_dir.mkdir(parents=True, exist_ok=True)
    return Skills(
        loaders=[
            LocalSkills(str(bundled_skills_path(config))),
            # Candidate Skills are deliberately excluded until approval.
            LocalSkills(str(active_user_dir)),
        ]
    )

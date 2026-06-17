from __future__ import annotations

from pathlib import Path

import backend.data_paths as _dp


# ── Global (shared across all users) ─────────────────────────────────────────

def user_skills_dir() -> Path:
    """全局共享技能目录（原 bundled + admin 创建的技能均在此）。"""
    return _dp.global_skills_dir()


def review_queue_dir() -> Path:
    """全局 review_queue。"""
    return _dp.global_review_queue_dir()


def skill_events_path() -> Path:
    return _dp.global_skills_dir() / "skill_events.jsonl"


# ── Per-user ──────────────────────────────────────────────────────────────────

def per_user_skills_dir(user_id: str) -> Path:
    return _dp.user_skills_dir(user_id)


def per_user_review_queue_dir(user_id: str) -> Path:
    return _dp.user_review_queue_dir(user_id)


def per_user_skill_events_path(user_id: str) -> Path:
    return _dp.user_skills_dir(user_id) / "skill_events.jsonl"

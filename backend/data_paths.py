"""
Central path resolver for iGraph data storage.

All on-disk paths are derived from IGRAPH_DATA_ROOT (default: "data"):

  data/
    global/
      skills/{skill}/SKILL.md     # shared user-created skills
      skills/.review_queue/
      datasets/{dataset_id}/      # shared datasets
      playbooks.json              # shared playbooks
    users/{user_id}/
      sessions/{session_id}/      # per-user chat sessions
      memory/                     # per-user long-term memory
      skills/{skill}/SKILL.md     # per-user private skills
      skills/.review_queue/
      datasets/{dataset_id}/      # per-user private datasets
      uploads/{session_id}/       # per-user graph data uploads
      playbooks.json              # per-user private playbooks

Bundled (read-only) skill resources live in agent_runtime/resources/skills/.
"""

from __future__ import annotations

import os
from pathlib import Path

_INVALID_PATH_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


def data_root() -> Path:
    raw = (
        os.getenv("IGRAPH_DATA_ROOT", "").strip()
        or os.getenv("GRAPHAGENT_DATA_ROOT", "").strip()
        or "data"
    )
    p = Path(raw)
    if not p.is_absolute():
        # Resolve relative to the project root (two levels up from this file).
        project_root = Path(__file__).resolve().parent.parent
        p = project_root / p
    return p.resolve()


# ── Global (shared across all users) ─────────────────────────────────────────

def global_dir() -> Path:
    return data_root() / "global"


def global_skills_dir() -> Path:
    return global_dir() / "skills"


def global_review_queue_dir() -> Path:
    return global_dir() / "skills" / ".review_queue"


def global_datasets_dir() -> Path:
    return global_dir() / "datasets"


def global_playbooks_path() -> Path:
    return global_dir() / "playbooks.json"


# ── Per-user ──────────────────────────────────────────────────────────────────

def user_dir(user_id: str) -> Path:
    return data_root() / "users" / user_id


def user_sessions_dir(user_id: str) -> Path:
    return user_dir(user_id) / "sessions"


def user_memory_dir(user_id: str) -> Path:
    return user_dir(user_id) / "memory"


def user_skills_dir(user_id: str) -> Path:
    return user_dir(user_id) / "skills"


def user_review_queue_dir(user_id: str) -> Path:
    return user_dir(user_id) / "skills" / ".review_queue"


def user_datasets_dir(user_id: str) -> Path:
    return user_dir(user_id) / "datasets"


def user_uploads_dir(user_id: str) -> Path:
    return user_dir(user_id) / "uploads"


def user_playbooks_path(user_id: str) -> Path:
    return user_dir(user_id) / "playbooks.json"


# ── Session index (session_id → user_id, O(1) lookup) ────────────────────────

def session_index_dir() -> Path:
    return data_root() / ".session_index"


def session_index_path(session_id: str) -> Path:
    return session_index_dir() / session_id


def safe_path_component(value: str, default: str = "_") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    cleaned = "".join(
        "_" if (ord(ch) < 32 or ch in _INVALID_PATH_CHARS) else ch
        for ch in text
    ).rstrip(" .")
    if not cleaned:
        cleaned = default
    if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned

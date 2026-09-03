"""Graph Engine-owned paths for graphs, uploads, datasets and artifacts."""

from __future__ import annotations

import os
from pathlib import Path

_INVALID_PATH_CHARS = frozenset('<>:"/\\|?*')


def data_root() -> Path:
    root = Path(os.getenv("IGRAPH_DATA_ROOT", "data"))
    return root.resolve()


def global_dir() -> Path:
    return data_root() / "global"


def global_datasets_dir() -> Path:
    return global_dir() / "datasets"


def user_dir(user_id: str) -> Path:
    return data_root() / "users" / safe_path_component(user_id)


def user_sessions_dir(user_id: str) -> Path:
    return user_dir(user_id) / "graph_sessions"


def user_datasets_dir(user_id: str) -> Path:
    return user_dir(user_id) / "datasets"


def user_uploads_dir(user_id: str) -> Path:
    return user_dir(user_id) / "uploads"


def safe_path_component(value: str, default: str = "_") -> str:
    text = str(value or "").strip()
    cleaned = "".join("_" if ord(ch) < 32 or ch in _INVALID_PATH_CHARS else ch for ch in text).rstrip(" .")
    return cleaned or default


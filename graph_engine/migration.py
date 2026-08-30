from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from graph_engine.paths import data_root, global_datasets_dir, user_sessions_dir

_LOG = logging.getLogger(__name__)


def migrate_global_datasets() -> None:
    legacy = Path(os.getenv("GRAPH_DATASET_DIR", "datasets")).resolve()
    if not legacy.is_dir():
        return
    target = global_datasets_dir()
    target.mkdir(parents=True, exist_ok=True)
    for source in legacy.iterdir():
        destination = target / source.name
        if source.is_dir() and not destination.exists():
            try:
                shutil.copytree(source, destination)
            except OSError:
                _LOG.warning("Unable to migrate dataset %s", source, exc_info=True)


def _contains_graph_state(path: Path) -> bool:
    """Return true only for a legacy directory containing durable graph state.

    Chat sessions also used a ``session_meta.json`` file, so the filename alone
    is not enough to distinguish them from Graph Engine snapshots.
    """

    if (path / "graph_current.npz").is_file() or (path / "graph_versions.json").is_file():
        return True
    snapshots = path / "snapshots"
    if snapshots.is_dir() and any(snapshots.glob("v*.npz")):
        return True
    meta_path = path / "session_meta.json"
    if not meta_path.is_file():
        return False
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    remote = payload.get("remote_graph") if isinstance(payload, dict) else None
    return isinstance(remote, dict) and bool(remote.get("graph_key"))


def migrate_user_graph_sessions() -> None:
    """Copy legacy per-user graph snapshots into Graph Engine-owned storage.

    Older releases mixed chat metadata and graph snapshots under ``sessions``
    and ``uploads``.  The four-module layout stores graph state exclusively in
    ``graph_sessions``.  Migration is deliberately copy-only and idempotent:
    an existing destination is never merged, overwritten, or deleted.
    """

    users_root = data_root() / "users"
    if not users_root.is_dir():
        return
    for user_path in users_root.iterdir():
        if not user_path.is_dir():
            continue
        target_root = user_sessions_dir(user_path.name)
        for legacy_name in ("sessions", "uploads"):
            legacy_root = user_path / legacy_name
            if not legacy_root.is_dir():
                continue
            for source in legacy_root.iterdir():
                if not source.is_dir() or not _contains_graph_state(source):
                    continue
                destination = target_root / source.name
                if destination.exists():
                    continue
                try:
                    target_root.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(source, destination)
                except OSError:
                    _LOG.warning(
                        "Unable to migrate graph session %s to %s",
                        source,
                        destination,
                        exc_info=True,
                    )


def migrate_legacy_graph_storage() -> None:
    """Run all safe, copy-only Graph Engine storage migrations."""

    migrate_global_datasets()
    migrate_user_graph_sessions()

"""
One-time migration: copy legacy data into the new data_paths structure.

Called at startup. Safe to call multiple times — skips items that already exist.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

import backend.data_paths as _dp

_LOG = logging.getLogger(__name__)


def _dir_has_content(p: Path) -> bool:
    try:
        return p.is_dir() and any(p.iterdir())
    except Exception:
        return False


def migrate_global_datasets() -> None:
    """Copy datasets/ → data/global/datasets/ (skips existing dataset_ids)."""
    legacy_dir = Path(os.getenv("GRAPH_DATASET_DIR", "datasets")).resolve()
    if not _dir_has_content(legacy_dir):
        return
    target = _dp.global_datasets_dir()
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for src in legacy_dir.iterdir():
        if not src.is_dir():
            continue
        dst = target / src.name
        if dst.exists():
            continue
        try:
            shutil.copytree(src, dst)
            moved += 1
        except Exception as e:
            _LOG.warning("migrate_global_datasets: failed to copy %s → %s: %s", src, dst, e)
    if moved:
        _LOG.info("migrate_global_datasets: copied %d dataset(s) from %s → %s", moved, legacy_dir, target)


def migrate_global_skills() -> None:
    """Copy ~/.igraph/skills/user/ → data/global/skills/ (skips existing skills)."""
    target = _dp.global_skills_dir()
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    configured_home = (
        os.getenv("IGRAPH_HOME", "").strip()
        or os.getenv("GRAPHAGENT_HOME", "").strip()
    )
    homes = [Path(configured_home).expanduser()] if configured_home else [
        Path.home() / ".igraph",
        Path.home() / ".graphagent",
    ]
    for home in homes:
        legacy_dir = home / "skills" / "user"
        if not _dir_has_content(legacy_dir):
            continue
        for src in legacy_dir.iterdir():
            if not src.is_dir():
                continue
            dst = target / src.name
            if dst.exists():
                continue
            try:
                shutil.copytree(src, dst)
                moved += 1
            except Exception as e:
                _LOG.warning("migrate_global_skills: failed to copy %s → %s: %s", src, dst, e)
    if moved:
        _LOG.info("migrate_global_skills: copied %d skill(s) from %s → %s", moved, legacy_dir, target)


def migrate_bundled_skills() -> None:
    """Copy agent_runtime/resources/skills/*/SKILL.md → data/global/skills/.

    Uses dirs_exist_ok=True so code updates propagate to global on restart.
    """
    project_root = Path(__file__).resolve().parent.parent
    src_root = project_root / "agent_runtime" / "resources" / "skills"
    target = _dp.global_skills_dir()
    target.mkdir(parents=True, exist_ok=True)
    moved = 0
    for src in src_root.iterdir():
        if not src.is_dir() or not (src / "SKILL.md").exists():
            continue
        dst = target / src.name
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            moved += 1
        except Exception as e:
            _LOG.warning("migrate_bundled_skills: failed to copy %s → %s: %s", src, dst, e)
    if moved:
        _LOG.info("migrate_bundled_skills: copied %d skill(s) from %s → %s", moved, src_root, target)


def migrate_all() -> None:
    migrate_global_datasets()
    migrate_global_skills()
    migrate_bundled_skills()

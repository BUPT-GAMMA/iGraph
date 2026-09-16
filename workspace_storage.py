"""Shared workspace storage. Standard library only; usable in Backend and Runtime images."""
from __future__ import annotations
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

_ZONES = ("Uploaded", "Ephemeral", "Derived", "Environment")
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_-]")
_UPLOADED_FILENAME = re.compile(r"^[0-9a-f]{32}_(?P<name>.+)$")

def data_root() -> Path:
    configured = Path(os.getenv("IGRAPH_DATA_ROOT", "").strip() or os.getenv("GRAPHAGENT_DATA_ROOT", "").strip() or "data")
    return (configured if configured.is_absolute() else Path(__file__).resolve().parent / configured).resolve()

def _safe_component(value: object, fallback: str) -> str:
    raw = str(value or "")
    cleaned = _SAFE_COMPONENT.sub("_", raw).strip(".") or fallback
    # Sanitising two different identities to the same directory would break
    # isolation, so retain a short identity suffix whenever input changed.
    if cleaned != raw:
        cleaned = f"{cleaned}_{hashlib.sha256(raw.encode()).hexdigest()[:10]}"
    return cleaned


def _ensure_layout(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for zone in _ZONES:
        zone_path = root / zone
        if zone_path.is_symlink():
            raise ValueError("工作区分区不能是符号链接")
        zone_path.mkdir(parents=True, exist_ok=True)


def _workspace_base() -> Path:
    configured = Path(os.getenv("IGRAPH_WORKSPACE_ROOT", "").strip() or (data_root() / "workspaces"))
    return (configured if configured.is_absolute() else Path(__file__).resolve().parent / configured).resolve()


def _workspace_root(user_id: object, session_id: object, *, create: bool) -> Path:
    if not str(user_id or "").strip():
        raise ValueError("当前 Run 缺少 user_id")
    user = _safe_component(user_id, "anonymous")
    session = _safe_component(session_id, "")
    if not str(session_id or "").strip():
        raise ValueError("当前 Run 缺少 session_id")
    base = _workspace_base()
    user_dir = base / user
    if user_dir.is_symlink():
        raise ValueError("工作区身份目录不能是符号链接")
    root = user_dir / session
    if root.is_symlink():
        raise ValueError("工作区身份目录不能是符号链接")
    if not create:
        if root.exists():
            root = root.resolve()
            if root != base and base not in root.parents:
                raise ValueError("工作区身份目录必须位于配置的工作区根内")
        return root
    user_dir.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    if root != base and base not in root.parents:
        raise ValueError("工作区身份目录必须位于配置的工作区根内")
    _ensure_layout(root)
    return root


def workspace_root_for(user_id: object, session_id: object) -> Path:
    """Return and initialise the isolated root for one user/session pair."""
    return _workspace_root(user_id, session_id, create=True)


def delete_workspace(user_id: object, session_id: object) -> bool:
    """Remove one session workspace. Returns True when a directory was removed."""

    root = _workspace_root(user_id, session_id, create=False)
    existed = root.exists()
    if existed:
        shutil.rmtree(root)
    try:
        root.parent.rmdir()
    except OSError:
        pass
    return existed


def delete_user_workspaces(user_id: object) -> bool:
    """Remove every session workspace of one user. Returns True when removed."""

    if not str(user_id or "").strip():
        raise ValueError("缺少 user_id")
    user_dir = _workspace_base() / _safe_component(user_id, "anonymous")
    if user_dir.is_symlink():
        raise ValueError("工作区身份目录不能是符号链接")
    if not user_dir.exists():
        return False
    shutil.rmtree(user_dir)
    return True



def visible_files(user_id, session_id):
    root = workspace_root_for(user_id, session_id)
    files = []
    for zone in ("Uploaded", "Derived"):
        for directory, dirs, names in os.walk(root / zone, followlinks=False):
            base = Path(directory)
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (base / d).is_symlink())
            for name in sorted(names):
                path = base / name
                if name.startswith(".") or path.is_symlink() or not path.is_file():
                    continue
                display_name = name
                if zone == "Uploaded":
                    match = _UPLOADED_FILENAME.match(name)
                    if match:
                        display_name = match.group("name")
                files.append({"path": path.relative_to(root).as_posix(), "name": display_name,
                    "zone": zone, "size": path.stat().st_size, "downloadable": zone == "Derived"})
    return files

def derived_file(user_id, session_id, artifact_path):
    relative = Path(artifact_path)
    if not artifact_path or "\\" in artifact_path or "\x00" in artifact_path or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid artifact path")
    root = workspace_root_for(user_id, session_id) / "Derived"
    target = root
    for part in relative.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError("Symbolic links are not artifacts")
    resolved = target.resolve()
    if root not in resolved.parents or not resolved.is_file():
        raise ValueError("Artifact not found")
    return resolved

def save_upload(user_id, session_id, token, filename, content):
    root = workspace_root_for(user_id, session_id)
    safe = Path(filename.replace("\\", "/")).name
    if safe in {"", ".", ".."}:
        safe = "upload.bin"
    destination = root / "Uploaded" / f"{token}_{safe}"
    fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination, destination.relative_to(root).as_posix()

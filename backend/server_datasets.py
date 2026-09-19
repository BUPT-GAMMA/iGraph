"""Read-only catalog for graph files provisioned directly on the server."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SUPPORTED_GRAPH_SUFFIXES = frozenset(
    {".txt", ".csv", ".tsv", ".edgelist", ".dat", ".json", ".pt", ".npz"}
)
SERVER_DATASET_METADATA_SUFFIX = ".meta.json"
MAX_METADATA_BYTES = 64 * 1024


@dataclass(frozen=True)
class ServerDatasetRecord:
    server_dataset_id: str
    name: str
    original_filename: str
    relative_path: str
    storage_path: str
    file_type: str
    size_bytes: int
    modified_at: float
    revision: str
    directed: bool
    metadata_error: str = ""

    def public_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("storage_path", None)
        return row


class ServerDatasetDirectory:
    """Enumerate regular graph files below one administrator-managed root.

    IDs are derived from relative paths.  Callers never submit or receive an
    absolute path, and every lookup rescans the root so removed files cannot
    keep being used through stale metadata.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_entries: int = 10_000,
        default_directed: bool = True,
    ) -> None:
        self.root = root.resolve()
        self.max_entries = max(1, int(max_entries))
        self.default_directed = bool(default_directed)

    @staticmethod
    def _dataset_id(relative_path: str) -> str:
        digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
        return f"server-{digest}"

    @staticmethod
    def _revision(
        relative_path: str,
        size_bytes: int,
        modified_at_ns: int,
        *,
        directed: bool = True,
    ) -> str:
        """Identify one immutable revision without reading a multi-GB source."""

        identity = f"{relative_path}\0{int(size_bytes)}\0{int(modified_at_ns)}"
        # Preserve existing revision IDs for the historical directed default.
        # An undirected declaration must get a distinct immutable snapshot.
        if not directed:
            identity += "\0directed=false"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _direction_metadata(self, path: Path) -> tuple[bool, str]:
        metadata_path = path.with_name(path.name + SERVER_DATASET_METADATA_SUFFIX)
        try:
            file_stat = metadata_path.lstat()
        except FileNotFoundError:
            return self.default_directed, ""
        except OSError as exc:
            return self.default_directed, f"无法读取方向元数据: {exc}"
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or metadata_path.is_symlink()
            or file_stat.st_size > MAX_METADATA_BYTES
        ):
            return self.default_directed, "方向元数据必须是小于 64 KiB 的普通文件"
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return self.default_directed, f"方向元数据不是有效 JSON: {exc}"
        if not isinstance(payload, dict) or not isinstance(payload.get("directed"), bool):
            return self.default_directed, "方向元数据必须包含布尔字段 directed"
        return bool(payload["directed"]), ""

    def list(self) -> list[ServerDatasetRecord]:
        if not self.root.is_dir():
            return []
        rows: list[ServerDatasetRecord] = []
        for current_root, directory_names, file_names in os.walk(
            self.root, followlinks=False
        ):
            current = Path(current_root)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if not name.startswith(".") and not (current / name).is_symlink()
            )
            for filename in sorted(file_names):
                if filename.startswith("."):
                    continue
                if filename.endswith(SERVER_DATASET_METADATA_SUFFIX):
                    continue
                path = current / filename
                if path.suffix.lower() not in SUPPORTED_GRAPH_SUFFIXES:
                    continue
                try:
                    file_stat = path.lstat()
                    if not stat.S_ISREG(file_stat.st_mode) or path.is_symlink():
                        continue
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(self.root):
                        continue
                    relative_path = resolved.relative_to(self.root).as_posix()
                except (OSError, RuntimeError, ValueError):
                    continue
                directed, metadata_error = self._direction_metadata(resolved)
                rows.append(
                    ServerDatasetRecord(
                        server_dataset_id=self._dataset_id(relative_path),
                        name=path.stem,
                        original_filename=path.name,
                        relative_path=relative_path,
                        storage_path=str(resolved),
                        file_type=path.suffix.lower().lstrip("."),
                        size_bytes=int(file_stat.st_size),
                        modified_at=float(file_stat.st_mtime),
                        revision=self._revision(
                            relative_path,
                            int(file_stat.st_size),
                            int(file_stat.st_mtime_ns),
                            directed=directed,
                        ),
                        directed=directed,
                        metadata_error=metadata_error,
                    )
                )
                if len(rows) >= self.max_entries:
                    return rows
        return rows

    def get(self, server_dataset_id: str) -> ServerDatasetRecord | None:
        target = str(server_dataset_id or "").strip()
        if not target:
            return None
        return next(
            (row for row in self.list() if row.server_dataset_id == target),
            None,
        )

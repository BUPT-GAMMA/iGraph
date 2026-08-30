"""Project-owned dataset metadata and source-file storage."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class ProjectDatasetRecord:
    dataset_id: str
    project_id: str
    user_id: str
    name: str
    original_filename: str
    storage_path: str
    file_type: str
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "ready"
    created_at: float = 0.0
    updated_at: float = 0.0


class ProjectDatasetDirectory:
    """Atomic JSON metadata repository with project-scoped source storage."""

    def __init__(self, metadata_path: Path, storage_root: Path) -> None:
        self.metadata_path = metadata_path
        self.storage_root = storage_root
        self._lock = threading.RLock()

    @staticmethod
    def _safe_filename(filename: str) -> str:
        basename = Path(str(filename or "dataset.bin")).name.strip() or "dataset.bin"
        safe = _SAFE_FILENAME.sub("_", basename).strip("._")
        return safe[:180] or "dataset.bin"

    def _load(self) -> dict[str, ProjectDatasetRecord]:
        if not self.metadata_path.is_file():
            return {}
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            return {
                item["dataset_id"]: ProjectDatasetRecord(**item)
                for item in payload.get("datasets", [])
                if isinstance(item, dict)
                and item.get("dataset_id")
                and item.get("project_id")
                and item.get("user_id")
            }
        except (OSError, ValueError, TypeError):
            return {}

    def _write(self, records: dict[str, ProjectDatasetRecord]) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "datasets": [asdict(records[key]) for key in sorted(records)],
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix="project-datasets-",
            suffix=".json",
            dir=str(self.metadata_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.metadata_path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def list(self, project_id: str, user_id: str) -> list[ProjectDatasetRecord]:
        rows = [
            item
            for item in self._load().values()
            if item.project_id == project_id and item.user_id == user_id
        ]
        return sorted(rows, key=lambda item: (item.created_at, item.dataset_id))

    def get(
        self,
        dataset_id: str,
        user_id: str,
        project_id: str | None = None,
    ) -> ProjectDatasetRecord | None:
        item = self._load().get(str(dataset_id or ""))
        if item is None or item.user_id != user_id:
            return None
        if project_id is not None and item.project_id != project_id:
            return None
        return item

    def create(
        self,
        *,
        project_id: str,
        user_id: str,
        name: str,
        original_filename: str,
        content: bytes,
    ) -> ProjectDatasetRecord:
        with self._lock:
            records = self._load()
            dataset_id = str(uuid.uuid4())
            safe_name = self._safe_filename(original_filename)
            dataset_dir = self.storage_root / project_id / "datasets" / dataset_id
            dataset_dir.mkdir(parents=True, exist_ok=False)
            source_path = dataset_dir / safe_name
            fd, tmp_name = tempfile.mkstemp(prefix="source-", dir=str(dataset_dir))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, source_path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            now = time.time()
            record = ProjectDatasetRecord(
                dataset_id=dataset_id,
                project_id=project_id,
                user_id=user_id,
                name=name.strip() or Path(safe_name).stem or "Dataset",
                original_filename=Path(original_filename).name or safe_name,
                storage_path=str(source_path.resolve()),
                file_type=Path(safe_name).suffix.lower().lstrip(".") or "binary",
                status="ready",
                created_at=now,
                updated_at=now,
            )
            records[dataset_id] = record
            try:
                self._write(records)
            except Exception:
                shutil.rmtree(dataset_dir, ignore_errors=True)
                raise
            return record

    def update(
        self,
        dataset_id: str,
        user_id: str,
        *,
        project_id: str | None = None,
        name: str | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProjectDatasetRecord | None:
        with self._lock:
            records = self._load()
            item = records.get(dataset_id)
            if item is None or item.user_id != user_id:
                return None
            if project_id is not None and item.project_id != project_id:
                return None
            if name is not None:
                item.name = name.strip()
            if status is not None:
                item.status = status
            if metadata is not None:
                item.metadata = dict(metadata)
            item.updated_at = time.time()
            records[dataset_id] = item
            self._write(records)
            return item

    def delete(self, dataset_id: str, user_id: str, project_id: str | None = None) -> bool:
        with self._lock:
            records = self._load()
            item = records.get(dataset_id)
            if item is None or item.user_id != user_id:
                return False
            if project_id is not None and item.project_id != project_id:
                return False
            del records[dataset_id]
            self._write(records)
            shutil.rmtree(Path(item.storage_path).parent, ignore_errors=True)
            return True

    def delete_project(self, project_id: str, user_id: str) -> int:
        rows = self.list(project_id, user_id)
        for item in rows:
            self.delete(item.dataset_id, user_id, project_id)
        project_dir = self.storage_root / project_id
        shutil.rmtree(project_dir, ignore_errors=True)
        return len(rows)

    def delete_user(self, user_id: str) -> int:
        rows = [item for item in self._load().values() if item.user_id == user_id]
        for item in rows:
            self.delete(item.dataset_id, user_id, item.project_id)
        return len(rows)

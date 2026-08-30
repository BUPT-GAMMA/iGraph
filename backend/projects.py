"""Backend-owned project directory for organizing AgentOS sessions."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class ProjectRecord:
    project_id: str
    user_id: str
    name: str
    description: str = ""
    is_default: bool = False
    default_dataset_id: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


class ProjectDirectory:
    """Small atomic JSON repository consistent with Backend's user store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    @staticmethod
    def default_project_id(user_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"igraph:general-project:{user_id}"))

    def _load(self) -> dict[str, ProjectRecord]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return {
                item["project_id"]: ProjectRecord(**item)
                for item in payload.get("projects", [])
                if isinstance(item, dict) and item.get("project_id") and item.get("user_id")
            }
        except (OSError, ValueError, TypeError):
            return {}

    def _write(self, records: dict[str, ProjectRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "projects": [asdict(records[key]) for key in sorted(records)],
        }
        fd, tmp_name = tempfile.mkstemp(prefix="projects-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def ensure_default(self, user_id: str) -> ProjectRecord:
        with self._lock:
            records = self._load()
            project_id = self.default_project_id(user_id)
            project = records.get(project_id)
            if project is None:
                now = time.time()
                project = ProjectRecord(
                    project_id=project_id,
                    user_id=user_id,
                    name="General",
                    description="Default workspace for conversations",
                    is_default=True,
                    created_at=now,
                    updated_at=now,
                )
                records[project_id] = project
                self._write(records)
            return project

    def list(self, user_id: str) -> list[ProjectRecord]:
        self.ensure_default(user_id)
        projects = [item for item in self._load().values() if item.user_id == user_id]
        return sorted(projects, key=lambda item: (not item.is_default, -item.updated_at, item.name.lower()))

    def get(self, project_id: str, user_id: str) -> ProjectRecord | None:
        self.ensure_default(user_id)
        project = self._load().get(project_id)
        return project if project and project.user_id == user_id else None

    def create(self, user_id: str, name: str, description: str = "") -> ProjectRecord:
        with self._lock:
            records = self._load()
            now = time.time()
            project = ProjectRecord(
                project_id=str(uuid.uuid4()),
                user_id=user_id,
                name=name.strip(),
                description=description.strip(),
                created_at=now,
                updated_at=now,
            )
            records[project.project_id] = project
            self._write(records)
            return project

    def update(self, project_id: str, user_id: str, *, name: str | None = None, description: str | None = None) -> ProjectRecord | None:
        with self._lock:
            records = self._load()
            project = records.get(project_id)
            if project is None or project.user_id != user_id:
                return None
            if name is not None:
                project.name = name.strip()
            if description is not None:
                project.description = description.strip()
            project.updated_at = time.time()
            records[project_id] = project
            self._write(records)
            return project

    def set_default_dataset(
        self,
        project_id: str,
        user_id: str,
        dataset_id: str | None,
    ) -> ProjectRecord | None:
        with self._lock:
            records = self._load()
            project = records.get(project_id)
            if project is None or project.user_id != user_id:
                return None
            project.default_dataset_id = str(dataset_id).strip() if dataset_id else None
            project.updated_at = time.time()
            records[project_id] = project
            self._write(records)
            return project

    def delete(self, project_id: str, user_id: str) -> bool:
        with self._lock:
            records = self._load()
            project = records.get(project_id)
            if project is None or project.user_id != user_id or project.is_default:
                return False
            del records[project_id]
            self._write(records)
            return True

    def delete_user(self, user_id: str) -> int:
        with self._lock:
            records = self._load()
            matched = [project_id for project_id, project in records.items() if project.user_id == user_id]
            for project_id in matched:
                del records[project_id]
            if matched:
                self._write(records)
            return len(matched)

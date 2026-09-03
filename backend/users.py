"""Backend-owned user directory.

The existing X-User-ID login contract is retained.  The former allow-list is
used only to seed this repository; Agent Runtime never reads user files.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from backend.model_policy import SUPPORTED_MODELS


@dataclass
class UserRecord:
    user_id: str
    enabled: bool = True
    roles: list[str] = field(default_factory=lambda: ["user"])
    allowed_models: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class UserDirectory:
    def __init__(
        self,
        path: Path,
        seed_path: Path,
        admin_user_id: str,
        default_admin_model: str = "qwen3.6-plus",
        default_user_model: str = "qwen3.6-plus",
    ) -> None:
        self.path = path
        self.seed_path = seed_path
        self.admin_user_id = admin_user_id
        self.default_admin_model = default_admin_model
        self.default_user_model = default_user_model
        self._lock = threading.RLock()

    @staticmethod
    def _ordered_models(default: str, values) -> list[str]:
        available = sorted({str(value) for value in values if str(value) in SUPPORTED_MODELS})
        selected = default if default in available else ("qwen3.6-plus" if "qwen3.6-plus" in available else "")
        return ([selected] if selected else []) + [value for value in available if value != selected]

    def _seed_ids(self) -> list[str]:
        try:
            return [
                line.strip()
                for line in self.seed_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        except OSError:
            return []

    def _load(self) -> dict[str, UserRecord]:
        with self._lock:
            if self.path.is_file():
                try:
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                    return {
                        item["user_id"]: UserRecord(**item)
                        for item in payload.get("users", [])
                        if isinstance(item, dict) and item.get("user_id")
                    }
                except (OSError, ValueError, TypeError):
                    pass
            records: dict[str, UserRecord] = {}
            for user_id in self._seed_ids():
                is_admin = user_id == self.admin_user_id
                records[user_id] = UserRecord(
                    user_id=user_id,
                    roles=["admin", "user"] if is_admin else ["user"],
                    allowed_models=(
                        self._ordered_models(self.default_admin_model, SUPPORTED_MODELS)
                        if is_admin
                        else self._default_user_models()
                    ),
                )
            return records

    def _default_user_models(self) -> list[str]:
        return self._ordered_models(
            self.default_user_model,
            (model for model in SUPPORTED_MODELS if not model.startswith(("gpt-", "claude-"))),
        )

    def _write(self, records: dict[str, UserRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "users": [asdict(records[key]) for key in sorted(records)]}
        fd, tmp_name = tempfile.mkstemp(prefix="users-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def get(self, user_id: str) -> UserRecord | None:
        return self._load().get(str(user_id or "").strip())

    def list(self) -> list[UserRecord]:
        return list(self._load().values())

    def save(self, record: UserRecord) -> UserRecord:
        with self._lock:
            records = self._load()
            existing = records.get(record.user_id)
            if existing:
                record.created_at = existing.created_at
            record.roles = list(dict.fromkeys(str(role).strip() for role in record.roles if str(role).strip())) or ["user"]
            if record.user_id == self.admin_user_id and "admin" not in record.roles:
                record.roles.insert(0, "admin")
            record.updated_at = time.time()
            allowed = [model for model in record.allowed_models if model in SUPPORTED_MODELS]
            record.allowed_models = allowed or (
                self._ordered_models(self.default_admin_model, SUPPORTED_MODELS)
                if "admin" in record.roles
                else self._default_user_models()
            )
            records[record.user_id] = record
            self._write(records)
            return record

    def delete(self, user_id: str) -> bool:
        with self._lock:
            records = self._load()
            if user_id not in records:
                return False
            del records[user_id]
            self._write(records)
            return True

"""Backend-owned user directory.

Legacy IDs are retained only to preserve old resource ownership. All accounts
have the same role and model defaults; browser access requires password login.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from backend.model_policy import SUPPORTED_MODELS


@dataclass
class UserRecord:
    user_id: str
    enabled: bool = True
    username: str = ""
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
        # The legacy constructor arguments remain accepted for existing deployments.
        self.default_user_model = default_user_model
        self._lock = threading.RLock()

    @contextmanager
    def _mutation_lock(self):
        """Serialize atomic JSON mutations across Backend worker processes."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_suffix(self.path.suffix + ".lock")
            with lock_path.open("a+b") as handle:
                os.chmod(lock_path, 0o600)
                if os.name == "nt":
                    import msvcrt
                    if handle.seek(0, os.SEEK_END) == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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
                        item["user_id"]: self._ordinary_user(UserRecord(**item))
                        for item in payload.get("users", [])
                        if isinstance(item, dict) and item.get("user_id")
                    }
                except (OSError, ValueError, TypeError):
                    pass
            records: dict[str, UserRecord] = {}
            for user_id in self._seed_ids():
                records[user_id] = UserRecord(
                    user_id=user_id,
                    roles=["user"],
                    allowed_models=self._default_user_models(),
                )
            return records

    def _default_user_models(self) -> list[str]:
        return self._ordered_models(
            self.default_user_model,
            SUPPORTED_MODELS,
        )

    def _ordinary_user(self, record: UserRecord) -> UserRecord:
        # Legacy role and model allow-list fields no longer grant privileges.
        record.roles = ["user"]
        record.allowed_models = self._default_user_models()
        return record

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

    def create(self, record: UserRecord) -> UserRecord:
        with self._mutation_lock():
            records = self._load()
            if any(key.lower() == record.user_id.lower() for key in records):
                raise ValueError("用户已存在")
            record.roles = ["user"]
            record.allowed_models = self._default_user_models()
            record.created_at = record.updated_at = time.time()
            records[record.user_id] = record
            self._write(records)
            return record

    def save(self, record: UserRecord) -> UserRecord:
        with self._mutation_lock():
            records = self._load()
            existing = records.get(record.user_id)
            if existing:
                record.created_at = existing.created_at
            self._ordinary_user(record)
            record.updated_at = time.time()
            records[record.user_id] = record
            self._write(records)
            return record

    def delete(self, user_id: str) -> bool:
        with self._mutation_lock():
            records = self._load()
            if user_id not in records:
                return False
            del records[user_id]
            self._write(records)
            return True

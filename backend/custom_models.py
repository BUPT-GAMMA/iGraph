"""Per-user storage for OpenAI-compatible custom model endpoints."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from backend.model_policy import SUPPORTED_MODELS


MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")


@dataclass
class CustomModelRecord:
    user_id: str
    model_id: str
    display_name: str
    base_url: str
    api_key: str = ""
    provider: str = "openai-compatible"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class CustomModelDirectory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _load(self) -> list[CustomModelRecord]:
        with self._lock:
            if not self.path.is_file():
                return []
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return []
            rows = payload.get("models", []) if isinstance(payload, dict) else []
            result: list[CustomModelRecord] = []
            for item in rows:
                if not isinstance(item, dict) or not item.get("user_id") or not item.get("model_id"):
                    continue
                try:
                    result.append(CustomModelRecord(**item))
                except (TypeError, ValueError):
                    continue
            return result

    def _write(self, rows: list[CustomModelRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="custom-models-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "models": [asdict(row) for row in rows]}, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def list(self, user_id: str) -> list[CustomModelRecord]:
        uid = str(user_id or "").strip()
        return [row for row in self._load() if row.user_id == uid]

    def get(self, user_id: str, model_id: str) -> CustomModelRecord | None:
        uid = str(user_id or "").strip()
        mid = str(model_id or "").strip()
        return next((row for row in self._load() if row.user_id == uid and row.model_id == mid), None)

    def save(self, record: CustomModelRecord) -> CustomModelRecord:
        record.user_id = str(record.user_id or "").strip()
        record.model_id = str(record.model_id or "").strip()
        record.display_name = str(record.display_name or record.model_id).strip()[:120]
        record.base_url = str(record.base_url or "").strip().rstrip("/")
        record.api_key = str(record.api_key or "").strip()
        if not record.user_id or not MODEL_ID_RE.fullmatch(record.model_id):
            raise ValueError("模型 ID 只能包含字母、数字以及 . _ : / -，长度不超过 120")
        if record.model_id in SUPPORTED_MODELS:
            raise ValueError("自定义模型 ID 不能与内置模型重名")
        if not record.display_name:
            raise ValueError("模型显示名称不能为空")
        if not (record.base_url.startswith("http://") or record.base_url.startswith("https://")):
            raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
        with self._lock:
            rows = self._load()
            existing = next((row for row in rows if row.user_id == record.user_id and row.model_id == record.model_id), None)
            if not record.api_key and existing is None:
                raise ValueError("首次配置自定义模型时必须填写 API Key")
            if existing:
                record.created_at = existing.created_at
                if not record.api_key:
                    record.api_key = existing.api_key
                rows = [row for row in rows if not (row.user_id == record.user_id and row.model_id == record.model_id)]
            record.updated_at = time.time()
            rows.append(record)
            self._write(rows)
            return record

    def delete(self, user_id: str, model_id: str) -> bool:
        uid = str(user_id or "").strip()
        mid = str(model_id or "").strip()
        with self._lock:
            rows = self._load()
            remaining = [row for row in rows if not (row.user_id == uid and row.model_id == mid)]
            if len(remaining) == len(rows):
                return False
            self._write(remaining)
            return True


def public_custom_model(record: CustomModelRecord) -> dict:
    return {
        "model_id": record.model_id,
        "display_name": record.display_name,
        "provider": record.provider,
        "base_url": record.base_url,
        "has_api_key": bool(record.api_key),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }

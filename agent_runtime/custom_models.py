"""In-memory custom model endpoints supplied by the Backend trust boundary."""

from __future__ import annotations

import threading


_LOCK = threading.RLock()
_CONFIGS: dict[tuple[str, str], dict[str, str]] = {}


def register_custom_model(user_id: str, config: dict) -> None:
    uid = str(user_id or "").strip()
    model_id = str(config.get("model_id") or "").strip()
    if not uid or not model_id:
        raise ValueError("自定义模型缺少用户或模型 ID")
    with _LOCK:
        _CONFIGS[(uid, model_id)] = {
            "model_id": model_id,
            "base_url": str(config.get("base_url") or "").strip().rstrip("/"),
            "api_key": str(config.get("api_key") or "").strip(),
            "provider": str(config.get("provider") or "openai-compatible").strip(),
        }


def get_custom_model(user_id: str, model_id: str) -> dict[str, str] | None:
    with _LOCK:
        value = _CONFIGS.get((str(user_id or "").strip(), str(model_id or "").strip()))
        return dict(value) if value else None


def remove_custom_model(user_id: str, model_id: str) -> None:
    with _LOCK:
        _CONFIGS.pop((str(user_id or "").strip(), str(model_id or "").strip()), None)

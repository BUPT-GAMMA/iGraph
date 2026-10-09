"""Private single-workspace construction connection, separate from graph artifacts."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from urllib.parse import urlsplit

from pydantic import BaseModel, Field
from graph_engine.paths import data_root

_lock = threading.RLock()


class ConstructionSettingsWrite(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=8192)
    clear_api_key: bool = False


def settings_path():
    return data_root() / "configuration" / "construction-model.json"


def read_settings():
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def public_settings():
    saved = read_settings()
    return {
        "model": saved.get("model") or os.getenv("GRAPH_CONSTRUCTION_MODEL") or "deepseek-v4-pro",
        "has_base_url": bool(saved.get("base_url")),
        "has_api_key": bool(saved.get("api_key")),
        "has_server_config": any(os.getenv(p + "_BASE_URL") and os.getenv(p + "_API_KEY")
            for p in ("GRAPH_CONSTRUCTION", "LLM", "OPENAI", "DEEPSEEK", "CLAUDE", "ANTHROPIC", "QWEN", "DASHSCOPE")),
    }


def save_settings(request: ConstructionSettingsWrite):
    model, base, key = request.model.strip(), request.base_url.strip(), request.api_key.strip()
    if not model or any(c.isspace() for c in model):
        raise ValueError("模型 ID 不能为空或包含空白。")
    if base:
        try:
            parsed = urlsplit(base)
            valid = (parsed.scheme in {"https", "http"} and parsed.hostname and
                     not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                     and not any(c.isspace() for c in base))
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("请输入有效的 HTTP(S) API 基础地址，不要包含账号、查询参数或片段。")
    if key and any(c.isspace() for c in key):
        raise ValueError("API Key 不能包含空白。")
    with _lock:
        saved = read_settings()
        saved["model"] = model
        if base:
            # Accept a gateway origin or its full OpenAI-compatible base path.
            saved["base_url"] = base.rstrip("/") + ("/v1" if not parsed.path.strip("/") else "")
        if key or request.clear_api_key:
            saved["api_key"] = "" if request.clear_api_key else key
        # Explicit clearing must not silently restore an environment key.
        if ("base_url" in saved) != ("api_key" in saved):
            raise ValueError("首次配置请同时填写 API URL 和 API Key。")
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".construction-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(saved, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return public_settings()

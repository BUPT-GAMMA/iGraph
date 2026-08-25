from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from typing import Any, Iterable

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                else:
                    parts.append(_content_text(item))
            else:
                parts.append(str(item))
        return "".join(parts)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(value)
    return str(value)


@lru_cache(maxsize=8)
def _encoding_for_model(model: str | None):
    try:
        import tiktoken  # type: ignore
    except Exception:
        return None
    try:
        if model:
            return tiktoken.encoding_for_model(model)
    except Exception:
        pass
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def estimate_text_tokens(text: str, *, model: str | None = None) -> int:
    text = str(text or "")
    if not text:
        return 0
    enc = _encoding_for_model(model)
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass

    cjk_count = len(_CJK_RE.findall(text))
    non_cjk = _CJK_RE.sub("", text)
    # Conservative fallback: CJK chars are commonly near one token each, while
    # Latin/code text averages roughly four chars per token.
    return cjk_count + math.ceil(len(non_cjk) / 4)


def count_message_tokens(
    messages: Iterable[dict],
    *,
    model: str | None = None,
    include_system: bool = False,
) -> int:
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        if role == "system" and not include_system:
            continue
        total += 4
        total += estimate_text_tokens(role, model=model)
        total += estimate_text_tokens(_content_text(msg.get("content")), model=model)
        if msg.get("tool_calls"):
            total += estimate_text_tokens(_content_text(msg.get("tool_calls")), model=model)
        if msg.get("name"):
            total += estimate_text_tokens(str(msg.get("name") or ""), model=model)
    return total


def context_usage(
    messages: Iterable[dict],
    *,
    limit: int,
    model: str | None = None,
) -> dict:
    safe_limit = max(int(limit or 1), 1)
    current = count_message_tokens(messages, model=model, include_system=False)
    percent = min(999.0, round((current / safe_limit) * 100, 2))
    if current >= safe_limit:
        status = "critical"
    elif current >= safe_limit * 0.8:
        status = "warning"
    else:
        status = "ok"
    return {
        "current_tokens": current,
        "token_limit": safe_limit,
        "percent": percent,
        "status": status,
        "excludes": ["system"],
    }

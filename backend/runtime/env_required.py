from __future__ import annotations

import os


def require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"缺少环境变量: {name}（请在 .env 中配置）")
    return value.strip()


def require_any_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    joined = ", ".join(names)
    raise ValueError(f"缺少环境变量（请在 .env 中配置其一）: {joined}")


def require_int_env(name: str) -> int:
    raw = require_env(name)
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"环境变量 {name} 必须是整数，当前值: {raw}") from e

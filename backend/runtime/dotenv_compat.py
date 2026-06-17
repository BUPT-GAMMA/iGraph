from __future__ import annotations

import os
from pathlib import Path


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _load_env_file(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False

    loaded = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = _strip_quotes(value.strip())
        if key not in os.environ:
            os.environ[key] = value
        loaded = True
    return loaded


def load_dotenv_compat() -> bool:
    try:
        from dotenv import load_dotenv

        return bool(load_dotenv())
    except Exception:
        pass

    cwd_env = Path.cwd() / ".env"
    if _load_env_file(cwd_env):
        return True

    project_env = Path(__file__).resolve().parents[2] / ".env"
    return _load_env_file(project_env)

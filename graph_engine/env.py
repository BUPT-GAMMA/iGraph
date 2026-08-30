from __future__ import annotations

import os

from dotenv import load_dotenv


def load_environment() -> None:
    load_dotenv(override=False)


def require_env(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def require_int_env(name: str, default: int | None = None) -> int:
    raw = os.getenv(name, "" if default is None else str(default)).strip()
    if not raw:
        raise RuntimeError(f"Missing required environment variable: {name}")
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def setup_no_proxy() -> None:
    hosts = {"127.0.0.1", "localhost", "graph-engine-api", "graph-engine-mcp"}
    current = os.getenv("NO_PROXY", os.getenv("no_proxy", ""))
    hosts.update(item.strip() for item in current.split(",") if item.strip())
    value = ",".join(sorted(hosts))
    os.environ["NO_PROXY"] = value
    os.environ["no_proxy"] = value


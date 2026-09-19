"""Shared multiprocessing policy for every large-graph process pool."""

from __future__ import annotations

import multiprocessing
import os
from multiprocessing.context import BaseContext


def process_context() -> BaseContext:
    """Return the configured safe context for job and nested worker processes.

    Large jobs are launched from a service that already owns threads and
    SQLite connections.  Falling back to the platform default (usually
    ``fork`` on Linux) can inherit locked runtime state and deadlock a nested
    pool.  One setting therefore governs both the durable job process and all
    of its map/reduce children.
    """

    name = os.getenv("LARGE_GRAPH_MP_START_METHOD", "spawn").strip().lower()
    if name not in {"spawn", "forkserver"}:
        raise ValueError(
            "LARGE_GRAPH_MP_START_METHOD 必须是 spawn 或 forkserver；"
            "大图 Worker 禁止在线程化服务内使用 fork"
        )
    return multiprocessing.get_context(name)

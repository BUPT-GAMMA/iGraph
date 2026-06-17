"""
macOS 系统代理兼容：防止 localhost/127.0.0.1 请求被系统代理劫持。

在所有入口（main.py、api_gateway/__main__.py、mcp_service/__main__.py）
调用一次 setup_no_proxy()。
"""

from __future__ import annotations

import os


def setup_no_proxy() -> None:
    """确保 localhost 和 127.0.0.1 不经过系统代理。"""
    for key in ("NO_PROXY", "no_proxy"):
        existing = os.environ.get(key, "")
        entries = [e.strip() for e in existing.split(",") if e.strip()]
        changed = False
        for host in ("localhost", "127.0.0.1"):
            if host not in entries:
                entries.append(host)
                changed = True
        if changed:
            os.environ[key] = ",".join(entries)

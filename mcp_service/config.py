"""MCP server runtime 配置。"""

from __future__ import annotations

from dataclasses import dataclass

from backend.runtime.env_required import require_env, require_int_env


@dataclass(frozen=True)
class MCPConfig:
    host: str
    port: int
    graph_data_url: str

    @classmethod
    def from_env(cls) -> "MCPConfig":
        return cls(
            host=require_env("MCP_HOST"),
            port=require_int_env("MCP_PORT"),
            graph_data_url=require_env("GRAPH_DATA_SERVER_URL"),
        )

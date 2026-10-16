"""Configuration for the standalone Agent Runtime service.

The Agent Runtime owns AgentOS, sessions, runs, learning and skills.  It does
not own users or graph data; those identities and resources are supplied by
Backend and Graph Engine respectively.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _required(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class AgentRuntimeConfig:
    host: str
    port: int
    database_url: str
    mcp_url: str
    llm_api_key: str
    llm_base_url: str
    default_model: str
    embedding_model: str
    embedding_api_key: str
    embedding_base_url: str
    skills_root: Path
    telemetry: bool
    tracing: bool
    authorization: bool
    job_poll_interval_seconds: float = 2.0
    job_wait_timeout_seconds: float = 0.0
    remote_graph_secret_key: str = field(default='', repr=False)
    # Used only when no explicit key is configured; created on first use.
    remote_graph_secret_key_file: Path | None = None
    remote_graph_timeout_seconds: float = 30
    remote_graph_max_rows: int = 10000
    remote_graph_max_bytes: int = 10 * 1024 * 1024
    remote_graph_preview_rows: int = 100
    remote_graph_preview_bytes: int = 64 * 1024
    llm_timeout_seconds: float = 180.0

    @classmethod
    def from_env(cls) -> "AgentRuntimeConfig":
        data_root = Path(os.getenv("IGRAPH_DATA_ROOT", "data")).resolve()
        skills_root = Path(
            os.getenv("AGENT_RUNTIME_SKILLS_ROOT", str(data_root / "agent_runtime" / "skills"))
        ).resolve()
        return cls(
            host=os.getenv("AGENT_RUNTIME_HOST", "0.0.0.0"),
            port=int(os.getenv("AGENT_RUNTIME_PORT", "21112")),
            database_url=_required(
                "AGENT_RUNTIME_DATABASE_URL",
                "postgresql+psycopg://igraph:igraph@postgres:5432/igraph",
            ),
            mcp_url=_required("GRAPH_ENGINE_MCP_URL", os.getenv("MCP_SERVER_URL", "")),
            # Provider-specific credentials are resolved lazily per selected
            # model, retaining the legacy QWEN/DEEPSEEK/ARK fallbacks.
            llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
            llm_base_url=os.getenv("LLM_BASE_URL", "").strip(),
            default_model=os.getenv("IGRAPH_DEFAULT_MODEL", "qwen3.6-plus").strip(),
            embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small").strip(),
            embedding_api_key=os.getenv(
                "EMBEDDING_API_KEY",
                os.getenv("OPENAI_API_KEY", os.getenv("LLM_API_KEY", os.getenv("QWEN_API_KEY", ""))),
            ).strip(),
            embedding_base_url=os.getenv(
                "EMBEDDING_BASE_URL",
                os.getenv("OPENAI_BASE_URL", os.getenv("LLM_BASE_URL", os.getenv("QWEN_BASE_URL", ""))),
            ).strip(),
            skills_root=skills_root,
            telemetry=_env_bool("AGNO_TELEMETRY", False),
            tracing=_env_bool("AGENT_RUNTIME_TRACING", True),
            authorization=_env_bool("AGENT_RUNTIME_AUTHORIZATION", False),
            remote_graph_secret_key=os.getenv("IGRAPH_REMOTE_GRAPH_SECRET_KEY", "").strip(),
            remote_graph_secret_key_file=Path(os.getenv(
                "IGRAPH_REMOTE_GRAPH_SECRET_KEY_FILE",
                str(data_root / "configuration" / "remote-graph-secret.key"),
            )).resolve(),
            remote_graph_timeout_seconds=float(os.getenv('IGRAPH_REMOTE_GRAPH_TIMEOUT_SECONDS', '30')),
            remote_graph_max_rows=int(os.getenv('IGRAPH_REMOTE_GRAPH_MAX_ROWS', '10000')),
            remote_graph_max_bytes=int(os.getenv('IGRAPH_REMOTE_GRAPH_MAX_BYTES', str(10 * 1024 * 1024))),
            remote_graph_preview_rows=int(os.getenv('IGRAPH_REMOTE_GRAPH_PREVIEW_ROWS', '100')),
            remote_graph_preview_bytes=int(os.getenv('IGRAPH_REMOTE_GRAPH_PREVIEW_BYTES', str(64 * 1024))),
            job_poll_interval_seconds=float(
                os.getenv("AGENT_RUNTIME_JOB_POLL_INTERVAL_SECONDS", "2")
            ),
            job_wait_timeout_seconds=float(
                os.getenv("AGENT_RUNTIME_JOB_WAIT_TIMEOUT_SECONDS", "0")
            ),
            llm_timeout_seconds=max(1.0, float(os.getenv("LLM_TIMEOUT_SECONDS", "180"))),
        )

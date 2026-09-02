"""Configuration for the public Backend/BFF module."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BackendConfig:
    host: str
    port: int
    agent_runtime_url: str
    graph_engine_url: str
    agent_id: str
    request_timeout: float
    stream_timeout: float
    users_file: Path
    projects_file: Path
    supported_ids_file: Path
    admin_user_id: str
    default_admin_model: str
    default_user_model: str
    custom_models_file: Path | None = None

    @classmethod
    def from_env(cls) -> "BackendConfig":
        project_root = Path(__file__).resolve().parent.parent
        data_root = Path(os.getenv("IGRAPH_DATA_ROOT", "data")).resolve()
        supported = Path(
            os.getenv("IGRAPH_SUPPORTED_IDS_FILE", str(project_root / "config" / "supported_ids.txt"))
        ).resolve()
        return cls(
            host=os.getenv("BACKEND_HOST", os.getenv("API_SERVER_HOST", "0.0.0.0")),
            port=int(os.getenv("BACKEND_PORT", os.getenv("API_SERVER_PORT", "21109"))),
            agent_runtime_url=os.getenv("AGENT_RUNTIME_URL", "http://agent-runtime:21112").rstrip("/"),
            graph_engine_url=os.getenv("GRAPH_ENGINE_URL", os.getenv("GRAPH_DATA_SERVER_URL", "http://graph-engine-api:21111")).rstrip("/"),
            agent_id=os.getenv("AGENT_RUNTIME_AGENT_ID", "igraph-agent"),
            request_timeout=float(os.getenv("BACKEND_REQUEST_TIMEOUT_SECONDS", "120")),
            stream_timeout=float(os.getenv("BACKEND_STREAM_TIMEOUT_SECONDS", "1800")),
            users_file=Path(os.getenv("BACKEND_USERS_FILE", str(data_root / "backend" / "users.json"))).resolve(),
            projects_file=Path(os.getenv("BACKEND_PROJECTS_FILE", str(data_root / "backend" / "projects.json"))).resolve(),
            supported_ids_file=supported,
            admin_user_id=os.getenv("IGRAPH_ADMIN_USER_ID", "admin").strip() or "admin",
            default_admin_model=os.getenv("IGRAPH_DEFAULT_ADMIN_MODEL", "qwen3.6-plus").strip(),
            default_user_model=os.getenv("IGRAPH_DEFAULT_USER_MODEL", "qwen3.6-plus").strip(),
            custom_models_file=Path(os.getenv("BACKEND_CUSTOM_MODELS_FILE", str(data_root / "backend" / "custom_models.json"))).resolve(),
        )

"""Standalone AgentOS composition root for iGraph."""

from __future__ import annotations

from fastapi import FastAPI
from agno.os import AgentOS

from agent_runtime.agno_factory import create_db, create_igraph_factory
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.skill_api import create_skill_router


def create_app(config: AgentRuntimeConfig | None = None) -> FastAPI:
    settings = config or AgentRuntimeConfig.from_env()
    db = create_db(settings)
    base_app = FastAPI(title="iGraph Agent Runtime", version="3.0.0")
    base_app.include_router(create_skill_router(settings))

    @base_app.get("/internal/health")
    def internal_health():
        return {
            "status": "ok",
            "module": "agent-runtime",
            "runtime": "AgentOS",
            "agent_id": "igraph-agent",
        }

    agent_os = AgentOS(
        id="igraph-agent-os",
        name="iGraph Agent Runtime",
        description="AgentOS runtime for iGraph sessions, memory, learning and skills",
        agents=[create_igraph_factory(settings, db)],
        db=db,
        base_app=base_app,
        on_route_conflict="preserve_agentos",
        authorization=settings.authorization,
        tracing=settings.tracing,
        telemetry=settings.telemetry,
    )
    return agent_os.get_app()


app = create_app()


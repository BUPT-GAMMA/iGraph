"""Standalone AgentOS composition root for iGraph."""

from __future__ import annotations

from fastapi import FastAPI
from agno.os import AgentOS
from pydantic import BaseModel, Field

from agent_runtime.agno_factory import create_db, create_igraph_factory
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.custom_models import register_custom_model, remove_custom_model
from agent_runtime.skill_api import create_skill_router
from agent_runtime.recovery import create_recovery_router


class RuntimeCustomModelReq(BaseModel):
    user_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    provider: str = "openai-compatible"


def create_app(config: AgentRuntimeConfig | None = None) -> FastAPI:
    settings = config or AgentRuntimeConfig.from_env()
    db = create_db(settings)
    base_app = FastAPI(title="iGraph Agent Runtime", version="3.0.0")
    base_app.include_router(create_skill_router(settings))
    base_app.include_router(create_recovery_router(db))

    @base_app.get("/internal/health")
    def internal_health():
        return {
            "status": "ok",
            "module": "agent-runtime",
            "runtime": "AgentOS",
            "agent_id": "igraph-agent",
        }

    @base_app.put("/internal/models/custom")
    def configure_custom_model(req: RuntimeCustomModelReq):
        register_custom_model(req.user_id, req.model_dump())
        return {"success": True, "model_id": req.model_id}

    @base_app.delete("/internal/models/custom/{user_id}/{model_id:path}")
    def forget_custom_model(user_id: str, model_id: str):
        remove_custom_model(user_id, model_id)
        return {"success": True, "model_id": model_id}

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

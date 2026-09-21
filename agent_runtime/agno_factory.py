"""Request-scoped native Agno Agent factory."""

from __future__ import annotations

import os
from typing import Literal

from agno.agent import Agent, AgentFactory
from agno.db.postgres import PostgresDb
from agno.factory import FactoryPermissionError, RequestContext
from pydantic import BaseModel, Field

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.custom_models import get_custom_model
from agent_runtime.graph_tools import create_graph_tools
from agent_runtime.workspace_tools import create_workspace_tools
from agent_runtime.agent_notes import update_agent_notes
from agent_runtime.loop_guard import loop_guard_tool_hook, loop_guard_post_hook
from agent_runtime.instructions import instructions_for
from agent_runtime.learning import create_learning_machine
from agent_runtime.model_factory import allowed_model, create_model
from agent_runtime.skills_runtime import build_skills
from agent_runtime.cancellation_compat import ControlledAgent, cancellable_tool_hook
from agent_runtime.construction_state import construction_tool_hook


AGENT_ID = "igraph-agent"


class IGraphFactoryInput(BaseModel):
    model: str = "qwen3.6-plus"
    reasoning_mode: Literal["direct", "staged"] = "direct"
    planning_mode: Literal["never", "auto", "always"] = "auto"
    output_format: Literal["chat", "report", "json"] = "chat"
    response_language: str = Field(default="Auto", max_length=32)


def create_db(config: AgentRuntimeConfig) -> PostgresDb:
    return PostgresDb(
        id="igraph-agentos-db",
        db_url=config.database_url,
        session_table="igraph_agent_sessions",
        memory_table="igraph_user_memories",
    )


def create_igraph_factory(
    config: AgentRuntimeConfig,
    db: PostgresDb,
    execution_controller=None,
) -> AgentFactory:
    def build_agent(ctx: RequestContext) -> Agent:
        raw_input = ctx.input.model_dump() if hasattr(ctx.input, "model_dump") else (ctx.input or {})
        fields_set = getattr(ctx.input, "model_fields_set", set())
        if "model" not in fields_set:
            raw_input["model"] = config.default_model
        options = IGraphFactoryInput.model_validate(raw_input)
        claims = dict(ctx.trusted.claims or {})
        user_id = str(ctx.user_id or claims.get("sub") or "anonymous")
        snapshot = getattr(getattr(ctx.request, 'state', None), 'igraph_model_snapshot', None)
        frozen = getattr(getattr(ctx.request, 'state', None), 'igraph_model_config', None)
        custom_model = get_custom_model(user_id, options.model)
        if snapshot and frozen:
            options.model = snapshot['config_id']
            custom_model = {**frozen, 'model_id': snapshot['config_id'], 'upstream_model_id': snapshot['model_id'], 'allow_anonymous': True}
        try:
            model_id = allowed_model(options.model, claims, config, custom_model)
        except (PermissionError, ValueError) as exc:
            raise FactoryPermissionError(str(exc)) from exc

        agent = (ControlledAgent if execution_controller else Agent)(
            id=AGENT_ID,
            name="iGraph Agent",
            model=create_model(model_id, config, custom_model),
            db=db,
            metadata={'model_snapshot': snapshot} if snapshot else None,
            tools=[create_graph_tools(config, response_language=options.response_language), *create_workspace_tools(), update_agent_notes],
            skills=build_skills(config, user_id),
            learning=create_learning_machine(config, planning_mode=options.planning_mode),
            instructions=instructions_for(
                reasoning_mode=options.reasoning_mode,
                planning_mode=options.planning_mode,
                response_language=options.response_language,
            ),
            add_history_to_context=True,
            add_session_state_to_context=True,
            num_history_runs=20,
            markdown=options.output_format != "json",
            reasoning=options.reasoning_mode == "staged" or options.planning_mode == "always",
            stream_events=True,
            store_events=True,
            events_to_skip=[],
            checkpoint="tool-batch",
            compress_tool_results=os.getenv("IGRAPH_COMPRESS_TOOL_RESULTS", "").lower() == "true",
            enable_session_summaries=os.getenv("IGRAPH_SESSION_SUMMARIES", "").lower() == "true",
            add_session_summary_to_context=True,
            post_hooks=[loop_guard_post_hook],
            tool_hooks=([cancellable_tool_hook] if execution_controller else []) + [loop_guard_tool_hook, construction_tool_hook],
        )
        if execution_controller:
            agent.execution_controller = execution_controller
        return agent

    return AgentFactory(
        id=AGENT_ID,
        name="iGraph Agent",
        description="基于 AgentOS 的图数据分析 Agent",
        db=db,
        factory=build_agent,
        input_schema=IGraphFactoryInput,
    )

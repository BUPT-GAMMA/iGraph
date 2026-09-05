"""Request-scoped native Agno Agent factory."""

from __future__ import annotations

from typing import Literal

from agno.agent import Agent, AgentFactory
from agno.db.postgres import PostgresDb
from agno.factory import FactoryPermissionError, RequestContext
from pydantic import BaseModel

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.custom_models import get_custom_model
from agent_runtime.graph_tools import create_graph_tools
from agent_runtime.instructions import instructions_for
from agent_runtime.learning import create_learning_machine
from agent_runtime.model_factory import allowed_model, create_model
from agent_runtime.skills_runtime import build_skills
from agent_runtime.cancellation_compat import ControlledAgent, cancellable_tool_hook


AGENT_ID = "igraph-agent"


class IGraphFactoryInput(BaseModel):
    model: str = "qwen3.6-plus"
    reasoning_mode: Literal["direct", "staged"] = "direct"
    planning_mode: Literal["never", "auto", "always"] = "auto"
    output_format: Literal["chat", "report", "json"] = "chat"


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
        custom_model = get_custom_model(user_id, options.model)
        try:
            model_id = allowed_model(options.model, claims, config, custom_model)
        except (PermissionError, ValueError) as exc:
            raise FactoryPermissionError(str(exc)) from exc

        agent = (ControlledAgent if execution_controller else Agent)(
            id=AGENT_ID,
            name="iGraph Agent",
            model=create_model(model_id, config, custom_model),
            db=db,
            tools=[create_graph_tools(config)],
            skills=build_skills(config, user_id),
            learning=create_learning_machine(config, planning_mode=options.planning_mode),
            instructions=instructions_for(
                reasoning_mode=options.reasoning_mode,
                planning_mode=options.planning_mode,
            ),
            add_history_to_context=True,
            add_session_state_to_context=True,
            num_history_runs=20,
            markdown=options.output_format != "json",
            reasoning=options.reasoning_mode == "staged" or options.planning_mode == "always",
            stream_events=True,
            store_events=True,
            tool_hooks=[cancellable_tool_hook] if execution_controller else None,
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

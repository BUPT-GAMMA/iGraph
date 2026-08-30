"""Graph Engine MCP tools for native Agno runs."""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from agno.run import RunContext
from agno.tools.mcp import MCPTools

from agent_runtime.approval_policy import GRAPH_TOOLS_REQUIRING_CONFIRMATION
from agent_runtime.config import AgentRuntimeConfig

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.team import Team


def graph_identity_headers(
    run_context: RunContext,
    agent: Optional["Agent"] = None,
    team: Optional["Team"] = None,
) -> dict[str, str]:
    return {
        "X-User-ID": str(run_context.user_id or ""),
        "X-Session-ID": str(run_context.session_id or ""),
        "X-Run-ID": str(run_context.run_id or ""),
        "X-Agent-ID": str(getattr(agent, "id", "") or ""),
    }


def create_graph_tools(config: AgentRuntimeConfig) -> MCPTools:
    return MCPTools(
        url=config.mcp_url,
        header_provider=graph_identity_headers,
        requires_confirmation_tools=sorted(GRAPH_TOOLS_REQUIRING_CONFIRMATION),
    )

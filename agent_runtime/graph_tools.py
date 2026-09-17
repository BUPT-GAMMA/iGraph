"""Graph Engine MCP tools for native Agno runs."""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from agno.run import RunContext
from agno.tools.mcp import MCPTools

from agent_runtime.approval_policy import GRAPH_TOOLS_REQUIRING_CONFIRMATION
from agent_runtime.config import AgentRuntimeConfig
from execution_protocol import controls_enabled, service_headers

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.team import Team


def graph_identity_headers(
    run_context: Optional[RunContext],
    agent: Optional["Agent"] = None,
    team: Optional["Team"] = None,
) -> dict[str, str]:
    if run_context is None:
        return {}
    scope = None
    if controls_enabled():
        from agent_runtime.execution_scope import current_scope, operation_id
        scope = current_scope.get()
    return {
        "X-User-ID": str(run_context.user_id or ""),
        "X-Session-ID": str(run_context.session_id or ""),
        "X-Run-ID": str(run_context.run_id or ""),
        "X-Agent-ID": str(getattr(agent, "id", "") or ""),
        **({**service_headers(scope.user_id), "X-Execution-Generation": str(scope.generation),
            "X-Operation-ID": operation_id.get() or ""} if scope else {}),
    }


def create_graph_tools(config: AgentRuntimeConfig) -> MCPTools:
    from agent_runtime.graph_jobs import GraphMCPTools
    return GraphMCPTools(
        url=config.mcp_url,
        header_provider=graph_identity_headers,
        requires_confirmation_tools=sorted(GRAPH_TOOLS_REQUIRING_CONFIRMATION),
    )

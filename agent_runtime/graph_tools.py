"""Graph Engine MCP tools for native Agno runs."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional, TYPE_CHECKING

from agno.run import RunContext
from agno.tools.function import ToolResult
from agno.tools.mcp import MCPTools

from agent_runtime.approval_policy import GRAPH_TOOLS_REQUIRING_CONFIRMATION
from agent_runtime.config import AgentRuntimeConfig
from execution_protocol import controls_enabled, service_headers

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.team import Team


_JOB_CONTROL_TOOLS = frozenset({"get_job_status", "get_job_result", "cancel_job"})
_TERMINAL_JOB_STATUSES = frozenset({"done", "failed", "cancelled"})


def _tool_result_payload(result: ToolResult) -> dict | None:
    """Decode the JSON object returned by Graph Engine MCP tools."""
    content = getattr(result, "content", "")
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


async def wait_for_async_job_result(
    initial_result: ToolResult,
    *,
    call_status,
    call_result,
    poll_interval_seconds: float,
    wait_timeout_seconds: float,
) -> ToolResult:
    """Turn an async Graph Engine response into one terminal tool result.

    The model must not own durable-job polling: repeated model turns consume
    tokens, can hit step limits, and can decide to stop before the job is done.
    Keeping the loop here makes one tool call mean one completed operation to
    the native Agno agent while the durable worker remains independently safe.
    """
    initial_payload = _tool_result_payload(initial_result)
    if not (
        isinstance(initial_payload, dict)
        and initial_payload.get("async") is True
        and str(initial_payload.get("job_id") or "").strip()
    ):
        return initial_result

    job_id = str(initial_payload["job_id"]).strip()
    poll_interval = max(0.1, min(float(poll_interval_seconds), 60.0))
    timeout = max(0.0, float(wait_timeout_seconds))
    started_at = time.monotonic()

    while True:
        if timeout and time.monotonic() - started_at >= timeout:
            return ToolResult(
                content=json.dumps(
                    {
                        "success": False,
                        "status": "running",
                        "error": "后台任务仍在运行，但自动等待已达到运行时上限。",
                    },
                    ensure_ascii=False,
                )
            )

        status_result = await call_status(job_id)
        status_payload = _tool_result_payload(status_result)
        if not isinstance(status_payload, dict):
            return status_result

        job = status_payload.get("job")
        if isinstance(job, dict):
            status = str(job.get("status") or "").strip().lower()
        else:
            status = str(status_payload.get("status") or "").strip().lower()

        if status in _TERMINAL_JOB_STATUSES:
            return await call_result(job_id)
        if status not in {"queued", "running", "cancelling"}:
            return status_result

        await asyncio.sleep(poll_interval)


class AutoWaitingMCPTools(MCPTools):
    """MCP toolkit that hides durable-job polling from the language model."""

    def __init__(
        self,
        *args,
        job_poll_interval_seconds: float = 2.0,
        job_wait_timeout_seconds: float = 0.0,
        **kwargs,
    ) -> None:
        self.job_poll_interval_seconds = job_poll_interval_seconds
        self.job_wait_timeout_seconds = job_wait_timeout_seconds
        super().__init__(*args, **kwargs)

    async def build_tools(self) -> None:
        await super().build_tools()
        status_function = self.functions.get("get_job_status")
        result_function = self.functions.get("get_job_result")
        if status_function is None or result_function is None:
            return

        status_entrypoint = status_function.entrypoint
        result_entrypoint = result_function.entrypoint
        if status_entrypoint is None or result_entrypoint is None:
            return

        for tool_name, function in self.functions.items():
            if tool_name in _JOB_CONTROL_TOOLS or function.entrypoint is None:
                continue
            function.entrypoint = self._auto_waiting_entrypoint(
                function.entrypoint,
                status_entrypoint=status_entrypoint,
                result_entrypoint=result_entrypoint,
            )

    def _auto_waiting_entrypoint(
        self,
        original_entrypoint,
        *,
        status_entrypoint,
        result_entrypoint,
    ):
        async def call(
            _agno_run_context=None,
            _agno_agent=None,
            _agno_team=None,
            **kwargs,
        ) -> ToolResult:
            injected = {
                "_agno_run_context": _agno_run_context,
                "_agno_agent": _agno_agent,
                "_agno_team": _agno_team,
            }
            initial_result = await original_entrypoint(**injected, **kwargs)

            async def call_status(job_id: str) -> ToolResult:
                return await status_entrypoint(**injected, job_id=job_id)

            async def call_result(job_id: str) -> ToolResult:
                return await result_entrypoint(**injected, job_id=job_id)

            return await wait_for_async_job_result(
                initial_result,
                call_status=call_status,
                call_result=call_result,
                poll_interval_seconds=self.job_poll_interval_seconds,
                wait_timeout_seconds=self.job_wait_timeout_seconds,
            )

        return call


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
        job_poll_interval_seconds=getattr(config, "job_poll_interval_seconds", 2.0),
        job_wait_timeout_seconds=getattr(config, "job_wait_timeout_seconds", 0.0),
    )

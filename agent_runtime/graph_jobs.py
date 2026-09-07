"""Resolve background Graph Engine work before completing a native tool call."""
from __future__ import annotations

import asyncio
import json

from agno.tools.function import ToolResult

from agent_runtime.construction_state import _result_payload
from agent_runtime.mcp_compat import OwnedMCPTools


async def completed_graph_result(result, poll, *, interval=1.0):
    # Agno 2.9 converts MCP transport/isError failures into ToolResult text
    # without setting FunctionCall.error. Restore a structured failure before
    # the DAG decides whether dependent nodes may execute.
    content = getattr(result, "content", None)
    if isinstance(content, str) and (
        content.startswith(("Error:", "Error from MCP tool '"))
        or (content.startswith("MCP tool '") and "' failed:" in content)
    ):
        return ToolResult(content=json.dumps({
            "success": False, "code": "GRAPH_MCP_TOOL_FAILED", "error": content,
        }, ensure_ascii=False))
    payload = _result_payload(result)
    if not isinstance(payload, dict) or payload.get("success") is False or not payload.get("async") or not payload.get("job_id"):
        return result
    job_id = payload["job_id"]
    visited = {job_id}
    while True:
        status = _result_payload(await poll(job_id))
        job = status.get("job") if isinstance(status, dict) else None
        if not isinstance(job, dict) or status.get("success") is False:
            raise RuntimeError(f"GRAPH_JOB_STATUS_UNAVAILABLE: {job_id}")
        state = job.get("status")
        if state == "done":
            final = job.get("result")
            if not isinstance(final, dict):
                raise RuntimeError(f"GRAPH_JOB_RESULT_UNAVAILABLE: {job_id}")
            if final.get("success") is not False and final.get("async"):
                child_id = final.get("job_id")
                if not isinstance(child_id, str) or not child_id or child_id in visited:
                    raise RuntimeError(f"GRAPH_JOB_DEPENDENCY_INVALID: {job_id}")
                visited.add(child_id)
                job_id = child_id
                continue
            return ToolResult(content=json.dumps({**final, "job_id": job_id, "async": False}, ensure_ascii=False))
        if state in {"failed", "cancelled", "canceled"}:
            return ToolResult(content=json.dumps({
                "success": False, "code": "GRAPH_JOB_FAILED", "job_id": job_id,
                "status": state, "error": job.get("message") or state,
            }, ensure_ascii=False))
        if state not in {"queued", "running", "cancelling"}:
            raise RuntimeError(f"GRAPH_JOB_STATUS_INVALID: {job_id}")
        await asyncio.sleep(interval)


class GraphMCPTools(OwnedMCPTools):
    """Each concurrent call owns its MCP transport and background-job wait."""

    async def build_tools(self):
        await super().build_tools()
        for function in self.functions.values():
            function.entrypoint = self._completed_entrypoint(function.entrypoint)

    def _completed_entrypoint(self, entrypoint):
        async def invoke(_agno_run_context=None, _agno_agent=None, _agno_team=None, **arguments):
            identity = dict(_agno_run_context=_agno_run_context, _agno_agent=_agno_agent, _agno_team=_agno_team)
            result = await entrypoint(**identity, **arguments)

            async def poll(job_id):
                async with self.get_temporary_session_for_run(
                    run_context=_agno_run_context, agent=_agno_agent, team=_agno_team,
                ) as session:
                    return await session.call_tool("get_job_status", arguments={"job_id": job_id})

            return await completed_graph_result(result, poll)
        return invoke

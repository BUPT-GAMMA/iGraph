"""Resolve background Graph Engine work before completing a native tool call."""
from __future__ import annotations

import asyncio
import json

import httpx
from anyio import BrokenResourceError, ClosedResourceError, EndOfStream

from agno.tools.function import ToolResult

from agent_runtime.construction_state import _result_payload
from agent_runtime.mcp_compat import OwnedMCPTools
from agent_runtime.graph_progress import emit_job_progress


async def _poll_status(poll, job_id, cursor, interval):
    # Cursor reads are replayable. Retry only transport failures, with the
    # exact same job identity and cursor; cancellation always propagates.
    for attempt in range(3):
        try:
            return await poll(job_id, after_seq=cursor)
        except (httpx.TransportError, ConnectionError, TimeoutError, BrokenResourceError, ClosedResourceError, EndOfStream):
            if attempt == 2:
                raise
            await asyncio.sleep(min(interval, .25) * (2 ** attempt))


async def completed_graph_result(result, poll, *, interval=1.0, tool_name=""):
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
    cursor = 0
    last_snapshot = None
    await emit_job_progress({"job": {"job_id": job_id, "tool_name": tool_name, "status": payload.get("status", "queued"),
                                    "progress": 0, "message": payload.get("message", "作业已提交")},
                             "progress_updates": [], "progress_cursor": 0})
    while True:
        status = _result_payload(await _poll_status(poll, job_id, cursor, interval))
        job = status.get("job") if isinstance(status, dict) else None
        if not isinstance(job, dict) or status.get("success") is False:
            raise RuntimeError(f"GRAPH_JOB_STATUS_UNAVAILABLE: {job_id}")
        if job.get("job_id", job_id) != job_id:
            raise RuntimeError(f"GRAPH_JOB_IDENTITY_MISMATCH: {job_id}")
        state = "cancelled" if job.get("status") == "canceled" else job.get("status")
        if state not in {"queued", "running", "cancelling", "done", "failed", "cancelled", "canceled"}:
            raise RuntimeError(f"GRAPH_JOB_STATUS_INVALID: {job_id}")
        updates = status.get("progress_updates") or []
        if not isinstance(updates, list) or any(not isinstance(row, dict) for row in updates):
            raise RuntimeError(f"GRAPH_JOB_PROGRESS_INVALID: {job_id}")
        next_cursor = status.get("progress_cursor", cursor)
        has_more = status.get("progress_has_more", False)
        if (not isinstance(has_more, bool) or isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor < cursor
                or has_more and next_cursor <= cursor):
            raise RuntimeError(f"GRAPH_JOB_PROGRESS_CURSOR_INVALID: {job_id}")
        if "progress_cursor" in status:
            sequences = [row.get("progress_seq") for row in updates]
            previous = cursor
            for sequence in sequences:
                if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= previous or sequence > next_cursor:
                    raise RuntimeError(f"GRAPH_JOB_PROGRESS_CURSOR_INVALID: {job_id}")
                previous = sequence
            if previous != next_cursor:
                raise RuntimeError(f"GRAPH_JOB_PROGRESS_CURSOR_INVALID: {job_id}")
        final = job.get("result")
        chained = state == "done" and isinstance(final, dict) and final.get("success") is not False and bool(final.get("async"))
        if state == "done" and not has_more:
            if not isinstance(final, dict):
                raise RuntimeError(f"GRAPH_JOB_RESULT_UNAVAILABLE: {job_id}")
            if chained:
                child_id = final.get("job_id")
                if not isinstance(child_id, str) or not child_id or child_id in visited:
                    raise RuntimeError(f"GRAPH_JOB_DEPENDENCY_INVALID: {job_id}")
        snapshot = {**job, "job_id": job_id, "status": state, "tool_name": job.get("tool_name") or tool_name}
        snapshot.pop("result", None)
        if state == "done" and isinstance(final, dict) and final.get("success") is False:
            snapshot["status"] = "failed"
        if updates or snapshot != last_snapshot or (state in {"done", "failed", "cancelled", "canceled"} and not has_more):
            await emit_job_progress({"job": snapshot, "progress_updates": updates,
                                     "progress_cursor": next_cursor, "progress_has_more": bool(has_more),
                                     "job_chain_continues": chained})
            last_snapshot = snapshot
        cursor = next_cursor
        if has_more:
            continue
        if state == "done":
            if chained:
                visited.add(child_id)
                job_id = child_id
                cursor = 0
                last_snapshot = None
                continue
            return ToolResult(content=json.dumps({**final, "job_id": job_id, "async": False}, ensure_ascii=False))
        if state in {"failed", "cancelled", "canceled"}:
            return ToolResult(content=json.dumps({
                "success": False, "code": "GRAPH_JOB_FAILED", "job_id": job_id,
                "status": state, "error": job.get("message") or state,
            }, ensure_ascii=False))
        await asyncio.sleep(interval)


class GraphMCPTools(OwnedMCPTools):
    """Each concurrent call owns its MCP transport and background-job wait."""

    def __init__(
        self,
        *args,
        job_poll_interval_seconds: float = 1.0,
        job_wait_timeout_seconds: float = 0.0,
        **kwargs,
    ):
        # These are runtime policy knobs, not Agno Toolkit arguments.  Consume
        # them here so they cannot leak into Toolkit.__init__.
        self.job_poll_interval_seconds = max(
            0.0, min(float(job_poll_interval_seconds), 60.0)
        )
        self.job_wait_timeout_seconds = max(
            0.0, float(job_wait_timeout_seconds)
        )
        super().__init__(*args, **kwargs)

    async def build_tools(self):
        await super().build_tools()
        for function in self.functions.values():
            function.entrypoint = self._completed_entrypoint(function.entrypoint, function.name)

    def _completed_entrypoint(self, entrypoint, tool_name=""):
        async def invoke(_agno_run_context=None, _agno_agent=None, _agno_team=None, **arguments):
            identity = dict(_agno_run_context=_agno_run_context, _agno_agent=_agno_agent, _agno_team=_agno_team)
            result = await entrypoint(**identity, **arguments)

            async def poll(job_id, *, after_seq):
                async with self.get_temporary_session_for_run(
                    run_context=_agno_run_context, agent=_agno_agent, team=_agno_team,
                ) as session:
                    status_result = await session.call_tool(
                        "get_job_status",
                        arguments={"job_id": job_id, "after_seq": after_seq},
                    )
                    status_payload = _result_payload(status_result)
                    job = status_payload.get("job") if isinstance(status_payload, dict) else None
                    if (
                        isinstance(job, dict)
                        and str(job.get("status") or "").lower() == "done"
                        and not isinstance(job.get("result"), dict)
                    ):
                        # Durable large-graph jobs deliberately keep status
                        # responses bounded. Fetch their terminal Summary /
                        # Preview / Artifact payload only after completion.
                        result_payload = _result_payload(
                            await session.call_tool(
                                "get_job_result", arguments={"job_id": job_id}
                            )
                        )
                        final = (
                            result_payload.get("result")
                            if isinstance(result_payload, dict)
                            else None
                        )
                        if isinstance(final, dict):
                            status_payload = {
                                **status_payload,
                                "job": {**job, "result": final},
                            }
                            return ToolResult(
                                content=json.dumps(status_payload, ensure_ascii=False)
                            )
                    return status_result

            return await completed_graph_result(
                result,
                poll,
                interval=self.job_poll_interval_seconds,
                tool_name=tool_name,
            )
        return invoke

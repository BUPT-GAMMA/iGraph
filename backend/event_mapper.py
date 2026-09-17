"""Translate AgentOS SSE events to the existing frontend event contract."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator


def _tool(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("tool") or payload.get("tool_call") or {}
    if isinstance(value, list):
        value = value[0] if value else {}
    return value if isinstance(value, dict) else {}


def _decoded_container(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _as_rows(value: Any) -> list[dict[str, Any]]:
    """Normalize AgentOS requirement/tool containers across minor versions."""
    value = _decoded_container(value)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _payload_variants(payload: dict[str, Any]) -> list[dict[str, Any]]:
    variants = [payload]
    for key in ("run", "data", "event_data", "response", "run_response", "request", "payload"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            variants.append(nested)
    return variants


def _first_value(payloads: list[dict[str, Any]], *keys: str) -> Any:
    for item in payloads:
        for key in keys:
            value = item.get(key)
            if value is not None and value not in ("", [], {}):
                return value
    return None


def _decoded_tool_result(payload: dict[str, Any]) -> Any:
    tool = _tool(payload)
    value = tool.get("result") if tool else payload.get("content")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _terminal_job_status(status: Any) -> bool:
    return str(status or "").lower() in {"done", "failed", "cancelled"}


def feedback_request_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    variants = _payload_variants(payload)
    requirements = _as_rows(_first_value(variants, "requirements", "requirement"))
    pending_requirements: list[dict[str, Any]] = []
    for requirement in requirements:
        execution = requirement.get("tool_execution") or requirement.get("tool_call") or requirement.get("tool") or {}
        confirmed = execution.get("confirmed") if isinstance(execution, dict) else None
        if confirmed is None:
            confirmed = requirement.get("confirmed")
        if confirmed is None:
            pending_requirements.append(requirement)
    first = pending_requirements[0] if pending_requirements else requirements[0] if requirements else {}
    tools: Any = _first_value(variants, "tools", "tool_calls") or []
    tool_rows = _as_rows(tools)
    paused_tool = next(
        (
            item for item in reversed(tool_rows)
            if item.get("confirmed") is None
            or item.get("requires_confirmation") is True
        ),
        tool_rows[-1] if tool_rows else {},
    )
    execution = (
        first.get("tool_execution")
        or first.get("tool_call")
        or first.get("tool")
        or paused_tool
        or {}
    )
    if not isinstance(execution, dict):
        execution = {}
    execution = {**execution}
    nested_tool = execution.get("tool") or execution.get("tool_call")
    if isinstance(nested_tool, dict):
        execution = {**nested_tool, **execution}
    run_id = str(_first_value(variants, "run_id", "runId") or "")
    call_id = (
        execution.get("tool_call_id")
        or execution.get("call_id")
        or first.get("tool_call_id")
        or first.get("call_id")
        or ""
    )
    tool_name = execution.get("tool_name") or execution.get("name") or first.get("tool_name") or ""
    proposed_args = (
        execution.get("tool_args")
        or execution.get("arguments")
        or first.get("tool_args")
        or first.get("arguments")
        or {}
    )
    description = first.get("description") or first.get("message") or first.get("user_message")
    return {
        "run_id": run_id,
        "request_id": run_id,
        "call_id": call_id,
        "tool_name": tool_name,
        "proposed_args": proposed_args if isinstance(proposed_args, dict) else {},
        "description": description or "该工具调用需要确认",
        "requirements": requirements,
        "pending_requirements": pending_requirements,
    }


def graph_job_progress_payload(payload: dict[str, Any]) -> dict | None:
    if payload.get("event") != "CustomEvent":
        return None
    content = _decoded_container(payload.get("content"))
    if not isinstance(content, dict) or content.get("kind") != "graph_job_progress":
        return None
    job = content.get("job")
    if not isinstance(job, dict) or not job.get("job_id"):
        return None
    return content


def _graph_job_progress_events(payload: dict[str, Any], content: dict) -> list[dict]:
    job = content["job"]
    name = str(content.get("tool_name") or job.get("tool_name") or "")
    common = {key: payload[key] for key in ("run_id", "session_id", "event_index") if payload.get(key) is not None}
    identity = {"type": "tool_progress", "tool": name,
                "call_id": str(payload.get("tool_call_id") or job["job_id"]), **common}
    updates = [row for row in (content.get("progress_updates") or []) if isinstance(row, dict)]
    rows = [{**identity, "progress": {**update, "job_id": job["job_id"], "status": job.get("status"),
                                     "progress": update.get("progress", job.get("progress", 0)),
                                     "message": update.get("message", job.get("message", ""))}}
            for update in updates]
    terminal = _terminal_job_status(job.get("status")) and not content.get("progress_has_more") and not content.get("job_chain_continues")
    if terminal or not updates:
        state = job.get("status")
        kind = "job_queued" if state == "queued" else "job_progress"
        if terminal:
            kind = ("train_done" if name == "train" else "job_done") if state == "done" else "job_" + str(state)
        rows.append({**identity, "progress": {
            "kind": kind, "job_id": job["job_id"], "status": state,
            "progress": 1 if terminal and state == "done" else job.get("progress", 0),
            "message": job.get("message", ""),
        }})
    return rows


def map_agentos_event(payload: dict[str, Any]) -> dict[str, Any]:
    variants = _payload_variants(payload)
    event = str(_first_value(variants, "event", "type", "event_name") or "")
    common = {
        key: payload[key]
        for key in ("run_id", "session_id", "event_index")
        if payload.get(key) is not None
    }
    for key in ("run_id", "session_id", "event_index"):
        if key not in common:
            value = _first_value(variants, key, {"run_id": "runId", "session_id": "sessionId", "event_index": "eventIndex"}[key])
            if value is not None:
                common[key] = value
    if event in {"RunStarted", "TeamRunStarted"}:
        return {"type": "model_turn_start", **common}
    if event in {"RunContinued", "TeamRunContinued"}:
        return {"type": "run_continued", **common}
    if event in {"RunContent", "TeamRunContent"}:
        run_id = str(common.get("run_id") or "")
        message_id = str(_first_value(variants, "message_id", "messageId") or f"assistant:{run_id}")
        return {
            "type": "token",
            "message_id": message_id,
            "content": str(payload.get("content") or ""),
            **common,
        }
    if event in {"ReasoningStarted", "TeamReasoningStarted"}:
        return {"type": "thinking_start", "phase": "reasoning", **common}
    if event in {"ReasoningContent", "RunReasoningContent", "ReasoningStep", "TeamReasoningStep"}:
        return {
            "type": "thinking",
            "phase": "reasoning",
            "content": str(payload.get("reasoning_content") or payload.get("content") or ""),
            **common,
        }
    if event in {"ReasoningCompleted", "TeamReasoningCompleted"}:
        return {"type": "thinking_end", "phase": "reasoning", **common}
    if "ToolCallStarted" in event or event == "ToolCallStarted":
        tool = _tool(payload)
        return {
            "type": "tool_call",
            "tool": tool.get("tool_name") or tool.get("name") or "",
            "args": tool.get("tool_args") or tool.get("arguments") or {},
            "name": tool.get("tool_name") or tool.get("name") or "",
            "arguments": tool.get("tool_args") or tool.get("arguments") or {},
            "call_id": tool.get("tool_call_id") or "",
            **common,
        }
    if "ToolCallCompleted" in event or event == "ToolCallCompleted":
        tool = _tool(payload)
        result = tool.get("result") if tool else payload.get("content")
        decoded_result = _decoded_container(result)
        pending_async = (
            isinstance(decoded_result, dict)
            and decoded_result.get("async") is True
            and bool(decoded_result.get("job_id"))
        )
        return {
            "type": "tool_result",
            "tool": tool.get("tool_name") or tool.get("name") or "",
            "name": tool.get("tool_name") or tool.get("name") or "",
            "result": result,
            "call_id": tool.get("tool_call_id") or "",
            "done": not pending_async,
            **common,
        }
    if event in {"RunPaused", "TeamRunPaused"} or event.lower().endswith("runpaused"):
        return {
            "type": "await_user_feedback",
            "request": feedback_request_from_payload(payload),
            **common,
        }
    if event in {"RunCompleted", "TeamRunCompleted"}:
        run_id = str(common.get("run_id") or "")
        message_id = str(_first_value(variants, "message_id", "messageId") or f"assistant:{run_id}")
        return {
            "type": "done",
            "message_id": message_id,
            **({"content": payload["content"]} if isinstance(payload.get("content"), str) else {}),
            "context_usage": payload.get("metrics") or {},
            **common,
        }
    if event in {"RunError", "RunCancelled", "TeamRunError", "TeamRunCancelled", "error"}:
        return {"type": "error", "cancelled": 'Cancelled' in event, "error": str(payload.get("content") or payload.get("error") or event), **common}
    if event == "stream_recovery_required":
        return {"type": event, "reason": payload.get("reason"), "code": payload.get("code"), **common}
    if event in {"catch_up", "replay", "subscribed"}:
        return {"type": "stream_status", "status": event, "run_status": payload.get("status"),
                "remaining_events": payload.get("missed_events", payload.get("total_events")), **common}
    return {"type": "agent_event", "event": event, "payload": payload, **common}


def map_product_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Restore product UI events that used to be emitted by the legacy runtime."""

    event = str(payload.get("event") or payload.get("type") or "")
    if event in {"RunContent", "TeamRunContent"}:
        # Provider reasoning deltas share a native event with ordinary content.
        # Keep both in the same V2 envelope so replay applies them atomically.
        reasoning = payload.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            return [{"type": "thinking", "phase": "reasoning", "content": reasoning,
                     **{key: payload[key] for key in ("run_id", "session_id", "event_index") if payload.get(key) is not None}}]
        return []
    progress_content = graph_job_progress_payload(payload)
    if progress_content is not None:
        return _graph_job_progress_events(payload, progress_content)
    if "ToolCallCompleted" not in event:
        return []
    tool = _tool(payload)
    name = str(tool.get("tool_name") or tool.get("name") or "")
    result = _decoded_tool_result(payload)
    if not isinstance(result, dict) or result.get("success") is False:
        return []
    common = {
        key: payload[key]
        for key in ("run_id", "session_id", "event_index")
        if payload.get(key) is not None
    }
    progress_tools = {"train", "load_graph_from_text", "load_graph_from_upload", "load_graph_classification_dataset"}
    if name in progress_tools and result.get("async") and result.get("job_id"):
        return [{
            "type": "tool_progress",
            "tool": name,
            "call_id": str(result.get("job_id") or tool.get("tool_call_id") or ""),
            "progress": {
                "kind": "job_queued",
                "job_id": result.get("job_id"),
                "status": result.get("status", "queued"),
                "progress": 0,
                "message": result.get("message", "作业已提交"),
            },
            **common,
        }]
    if name == "get_job_status" and isinstance(result.get("job"), dict):
        job = result["job"]
        source_tool = str(job.get("tool_name") or "")
        if source_tool in progress_tools:
            updates = [item for item in (result.get("progress_updates") or []) if isinstance(item, dict)]
            if not updates:
                updates = [job.get("detail") if isinstance(job.get("detail"), dict) else {}]
            rows: list[dict[str, Any]] = []
            for update in updates:
                progress = {
                    **update,
                    "job_id": job.get("job_id"),
                    "status": job.get("status"),
                    "progress": update.get("progress", job.get("progress", 0)),
                    "message": update.get("message", job.get("message", "")),
                }
                if job.get("status") == "done":
                    progress["progress"] = 1
                    progress["kind"] = (
                        "train_done" if source_tool == "train" else
                        "text2graph_done" if source_tool in {"load_graph_from_text", "load_graph_from_upload"} else
                        "job_done"
                    )
                rows.append({
                    "type": "tool_progress",
                    "tool": source_tool,
                    "call_id": str(job.get("job_id") or tool.get("tool_call_id") or ""),
                    "progress": progress,
                    **common,
                })
            if source_tool == "train" and _terminal_job_status(job.get("status")):
                job_result = _decoded_container(job.get("result"))
                if isinstance(job_result, dict):
                    final_result = dict(job_result)
                else:
                    final_result = {"result": job_result} if job_result is not None else {}
                status = str(job.get("status") or "").lower()
                final_result.setdefault("success", status == "done")
                final_result.update({
                    "job_id": job.get("job_id"),
                    "job_status": status,
                    "history": updates,
                    "progress": job.get("detail") if isinstance(job.get("detail"), dict) else {},
                })
                if status != "done" and not final_result.get("error"):
                    final_result["error"] = job.get("message") or f"training job {status}"
                rows.append({
                    "type": "tool_result",
                    "tool": "train",
                    "name": "train",
                    "call_id": str(job.get("job_id") or tool.get("tool_call_id") or ""),
                    "result": final_result,
                    "done": True,
                    **common,
                })
            return rows
    if name == "visualize_subgraph":
        graph = result.get("data") if isinstance(result.get("data"), dict) else result
        if isinstance(graph.get("nodes"), list) and isinstance(graph.get("links", graph.get("edges")), list):
            graph = {**graph, "links": graph.get("links", graph.get("edges", [])),
                     "edges": graph.get("edges", graph.get("links", []))}
            return [{"type": "graph_visualize", "data": graph, "call_id": tool.get("tool_call_id") or "", **common}]
    if name == "create_analysis_report" and isinstance(result.get("report"), dict):
        return [{
            "type": "analysis_report",
            "data": {"report": result["report"]},
            "call_id": tool.get("tool_call_id") or "",
            **common,
        }]
    return []


def _native_reasoning_step(payload: dict[str, Any], step_id: int) -> dict[str, Any]:
    """Project an Agno ReasoningStep into the former read-only plan card."""

    raw = payload.get("reasoning_step") or payload.get("step") or {}
    step = raw if isinstance(raw, dict) else {}
    goal = str(
        step.get("title")
        or step.get("action")
        or step.get("goal")
        or payload.get("reasoning_content")
        or payload.get("content")
        or f"推理步骤 {step_id}"
    ).strip()
    summary = str(
        step.get("result")
        or step.get("conclusion")
        or step.get("reasoning")
        or payload.get("reasoning_content")
        or payload.get("content")
        or ""
    ).strip()
    return {
        "step_id": step_id,
        "goal": goal[:500],
        "status": "done",
        "result_summary": summary[:2000],
        "tool_hints": [],
        "depends_on": [],
        "source": "agentos_reasoning",
    }


async def translate_sse(
    lines: AsyncIterator[str],
    *,
    protocol_version: int = 1,
    session_id: str = "",
    run_id: str = "",
) -> AsyncIterator[str]:
    """Translate one AgentOS event into one atomic downstream SSE frame.

    Version 1 preserves the legacy one-UI-event-per-frame behavior. Version 2
    keeps every UI event derived from a single AgentOS event in one envelope so
    a resume cursor can never land in the middle of a logical event.
    """

    planning_started = False
    planning_step_id = 0
    current_run_id = run_id
    async for line in lines:
        if line.startswith(":"):
            yield line + "\n\n"
            continue
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        event_payload: dict[str, Any] = {}
        try:
            payload = json.loads(raw)
            event_payload = payload if isinstance(payload, dict) else {"content": payload}
            current_run_id = str(event_payload.get("run_id") or current_run_id)
            mapped = map_agentos_event(event_payload)
            current_run_id = str(mapped.get("run_id") or current_run_id)
            if mapped.get("type") in {"token", "done"} and mapped.get("message_id") in {None, "", "assistant:"}:
                mapped["message_id"] = f"assistant:{current_run_id}"
            extras = map_product_events(event_payload)
            event_name = str(event_payload.get("event") or event_payload.get("type") or "")
            plan_events: list[dict[str, Any]] = []
            if event_name in {"ReasoningStep", "TeamReasoningStep"}:
                planning_step_id += 1
                plan_id = str(event_payload.get("run_id") or "")
                if not planning_started:
                    planning_started = True
                    plan_events.append({"type": "plan_analyzing", "plan_id": plan_id})
                plan_events.append({
                    "type": "plan_step",
                    "plan_id": plan_id,
                    "step": _native_reasoning_step(event_payload, event_payload.get("event_index", planning_step_id)),
                })
            elif event_name in {"ReasoningCompleted", "TeamReasoningCompleted"} and planning_started:
                plan_events.append({
                    "type": "plan_complete",
                    "plan_id": str(event_payload.get("run_id") or ""),
                })
        except Exception as exc:
            # A malformed optional event must not silently terminate a long
            # running stream. Preserve the event name so the client can show
            # a useful state instead of leaving a tool stuck at "running".
            fallback_event = str(event_payload.get("event") or event_payload.get("type") or "") if isinstance(event_payload, dict) else ""
            if fallback_event.lower().endswith("runpaused"):
                mapped = {
                    "type": "await_user_feedback",
                    "request": feedback_request_from_payload(event_payload),
                    **{
                        key: event_payload[key]
                        for key in ("run_id", "session_id", "event_index")
                        if event_payload.get(key) is not None
                    },
                }
            else:
                mapped = {"type": "agent_event", "event": fallback_event, "error": str(exc), "payload": event_payload}
            extras = []
            plan_events = []
        derived_events = [mapped, *plan_events, *extras]
        if protocol_version == 2:
            envelope = {
                "protocol_version": 2,
                "session_id": str(event_payload.get("session_id") or mapped.get("session_id") or session_id),
                "run_id": str(event_payload.get("run_id") or mapped.get("run_id") or current_run_id),
                "event_index": event_payload.get("event_index", mapped.get("event_index")),
                "events": derived_events,
            }
            yield f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n"
            continue
        for event in derived_events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

def project_run(run: dict[str, Any]) -> dict[str, Any]:
    """Project persisted AgentOS events into the ordered visible workbench Items."""
    events = run.get("events") or []
    if not isinstance(events, list):
        events = []
    pending: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    final_parts: list[str] = []
    def event_index(raw: dict, fallback: int) -> int:
        try:
            return int(raw.get("event_index"))
        except (TypeError, ValueError, OverflowError):
            return fallback
    def flush_text() -> None:
        if not pending:
            return
        items.append({"id": f"text:{pending[0]['sequence']}", "kind": "text", "sequence": pending[0]["sequence"], "content": "".join(entry["content"] for entry in pending)})
        pending.clear()
    def add_reasoning(content: str, sequence: int) -> None:
        if items and items[-1]["kind"] == "reasoning":
            items[-1]["content"] += content
            return
        items.append({"id": f"reasoning:{sequence}", "kind": "reasoning", "sequence": sequence, "content": content})
    for index, raw in enumerate(events):
        if not isinstance(raw, dict):
            continue
        payload = {**raw, "event_index": event_index(raw, index), "run_id": raw.get("run_id", run.get("run_id", ""))}
        event = str(payload.get("event") or payload.get("type") or "")
        if event in {"RunContent", "TeamRunContent"}:
            content = payload.get("content")
            if content not in (None, ""):
                pending.append({"content": str(content), "sequence": int(payload["event_index"])})
            reasoning = payload.get("reasoning_content")
            if reasoning:
                add_reasoning(str(reasoning), int(payload["event_index"]))
            continue
        if event in {"ReasoningContent", "RunReasoningContent", "ReasoningStep", "TeamReasoningStep"}:
            reasoning = str(payload.get("reasoning_content") or payload.get("content") or "")
            if reasoning:
                add_reasoning(reasoning, int(payload["event_index"]))
            continue
        if "ToolCallStarted" in event:
            flush_text()
            tool = _tool(payload)
            name = str(tool.get("tool_name") or tool.get("name") or "")
            call_id = str(tool.get("tool_call_id") or "")
            items.append({"id": f"tool:{call_id or payload['event_index']}", "kind": "tool", "sequence": int(payload["event_index"]), "tool": {"tool": name, "call_id": call_id, "args": tool.get("tool_args") or tool.get("arguments") or {}, "done": False, "status": "running"}})
            continue
        if "ToolCallCompleted" in event:
            tool = _tool(payload)
            call_id = str(tool.get("tool_call_id") or "")
            for item in items:
                if item["kind"] == "tool" and item["tool"].get("call_id") == call_id:
                    result = _decoded_tool_result(payload)
                    failed = bool(tool.get("tool_call_error")) or isinstance(result, dict) and result.get("success") is False
                    item["tool"].update({"done": True, "status": "failed" if failed else "completed", "result": result})
            continue
        if event == "RunContentCompleted":
            continue
        if event == "RunCompleted":
            if pending:
                final_parts.append("".join(entry["content"] for entry in pending))
                pending.clear()
            elif payload.get("content"):
                final_parts.append(str(payload["content"]))
    status = str(run.get("status") or "").lower()
    if status in {"completed", "error", "failed", "cancelled", "canceled"}:
        for item in items:
            if item.get("kind") == "tool" and not item["tool"].get("done"):
                item["tool"].update(done=True, status="cancelled" if status in {"cancelled", "canceled"} else "failed")
    if not final_parts and pending:
        final_parts.extend(entry["content"] for entry in pending)
    if not final_parts:
        value = run.get("content")
        if isinstance(value, str): final_parts.append(value)
    return {"processItems": sorted(items, key=lambda item: item["sequence"]), "content": "".join(final_parts), "last_event_index": max((event_index(item, index) for index, item in enumerate(events) if isinstance(item, dict)), default=-1)}

"""Public Backend/BFF for the four-module architecture.

Backend owns users, authentication, authorization and public API compatibility.
AgentOS owns agent sessions/runs/memory; Graph Engine owns graph state and jobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
import hashlib
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from workspace_storage import delete_user_workspaces, delete_workspace, visible_files, derived_file, save_upload
from runtime_observability import RequestLoggingMiddleware
from response_language import resolve_response_language
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from backend.general_migration import GeneralMigration
from backend.session_pagination import session_page
from backend.config import BackendConfig
from backend.dataset_api import DATASET_API_MIGRATION, MIGRATION_GUIDE_PATH, legacy_dataset_api_notice
from backend.construction_access import install_construction_access
from construction_access import scope_headers
from backend.construction import install_construction_routes, sync_record as sync_construction_record
from backend.constructed_import import install_constructed_import_routes
from backend.custom_models import CustomModelDirectory, CustomModelRecord, public_custom_model
from backend.event_mapper import feedback_request_from_payload, graph_job_progress_payload, project_run, translate_sse
from backend.identity import current_user, public_user, require_admin
from backend.project_datasets import ProjectDatasetDirectory, ProjectDatasetRecord
from backend.projects import ProjectDirectory, ProjectRecord
from backend.server_datasets import ServerDatasetDirectory, ServerDatasetRecord
from backend.service_client import ServiceClient, forwarded_headers
from backend.stream_recovery import close_observation, observation_events
from backend.users import UserDirectory, UserRecord
from execution_protocol import (CancelRequest, JobCancelRequest, Target, controls_enabled,
                                execution_headers, require_service, service_headers,
                                validate_service_configuration)
from execution_lifecycle import ProcessLease


class CreateSessionReq(BaseModel):
    client_session_id: str | None = Field(default=None, max_length=64)
    model: str | None = None
    project_id: str | None = None
    active_dataset_id: str | None = None
    initial_message_preview: str = Field(default="", max_length=1000)
    reasoning_mode: Literal["staged", "direct"] = "direct"
    planning_mode: Literal["never", "auto", "always"] = "auto"


class ProjectWriteReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


class ProjectPatchReq(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)


class ProjectDatasetPatchReq(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)


class ServerDatasetImportReq(BaseModel):
    server_dataset_id: str = Field(min_length=1, max_length=128)
    name: str = Field(default="", max_length=120)
    set_as_default: bool = False
    session_id: str = Field(default="", max_length=64)


class SessionDatasetReq(BaseModel):
    active_dataset_id: str | None = None


class DeleteProjectReq(BaseModel):
    conversation_action: Literal["move_to_general", "delete"]


class MoveSessionReq(BaseModel):
    project_id: str = Field(min_length=1)


class AttachmentRef(BaseModel):
    file_token: str
    filename: str | None = None


class ChatReq(BaseModel):
    message: str
    attachments: list[AttachmentRef] | None = None
    reasoning_mode: Literal["staged", "direct"] | None = None
    planning_mode: Literal["never", "auto", "always"] | None = None
    output_format: Literal["chat", "report", "json"] = "chat"
    regenerate: bool = False
    regenerate_of: str | None = None
    replace_run_id: uuid.UUID | None = None
    start_request_id: uuid.UUID | None = None
    model_revision: int | None = None


class SessionSettingReq(BaseModel):
    model: str | None = None
    model_revision: int | None = None
    reasoning_mode: Literal["staged", "direct"] | None = None
    planning_mode: Literal["never", "auto", "always"] | None = None


class MemoryWriteReq(BaseModel):
    scope: str = "long_term"
    category: str = "fact"
    key: str
    value: str
    confidence: float = 0.8
    reason: str = ""


class MemoryPatchReq(BaseModel):
    scope: str = "long_term"
    key: str = ""
    value: str = ""
    reason: str = ""


class FeedbackReq(BaseModel):
    run_id: str = ""
    request_id: str = ""
    decision_id: str = ""
    decision: Literal["approve", "modify", "reject"]
    patched_args: dict[str, Any] | None = None
    comment: str = ""
    tool_call_id: str = ""
    tool_name: str = ""


class UserWriteReq(BaseModel):
    user_id: str = Field(min_length=1)
    enabled: bool = True
    roles: list[str] = Field(default_factory=lambda: ["user"])
    allowed_models: list[str] = Field(default_factory=list)


class CustomModelReq(BaseModel):
    model_id: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default="", max_length=500)


class GenerateSkillReq(BaseModel):
    playbook_id: str = Field(min_length=1)
    session_id: str = ""


class PromotePlaybookReq(BaseModel):
    playbook_id: str = Field(min_length=1)
    skill_name: str = Field(min_length=1)
    description: str = ""
    domain: str = "generated"
    risk_level: Literal["low", "medium", "high"] = "medium"


def _state(session: dict) -> dict:
    value = session.get("session_state") or {}
    return value if isinstance(value, dict) else {}


def _messages(session: dict) -> list[dict]:
    history = session.get("chat_history") or []
    if not history:
        history = _state(session).get("legacy_messages") or []
    result: list[dict] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = item.get("content")
        if role in {"user", "assistant"} and content is not None:
            result.append({"role": role, "content": str(content)})
    return result


def _data_rows(payload: Any) -> list[dict]:
    """Normalize AgentOS list and paginated response shapes."""

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data") or payload.get("items") or []
    return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []


def _ordered_runs(runs: list[dict]) -> list[dict]:
    """Return AgentOS runs oldest-first for deterministic UI restoration."""

    def timestamp(item: dict) -> float:
        value = item.get("created_at") or item.get("started_at") or item.get("updated_at")
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip()
        if not text:
            return float("inf")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return float("inf")

    return [
        run
        for _, run in sorted(
            enumerate(runs),
            key=lambda pair: (timestamp(pair[1]), pair[0]),
        )
    ]


_ACTIVE_RUN_STATUSES = {"pending", "running", "paused"}


def _active_run_record(runs: list[dict]) -> dict[str, Any] | None:
    for run in reversed(_ordered_runs(runs)):
        status = str(run.get("status") or "").lower()
        if status in _ACTIVE_RUN_STATUSES:
            return run
    return None


def _run_summary(run: dict) -> dict[str, Any]:
    indexes: list[int] = []
    for value in (run.get("last_event_index"), run.get("event_index")):
        try:
            if value is not None:
                indexes.append(int(value))
        except (TypeError, ValueError):
            pass
    events = run.get("events") or []
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            try:
                if event.get("event_index") is not None:
                    indexes.append(int(event["event_index"]))
            except (TypeError, ValueError):
                pass
    return {
        "run_id": str(run.get("run_id") or run.get("id") or ""),
        "status": str(run.get("status") or "").lower(),
        "last_event_index": max(indexes, default=-1),
    }


def _public_feedback(feedback: dict[str, Any] | None) -> dict[str, Any] | None:
    if not feedback:
        return None
    return {
        key: feedback.get(key)
        for key in ("run_id", "request_id", "call_id", "tool_name", "proposed_args", "description")
    }


def _active_run_snapshot(
    runs: list[dict],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Return active run, its matching public feedback, and the source record."""

    record = _active_run_record(runs)
    if record is None:
        return None, None, None
    active_run = _run_summary(record)
    feedback = None
    if active_run["status"] == "paused":
        feedback = feedback_request_from_payload(record)
        feedback["run_id"] = active_run["run_id"]
        feedback["request_id"] = active_run["run_id"]
    return active_run, _public_feedback(feedback), record


def _active_run_summary(runs: list[dict]) -> dict[str, Any] | None:
    """Return the newest non-terminal run and its durable resume cursor."""

    return _active_run_snapshot(runs)[0]


def _stream_event_frame(
    event: dict[str, Any],
    *,
    protocol_version: int,
    session_id: str,
    run_id: str,
) -> str:
    if protocol_version == 2:
        payload = {
            "protocol_version": 2,
            "session_id": session_id,
            "run_id": run_id,
            "events": [event],
        }
    else:
        payload = event
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# AgentOS does not persist the metadata form field sent with each run. Keep a
# versioned, self-delimiting copy of attachment references in run_input so a
# restored turn can still be edited without losing its upload identity.
_ATTACHMENT_BLOCK_START = "\n\n[附件文件:iGraph-v2]\n"
_ATTACHMENT_METADATA_MARKER = "\n[附件引用:iGraph-v2]\n"
_ATTACHMENT_BLOCK_END = "\n[/附件文件:iGraph-v2]"
_ATTACHMENT_BLOCK_HEADER = "以下文件已上传并注册到当前会话：\n"
_ATTACHMENT_BLOCK_FOOTER = "\n用户说‘这个文件/图数据’时即指上述附件；不要要求用户再次上传。"
_LEGACY_ATTACHMENT_BLOCK_MARKER = "\n\n[附件文件]\n"


def _parse_run_input(text: str) -> "tuple[str, list[dict]]":
    """Return the user-authored text and recoverable attachment references."""

    idx = text.rfind(_ATTACHMENT_BLOCK_START)
    if idx >= 0 and text.endswith(_ATTACHMENT_BLOCK_END):
        block = text[idx + len(_ATTACHMENT_BLOCK_START) : -len(_ATTACHMENT_BLOCK_END)]
        marker = block.rfind(_ATTACHMENT_METADATA_MARKER)
        human_context = block[:marker] if marker >= 0 else ""
        if (marker >= 0 and human_context.startswith(_ATTACHMENT_BLOCK_HEADER)
                and human_context.endswith(_ATTACHMENT_BLOCK_FOOTER)):
            try:
                payload = json.loads(block[marker + len(_ATTACHMENT_METADATA_MARKER) :])
            except (TypeError, ValueError):
                payload = None
            values = payload.get("attachments") if isinstance(payload, dict) else None
            if isinstance(values, list) and values:
                attachments = []
                for value in values:
                    if not isinstance(value, dict):
                        attachments = []
                        break
                    filename = value.get("filename")
                    file_token = value.get("file_token")
                    if not isinstance(filename, str) or not filename or not isinstance(file_token, str) or not file_token:
                        attachments = []
                        break
                    attachments.append({"file_token": file_token, "filename": filename})
                if attachments:
                    return text[:idx], attachments

    # Runs created before v2 contain only a human-readable block. Require its
    # complete fixed framing so ordinary user text containing "[附件文件]" is
    # not mistaken for injected context.
    idx = text.rfind(_LEGACY_ATTACHMENT_BLOCK_MARKER)
    if idx < 0 or not text.endswith(_ATTACHMENT_BLOCK_FOOTER):
        return text, []
    block = text[idx + len(_LEGACY_ATTACHMENT_BLOCK_MARKER) : -len(_ATTACHMENT_BLOCK_FOOTER)]
    if not block.startswith(_ATTACHMENT_BLOCK_HEADER):
        return text, []
    lines = block[len(_ATTACHMENT_BLOCK_HEADER) :].splitlines()
    attachments: list[dict] = []
    for line in lines:
        value = line.strip()
        if not value.startswith("- "):
            return text, []
        filename, separator, hint = value[2:].rpartition("（")
        if not separator or not filename or not hint.endswith("）"):
            return text, []
        attachments.append({"file_token": "", "filename": filename})
    return (text[:idx], attachments) if attachments else (text, [])


def _restore_attachment_tokens(
    attachments: list[AttachmentRef] | None,
    *,
    user_id: str,
    session_id: str,
) -> list[AttachmentRef]:
    """Rebind display-only legacy attachments to one uploaded session file."""

    values = list(attachments or [])
    if not any(not item.file_token.strip() for item in values):
        return values
    by_filename: dict[str, list[str]] = {}
    for item in visible_files(user_id, session_id):
        if item.get("zone") != "Uploaded":
            continue
        stored_name = Path(str(item.get("path") or "")).name
        file_token, separator, filename = stored_name.partition("_")
        if separator and file_token and filename:
            by_filename.setdefault(filename, []).append(file_token)
    restored: list[AttachmentRef] = []
    for item in values:
        if item.file_token.strip():
            restored.append(item)
            continue
        filename = str(item.filename or "")
        matches = by_filename.get(filename, [])
        if len(matches) != 1:
            message = (
                f"无法唯一定位历史附件“{filename}”，请重新上传后再修改消息"
                if len(matches) > 1
                else f"历史附件“{filename}”已失效，请重新上传后再修改消息"
            )
            raise HTTPException(409, {"code": "ATTACHMENT_REFERENCE_EXPIRED", "message": message})
        restored.append(item.model_copy(update={"file_token": matches[0]}))
    return restored


async def _sse_with_heartbeat(
    source: AsyncIterator[str],
    *,
    heartbeat_seconds: float,
) -> AsyncIterator[str]:
    """Forward an SSE stream and keep every downstream hop alive while idle.

    ``asyncio.wait_for`` cannot be used here because timing out ``__anext__``
    would cancel the in-flight upstream HTTP read. Keep that read task alive
    and emit an SSE comment whenever no application event arrives in time.
    """

    interval = max(0.01, float(heartbeat_seconds))
    iterator = source.__aiter__()
    pending: asyncio.Task[str] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield ": keep-alive\n\n"
                continue
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield chunk
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        close = getattr(iterator, "aclose", None)
        if close is not None:
            with suppress(RuntimeError):
                await close()


def _run_input_text(run: dict) -> str:
    metadata = run.get("metadata")
    turn = metadata.get("igraph_turn") if isinstance(metadata, dict) else None
    if isinstance(turn, dict) and isinstance(turn.get("user_message"), str):
        return turn["user_message"]
    value = run.get("run_input")
    if isinstance(value, str):
        clean, _ = _parse_run_input(value)
        return clean
    if isinstance(value, dict):
        for key in ("input_content", "message", "content"):
            if value.get(key) is not None:
                clean, _ = _parse_run_input(str(value[key]))
                return clean
        return json.dumps(value, ensure_ascii=False)
    if value is not None:
        return str(value)
    messages = run.get("messages") or []
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "user" and not message.get("from_history"):
                return str(message.get("content") or "")
    return ""


def _run_output_text(run: dict) -> str:
    value = run.get("content")
    if isinstance(value, str):
        return value
    if value is None:
        messages = run.get("messages") or []
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("role") == "assistant" and not message.get("from_history"):
                    return str(message.get("content") or "")
        return ""
    return json.dumps(value, ensure_ascii=False)


def _decoded_result(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _merge_training_job_card(card: dict, status_result: dict) -> None:
    job = status_result.get("job")
    if not isinstance(job, dict) or str(job.get("tool_name") or "") != "train":
        return

    history = [item for item in (card.get("history") or []) if isinstance(item, dict)]
    history.extend(
        item
        for item in (status_result.get("progress_updates") or [])
        if isinstance(item, dict) and item.get("kind") in {None, "train_epoch"}
    )
    detail = job.get("detail") if isinstance(job.get("detail"), dict) else {}
    if detail.get("epoch") is not None and detail.get("kind") in {None, "train_epoch"}:
        history.append(detail)
    by_epoch: dict[int, dict] = {}
    without_epoch: list[dict] = []
    for item in history:
        try:
            epoch = int(item["epoch"])
        except (KeyError, TypeError, ValueError):
            without_epoch.append(item)
        else:
            by_epoch[epoch] = item
    merged_history = without_epoch + [by_epoch[key] for key in sorted(by_epoch)]

    current_result = card.get("result") if isinstance(card.get("result"), dict) else {}
    final_result = _decoded_result(job.get("result"))
    if isinstance(final_result, dict):
        current_result = {**current_result, **final_result}
    current_result["job_id"] = job.get("job_id")
    current_result["job_status"] = job.get("status")
    if job.get("error"):
        current_result["error"] = job["error"]
    current_result["history"] = merged_history
    card["result"] = current_result
    card["history"] = merged_history
    card["progress"] = {
        **detail,
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "progress": job.get("progress", detail.get("progress", 0)),
        "message": job.get("message", detail.get("message", "")),
    }
    card["done"] = str(job.get("status") or "").lower() in {"done", "failed", "cancelled", "unavailable"}


def _training_cards_from_runs(runs: list[dict]) -> list[dict]:
    """Restore normalized train tool cards from persisted AgentOS runs."""

    cards: list[dict] = []
    for message in _messages_from_runs(runs):
        for tool in message.get("tools") or []:
            if not isinstance(tool, dict) or str(tool.get("tool") or "") != "train":
                continue
            cards.append(dict(tool))
    return cards


def _project_training_summary(cards: list[dict], limit: int = 30) -> list[dict]:
    """Build a bounded, backend-verified context for cross-session comparisons."""

    summary: list[dict] = []
    for card in cards:
        result = card.get("result") if isinstance(card.get("result"), dict) else {}
        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        if not metrics or card.get("status_unavailable"):
            continue
        args = card.get("args") if isinstance(card.get("args"), dict) else {}
        progress = card.get("progress") if isinstance(card.get("progress"), dict) else {}
        summary.append({
            "training_run_id": card.get("training_run_id"),
            "session_id": card.get("session_id"),
            "session_title": card.get("session_title"),
            "dataset_id": card.get("dataset_id"),
            "dataset_name": card.get("dataset_name"),
            "task": result.get("task") or progress.get("task") or args.get("task"),
            "model": result.get("display_model") or result.get("model") or args.get("model") or args.get("method"),
            "model_id": result.get("model_id"),
            "train_config": _decoded_result(args.get("train_config")),
            "algo_config": _decoded_result(args.get("algo_config")),
            "status": result.get("job_status") or progress.get("status") or result.get("status")
            or ("failed" if result.get("success") is False or result.get("error") else None)
            or card.get("status") or ("done" if card.get("done") else "running"),
            "metrics": metrics,
        })
    return summary[:max(1, limit)]


def _plan_from_run(run: dict) -> dict | None:
    """Rebuild the legacy read-only plan card from native reasoning steps."""

    values = run.get("reasoning_steps") or []
    if isinstance(values, dict):
        values = values.get("steps") or []
    if not isinstance(values, list):
        return None
    steps: list[dict] = []
    for index, raw in enumerate(values, start=1):
        if not isinstance(raw, dict):
            continue
        goal = str(raw.get("title") or raw.get("action") or raw.get("goal") or f"推理步骤 {index}").strip()
        summary = str(raw.get("result") or raw.get("conclusion") or raw.get("reasoning") or "").strip()
        steps.append({
            "step_id": index,
            "goal": goal[:500],
            "status": "done",
            "result_summary": summary[:2000],
            "tool_hints": [],
            "depends_on": [],
            "source": "agentos_reasoning",
        })
    if not steps:
        return None
    return {
        "plan_id": str(run.get("run_id") or ""),
        "completed": str(run.get("status") or "").lower() == "completed",
        "steps": steps,
        "source": "agentos_reasoning",
    }


def _messages_from_runs(runs: list[dict]) -> list[dict]:
    """Rebuild the legacy UI message/artifact contract from AgentOS runs."""

    messages: list[dict] = []
    async_training_cards: dict[str, dict] = {}
    for run in runs:
        run_id = str(run.get("run_id") or run.get("id") or "")
        status = str(run.get("status") or "").lower()
        user_text = _run_input_text(run).strip()
        metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        turn = metadata.get("igraph_turn") if isinstance(metadata.get("igraph_turn"), dict) else {}
        if user_text:
            # Prefer the clean metadata attachments list; fall back to parsing
            # filenames out of the injected run_input block when AgentOS has not
            # persisted the metadata field (which is the common case).
            attachments = turn.get("attachments")
            if not attachments:
                raw_input = run.get("run_input") or ""
                if isinstance(raw_input, str):
                    _, attachments = _parse_run_input(raw_input)
            messages.append({"id": f"user:{run_id}", "run_id": run_id, "role": "user", "content": user_text,
                             "attachments": attachments or []})

        tool_cards: list[dict] = []
        graphs: list[dict] = []
        reports: list[dict] = []
        artifacts: list[dict] = []
        projection = project_run(run)
        tools = run.get("tools") or []
        if not tools:
            # Some native/checkpoint records retain tool events without a tools
            # summary. Rebuild the existing dev cards so snapshots do not erase
            # training controls or artifacts when the next run starts.
            tools = [{"tool_name": item["tool"].get("tool"),
                      "tool_call_id": item["tool"].get("call_id"),
                      "tool_args": item["tool"].get("args") or {},
                      "result": item["tool"].get("result"),
                      "status": item["tool"].get("status")}
                     for item in projection["processItems"] if item.get("kind") == "tool"]
        for tool in tools if isinstance(tools, list) else []:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("tool_name") or tool.get("name") or "")
            if not name:
                continue
            call_id = str(tool.get("tool_call_id") or tool.get("id") or "")
            result = _decoded_result(tool.get("result"))
            card = {
                "tool": name,
                "args": tool.get("tool_args") or tool.get("arguments") or {},
                "call_id": call_id,
                "done": True,
                "result": result,
            }
            if tool.get("tool_call_error") or tool.get("status") == "failed":
                card["status"] = "failed"
            elif tool.get("status") == "cancelled":
                card["status"] = "cancelled"
            tool_cards.append(card)
            if name == "train" and isinstance(result, dict) and result.get("async") and result.get("job_id"):
                job_id = str(result["job_id"])
                card["done"] = False
                card["job_id"] = job_id
                async_training_cards[job_id] = card
            elif name == "get_job_status" and isinstance(result, dict) and isinstance(result.get("job"), dict):
                job_id = str(result["job"].get("job_id") or "")
                training_card = async_training_cards.get(job_id)
                if training_card is not None:
                    _merge_training_job_card(training_card, result)
            if not isinstance(result, dict) or result.get("success") is False:
                continue
            if name == "visualize_subgraph":
                graph = result.get("data") if isinstance(result.get("data"), dict) else result
                if isinstance(graph.get("nodes"), list) and isinstance(graph.get("links", graph.get("edges")), list):
                    graphs.append({
                        **graph,
                        "id": str(graph.get("id") or f"run-{run.get('run_id', '')}-{call_id}"),
                        "edges": graph.get("edges", graph.get("links", [])),
                    })
            elif name == "create_analysis_report" and isinstance(result.get("report"), dict):
                reports.append(result["report"])
            elif name == "workspace_publish":
                # A published Derived file is a user-visible delivery for this turn.
                # Surface it on the assistant message, not only in the files panel.
                published = result.get("artifact") if isinstance(result.get("artifact"), dict) else result
                path = str(published.get("path") or published.get("relative_name") or "")
                if path.startswith("Derived/"):
                    artifacts.append({
                        "path": path,
                        "filename": path.split("/")[-1],
                        "size": published.get("size"),
                        "mime_type": published.get("mime_type"),
                        "artifact_id": published.get("artifact_id"),
                    })

        # Custom progress events are native, persisted run events. Rebuild the
        # same card by tool-call ID, including snapshots taken before completion.
        cards_by_id = {str(card.get("call_id") or ""): card for card in tool_cards}
        completed_results = {card["call_id"]: card["result"] for card in tool_cards
                             if card.get("result") is not None and card.get("done")}
        progress_calls = set()
        for event in run.get("events") or []:
            if not isinstance(event, dict):
                continue
            progress_content = graph_job_progress_payload(event)
            if progress_content is None:
                continue
            call_id = str(event.get("tool_call_id") or "")
            if not call_id:
                continue
            progress_calls.add(call_id)
            card = cards_by_id.get(call_id)
            if card is None:
                card = {"tool": progress_content.get("tool_name") or progress_content["job"].get("tool_name"),
                        "args": progress_content.get("tool_args") or {}, "call_id": call_id,
                        "done": False, "result": None}
                cards_by_id[call_id] = card
                tool_cards.append(card)
            job = progress_content["job"]
            if card["tool"] == "train":
                _merge_training_job_card(card, {"job": {**job, "tool_name": "train"},
                                               "progress_updates": progress_content.get("progress_updates") or []})
            else:
                card["progress"] = {**(job.get("detail") or {}), "job_id": job["job_id"],
                                    "status": job.get("status"), "progress": job.get("progress", 0),
                                    "message": job.get("message", "")}
                card["done"] = job.get("status") in {"done", "failed", "cancelled"}
            card["job_id"] = job["job_id"]
            if progress_content.get("progress_has_more") or progress_content.get("job_chain_continues"):
                card["done"] = False
            if not card["done"] and status in {"cancelled", "failed", "error"}:
                card["done"] = True
                card["progress"]["status"] = "cancelled" if status == "cancelled" else "failed"
                if isinstance(card.get("result"), dict):
                    card["result"]["job_status"] = card["progress"]["status"]

        for call_id in progress_calls & completed_results.keys():
            # A persisted native completion is newer than intermediate progress,
            # including when polling failed before a terminal progress event.
            card = cards_by_id[call_id]
            final = completed_results[call_id]
            card["done"] = True
            if isinstance(final, dict):
                current = card.get("result") if isinstance(card.get("result"), dict) else {}
                card["result"] = {**current, **final}
                if final.get("success") is not False:
                    card["progress"].update(status="done", progress=1)
                elif final.get("status") in {"failed", "cancelled", "canceled"}:
                    card["progress"]["status"] = "cancelled" if final["status"] == "canceled" else final["status"]
                card["result"]["job_status"] = card["progress"]["status"]
            else:
                card["result"] = final

        feedback_requests = (
            [feedback_request_from_payload(run)]
            if status == "paused"
            else []
        )
        if feedback_requests and tool_cards:
            pending_call_id = str(feedback_requests[0].get("call_id") or "")
            if pending_call_id:
                for card in tool_cards:
                    if str(card.get("call_id") or "") == pending_call_id:
                        card["done"] = False
            else:
                tool_cards[-1]["done"] = False
        assistant_text = _run_output_text(run)
        reasoning_text = str(run.get("reasoning_content") or "").strip()
        plan = _plan_from_run(run)
        if assistant_text or tool_cards or graphs or reports or feedback_requests or status in {"completed", "error", "failed", "cancelled"}:
            messages.append({
                "id": f"assistant:{run_id}",
                "run_id": run_id,
                "role": "assistant",
                "content": assistant_text,
                "run_status": status,
                "model_snapshot": metadata.get('model_snapshot'),
                "history_compacted": bool(metadata.get('history_compacted')),
                "tools": tool_cards,
                "graphs": graphs,
                "reports": reports,
                "artifacts": artifacts,
                "thinking": ([{"phase": "reasoning", "text": reasoning_text, "streaming": False}] if reasoning_text else []),
                "processItems": projection["processItems"],
                "regenerate_of": turn.get("regenerate_of"),
                "feedbackRequests": feedback_requests,
                "plan": plan,
            })
    return messages


def _sft_row(run: dict, output_format: str) -> dict | None:
    user_text = _run_input_text(run).strip()
    assistant_text = _run_output_text(run).strip()
    if not user_text or not assistant_text:
        return None
    tools = run.get("tools") or []
    tool_names = [
        str(item.get("tool_name") or item.get("name") or "")
        for item in tools
        if isinstance(item, dict) and (item.get("tool_name") or item.get("name"))
    ] if isinstance(tools, list) else []
    if output_format == "raw":
        return run
    if output_format == "sharegpt":
        conversations = [{"from": "human", "value": user_text}]
        conversations.extend(
            {"from": "function_call", "value": json.dumps({"tool": name}, ensure_ascii=False)}
            for name in tool_names
        )
        conversations.append({"from": "gpt", "value": assistant_text})
        return {"conversations": conversations}
    return {
        "instruction": user_text,
        "input": ", ".join(tool_names),
        "output": assistant_text,
    }


def _session_row(session: dict, default_model: str) -> dict:
    state = _state(session)
    messages = _messages(session)
    preview = str(state.get("last_message_preview") or "")
    if not preview and messages:
        preview = str(messages[-1].get("content") or "")[:120]
    return {
        "session_id": session.get("session_id"),
        "project_id": state.get("project_id") or None,
        "active_dataset_id": state.get("active_dataset_id"),
        "title": session.get("session_name") or "新会话",
        "last_message_preview": preview,
        "updated_at": session.get("updated_at"),
        "created_at": session.get("created_at"),
        "model": state.get("model", default_model),
        "provider": state.get("provider", "openai-compatible"),
        "reasoning_mode": state.get("reasoning_mode", "direct"),
        "planning_mode": state.get("planning_mode", "auto"),
        "archived": bool(state.get("archived", False)),
        "active": False,
    }


def _session_activity(session: dict) -> float:
    value = session.get('updated_at') or session.get('created_at') or 0
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        except ValueError:
            return 0


def _project_row(project: ProjectRecord, conversation_count: int = 0) -> dict:
    row = asdict(project)
    row["created_at"] = datetime.fromtimestamp(project.created_at, timezone.utc).isoformat()
    row["updated_at"] = datetime.fromtimestamp(project.updated_at, timezone.utc).isoformat()
    row["conversation_count"] = conversation_count
    return row


def _project_dataset_row(dataset: ProjectDatasetRecord, default_dataset_id: str | None = None) -> dict:
    row = asdict(dataset)
    row["created_at"] = datetime.fromtimestamp(dataset.created_at, timezone.utc).isoformat()
    row["updated_at"] = datetime.fromtimestamp(dataset.updated_at, timezone.utc).isoformat()
    row["is_default"] = dataset.dataset_id == default_dataset_id
    row.pop("storage_path", None)
    return row


_GRAPH_COLLECTION_FORMAT = "graph_classification_dataset"
_PARTITIONED_GRAPH_KIND = "partitioned_graph"
_PARTITIONED_GRAPH_REPRESENTATION = "partitioned_snapshot"
_SERVER_PREPROCESS_FILE_TYPES = frozenset({"txt", "csv", "tsv", "edgelist", "dat"})


def _graph_collection_summary(payload: Any) -> dict[str, Any]:
    """Extract a stable, JSON-safe graph collection summary."""

    source = payload if isinstance(payload, dict) else {}
    split_sizes = source.get("split_sizes")
    if not isinstance(split_sizes, dict):
        split_sizes = {}
    summary: dict[str, Any] = {
        "num_graphs": source.get("num_graphs"),
        "num_tasks": source.get("num_tasks"),
        "metric": source.get("metric"),
        "split_sizes": {
            str(name): int(value)
            for name, value in split_sizes.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
    }
    for key in (
        "node_vocab_size",
        "edge_vocab_size",
        "has_missing_labels",
        "missing_label_rate",
        "num_edgeless_graphs",
        "uses_edge_features",
        "semantic_type",
        "storage_type",
    ):
        if source.get(key) is not None:
            summary[key] = source[key]
    return {key: value for key, value in summary.items() if value is not None}


def _is_partitioned_graph_metadata(metadata: Any) -> bool:
    source = metadata if isinstance(metadata, dict) else {}
    return bool(
        str(source.get("dataset_kind") or "").strip() == _PARTITIONED_GRAPH_KIND
        or str(source.get("representation") or "").strip()
        == _PARTITIONED_GRAPH_REPRESENTATION
        or str(source.get("format_detected") or "").strip()
        == _PARTITIONED_GRAPH_REPRESENTATION
    )


def _bounded_count(value: Any) -> int | None:
    """Return a JSON-safe non-negative integer without coercing booleans/floats."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _partitioned_graph_context(
    payload: Any,
    *,
    dataset_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    """Build the bounded, session-bound context exposed to AgentOS.

    Project dataset metadata is shared by every conversation in a project, so
    it must never be treated as sufficient proof that another session owns the
    same snapshot.  The returned binding is deliberately small and contains no
    paths, source contents, shard lists or previews.
    """

    source = payload if isinstance(payload, dict) else {}
    if str(source.get("representation") or "").strip() != _PARTITIONED_GRAPH_REPRESENTATION:
        return None
    graph_id = str(source.get("graph_id") or "").strip()
    graph_version = _bounded_count(source.get("graph_version"))
    if graph_id != session_id or graph_version is None or graph_version < 1:
        raise ValueError("分区快照返回的 graph_id/graph_version 与当前会话不一致")

    context: dict[str, Any] = {
        "dataset_id": str(dataset_id),
        "graph_id": graph_id,
        "graph_version": graph_version,
        "representation": _PARTITIONED_GRAPH_REPRESENTATION,
        "current_graph_replaced": False,
    }
    for key in ("num_nodes", "num_edges"):
        count = _bounded_count(source.get(key))
        if count is not None:
            context[key] = count
    summary = source.get("summary") if isinstance(source.get("summary"), dict) else {}
    if isinstance(summary.get("directed"), bool):
        context["directed"] = summary["directed"]
    elif isinstance(source.get("directed"), bool):
        context["directed"] = source["directed"]
    return context


def _partitioned_graph_context_from_metadata(
    dataset: ProjectDatasetRecord,
    *,
    session_id: str,
) -> dict[str, Any] | None:
    metadata = dataset.metadata if isinstance(dataset.metadata, dict) else {}
    if not _is_partitioned_graph_metadata(metadata):
        return None
    # Metadata is project-scoped.  Only a newly stored exact session binding is
    # eligible for migration into session_state; older records are prepared
    # once more rather than risking reuse of another conversation's graph.
    if str(metadata.get("graph_id") or "").strip() != session_id:
        return None
    try:
        return _partitioned_graph_context(
            {
                **metadata,
                "representation": _PARTITIONED_GRAPH_REPRESENTATION,
                "graph_id": session_id,
            },
            dataset_id=dataset.dataset_id,
            session_id=session_id,
        )
    except ValueError:
        return None


def _normalize_partitioned_graph_context(
    value: Any,
    *,
    dataset_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    source = value if isinstance(value, dict) else {}
    if str(source.get("dataset_id") or "") != str(dataset_id):
        return None
    try:
        return _partitioned_graph_context(
            source,
            dataset_id=dataset_id,
            session_id=session_id,
        )
    except ValueError:
        return None


def _large_graph_prepare_context(
    payload: Any,
    *,
    dataset_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Return the bounded durable-job binding persisted in session_state."""

    source = payload if isinstance(payload, dict) else {}
    job_id = str(source.get("job_id") or "").strip()
    graph_id = str(source.get("graph_id") or "").strip()
    graph_version = _bounded_count(source.get("graph_version"))
    status = str(source.get("status") or "queued").strip().lower()
    if (
        not job_id.startswith("lg_")
        or graph_id != session_id
        or graph_version is None
        or graph_version < 1
        or status not in {"queued", "running"}
    ):
        raise ValueError("大图准备作业缺少有效的会话、版本或任务绑定")
    return {
        "dataset_id": str(dataset_id),
        "job_id": job_id,
        "graph_id": graph_id,
        "graph_version": graph_version,
        "status": status,
    }


def _normalize_large_graph_prepare_context(
    value: Any,
    *,
    dataset_id: str,
    session_id: str,
) -> dict[str, Any] | None:
    source = value if isinstance(value, dict) else {}
    if str(source.get("dataset_id") or "") != str(dataset_id):
        return None
    try:
        return _large_graph_prepare_context(
            source,
            dataset_id=dataset_id,
            session_id=session_id,
        )
    except ValueError:
        return None


def _large_graph_prepare_context_from_metadata(
    dataset: ProjectDatasetRecord,
    *,
    session_id: str,
) -> dict[str, Any] | None:
    """Recover the exact pending job after a Backend crash during state write."""

    metadata = dataset.metadata if isinstance(dataset.metadata, dict) else {}
    if (
        str(metadata.get("graph_id") or "") != session_id
        or str(metadata.get("preparation_status") or "")
        not in {"queued", "running"}
    ):
        return None
    return _normalize_large_graph_prepare_context(
        {
            "dataset_id": dataset.dataset_id,
            "job_id": metadata.get("preparation_job_id"),
            "graph_id": metadata.get("graph_id"),
            "graph_version": metadata.get("graph_version"),
            "status": metadata.get("preparation_status"),
        },
        dataset_id=dataset.dataset_id,
        session_id=session_id,
    )


def _large_graph_result_matches_prepare(payload: Any, pending: Any) -> bool:
    """Fence a terminal result to the durable prepare job that was submitted."""

    source = payload if isinstance(payload, dict) else {}
    binding = pending if isinstance(pending, dict) else {}
    return bool(
        str(source.get("job_id") or "") == str(binding.get("job_id") or "")
        and str(source.get("graph_id") or "") == str(binding.get("graph_id") or "")
        and _bounded_count(source.get("graph_version"))
        == _bounded_count(binding.get("graph_version"))
        and str(source.get("status") or "") == "done"
        and str(source.get("representation") or "")
        == _PARTITIONED_GRAPH_REPRESENTATION
    )


def _published_partitioned_version(
    payload: Any, context: Any
) -> dict[str, Any] | None:
    """Return the exact published row from a bounded version-list response."""

    source = payload if isinstance(payload, dict) else {}
    binding = context if isinstance(context, dict) else {}
    graph_id = str(binding.get("graph_id") or "")
    graph_version = _bounded_count(binding.get("graph_version"))
    if (
        source.get("success") is False
        or not graph_id
        or graph_version is None
        or str(source.get("graph_id") or "") != graph_id
    ):
        return None
    versions = source.get("versions")
    if not isinstance(versions, list):
        return None
    for row in versions:
        if not isinstance(row, dict):
            continue
        if (
            _bounded_count(row.get("graph_version")) == graph_version
            and str(row.get("status") or "") == "published"
            and (not row.get("graph_id") or str(row.get("graph_id")) == graph_id)
        ):
            return row
    return None


def _partitioned_version_is_published(payload: Any, context: Any) -> bool:
    """Validate an exact graph/version binding in a bounded control response."""

    return _published_partitioned_version(payload, context) is not None


def _dataset_view(dataset: ProjectDatasetRecord | None) -> dict[str, Any] | None:
    if dataset is None:
        return None
    metadata = dataset.metadata if isinstance(dataset.metadata, dict) else {}
    kind = str(metadata.get("dataset_kind") or "").strip()
    detected_format = str(metadata.get("format_detected") or "").strip()
    if kind == "graph_collection" or detected_format == _GRAPH_COLLECTION_FORMAT:
        return {
            "kind": "graph_collection",
            "visualization": {"supported": False, "code": "NON_SINGLE_GRAPH"},
            "summary": _graph_collection_summary(metadata),
        }
    if _is_partitioned_graph_metadata(metadata):
        storage_bytes = metadata.get("storage_bytes")
        if isinstance(storage_bytes, dict):
            storage_bytes = storage_bytes.get("total")
        summary: dict[str, Any] = {
            "representation": _PARTITIONED_GRAPH_REPRESENTATION,
        }
        for key in ("num_nodes", "num_edges", "graph_version", "partitions"):
            value = _bounded_count(metadata.get(key))
            if value is not None:
                summary[key] = value
        if isinstance(metadata.get("directed"), bool):
            summary["directed"] = metadata["directed"]
        if isinstance(metadata.get("has_weights"), bool):
            summary["has_weights"] = metadata["has_weights"]
        storage_total = _bounded_count(storage_bytes)
        if storage_total is not None:
            summary["storage_bytes"] = storage_total
        preparation_status = str(metadata.get("preparation_status") or "").strip()
        if preparation_status in {"queued", "running"}:
            summary["preparation_status"] = preparation_status
        return {
            "kind": _PARTITIONED_GRAPH_KIND,
            "visualization": {
                "supported": False,
                "code": "PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE",
            },
            "summary": summary,
        }
    return {
        "kind": "single_graph",
        "visualization": {"supported": True, "code": None},
        "summary": {
            key: metadata[key]
            for key in ("num_nodes", "num_edges", "directed", "has_node_features", "has_node_labels")
            if metadata.get(key) is not None
        },
    }


def _session_dataset_view(
    dataset: ProjectDatasetRecord | None,
    state: Any,
    *,
    session_id: str,
) -> dict[str, Any] | None:
    """Return a dataset view fenced to this conversation's snapshot binding.

    Project dataset metadata is shared, while every large snapshot belongs to
    one session.  A project-level graph version may therefore describe a
    different conversation and must not be presented as the active binding.
    """

    view = _dataset_view(dataset)
    if (
        dataset is None
        or not isinstance(view, dict)
        or view.get("kind") != _PARTITIONED_GRAPH_KIND
    ):
        return view

    session_state = state if isinstance(state, dict) else {}
    context = _normalize_partitioned_graph_context(
        session_state.get("large_graph_context"),
        dataset_id=dataset.dataset_id,
        session_id=session_id,
    )
    pending = _normalize_large_graph_prepare_context(
        session_state.get("large_graph_prepare"),
        dataset_id=dataset.dataset_id,
        session_id=session_id,
    )
    metadata = dataset.metadata if isinstance(dataset.metadata, dict) else {}

    binding = context or pending
    binding_version = (
        _bounded_count(binding.get("graph_version"))
        if isinstance(binding, dict)
        else None
    )
    metadata_matches = bool(
        str(metadata.get("graph_id") or "") == session_id
        and binding_version is not None
        and _bounded_count(metadata.get("graph_version")) == binding_version
    )

    summary: dict[str, Any] = {
        "representation": _PARTITIONED_GRAPH_REPRESENTATION,
    }
    if binding_version is not None:
        summary["graph_version"] = binding_version
    if context is not None:
        for key in ("num_nodes", "num_edges"):
            value = _bounded_count(context.get(key))
            if value is not None:
                summary[key] = value
        if isinstance(context.get("directed"), bool):
            summary["directed"] = context["directed"]
    elif pending is not None:
        summary["preparation_status"] = pending["status"]
    else:
        # The dataset is known to use partitioned snapshots but this session
        # has not submitted or recovered its own version yet.
        summary["preparation_status"] = "not_started"

    if metadata_matches:
        base_summary = view.get("summary")
        if isinstance(base_summary, dict):
            for key in (
                "num_nodes",
                "num_edges",
                "directed",
                "partitions",
                "has_weights",
                "storage_bytes",
            ):
                if base_summary.get(key) is not None and key not in summary:
                    summary[key] = base_summary[key]

    return {
        "kind": _PARTITIONED_GRAPH_KIND,
        "visualization": {
            "supported": False,
            "code": "PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE",
        },
        "summary": summary,
    }


def _message_with_attachments(req: ChatReq) -> str:
    attachments = [item for item in (req.attachments or []) if item.filename]
    if not attachments:
        return req.message
    hints: list[str] = []
    for attachment in attachments:
        name = str(attachment.filename)
        suffix = Path(name).suffix.lower()
        if suffix in {".sql", ".csv", ".txt"}:
            hint = "源文件已保存到数据集；从 data_state.construction_sources 获取 dataset_id，检查构图状态。算法待接入时引导用户使用数据集旁的构图按钮，不调用旧上传构图工具"
        elif suffix == ".pt":
            hint = "多图分类数据集使用 load_graph_classification_dataset；普通单图使用 load_graph_from_upload"
        elif suffix in {".npz", ".json", ".tsv", ".edgelist", ".dat"}:
            hint = "使用 load_graph_from_upload 自动检测并加载"
        else:
            hint = "根据文件格式选择上传文件加载工具"
        hints.append(f"- {name}（{hint}）")
    references = {
        "attachments": [
            {"file_token": item.file_token, "filename": item.filename}
            for item in attachments
        ]
    }
    return (
        req.message
        + _ATTACHMENT_BLOCK_START
        + _ATTACHMENT_BLOCK_HEADER
        + "\n".join(hints)
        + _ATTACHMENT_BLOCK_FOOTER
        + _ATTACHMENT_METADATA_MARKER
        + json.dumps(references, ensure_ascii=False, separators=(",", ":"))
        + _ATTACHMENT_BLOCK_END
    )


def _canonical_skill_name(value: str) -> str:
    name = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", value.strip().lower())).strip("-")
    if not name:
        raise HTTPException(status_code=400, detail="Skill 名称必须包含小写字母或数字")
    return name


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def create_app(config: BackendConfig | None = None) -> FastAPI:
    settings = config or BackendConfig.from_env()
    users = UserDirectory(
        settings.users_file,
        settings.supported_ids_file,
        settings.admin_user_id,
        settings.default_admin_model,
        settings.default_user_model,
    )
    custom_models = CustomModelDirectory(settings.custom_models_file or Path("data/backend/custom_models.json").resolve())
    projects = ProjectDirectory(settings.projects_file)
    data_root = settings.projects_file.parent.parent if settings.projects_file.parent.name == "backend" else settings.projects_file.parent
    project_datasets = ProjectDatasetDirectory(
        Path(os.getenv("BACKEND_PROJECT_DATASETS_FILE", str(settings.projects_file.with_name("project_datasets.json")))).resolve(),
        Path(os.getenv("BACKEND_PROJECT_DATA_ROOT", str(data_root / "projects"))).resolve(),
    )
    try:
        server_dataset_limit = max(
            1, int(os.getenv("BACKEND_SERVER_DATASETS_MAX_ENTRIES", "10000"))
        )
    except (TypeError, ValueError):
        server_dataset_limit = 10_000
    server_datasets = ServerDatasetDirectory(
        Path(
            os.getenv(
                "BACKEND_SERVER_DATASETS_DIR",
                str(data_root / "server_datasets"),
            )
        ).resolve(),
        max_entries=server_dataset_limit,
        default_directed=os.getenv(
            "BACKEND_SERVER_DATASET_DEFAULT_DIRECTED", "true"
        ).strip().lower()
        not in {"0", "false", "no", "off"},
    )
    try:
        server_preprocess_min_bytes = max(
            0,
            int(
                os.getenv(
                    "BACKEND_SERVER_DATASET_PREPROCESS_MIN_BYTES",
                    str(64 * 1024 * 1024),
                )
            ),
        )
    except (TypeError, ValueError):
        server_preprocess_min_bytes = 64 * 1024 * 1024
    try:
        server_dataset_scan_seconds = max(
            1.0,
            float(os.getenv("BACKEND_SERVER_DATASET_SCAN_SECONDS", "30")),
        )
    except (TypeError, ValueError):
        server_dataset_scan_seconds = 30.0
    runtime = ServiceClient(settings.agent_runtime_url, settings.request_timeout, settings.stream_timeout)
    graph = ServiceClient(settings.graph_engine_url, settings.request_timeout, settings.stream_timeout)
    general_migration = GeneralMigration(projects, project_datasets, runtime, settings.agent_id, graph if os.getenv("IGRAPH_CONTROL_SERVICE_KEY") else None)
    project_requests: dict[tuple[str, str], set[asyncio.Task]] = {}
    project_reconciler = None
    run_locks: dict[tuple[str, str], asyncio.Lock] = {}
    run_locks_guard = asyncio.Lock()
    session_creation_locks: dict[tuple[str, str], asyncio.Lock] = {}
    session_creation_lock_users: dict[tuple[str, str], int] = {}
    session_creation_locks_guard = asyncio.Lock()
    reserved_runs: set[tuple[str, str]] = set()
    preparations: dict[tuple[str, str, str], asyncio.Task] = {}
    preparation_finished: dict[tuple[str, str, str], dict] = {}
    preparation_lease = ProcessLease(settings.users_file.parent / 'executors') if controls_enabled() else None
    preparation_instance = preparation_lease.identity if preparation_lease else str(uuid.uuid4())
    starting_tasks: dict[tuple[str, str, str], set[asyncio.Task]] = {}
    starting_finished: dict[tuple[str, str, str], dict] = {}
    launch_tasks: set[asyncio.Task] = set()

    def own_launch(operation):
        task = asyncio.create_task(operation)
        launch_tasks.add(task)
        def finished(done):
            launch_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        return task
    feedback_attempts: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    feedback_attempt_ttl_seconds = 30 * 60

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        nonlocal project_reconciler
        validate_service_configuration()
        scanner = asyncio.create_task(
            scan_server_datasets(), name="server-dataset-scanner"
        )
        application.state.server_dataset_scanner = scanner
        if controls_enabled():
            project_reconciler = asyncio.create_task(reconcile_project_deletions())
        try:
            yield
        finally:
            tasks = set(launch_tasks) | set(preparations.values()) | {scanner}
            if project_reconciler:
                tasks.add(project_reconciler)
            for values in starting_tasks.values():
                tasks.update(values)
            tasks.discard(asyncio.current_task())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if preparation_lease:
                preparation_lease.close()

    app = FastAPI(title="iGraph Backend", version="3.0.0", lifespan=lifespan)

    def previous_preparation_exit(owner):
        if preparation_lease and ProcessLease.inspect(preparation_lease.root, owner) == 'exited':
            return {'execution_stopped': True, 'generation': 1, 'failed': True}
        return None

    @app.get('/api/capabilities')
    async def capabilities():
        # Deployment gates stay closed until every executor implements v1.
        if not controls_enabled():
            return {"run_cancel_v1": False, "job_cancel_v1": False}
        try:
            values = await asyncio.gather(*[
                client.request('GET', '/internal/capabilities', headers=service_headers(''))
                for client in (runtime, graph)
            ])
            ready = all(value.get('run_cancel_v1') and value.get('job_cancel_v1') for value in values)
            return {"run_cancel_v1": bool(ready), "job_cancel_v1": bool(ready),
                    "edit_latest_turn_v1": bool(ready and values[0].get("edit_latest_turn_v1"))}
        except Exception:
            return {"run_cancel_v1": False, "job_cancel_v1": False}

    @app.post('/internal/sessions/{session_id}/runs/{run_id}/cancel')
    async def cancel_preparation(session_id: str, run_id: str, request: Request):
        require_service(request)
        uid = request.headers.get('X-User-ID', '')
        key = (uid, session_id, run_id)
        task = preparations.get(key)
        if task and not task.done():
            task.cancel()
        body = await request.json()
        previous = previous_preparation_exit(body.get('preparation_owner'))
        if previous:
            return previous
        start_key = (uid, session_id, str(body.get('start_request_id') or ''))
        if body.get('preparation_owner') == preparation_instance:
            for starting in starting_tasks.get(start_key, set()):
                starting.cancel()
        evidence = preparation_finished.get(key) or (starting_finished.get(start_key) if body.get('preparation_owner') == preparation_instance else None)
        return {'execution_stopped': evidence is not None and not starting_tasks.get(start_key), **(evidence or {})}

    @app.get('/internal/sessions/{session_id}/runs/{run_id}/preparation')
    async def preparation_status(session_id: str, run_id: str, request: Request):
        require_service(request)
        previous = previous_preparation_exit(request.query_params.get('preparation_owner'))
        if previous:
            return previous
        key = (request.headers.get('X-User-ID', ''), session_id, run_id)
        start_key = (key[0], session_id, request.query_params.get('start_request_id', ''))
        evidence = preparation_finished.get(key) or (starting_finished.get(start_key) if request.query_params.get('preparation_owner') == preparation_instance else None)
        return {'execution_stopped': evidence is not None and not starting_tasks.get(start_key), **(evidence or {})}

    @app.post('/api/sessions/{session_id}/runs/cancel')
    async def cancel_run(session_id: str, req: CancelRequest, request: Request):
        user = current_user(request, users)
        # Runtime validates persisted ownership. Locally registered preparation
        # already carries trusted ownership and can stop during a DB outage.
        if not controls_enabled():
            raise HTTPException(409, {"code": "RUN_CANCEL_UNAVAILABLE"})
        if req.target.run_id:
            task = preparations.get((user.user_id, session_id, str(req.target.run_id)))
            if task:
                task.cancel()
        result = await runtime.request('POST', f'/internal/sessions/{session_id}/runs/cancel',
                                       headers=forwarded_headers(request, user.user_id), json_body=req.model_dump(mode='json'))
        task = preparations.get((user.user_id, session_id, str(result.get('run_id'))))
        if task:
            task.cancel()
        return JSONResponse(result, status_code=200 if result.get('execution_stopped') else 202)

    @app.get('/api/sessions/{session_id}/runs/control')
    async def control_run(session_id: str, request: Request, run_id: uuid.UUID | None = None, start_request_id: uuid.UUID | None = None):
        user = current_user(request, users)
        await get_session(session_id, request, user.user_id)
        if (run_id is None) == (start_request_id is None):
            raise HTTPException(422, {"code": "INVALID_CONTROL_TARGET"})
        return await runtime.request('GET', f'/internal/sessions/{session_id}/runs/control',
                                     headers=forwarded_headers(request, user.user_id), params=Target(run_id=run_id, start_request_id=start_request_id).lookup())

    @app.post('/api/sessions/{session_id}/jobs/{job_id}/cancel')
    async def cancel_job(session_id: str, job_id: str, req: JobCancelRequest, request: Request):
        user = current_user(request, users)
        if job_id.startswith('lg_'):
            await get_session(session_id, request, user.user_id)
            result = await graph.request('POST', f'/internal/sessions/{session_id}/large-jobs/{job_id}/cancel',
                headers={**forwarded_headers(request, user.user_id), **service_headers(user.user_id)},
                json_body=req.model_dump(mode='json'))
            return JSONResponse(result, status_code=200 if result.get('execution_stopped') else 202)
        result = await runtime.request('POST', f'/internal/sessions/{session_id}/jobs/{job_id}/cancel',
                                       headers=forwarded_headers(request, user.user_id), json_body=req.model_dump(mode='json'))
        return JSONResponse(result, status_code=200 if result.get('execution_stopped') else 202)

    @app.get('/api/sessions/{session_id}/jobs/{job_id}/control')
    async def job_control(session_id: str, job_id: str, request: Request):
        user = current_user(request, users)
        if job_id.startswith('lg_'):
            await get_session(session_id, request, user.user_id)
            return await graph.request('GET', f'/internal/sessions/{session_id}/large-jobs/{job_id}/control',
                headers={**forwarded_headers(request, user.user_id), **service_headers(user.user_id)})
        return await runtime.request('GET', f'/internal/sessions/{session_id}/jobs/{job_id}/summary',
                                     headers=forwarded_headers(request, user.user_id))

    async def model_catalog_for(user: UserRecord) -> dict:
        if not controls_enabled():
            raise HTTPException(409, {'code': 'MODEL_CONTROLS_REQUIRED'})
        return await runtime.request('POST', '/internal/model-catalog/bootstrap', headers=service_headers(user.user_id),
            json_body={'allowed': user.allowed_models, 'is_admin': 'admin' in user.roles,
                       'legacy': [asdict(item) for item in custom_models.list(user.user_id)]})

    async def available_models_for(user: UserRecord) -> list[str]:
        if controls_enabled():
            catalog = await model_catalog_for(user)
            return [item['config_id'] for item in catalog['models'] if not item.get('unavailable_reason')]
        custom_ids = [item.model_id for item in custom_models.list(user.user_id)]
        return list(dict.fromkeys([*user.allowed_models, *custom_ids]))

    def custom_model_for(user: UserRecord, model_id: str) -> CustomModelRecord | None:
        return custom_models.get(user.user_id, model_id)

    def custom_model_payload(user: UserRecord, model_id: str) -> dict[str, str] | None:
        item = custom_model_for(user, model_id)
        if item is None:
            return None
        return {"model_id": item.model_id, "base_url": item.base_url, "api_key": item.api_key, "provider": item.provider}

    async def sync_custom_model(user: UserRecord, model_id: str, request: Request) -> None:
        payload = custom_model_payload(user, model_id)
        if payload is None:
            return
        await runtime.request(
            "PUT",
            "/internal/models/custom",
            headers=forwarded_headers(request, user.user_id),
            json_body={"user_id": user.user_id, **payload},
        )
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Retry-After", "X-iGraph-Stream-Version", "X-iGraph-Feedback-Accepted"],
    )

    @app.get("/")
    @app.get("/health")
    def health():
        return {"status": "ok", "module": "backend"}

    @app.post("/api/auth/validate")
    async def validate(request: Request):
        user_id = request.headers.get("X-User-ID", "").strip()
        record = users.get(user_id)
        if not record or not record.enabled:
            return {"valid": False, "reason": "user_not_supported" if user_id else "missing_user_id"}
        models = await available_models_for(record)
        default = (await model_catalog_for(record))['default_model'] if controls_enabled() else (models[0] if models else '')
        return {
            "valid": True,
            "user_id": record.user_id,
            "is_admin": "admin" in record.roles,
            "available_models": models,
            "default_model": default,
        }

    @app.get("/api/models/custom")
    async def list_custom_models(request: Request):
        user = current_user(request, users)
        if controls_enabled():
            return {'success': True, 'models': [item for item in (await model_catalog_for(user))['models'] if item['source'] == 'custom']}
        return {"success": True, "models": [public_custom_model(item) for item in custom_models.list(user.user_id)]}

    @app.post("/api/models/custom")
    async def save_custom_model(req: CustomModelReq, request: Request):
        user = current_user(request, users)
        if controls_enabled():
            await model_catalog_for(user)
            return await runtime.request('POST', '/internal/model-catalog', headers=service_headers(user.user_id),
                json_body={**req.model_dump(exclude={'model_id'}), 'upstream_model_id': req.model_id})
        try:
            record = custom_models.save(CustomModelRecord(user_id=user.user_id, **req.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await sync_custom_model(user, record.model_id, request)
        return {"success": True, "model": public_custom_model(record)}

    @app.delete("/api/models/custom/{model_id:path}")
    async def delete_custom_model(model_id: str, request: Request):
        user = current_user(request, users)
        if controls_enabled():
            catalog = await model_catalog_for(user)
            model = next((item for item in catalog['models'] if item['config_id'] == model_id), None)
            if not model:
                raise HTTPException(404, {'code': 'MODEL_REMOVED'})
            from urllib.parse import quote
            result = await runtime.request('DELETE', '/internal/model-catalog/' + quote(model_id, safe=''),
                headers=service_headers(user.user_id), params={'revision': model['revision']})
            custom_models.delete(user.user_id, model_id)
            return result
        if not custom_models.delete(user.user_id, model_id):
            raise HTTPException(status_code=404, detail="自定义模型不存在")
        await runtime.request(
            "DELETE",
            f"/internal/models/custom/{user.user_id}/{model_id}",
            headers=forwarded_headers(request, user.user_id),
        )
        return {"success": True, "model_id": model_id}

    @app.get('/api/model-providers')
    async def model_providers(request: Request):
        user = current_user(request, users)
        await model_catalog_for(user)
        return await runtime.request('GET', '/internal/model-providers', headers=service_headers(user.user_id))

    @app.post('/api/model-providers')
    async def save_model_provider(req: dict, request: Request):
        user = current_user(request, users)
        await model_catalog_for(user)
        return await runtime.request('POST', '/internal/model-providers', headers=service_headers(user.user_id), json_body=req)

    @app.get('/api/model-providers/{provider_id}/credentials')
    async def model_provider_credentials(provider_id: str, revision: int, request: Request):
        user = current_user(request, users)
        await model_catalog_for(user)
        from urllib.parse import quote
        result = await runtime.request('GET', '/internal/model-providers/' + quote(provider_id, safe='') + '/credentials',
            headers=service_headers(user.user_id), params={'revision': revision})
        return JSONResponse(result, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})

    @app.post('/api/model-providers/discover')
    async def discover_provider_models(req: dict, request: Request):
        user = current_user(request, users)
        await model_catalog_for(user)
        return await runtime.request('POST', '/internal/model-providers/discover',
            headers=service_headers(user.user_id), json_body=req, timeout=25)

    @app.post('/api/model-providers/import')
    async def import_provider_models(req: dict, request: Request):
        user = current_user(request, users)
        await model_catalog_for(user)
        return await runtime.request('POST', '/internal/model-providers/import',
            headers=service_headers(user.user_id), json_body=req)

    @app.delete('/api/model-providers/{provider_id}')
    async def delete_model_provider(provider_id: str, revision: int, request: Request):
        user = current_user(request, users)
        await model_catalog_for(user)
        from urllib.parse import quote
        return await runtime.request('DELETE', '/internal/model-providers/' + quote(provider_id, safe=''),
            headers=service_headers(user.user_id), params={'revision': revision})

    @app.get('/api/models')
    async def model_catalog(request: Request):
        user = current_user(request, users)
        if not controls_enabled():
            raise HTTPException(409, {'code': 'MODEL_CONTROLS_REQUIRED'})
        return await model_catalog_for(user)

    @app.post('/api/models')
    async def save_model_configuration(req: dict, request: Request):
        user = current_user(request, users)
        await model_catalog_for(user)
        return await runtime.request('POST', '/internal/model-catalog', headers=service_headers(user.user_id), json_body=req)

    @app.patch('/api/models/state')
    async def update_model_state(req: dict, request: Request):
        user = current_user(request, users)
        await model_catalog_for(user)
        return await runtime.request('PATCH', '/internal/model-catalog/state', headers=service_headers(user.user_id), json_body=req)

    @app.put('/api/models/default')
    async def update_model_default(req: dict, request: Request):
        user = current_user(request, users)
        await model_catalog_for(user)
        return await runtime.request('PUT', '/internal/model-catalog/default', headers=service_headers(user.user_id), json_body=req)

    @app.post('/api/models/test')
    async def test_model_configuration(req: dict, request: Request):
        user = current_user(request, users)
        await model_catalog_for(user)
        return await runtime.request('POST', '/internal/model-catalog/test', headers=service_headers(user.user_id), json_body=req, timeout=25)

    @app.delete('/api/models/{config_id:path}')
    async def delete_model_configuration(config_id: str, revision: int, request: Request):
        user = current_user(request, users)
        await model_catalog_for(user)
        from urllib.parse import quote
        result = await runtime.request('DELETE', '/internal/model-catalog/' + quote(config_id, safe=''),
            headers=service_headers(user.user_id), params={'revision': revision})
        custom_models.delete(user.user_id, config_id)
        return result

    @app.get("/api/admin/users")
    def list_users(request: Request):
        require_admin(request, users)
        return {"success": True, "users": [public_user(item) for item in users.list()]}

    @app.post("/api/admin/users")
    def save_user(req: UserWriteReq, request: Request):
        require_admin(request, users)
        record = users.save(UserRecord(**req.model_dump()))
        return {"success": True, "user": public_user(record)}

    @app.delete("/api/admin/users/{user_id}")
    async def delete_user(user_id: str, request: Request):
        require_admin(request, users)
        if user_id == settings.admin_user_id:
            raise HTTPException(status_code=400, detail="不能删除主管理员")
        headers = forwarded_headers(request, user_id)
        sessions_payload = await runtime.request(
            "GET",
            "/sessions",
            headers=headers,
            params={"type": "agent", "component_id": settings.agent_id, "user_id": user_id, "limit": 1000},
        )
        for session in _data_rows(sessions_payload):
            session_id = str(session.get("session_id") or "") if isinstance(session, dict) else ""
            if not session_id:
                continue
            try:
                await graph.request("DELETE", f"/sessions/{session_id}", headers=headers)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
            await runtime.request(
                "DELETE", f"/sessions/{session_id}", headers=headers,
                params={"type": "agent", "user_id": user_id},
            )
        memories = await runtime.request("GET", "/memories", headers=headers, params={"user_id": user_id, "limit": 1000})
        for memory in _data_rows(memories):
            memory_id = str(memory.get("memory_id") or "") if isinstance(memory, dict) else ""
            if memory_id:
                await runtime.request("DELETE", f"/memories/{memory_id}", headers=headers, params={"user_id": user_id})
        await runtime.request("DELETE", f"/internal/skills/users/{user_id}/all", headers=headers)
        await runtime.request('DELETE', '/internal/model-catalog/user/all', headers=service_headers(user_id))
        for item in custom_models.list(user_id):
            custom_models.delete(user_id, item.model_id)
        for item in project_datasets._load().values():
            if item.user_id == user_id and "construction" in item.metadata:
                await graph.request("DELETE", f"/construction/{item.dataset_id}", headers={**service_headers(user_id), **scope_headers(item.project_id)})
        project_datasets.delete_user(user_id)
        projects.delete_user(user_id)
        try:
            await asyncio.to_thread(delete_user_workspaces, user_id)
        except Exception:
            logging.getLogger(__name__).warning('user_workspace_cleanup_failed user_id=%s', user_id, exc_info=True)
        return {"success": users.delete(user_id), "user_id": user_id}

    async def get_session(session_id: str, request: Request, user_id: str) -> dict:
        payload = await runtime.request(
            "GET",
            f"/sessions/{session_id}",
            headers=forwarded_headers(request, user_id),
            params={"type": "agent", "user_id": user_id},
        )
        if str(payload.get("user_id") or "") not in {"", user_id}:
            raise HTTPException(status_code=404, detail="会话不存在")
        pid = _state(payload).get('project_id')
        if pid:
            project_or_404(pid, user_id)
        if controls_enabled():
            user = users.get(user_id)
            if user:
                await model_catalog_for(user)
            selection = await runtime.request('GET', f'/internal/sessions/{session_id}/model-selection', headers=service_headers(user_id))
            payload['session_state'] = {**_state(payload), **selection}
        return payload

    async def graph_loaded(session_id: str, request: Request, user_id: str) -> bool:
        try:
            payload = await graph.request(
                "GET",
                f"/sessions/{session_id}/graph/info",
                headers=forwarded_headers(request, user_id),
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                return False
            raise
        return bool(payload.get("loaded", False) or payload.get("num_nodes", 0))

    async def get_playbook_memory(memory_id: str, request: Request, user_id: str) -> dict:
        payload = await runtime.request(
            "GET",
            f"/memories/{memory_id}",
            headers=forwarded_headers(request, user_id),
            params={"user_id": user_id},
        )
        topics = payload.get("topics") or []
        if topics and "playbook" not in {str(item) for item in topics}:
            raise HTTPException(status_code=400, detail="指定 Memory 不是 Playbook")
        return payload

    async def get_session_runs(session_id: str, request: Request, user_id: str) -> list[dict]:
        payload = await runtime.request(
            "GET",
            f"/sessions/{session_id}/runs",
            headers=forwarded_headers(request, user_id),
            params={"type": "agent", "user_id": user_id},
        )
        rows = _ordered_runs(_data_rows(payload))
        if controls_enabled() and rows:
            audit = await runtime.request('GET', f'/internal/sessions/{session_id}/model-provenance', headers=service_headers(user_id))
            for row in rows:
                metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
                row['metadata'] = {**metadata, **audit.get('runs', {}).get(row.get('run_id'), {})}
        return rows

    async def run_lock(user_id: str, session_id: str) -> asyncio.Lock:
        key = (user_id, session_id)
        async with run_locks_guard:
            lock = run_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                run_locks[key] = lock
            return lock

    async def reserve_new_run(session_id: str, request: Request) -> tuple[str, tuple[str, str]]:
        user = current_user(request, users)
        key = (user.user_id, session_id)
        lock = await run_lock(*key)
        async with lock:
            runs = await get_session_runs(session_id, request, user.user_id)
            active_run, pending, _ = _active_run_snapshot(runs)
            if key in reserved_runs or active_run is not None:
                awaiting_feedback = active_run is not None and active_run.get("status") == "paused"
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "RUN_AWAITING_FEEDBACK" if awaiting_feedback else "RUN_ACTIVE",
                        "message": "请先处理当前待确认操作" if awaiting_feedback else "该会话已有正在运行的任务",
                        "active_run": active_run,
                        "pending_feedback": pending,
                    },
                )
            reserved_runs.add(key)
        return user.user_id, key

    async def release_run_reservation(key: tuple[str, str]) -> None:
        lock = await run_lock(*key)
        async with lock:
            reserved_runs.discard(key)

    async def restored_messages(session: dict, session_id: str, request: Request, user_id: str) -> list[dict]:
        runs = await get_session_runs(session_id, request, user_id)
        return _messages_from_runs(runs) or _messages(session)

    async def pending_feedback(session_id: str, request: Request, user_id: str) -> dict | None:
        runs = await get_session_runs(session_id, request, user_id)
        return _active_run_snapshot(runs)[1]

    def project_or_404(project_id: str, user_id: str) -> ProjectRecord:
        project = projects.get(project_id, user_id)
        if project is None or projects.is_legacy_default(project):
            raise HTTPException(status_code=404, detail="项目不存在")
        if project.delete_status:
            raise HTTPException(409, {"code": "PROJECT_DELETING"})
        task = asyncio.current_task()
        if task:
            key = (user_id, project_id)
            active = project_requests.setdefault(key, set())
            if task not in active:
                active.add(task)
                def forget(done):
                    active.discard(done)
                    if not active and project_requests.get(key) is active:
                        project_requests.pop(key, None)
                task.add_done_callback(forget)
        return project

    def project_dataset_or_404(
        dataset_id: str,
        project_id: str | None,
        user_id: str,
    ) -> ProjectDatasetRecord:
        dataset = project_datasets.get(dataset_id, user_id, project_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="项目数据集不存在或不属于当前项目")
        return dataset

    def session_project_id(session: dict, user_id: str) -> str | None:
        return _state(session).get("project_id") or None

    def server_dataset_requires_preparation(source: ServerDatasetRecord) -> bool:
        return bool(
            source.file_type in _SERVER_PREPROCESS_FILE_TYPES
            and source.size_bytes >= server_preprocess_min_bytes
        )

    def public_server_preparation(payload: Any) -> dict[str, Any]:
        source = payload if isinstance(payload, dict) else {}
        status = str(source.get("status") or "failed").strip().lower()
        if status not in {
            "not_required",
            "not_prepared",
            "queued",
            "running",
            "ready",
            "failed",
            "cancelled",
            "unavailable",
        }:
            status = "failed"
        result: dict[str, Any] = {"status": status}
        for key in (
            "progress",
            "message",
            "error",
            "job_id",
            "graph_version",
            "num_nodes",
            "num_edges",
            "partitions",
            "directed",
            "has_weights",
            "storage_bytes",
        ):
            if source.get(key) is not None:
                result[key] = source[key]
        return result

    async def request_server_dataset_preparation(
        source: ServerDatasetRecord,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        if source.metadata_error:
            return {
                "status": "failed",
                "error": source.metadata_error,
                "message": "服务器数据源方向配置无效",
            }
        if not server_dataset_requires_preparation(source):
            return {
                "status": "not_required",
                "message": "该文件低于服务器预处理阈值，将在会话中直接加载",
            }
        try:
            payload = await graph.request(
                "POST",
                "/server-datasets/prepare",
                headers=headers,
                json_body={
                    "server_dataset_id": source.server_dataset_id,
                    "revision": source.revision,
                    "relative_path": source.relative_path,
                    "original_filename": source.original_filename,
                    "source_path": source.storage_path,
                    "directed": source.directed,
                },
            )
        except (HTTPException, httpx.HTTPError) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("error") or str(detail)
            return {
                "status": "unavailable",
                "message": str(detail or "Graph Engine 暂不可用"),
            }
        preparation = public_server_preparation(payload)
        if isinstance(payload, dict) and payload.get("success") is False:
            preparation["status"] = (
                "cancelled"
                if preparation.get("status") == "cancelled"
                else "failed"
            )
            preparation.setdefault(
                "error", str(payload.get("error") or "服务器数据预处理提交失败")
            )
        return preparation

    async def ensure_server_dataset_preparation(
        source: ServerDatasetRecord,
        request: Request,
        user_id: str,
    ) -> dict[str, Any]:
        return await request_server_dataset_preparation(
            source, forwarded_headers(request, user_id)
        )

    async def scan_server_datasets() -> None:
        """Discover atomically published server files and enqueue them once."""

        while True:
            for source in server_datasets.list():
                if not server_dataset_requires_preparation(source):
                    continue
                await request_server_dataset_preparation(
                    source, {"X-User-ID": settings.admin_user_id}
                )
            await asyncio.sleep(server_dataset_scan_seconds)

    async def write_session_state(
        session_id: str,
        session: dict,
        request: Request,
        user_id: str,
        patch: dict[str, Any],
    ) -> dict:
        effective_patch = dict(patch)
        if "active_dataset_id" in effective_patch:
            current_dataset_id = str(_state(session).get("active_dataset_id") or "")
            next_dataset_id = str(effective_patch.get("active_dataset_id") or "")
            if not next_dataset_id or next_dataset_id != current_dataset_id:
                # A session-bound snapshot must not follow the conversation to
                # a different project dataset (or survive dataset removal).
                effective_patch.setdefault("large_graph_context", None)
                effective_patch.setdefault("large_graph_prepare", None)
        state = {**_state(session), **effective_patch}
        if "active_dataset_id" in effective_patch:
            active = project_datasets.get(str(effective_patch["active_dataset_id"]), user_id,
                session_project_id(session, user_id)) if effective_patch["active_dataset_id"] else None
            state["data_state"] = dataset_runtime_state(active)
        await runtime.request(
            "PATCH",
            f"/sessions/{session_id}",
            headers=forwarded_headers(request, user_id),
            params={"type": "agent", "user_id": user_id},
            json_body={"session_state": state},
        )
        # Callers commonly continue building the same request from ``session``.
        # Keep the in-memory view coherent so a just-persisted large graph
        # binding is not overwritten by a later PATCH in that request.
        session["session_state"] = state
        return state

    def dataset_runtime_state(dataset: ProjectDatasetRecord | None) -> dict:
        construction = dataset.metadata.get("construction", {}) if dataset else {}
        return {"active_dataset_id": dataset.dataset_id if dataset else None,
            "construction_status": construction.get("status"),
            "construction_revision": construction.get("revision"),
            "source_format": dataset.file_type if dataset else None}

    async def reset_graph_runtime(
        session_id: str,
        request: Request,
        user_id: str,
        model: str = "",
    ) -> None:
        headers = forwarded_headers(request, user_id)
        try:
            await graph.request("DELETE", f"/sessions/{session_id}", headers=headers)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        await graph.request("POST", f"/sessions/{session_id}", headers=headers, json_body={})
        if model:
            await graph.request(
                "PATCH",
                f"/sessions/{session_id}/config",
                headers=headers,
                json_body={"llm_model": model},
            )

    def record_pending_large_graph_dataset(
        dataset: ProjectDatasetRecord,
        *,
        project_id: str,
        user_id: str,
        pending: dict[str, Any],
        format_detected: str = "",
    ) -> ProjectDatasetRecord | None:
        previous_metadata = (
            dict(dataset.metadata) if isinstance(dataset.metadata, dict) else {}
        )
        metadata = {
            **previous_metadata,
            "dataset_kind": _PARTITIONED_GRAPH_KIND,
            "format_detected": format_detected
            or previous_metadata.get("format_detected")
            or _PARTITIONED_GRAPH_REPRESENTATION,
            "graph_id": pending["graph_id"],
            "graph_version": pending["graph_version"],
            "representation": _PARTITIONED_GRAPH_REPRESENTATION,
            "preparation_job_id": pending["job_id"],
            "preparation_status": pending["status"],
        }
        return project_datasets.update(
            dataset.dataset_id,
            user_id,
            project_id=project_id,
            metadata=metadata,
        )

    def record_completed_large_graph_dataset(
        dataset: ProjectDatasetRecord,
        result: dict[str, Any],
        *,
        project_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        try:
            large_graph_context = _partitioned_graph_context(
                result,
                dataset_id=dataset.dataset_id,
                session_id=session_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if large_graph_context is None:
            raise HTTPException(
                status_code=502,
                detail="Graph Engine 大图准备结果缺少分区快照标识",
            )
        result["large_graph_context"] = large_graph_context
        result_summary = (
            result.get("summary") if isinstance(result.get("summary"), dict) else {}
        )
        storage_bytes = result_summary.get("storage_bytes")
        if isinstance(storage_bytes, dict):
            storage_bytes = storage_bytes.get("total")
        previous_metadata = (
            dict(dataset.metadata) if isinstance(dataset.metadata, dict) else {}
        )
        previous_metadata.pop("preparation_job_id", None)
        previous_metadata.pop("preparation_status", None)
        metadata = {
            **previous_metadata,
            "dataset_kind": _PARTITIONED_GRAPH_KIND,
            "format_detected": result.get("format_detected")
            or previous_metadata.get("format_detected")
            or _PARTITIONED_GRAPH_REPRESENTATION,
            "graph_id": result.get("graph_id"),
            "graph_version": result.get("graph_version"),
            "representation": _PARTITIONED_GRAPH_REPRESENTATION,
            "num_nodes": result.get("num_nodes"),
            "num_edges": result.get("num_edges"),
            "directed": result_summary.get("directed"),
            "partitions": result_summary.get("partitions"),
            "has_weights": result_summary.get("has_weights"),
            "storage_bytes": storage_bytes,
        }
        updated = project_datasets.update(
            dataset.dataset_id,
            user_id,
            project_id=project_id,
            metadata={key: value for key, value in metadata.items() if value is not None},
        )
        if updated is not None:
            result["dataset_view"] = _dataset_view(updated)
        return result

    async def poll_large_graph_prepare_once(
        session_id: str,
        pending: dict[str, Any],
        request: Request,
        user_id: str,
    ) -> tuple[str, dict[str, Any] | None, str]:
        """Inspect a durable prepare job once; never sleep or resubmit it."""

        headers = forwarded_headers(request, user_id)
        status_payload = await graph.request(
            "POST",
            f"/sessions/{session_id}/tools/get_job_status",
            headers=headers,
            json_body={"arguments": {"job_id": pending["job_id"]}},
        )
        job = status_payload.get("job") if isinstance(status_payload, dict) else None
        if not isinstance(job, dict):
            return pending["status"], None, "大图准备作业状态暂不可用"
        returned_job_id = str(job.get("job_id") or pending["job_id"])
        if returned_job_id != pending["job_id"]:
            raise HTTPException(status_code=502, detail="Graph Engine 返回了不匹配的大图作业")
        status = str(job.get("status") or "").strip().lower()
        if status == "done":
            result_payload = await graph.request(
                "POST",
                f"/sessions/{session_id}/tools/get_job_result",
                headers=headers,
                json_body={"arguments": {"job_id": pending["job_id"]}},
            )
            completed = (
                result_payload.get("result")
                if isinstance(result_payload, dict)
                and isinstance(result_payload.get("result"), dict)
                else None
            )
            if (
                not isinstance(result_payload, dict)
                or result_payload.get("success") is False
                or completed is None
                or not _large_graph_result_matches_prepare(completed, pending)
            ):
                raise HTTPException(
                    status_code=502,
                    detail="Graph Engine 大图准备作业完成但结果绑定无效",
                )
            return "done", dict(completed), ""
        if status in {"failed", "cancelled"}:
            return status, None, str(
                job.get("message") or job.get("error") or "项目大图准备失败"
            )
        if status in {"queued", "running"}:
            return status, None, str(job.get("message") or "大图准备中")
        return pending["status"], None, "大图准备作业返回了未知状态"

    async def load_project_dataset_into_session(
        session_id: str,
        session: dict,
        dataset: ProjectDatasetRecord,
        request: Request,
        user_id: str,
        *,
        reset: bool = False,
    ) -> dict:
        project_id = session_project_id(session, user_id)
        if dataset.project_id != project_id:
            raise HTTPException(status_code=403, detail="会话不能加载其他项目的数据集")
        if dataset.status != "ready":
            raise HTTPException(status_code=409, detail=f"数据集当前状态为 {dataset.status}，尚不可加载")
        if dataset.metadata.get("construction", {}).get("status") == "ready":
            if reset:
                await reset_graph_runtime(session_id, request, user_id, str(_state(session).get("model") or ""))
            result = await graph.request("POST", f"/sessions/{session_id}/graph/load_construction",
                headers={**forwarded_headers(request, user_id), **service_headers(user_id), "X-Session-ID": session_id},
                json_body={"dataset_id": dataset.dataset_id})
            if not isinstance(result, dict) or result.get("success") is False:
                raise HTTPException(502, "已保存的构图结果加载失败")
            result["dataset_view"] = _dataset_view(dataset)
            return result
        source_path = Path(dataset.storage_path)
        server_preparation: dict[str, Any] | None = None
        server_source: ServerDatasetRecord | None = None
        if dataset.source_kind == "server":
            server_source = server_datasets.get(dataset.source_id or "")
            if server_source is None:
                raise HTTPException(
                    status_code=409,
                    detail="服务器数据源已被移除或不再可用",
                )
            source_path = Path(server_source.storage_path)
            try:
                if source_path.resolve(strict=True) != Path(
                    dataset.storage_path
                ).resolve(strict=True):
                    raise HTTPException(
                        status_code=409,
                        detail="服务器数据源路径已变化，请从服务器数据源库重新添加",
                    )
            except (OSError, RuntimeError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail="服务器数据源已被移除或不再可用",
                ) from exc
            recorded_revision = str(
                (dataset.metadata or {}).get("source_revision") or ""
            )
            if recorded_revision and recorded_revision != server_source.revision:
                raise HTTPException(
                    status_code=409,
                    detail="服务器数据源内容已经变化，请从服务器数据源库重新添加",
                )
            server_preparation = await ensure_server_dataset_preparation(
                server_source, request, user_id
            )
            if server_dataset_requires_preparation(server_source) and str(
                server_preparation.get("status") or ""
            ) != "ready":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "SERVER_DATASET_PREPARING",
                        "message": server_preparation.get("error")
                        or server_preparation.get("message")
                        or "服务器数据源仍在预处理，请稍后再试",
                        "preparation": server_preparation,
                    },
                )
            if not recorded_revision:
                pinned = project_datasets.update(
                    dataset.dataset_id,
                    user_id,
                    project_id=project_id,
                    metadata={
                        **(
                            dict(dataset.metadata)
                            if isinstance(dataset.metadata, dict)
                            else {}
                        ),
                        "source_revision": server_source.revision,
                        "server_preparation": server_preparation,
                    },
                )
                if pinned is not None:
                    dataset = pinned
        if not source_path.is_file():
            project_datasets.update(
                dataset.dataset_id,
                user_id,
                project_id=project_id,
                status="failed",
            )
            raise HTTPException(status_code=409, detail="项目数据集源文件不存在")
        if reset:
            await reset_graph_runtime(
                session_id,
                request,
                user_id,
                str(_state(session).get("model") or ""),
            )
        headers = forwarded_headers(request, user_id)
        if (
            server_source is not None
            and server_preparation is not None
            and server_preparation.get("status") == "ready"
        ):
            result = await graph.request(
                "POST",
                f"/sessions/{session_id}/server-datasets/attach",
                headers=headers,
                json_body={
                    "server_dataset_id": server_source.server_dataset_id,
                    "revision": server_source.revision,
                },
            )
        else:
            # Small and non-text sources retain the ordinary session-local
            # loading path. Large text sources never cross this boundary once
            # their server-owned immutable snapshot is ready.
            with source_path.open("rb") as source_file:
                await graph.request(
                    "POST",
                    f"/sessions/{session_id}/uploads/ingest",
                    headers=headers,
                    data={
                        "file_token": f"project-{dataset.dataset_id}",
                        "original_filename": dataset.original_filename,
                    },
                    files={
                        "file": (
                            dataset.original_filename,
                            source_file,
                            "application/octet-stream",
                        )
                    },
                    timeout=httpx.Timeout(
                        settings.stream_timeout, connect=settings.request_timeout
                    ),
                )
            result = await graph.request(
                "POST",
                f"/sessions/{session_id}/graph/load_upload",
                headers=headers,
                json_body={
                    "filename": dataset.original_filename,
                    "append": False,
                    "directed": (
                        server_source.directed if server_source is not None else True
                    ),
                },
            )
        if not isinstance(result, dict):
            raise HTTPException(status_code=502, detail="Graph Engine 返回了无效的数据集加载结果")
        if result.get("success") is False:
            project_datasets.update(
                dataset.dataset_id,
                user_id,
                project_id=project_id,
                status="failed",
            )
            raise HTTPException(
                status_code=422,
                detail=str(result.get("error") or "项目数据集加载失败"),
            )
        load_context = {
            key: result[key]
            for key in ("filename", "format_detected", "dataset_profile")
            if result.get(key) is not None
        }
        if result.get("async"):
            job_id = str(result.get("job_id") or "").strip()
            if not job_id:
                raise HTTPException(status_code=502, detail="Graph Engine 未返回数据集加载任务 ID")
            if job_id.startswith("lg_"):
                try:
                    pending_large_graph = _large_graph_prepare_context(
                        result,
                        dataset_id=dataset.dataset_id,
                        session_id=session_id,
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                updated = record_pending_large_graph_dataset(
                    dataset,
                    project_id=project_id,
                    user_id=user_id,
                    pending=pending_large_graph,
                    format_detected=str(result.get("format_detected") or ""),
                )
                # Persist before returning control. A billion-edge preparation
                # can outlive an HTTP request or Backend process; future
                # ensure calls resume this exact durable job instead of
                # uploading the source and reserving another graph version.
                pending_state_write = asyncio.create_task(
                    write_session_state(
                        session_id,
                        session,
                        request,
                        user_id,
                        {
                            "active_dataset_id": dataset.dataset_id,
                            "large_graph_context": None,
                            "large_graph_prepare": pending_large_graph,
                        },
                    )
                )
                try:
                    await asyncio.shield(pending_state_write)
                except asyncio.CancelledError:
                    # Finish this small control write before propagating the
                    # disconnected request. The durable worker must remain
                    # discoverable on the next Backend process/request.
                    await pending_state_write
                    raise
                return {
                    **result,
                    "large_graph_prepare": pending_large_graph,
                    "dataset_view": _dataset_view(updated),
                }
            timeout = float(os.getenv("BACKEND_PROJECT_DATASET_LOAD_TIMEOUT_SECONDS", "300"))
            deadline = asyncio.get_running_loop().time() + max(1.0, timeout)
            while True:
                status_payload = await graph.request(
                    "POST",
                    f"/sessions/{session_id}/tools/get_job_status",
                    headers=headers,
                    json_body={"arguments": {"job_id": job_id}},
                )
                job = status_payload.get("job") if isinstance(status_payload, dict) else None
                status = str(job.get("status") or "") if isinstance(job, dict) else ""
                if status == "done":
                    completed = (
                        job.get("result")
                        if isinstance(job.get("result"), dict)
                        else result
                    )
                    result = {**load_context, **completed}
                    break
                if status == 'cancelled':
                    # User cancellation leaves the validated project source
                    # available for the next Run and other conversations.
                    raise HTTPException(409, {'code': 'RUN_CANCELLED'})
                if status == "failed":
                    failed_metadata = (
                        dict(dataset.metadata)
                        if isinstance(dataset.metadata, dict)
                        else {}
                    )
                    failed_metadata.pop("preparation_job_id", None)
                    failed_metadata.pop("preparation_status", None)
                    project_datasets.update(
                        dataset.dataset_id,
                        user_id,
                        project_id=project_id,
                        status="failed",
                        metadata=failed_metadata,
                    )
                    raise HTTPException(
                        status_code=422,
                        detail=str(job.get("message") or "项目数据集加载失败"),
                    )
                if asyncio.get_running_loop().time() >= deadline:
                    raise HTTPException(status_code=504, detail="项目数据集加载超时，请稍后重试")
                await asyncio.sleep(0.25)
        if result.get("success") is False:
            project_datasets.update(
                dataset.dataset_id,
                user_id,
                project_id=project_id,
                status="failed",
            )
            raise HTTPException(
                status_code=422,
                detail=str(result.get("error") or "项目数据集加载失败"),
            )
        is_collection = (
            str(result.get("format_detected") or "") == _GRAPH_COLLECTION_FORMAT
            or isinstance(result.get("readiness"), dict)
        )
        if is_collection:
            readiness = result.get("readiness") if isinstance(result.get("readiness"), dict) else {}
            dataset_summary = result.get("dataset") if isinstance(result.get("dataset"), dict) else {}
            summary = _graph_collection_summary({**dataset_summary, **readiness})
            previous_metadata = (
                dict(dataset.metadata) if isinstance(dataset.metadata, dict) else {}
            )
            for key in (
                "graph_id",
                "graph_version",
                "representation",
                "partitions",
                "has_weights",
                "storage_bytes",
                "preparation_job_id",
                "preparation_status",
            ):
                previous_metadata.pop(key, None)
            updated = project_datasets.update(
                dataset.dataset_id,
                user_id,
                project_id=project_id,
                metadata={
                    **previous_metadata,
                    **summary,
                    "dataset_kind": "graph_collection",
                    "format_detected": _GRAPH_COLLECTION_FORMAT,
                    "dataset_profile": result.get("dataset_profile") or dataset_summary.get("name"),
                },
            )
            if updated is not None:
                result["dataset_view"] = _dataset_view(updated)
        else:
            is_partitioned_snapshot = (
                str(result.get("representation") or "")
                == _PARTITIONED_GRAPH_REPRESENTATION
            )
            if is_partitioned_snapshot:
                result = record_completed_large_graph_dataset(
                    dataset,
                    result,
                    project_id=project_id,
                    user_id=user_id,
                    session_id=session_id,
                )
            else:
                try:
                    info = await graph.request(
                        "GET",
                        f"/sessions/{session_id}/graph/info",
                        headers=headers,
                    )
                    previous_metadata = (
                        dict(dataset.metadata)
                        if isinstance(dataset.metadata, dict)
                        else {}
                    )
                    for key in (
                        "graph_id",
                        "graph_version",
                        "representation",
                        "partitions",
                        "has_weights",
                        "storage_bytes",
                        "preparation_job_id",
                        "preparation_status",
                    ):
                        previous_metadata.pop(key, None)
                    metadata = {
                        **previous_metadata,
                        "dataset_kind": "single_graph",
                        "format_detected": result.get("format_detected")
                        or "single_graph",
                        "num_nodes": info.get("num_nodes"),
                        "num_edges": info.get("num_edges"),
                        "directed": info.get("directed"),
                        "has_node_features": info.get("has_node_features"),
                        "has_node_labels": info.get("has_node_labels"),
                    }
                    updated = project_datasets.update(
                        dataset.dataset_id,
                        user_id,
                        project_id=project_id,
                        metadata={
                            key: value
                            for key, value in metadata.items()
                            if value is not None
                        },
                    )
                    if updated is not None:
                        result["dataset_view"] = _dataset_view(updated)
                except HTTPException:
                    pass
        return result

    async def switch_session_dataset(
        session_id: str,
        dataset: ProjectDatasetRecord | None,
        request: Request,
        user_id: str,
    ) -> tuple[dict | None, dict[str, Any] | None]:
        """Atomically switch the persisted selection and Graph Runtime state."""

        key = (user_id, session_id)
        lock = await run_lock(*key)
        async with lock:
            session = await get_session(session_id, request, user_id)
            runs = await get_session_runs(session_id, request, user_id)
            active_run, pending, _ = _active_run_snapshot(runs)
            if key in reserved_runs or active_run is not None:
                awaiting_feedback = active_run is not None and active_run.get("status") == "paused"
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "RUN_AWAITING_FEEDBACK" if awaiting_feedback else "RUN_ACTIVE",
                        "message": "请先处理当前待确认操作" if awaiting_feedback else "活动 Run 完成前不能切换数据集",
                        "active_run": active_run,
                        "pending_feedback": pending,
                    },
                )
            project_id = session_project_id(session, user_id)
            if dataset is not None and dataset.project_id != project_id:
                raise HTTPException(status_code=403, detail="会话不能加载其他项目的数据集")
            state = _state(session)
            previous_id = str(state.get("active_dataset_id") or "").strip()
            previous = (
                project_datasets.get(previous_id, user_id, project_id)
                if previous_id
                else None
            )
            model = str(state.get("model") or "")

            async def restore_previous() -> None:
                await reset_graph_runtime(session_id, request, user_id, model)
                restored_context = None
                restored_pending = None
                if previous is not None and previous.status == "ready":
                    restored_result = await load_project_dataset_into_session(
                        session_id,
                        session,
                        previous,
                        request,
                        user_id,
                    )
                    if isinstance(restored_result, dict):
                        restored_context = _normalize_partitioned_graph_context(
                            restored_result.get("large_graph_context"),
                            dataset_id=previous.dataset_id,
                            session_id=session_id,
                        )
                        restored_pending = _normalize_large_graph_prepare_context(
                            restored_result.get("large_graph_prepare"),
                            dataset_id=previous.dataset_id,
                            session_id=session_id,
                        )
                if restored_pending is None:
                    await write_session_state(
                        session_id,
                        session,
                        request,
                        user_id,
                        {
                            "active_dataset_id": previous.dataset_id if previous else None,
                            "large_graph_context": restored_context,
                            "large_graph_prepare": None,
                        },
                    )

            try:
                await reset_graph_runtime(session_id, request, user_id, model)
                load_result = None
                if dataset is not None:
                    load_result = await load_project_dataset_into_session(
                        session_id,
                        session,
                        dataset,
                        request,
                        user_id,
                    )
                large_graph_context = (
                    _normalize_partitioned_graph_context(
                        load_result.get("large_graph_context"),
                        dataset_id=dataset.dataset_id,
                        session_id=session_id,
                    )
                    if dataset is not None and isinstance(load_result, dict)
                    else None
                )
                pending_large_graph = (
                    _normalize_large_graph_prepare_context(
                        load_result.get("large_graph_prepare"),
                        dataset_id=dataset.dataset_id,
                        session_id=session_id,
                    )
                    if dataset is not None and isinstance(load_result, dict)
                    else None
                )
                if pending_large_graph is None:
                    await write_session_state(
                        session_id,
                        session,
                        request,
                        user_id,
                        {
                            "active_dataset_id": dataset.dataset_id if dataset else None,
                            "large_graph_context": large_graph_context,
                            "large_graph_prepare": None,
                        },
                    )
            except asyncio.CancelledError:
                persisted_pending = (
                    _normalize_large_graph_prepare_context(
                        _state(session).get("large_graph_prepare"),
                        dataset_id=dataset.dataset_id,
                        session_id=session_id,
                    )
                    if dataset is not None
                    else None
                )
                if persisted_pending is None:
                    try:
                        await asyncio.shield(restore_previous())
                    except Exception:
                        pass
                raise
            except Exception:
                try:
                    await restore_previous()
                except Exception:
                    # Persisted session state remains authoritative if Runtime recovery fails.
                    pass
                raise

            refreshed = (
                project_datasets.get(dataset.dataset_id, user_id, project_id)
                if dataset is not None
                else None
            )
            return load_result, _session_dataset_view(
                refreshed,
                _state(session),
                session_id=session_id,
            )

    async def ensure_active_dataset_loaded(
        session_id: str,
        session: dict,
        request: Request,
        user_id: str,
    ) -> bool:
        # Activation, info and chat may arrive concurrently. Serialize the
        # potentially expensive transfer/preparation with dataset switching
        # and run reservation for this conversation.
        lock = await run_lock(user_id, session_id)
        async with lock:
            state = _state(session)
            dataset_id = str(state.get("active_dataset_id") or "").strip()
            if not dataset_id:
                if state.get("large_graph_context") is not None:
                    await write_session_state(
                        session_id,
                        session,
                        request,
                        user_id,
                        {"large_graph_context": None},
                    )
                return await graph_loaded(session_id, request, user_id)

            project_id = session_project_id(session, user_id)
            dataset = project_dataset_or_404(dataset_id, project_id, user_id)
            loaded = await graph_loaded(session_id, request, user_id)
            force_reload = False
            if "construction" in dataset.metadata:
                latest = await graph.request(
                    "GET",
                    f"/construction/{dataset_id}",
                    headers={
                        **forwarded_headers(request, user_id),
                        **service_headers(user_id),
                        "X-Session-ID": session_id,
                    },
                )
                dataset = sync_construction_record(project_datasets, dataset, latest)
                if loaded:
                    info = await graph.request(
                        "GET",
                        f"/sessions/{session_id}/graph/info",
                        headers=forwarded_headers(request, user_id),
                    )
                    if (
                        info.get("construction_dataset_id") == dataset_id
                        and info.get("construction_revision") == latest.get("revision")
                    ):
                        return True
                    # Do not replace a snapshot still used by another active Run.
                    active = _active_run_record(
                        await get_session_runs(session_id, request, user_id)
                    )
                    if (
                        active
                        and str(active.get("run_id"))
                        != execution_headers.get().get("X-Run-ID")
                    ):
                        return True
                    force_reload = True
            raw_pending = state.get("large_graph_prepare")
            pending = _normalize_large_graph_prepare_context(
                raw_pending,
                dataset_id=dataset_id,
                session_id=session_id,
            )
            if raw_pending is None:
                pending = _large_graph_prepare_context_from_metadata(
                    dataset,
                    session_id=session_id,
                )
                if pending is not None:
                    await write_session_state(
                        session_id,
                        session,
                        request,
                        user_id,
                        {
                            "large_graph_context": None,
                            "large_graph_prepare": pending,
                        },
                    )
            if raw_pending is not None and pending is None:
                # Never replace a durable job merely because its persisted
                # binding is malformed. An automatic retry here could reserve
                # another version while the original worker is still writing.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "LARGE_GRAPH_PREPARE_BINDING_INVALID",
                        "message": "大图准备作业绑定无效，已停止自动重提交流程",
                    },
                )
            if pending is not None:
                status, completed, message = await poll_large_graph_prepare_once(
                    session_id,
                    pending,
                    request,
                    user_id,
                )
                if status == "done" and completed is not None:
                    metadata = (
                        dataset.metadata if isinstance(dataset.metadata, dict) else {}
                    )
                    completed.setdefault(
                        "format_detected",
                        metadata.get("format_detected")
                        or _PARTITIONED_GRAPH_REPRESENTATION,
                    )
                    completed = record_completed_large_graph_dataset(
                        dataset,
                        completed,
                        project_id=project_id,
                        user_id=user_id,
                        session_id=session_id,
                    )
                    context = _normalize_partitioned_graph_context(
                        completed.get("large_graph_context"),
                        dataset_id=dataset_id,
                        session_id=session_id,
                    )
                    if context is None:
                        raise HTTPException(
                            status_code=502,
                            detail="大图准备结果未生成有效的会话版本绑定",
                        )
                    await write_session_state(
                        session_id,
                        session,
                        request,
                        user_id,
                        {
                            "large_graph_context": context,
                            "large_graph_prepare": None,
                        },
                    )
                    return True
                if status in {"failed", "cancelled"}:
                    project_datasets.update(
                        dataset.dataset_id,
                        user_id,
                        project_id=project_id,
                        status="failed",
                    )
                    await write_session_state(
                        session_id,
                        session,
                        request,
                        user_id,
                        {
                            "large_graph_context": None,
                            "large_graph_prepare": None,
                        },
                    )
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "LARGE_GRAPH_DATASET_PREPARE_FAILED",
                            "message": message or "项目大图准备失败",
                            "job_id": pending["job_id"],
                            "status": status,
                        },
                    )
                next_pending = _large_graph_prepare_context(
                    {**pending, "status": status},
                    dataset_id=dataset_id,
                    session_id=session_id,
                )
                if next_pending != pending:
                    record_pending_large_graph_dataset(
                        dataset,
                        project_id=project_id,
                        user_id=user_id,
                        pending=next_pending,
                    )
                    await write_session_state(
                        session_id,
                        session,
                        request,
                        user_id,
                        {"large_graph_prepare": next_pending},
                    )
                return False

            context = _normalize_partitioned_graph_context(
                state.get("large_graph_context"),
                dataset_id=dataset_id,
                session_id=session_id,
            )
            if context is None:
                context = _partitioned_graph_context_from_metadata(
                    dataset,
                    session_id=session_id,
                )

            # A partitioned snapshot intentionally leaves /graph/info empty.
            # Verify the exact session-owned immutable version instead of
            # interpreting that as an unloaded graph and preparing it again.
            if context is not None:
                versions = await graph.request(
                    "POST",
                    f"/sessions/{session_id}/tools/list_large_graph_versions",
                    headers=forwarded_headers(request, user_id),
                    json_body={
                        "arguments": {"graph_id": context["graph_id"]},
                    },
                )
                published_version = _published_partitioned_version(
                    versions, context
                )
                if published_version is not None:
                    metadata = (
                        dataset.metadata
                        if isinstance(dataset.metadata, dict)
                        else {}
                    )
                    if str(metadata.get("preparation_status") or "") in {
                        "queued",
                        "running",
                    }:
                        recovered_metadata = dict(metadata)
                        recovered_metadata.pop("preparation_job_id", None)
                        recovered_metadata.pop("preparation_status", None)
                        for source_key, target_key in (
                            ("node_count", "num_nodes"),
                            ("edge_count", "num_edges"),
                            ("partitions", "partitions"),
                            ("directed", "directed"),
                            ("has_weights", "has_weights"),
                        ):
                            value = published_version.get(source_key)
                            if value is not None:
                                recovered_metadata[target_key] = value
                        refreshed = project_datasets.update(
                            dataset.dataset_id,
                            user_id,
                            project_id=project_id,
                            metadata=recovered_metadata,
                        )
                        if refreshed is not None:
                            dataset = refreshed
                        context = _partitioned_graph_context_from_metadata(
                            dataset,
                            session_id=session_id,
                        ) or context
                    if state.get("large_graph_context") != context:
                        await write_session_state(
                            session_id,
                            session,
                            request,
                            user_id,
                            {"large_graph_context": context},
                        )
                    return True

            metadata = dataset.metadata if isinstance(dataset.metadata, dict) else {}
            if (
                str(metadata.get("graph_id") or "") == session_id
                and str(metadata.get("preparation_status") or "")
                in {"queued", "running"}
            ):
                # Legacy metadata may lack the job id needed to poll safely.
                # Keep waiting rather than reserving a duplicate graph version.
                return False

            is_partitioned_dataset = bool(
                context is not None
                or _is_partitioned_graph_metadata(dataset.metadata)
            )
            if not is_partitioned_dataset and loaded and not force_reload:
                if state.get("large_graph_context") is not None:
                    await write_session_state(
                        session_id,
                        session,
                        request,
                        user_id,
                        {"large_graph_context": None},
                    )
                return True

            result = await load_project_dataset_into_session(
                session_id,
                session,
                dataset,
                request,
                user_id,
            )
            loaded_context = (
                _normalize_partitioned_graph_context(
                    result.get("large_graph_context"),
                    dataset_id=dataset_id,
                    session_id=session_id,
                )
                if isinstance(result, dict)
                else None
            )
            await write_session_state(
                session_id,
                session,
                request,
                user_id,
                {
                    "large_graph_context": loaded_context,
                    "large_graph_prepare": (
                        result.get("large_graph_prepare")
                        if isinstance(result, dict)
                        else None
                    ),
                },
            )
            if loaded_context is not None:
                # Preparation completion and context construction already bind
                # this exact published version; a second list call here would
                # add latency without strengthening that transaction boundary.
                return True
            if isinstance(result, dict) and result.get("large_graph_prepare"):
                return False
            return await graph_loaded(session_id, request, user_id)

    async def user_sessions(request: Request, user_id: str, limit: int | None = None) -> list[dict]:
        rows = await general_migration.sessions(user_id)
        if controls_enabled():
            deleting = await runtime.request('GET', '/internal/sessions/deleting', headers=forwarded_headers(request, user_id))
            hidden = set(deleting.get('session_ids', []))
            rows = [row for row in rows if row.get('session_id') not in hidden]
        deleting_projects = {p.project_id for p in projects._load().values() if p.user_id == user_id and p.delete_status}
        rows = [row for row in rows if session_project_id(row, user_id) not in deleting_projects]
        # Enumeration uses stable IDs; the UI still needs the newest activity,
        # including when its limited result spans multiple native pages.
        rows.sort(key=_session_activity, reverse=True)
        return rows[:limit] if limit else rows

    async def delete_owned_session(session_id: str, request: Request, user_id: str, *, verify: bool = True) -> None:
        if verify:
            await get_session(session_id, request, user_id)
        headers = forwarded_headers(request, user_id)
        if controls_enabled():
            await runtime.request('POST', f'/internal/sessions/{session_id}/prepare-delete', headers=headers)
        try:
            await graph.request("DELETE", f"/sessions/{session_id}", headers=headers)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        await runtime.request(
            "DELETE",
            f"/sessions/{session_id}",
            headers=headers,
            params={"type": "agent", "user_id": user_id},
        )
        try:
            await asyncio.to_thread(delete_workspace, user_id, session_id)
        except Exception:
            logging.getLogger(__name__).warning('workspace_cleanup_failed session_id=%s', session_id, exc_info=True)

    @app.get("/api/projects")
    async def list_projects(request: Request):
        user = current_user(request, users)
        migration = await general_migration.run(user.user_id)
        rows = await user_sessions(request, user.user_id)
        counts: dict[str, int] = {}
        for session in rows:
            project_id = session_project_id(session, user.user_id)
            counts[project_id] = counts.get(project_id, 0) + 1
        return {
            "success": True,
            "projects": [{**_project_row(project, counts.get(project.project_id, 0)), "dataset_count": len(project_datasets.list(project.project_id, user.user_id))} for project in projects.list(user.user_id)],
            "legacy_default_project_id": migration.get("legacy_default_project_id"),
        }

    @app.post("/api/projects")
    async def create_project(req: ProjectWriteReq, request: Request):
        user = current_user(request, users)
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="项目名称不能为空")
        project = projects.create(user.user_id, name, req.description)
        return {"success": True, "project": _project_row(project)}

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str, request: Request):
        user = current_user(request, users)
        project = project_or_404(project_id, user.user_id)
        rows = await user_sessions(request, user.user_id)
        count = sum(session_project_id(session, user.user_id) == project_id for session in rows)
        return {"success": True, "project": _project_row(project, count)}

    @app.get("/api/projects/{project_id}/datasets")
    async def list_project_datasets(request: Request, project_id: str | None = None):
        user = current_user(request, users)
        project = project_or_404(project_id, user.user_id) if project_id else None
        default_dataset_id = project.default_dataset_id if project else None
        rows = project_datasets.list(project_id, user.user_id)
        return {
            "success": True,
            "project_id": project_id,
            "default_dataset_id": project.default_dataset_id if project else None,
            "datasets": [
                _project_dataset_row(item, default_dataset_id) for item in rows
            ],
        }

    @app.get("/api/server-datasets")
    async def list_server_datasets(request: Request):
        user = current_user(request, users)
        rows = server_datasets.list()

        async def describe(source: ServerDatasetRecord) -> dict[str, Any]:
            row = source.public_dict()
            row["preparation"] = await ensure_server_dataset_preparation(
                source, request, user.user_id
            )
            return row

        return {
            "success": True,
            "datasets": await asyncio.gather(*(describe(item) for item in rows)),
        }

    @app.post("/api/projects/{project_id}/datasets/from-server")
    async def create_project_dataset_from_server(
        project_id: str,
        req: ServerDatasetImportReq,
        request: Request,
    ):
        user = current_user(request, users)
        project = project_or_404(project_id, user.user_id)
        source = server_datasets.get(req.server_dataset_id)
        if source is None:
            raise HTTPException(status_code=404, detail="服务器数据源不存在或已被移除")
        if source.metadata_error:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "SERVER_DATASET_METADATA_INVALID",
                    "message": source.metadata_error,
                },
            )
        preparation = await ensure_server_dataset_preparation(
            source, request, user.user_id
        )
        if server_dataset_requires_preparation(source) and str(
            preparation.get("status") or ""
        ) != "ready":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "SERVER_DATASET_PREPARING",
                    "message": preparation.get("error")
                    or preparation.get("message")
                    or "服务器数据源仍在预处理，请稍后再试",
                    "preparation": preparation,
                },
            )

        target_session = None
        normalized_session_id = req.session_id.strip()
        if normalized_session_id:
            target_session = await get_session(
                normalized_session_id, request, user.user_id
            )
            if session_project_id(target_session, user.user_id) != project_id:
                raise HTTPException(
                    status_code=400,
                    detail="只能为当前项目中的会话激活数据集",
                )

        dataset, created = project_datasets.create_reference(
            project_id=project_id,
            user_id=user.user_id,
            name=req.name or source.name,
            original_filename=source.original_filename,
            storage_path=Path(source.storage_path),
            source_id=source.server_dataset_id,
            metadata={
                "source_kind": "server",
                "source_relative_path": source.relative_path,
                "source_size_bytes": source.size_bytes,
                "source_modified_at": source.modified_at,
                "source_revision": source.revision,
                "directed": source.directed,
                "server_preparation": preparation,
            },
        )
        previous_default_dataset_id = project.default_dataset_id
        if project.default_dataset_id is None or req.set_as_default:
            project = (
                projects.set_default_dataset(
                    project_id, user.user_id, dataset.dataset_id
                )
                or project
            )

        activated_session_id = None
        dataset_view = None
        if target_session is not None:
            def rollback_import() -> None:
                if project.default_dataset_id == dataset.dataset_id:
                    projects.set_default_dataset(
                        project_id,
                        user.user_id,
                        previous_default_dataset_id,
                    )
                if created:
                    project_datasets.delete(
                        dataset.dataset_id, user.user_id, project_id
                    )

            try:
                _, dataset_view = await switch_session_dataset(
                    normalized_session_id,
                    dataset,
                    request,
                    user.user_id,
                )
            except asyncio.CancelledError:
                rollback_import()
                raise
            except Exception:
                rollback_import()
                raise
            activated_session_id = normalized_session_id
            dataset = (
                project_datasets.get(dataset.dataset_id, user.user_id, project_id)
                or dataset
            )
        return {
            "success": True,
            "created": created,
            "dataset": _project_dataset_row(
                dataset, project.default_dataset_id
            ),
            "default_dataset_id": project.default_dataset_id,
            "activated_session_id": activated_session_id,
            "dataset_view": dataset_view,
        }

    @app.post("/api/projects/{project_id}/datasets")
    async def create_project_dataset(
        request: Request,
        project_id: str | None = None,
        file: UploadFile | None = File(None),
        files: list[UploadFile] | None = File(None),
        csv_schema: str = Form(""),
        name: str = Form(""),
        description: str = Form("", max_length=4000),
        set_as_default: bool = Form(False),
        session_id: str = Form(""),
    ):
        user = current_user(request, users)
        project = project_or_404(project_id, user.user_id) if project_id else None
        default_dataset_id = project.default_dataset_id if project else None
        target_session = None
        normalized_session_id = session_id.strip()
        if normalized_session_id:
            target_session = await get_session(normalized_session_id, request, user.user_id)
            if session_project_id(target_session, user.user_id) != project_id:
                raise HTTPException(status_code=400, detail="只能为当前项目中的会话激活数据集")
        selected_files = list(files or [])
        if file is not None:
            selected_files.insert(0, file)
        if not selected_files:
            raise HTTPException(status_code=400, detail={"code": "DATASET_FILE_REQUIRED", "message": "请选择数据集文件"})
        if len(selected_files) > 128:
            raise HTTPException(status_code=422, detail={"code": "CSV_BUNDLE_FILE_COUNT", "message": "一次最多上传 128 个 CSV 文件"})
        if project_id:
            project_or_404(project_id, user.user_id)
        if len(selected_files) > 1 or csv_schema.strip():
            if any(Path(item.filename or "").suffix.lower() != ".csv" for item in selected_files):
                raise HTTPException(status_code=422, detail={"code": "CSV_BUNDLE_FILE_TYPE", "message": "多文件构图只接受 CSV 文件"})
            try:
                schema = json.loads(csv_schema)
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=422, detail={"code": "CSV_BUNDLE_SCHEMA", "message": "CSV 表结构配置不是有效 JSON"}) from exc
            bundle_name = name.strip() or (Path(selected_files[0].filename or "csv-bundle").stem + f" 等 {len(selected_files)} 张表")
            try:
                dataset = project_datasets.create_csv_bundle(
                    project_id=project_id,
                    user_id=user.user_id,
                    name=bundle_name,
                    original_filename=(re.sub(r"[^A-Za-z0-9._-]+", "_", bundle_name).strip("._") or "csv-bundle") + ".csv",
                    files=[(Path(item.filename or "").name, item.file) for item in selected_files],
                    schema=schema,
                    description=description or str(schema.get("description") or ""),
                )
            except ValueError as exc:
                code = str(exc) if str(exc).startswith("CSV_BUNDLE_") else "CSV_BUNDLE_SCHEMA"
                raise HTTPException(status_code=422, detail={"code": code, "message": "CSV 文件、表名或 PK/FK 配置无效"}) from exc
        else:
            uploaded = selected_files[0]
            filename = uploaded.filename or "dataset.bin"
            content = await uploaded.read()
            if not content:
                raise HTTPException(status_code=400, detail="数据集文件不能为空")
            dataset = project_datasets.create(
                project_id=project_id,
                user_id=user.user_id,
                name=name or Path(filename).stem,
                original_filename=filename,
                content=content,
                description=description,
            )
        previous_default_dataset_id = default_dataset_id
        if dataset.status == "ready" and project and (project.default_dataset_id is None or set_as_default):
            project = projects.set_default_dataset(project_id, user.user_id, dataset.dataset_id) or project
        activated_session_id = None
        dataset_view = None
        if target_session is not None and dataset.status == "ready":
            try:
                _, dataset_view = await switch_session_dataset(
                    normalized_session_id,
                    dataset,
                    request,
                    user.user_id,
                )
            except asyncio.CancelledError:
                if project and project.default_dataset_id == dataset.dataset_id:
                    projects.set_default_dataset(project_id, user.user_id, previous_default_dataset_id)
                raise
            except Exception:
                if project and project.default_dataset_id == dataset.dataset_id:
                    projects.set_default_dataset(project_id, user.user_id, previous_default_dataset_id)
                raise
            activated_session_id = normalized_session_id
            dataset = project_datasets.get(dataset.dataset_id, user.user_id, project_id) or dataset
        return {
            "success": True,
            "dataset": _project_dataset_row(dataset, project.default_dataset_id if project else None),
            "default_dataset_id": project.default_dataset_id if project else None,
            "activated_session_id": activated_session_id,
            "dataset_view": dataset_view,
            "construction_required": dataset.status == "source_ready",
        }

    @app.patch("/api/projects/{project_id}/datasets/{dataset_id}")
    async def update_project_dataset(
        dataset_id: str,
        req: ProjectDatasetPatchReq,
        request: Request,
        project_id: str | None = None,
    ):
        user = current_user(request, users)
        project = project_or_404(project_id, user.user_id) if project_id else None
        default_dataset_id = project.default_dataset_id if project else None
        project_dataset_or_404(dataset_id, project_id, user.user_id)
        dataset = project_datasets.update(
            dataset_id,
            user.user_id,
            project_id=project_id,
            name=req.name,
            description=req.description,
        )
        return {
            "success": True,
            "dataset": _project_dataset_row(dataset, project.default_dataset_id if project else None),
        }

    @app.post("/api/projects/{project_id}/datasets/{dataset_id}/set-default")
    async def set_default_project_dataset(project_id: str, dataset_id: str, request: Request):
        user = current_user(request, users)
        project_or_404(project_id, user.user_id)
        dataset = project_dataset_or_404(dataset_id, project_id, user.user_id)
        if dataset.status != "ready":
            raise HTTPException(status_code=409, detail="只有 ready 状态的数据集可以设为默认")
        project = projects.set_default_dataset(project_id, user.user_id, dataset_id)
        return {
            "success": True,
            "project": _project_row(project),
            "default_dataset_id": dataset_id,
        }

    @app.delete("/api/projects/{project_id}/datasets/{dataset_id}")
    async def delete_project_dataset(dataset_id: str, request: Request, project_id: str | None = None):
        user = current_user(request, users)
        project = project_or_404(project_id, user.user_id) if project_id else None
        default_dataset_id = project.default_dataset_id if project else None
        project_dataset_or_404(dataset_id, project_id, user.user_id)
        affected = 0
        referencing = [row for row in await user_sessions(request, user.user_id)
                       if session_project_id(row, user.user_id) == project_id
                       and _state(row).get('active_dataset_id') == dataset_id]
        for row in referencing:
            if _active_run_record(await get_session_runs(row['session_id'], request, user.user_id)):
                raise HTTPException(409, {'code': 'DATASET_IN_USE', 'message': '请先结束使用此数据集的任务'})
            if controls_enabled():
                status = await graph.request('GET', f"/internal/sessions/{row['session_id']}/resource-status",
                                             headers=service_headers(user.user_id))
                if not status.get('execution_stopped'):
                    raise HTTPException(409, {'code': 'DATASET_IN_USE', 'message': '请先结束使用此数据集的后台作业'})
        for session in referencing:
            state = _state(session)
            if session_project_id(session, user.user_id) != project_id:
                continue
            if str(state.get("active_dataset_id") or "") != dataset_id:
                continue
            session_id = str(session.get("session_id") or "")
            if not session_id:
                continue
            await write_session_state(
                session_id,
                session,
                request,
                user.user_id,
                {"active_dataset_id": None},
            )
            await reset_graph_runtime(
                session_id,
                request,
                user.user_id,
                str(state.get("model") or ""),
            )
            affected += 1
        if project and project.default_dataset_id == dataset_id:
            projects.set_default_dataset(project_id, user.user_id, None)
        removed_dataset = project_dataset_or_404(dataset_id, project_id, user.user_id)
        if "construction" in removed_dataset.metadata:
            await graph.request("DELETE", f"/construction/{dataset_id}", headers={**service_headers(user.user_id), **scope_headers(project_id)})
        if not project_datasets.delete(dataset_id, user.user_id, project_id):
            raise HTTPException(status_code=409, detail="项目数据集删除失败")
        return {
            "success": True,
            "dataset_id": dataset_id,
            "cleared_conversations": affected,
            "default_dataset_id": None if default_dataset_id == dataset_id else default_dataset_id,
        }

    @app.get("/api/personal-datasets")
    async def list_personal_datasets(request: Request):
        return await list_project_datasets(request)

    @app.post("/api/personal-datasets")
    async def create_personal_dataset(request: Request, file: UploadFile | None = File(None),
                                      files: list[UploadFile] | None = File(None),
                                      csv_schema: str = Form(""), name: str = Form(""),
                                      description: str = Form("", max_length=4000),
                                      session_id: str = Form("")):
        return await create_project_dataset(request, file=file, files=files, csv_schema=csv_schema,
                                            name=name, description=description, session_id=session_id,
                                            set_as_default=False)

    @app.patch("/api/personal-datasets/{dataset_id}")
    async def update_personal_dataset(dataset_id: str, req: ProjectDatasetPatchReq, request: Request):
        return await update_project_dataset(dataset_id, req, request)

    @app.delete("/api/personal-datasets/{dataset_id}")
    async def delete_personal_dataset(dataset_id: str, request: Request):
        return await delete_project_dataset(dataset_id, request)

    @app.patch("/api/projects/{project_id}")
    async def update_project(project_id: str, req: ProjectPatchReq, request: Request):
        user = current_user(request, users)
        current = project_or_404(project_id, user.user_id)
        if req.name is not None and not req.name.strip():
            raise HTTPException(status_code=400, detail="项目名称不能为空")
        project = projects.update(
            project_id,
            user.user_id,
            name=req.name,
            description=req.description,
        )
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        return {"success": True, "project": _project_row(project)}

    @app.get("/api/projects/{project_id}/conversations")
    async def project_conversations(project_id: str, request: Request):
        user = current_user(request, users)
        project_or_404(project_id, user.user_id)
        rows = await user_sessions(request, user.user_id)
        available_models = await available_models_for(user)
        default_model = available_models[0] if available_models else "qwen3.6-plus"
        sessions = [
            _session_row(session, default_model)
            for session in rows
            if session_project_id(session, user.user_id) == project_id
        ]
        return {"success": True, "project_id": project_id, "conversations": sessions}

    @app.get("/api/projects/{project_id}/training-runs")
    async def get_project_training_runs(project_id: str, request: Request):
        return await project_training_runs(project_id, request)

    async def project_training_runs(project_id: str | None, request: Request, *, current_session_id: str | None = None):
        user = current_user(request, users)
        if project_id:
            project_or_404(project_id, user.user_id)
        rows = [
            session
            for session in await user_sessions(request, user.user_id)
            if session_project_id(session, user.user_id) == project_id
            and (project_id is not None or session.get('session_id') == current_session_id)
        ]
        dataset_names = {
            dataset.dataset_id: dataset.name
            for dataset in project_datasets.list(project_id, user.user_id)
        }
        headers = forwarded_headers(request, user.user_id)

        async def collect_session_training(session: dict) -> list[dict]:
            session_id = str(session.get("session_id") or "")
            if not session_id:
                return []
            runs = await get_session_runs(session_id, request, user.user_id)
            cards = _training_cards_from_runs(runs)
            state = _state(session)
            stored = state.get("training_job_snapshots")
            snapshots = dict(stored) if isinstance(stored, dict) else {}
            has_active_agent_run = _active_run_record(runs) is not None
            snapshots_changed = False

            for card in cards:
                initial_result = card.get("result") if isinstance(card.get("result"), dict) else {}
                job_id = str(card.get("job_id") or initial_result.get("job_id") or "").strip()
                saved = snapshots.get(job_id) if job_id else None
                if isinstance(saved, dict) and isinstance(saved.get("job"), dict):
                    _merge_training_job_card(card, {
                        "job": saved["job"],
                        "progress_updates": saved.get("history") or [],
                    })
                    if str(saved["job"].get("status") or "").lower() == "unavailable":
                        card["status_unavailable"] = True

                if not job_id or card.get("done") or has_active_agent_run:
                    continue
                try:
                    status_result = await graph.request(
                        "POST",
                        f"/sessions/{session_id}/tools/get_job_status",
                        headers=headers,
                        json_body={"arguments": {"job_id": job_id}},
                    )
                except HTTPException as exc:
                    card["sync_error"] = str(exc.detail)
                    continue
                if not isinstance(status_result, dict) or not isinstance(status_result.get("job"), dict):
                    previous_progress = card.get("progress") if isinstance(card.get("progress"), dict) else {}
                    unavailable_job = {
                        "job_id": job_id,
                        "tool_name": "train",
                        "status": "unavailable",
                        "progress": previous_progress.get("progress", 0),
                        "message": "图引擎已重启，无法恢复该作业的最终状态",
                        "detail": previous_progress,
                    }
                    _merge_training_job_card(card, {"job": unavailable_job, "progress_updates": []})
                    card["status_unavailable"] = True
                    snapshots[job_id] = {"job": unavailable_job, "history": card.get("history") or []}
                    snapshots_changed = True
                    continue
                _merge_training_job_card(card, status_result)
                snapshots[job_id] = {
                    "job": status_result["job"],
                    "history": card.get("history") or [],
                }
                snapshots_changed = True

            if snapshots_changed:
                await write_session_state(
                    session_id,
                    session,
                    request,
                    user.user_id,
                    {"training_job_snapshots": snapshots},
                )

            dataset_id = str(state.get("active_dataset_id") or "")
            session_title = str(session.get("session_name") or "新会话")
            for index, card in enumerate(cards):
                result = card.get("result") if isinstance(card.get("result"), dict) else {}
                job_id = str(card.get("job_id") or result.get("job_id") or "")
                card.update({
                    "training_run_id": job_id or str(card.get("call_id") or f"{session_id}:{index}"),
                    "session_id": session_id,
                    "session_title": session_title,
                    "dataset_id": dataset_id or None,
                    "dataset_name": dataset_names.get(dataset_id),
                    "session_updated_at": session.get("updated_at"),
                })
            return cards

        groups = await asyncio.gather(*(collect_session_training(session) for session in rows))
        training_runs = [card for group in groups for card in group]
        training_runs.sort(
            key=lambda card: str(card.get("session_updated_at") or ""),
            reverse=True,
        )
        return {
            "success": True,
            "project_id": project_id,
            "training_runs": training_runs,
            "has_running": any(not bool(card.get("done")) for card in training_runs),
        }

    async def submit_project_deletion(project):
        return await runtime.request('POST', f'/internal/projects/{project.project_id}/delete',
            headers=service_headers(project.user_id), json_body={'preparation_owner': preparation_instance})

    async def reconcile_project_deletions():
        while True:
            for project in projects._load().values():
                if project.delete_status:
                    try:
                        await submit_project_deletion(project)
                    except Exception:
                        logging.getLogger(__name__).warning('project_delete_delivery_pending project_id=%s', project.project_id)
            await asyncio.sleep(5)

    @app.post('/internal/projects/{project_id}/cancel-preparations')
    async def cancel_project_preparations(project_id: str, request: Request):
        require_service(request)
        uid = request.headers.get('X-User-ID', '')
        body = await request.json()
        active = project_requests.get((uid, project_id), set())
        for task in tuple(active):
            if not task.done():
                task.cancel()
        owner = body.get('preparation_owner')
        known_exit = owner == preparation_instance or previous_preparation_exit(owner)
        return {'execution_stopped': bool(known_exit) and not any(not task.done() for task in active)}

    @app.delete('/internal/projects/{project_id}/resources')
    async def delete_project_resources(project_id: str, request: Request):
        require_service(request)
        uid = request.headers.get('X-User-ID', '')
        record = await runtime.request('GET', f'/internal/projects/{project_id}/deletion', headers=service_headers(uid))
        if not record['state'].get('sessions_deleted'):
            raise HTTPException(409, {'code': 'PROJECT_DELETE_NOT_READY'})
        await delete_project_constructions(project_id, uid)
        project_datasets.delete_project(project_id, uid)
        projects.delete(project_id, uid)
        return {'success': True}

    @app.get('/api/projects/{project_id}/deletion')
    async def project_deletion_status(project_id: str, request: Request):
        user = current_user(request, users)
        try:
            record = await runtime.request('GET', f'/internal/projects/{project_id}/deletion',
                headers=forwarded_headers(request, user.user_id))
        except Exception:
            project = projects.get(project_id, user.user_id)
            if project and project.delete_status:
                return {'project_id': project_id, 'delete_status': project.delete_status,
                        'resources_deleted': False, 'delivery_pending': True}
            raise
        return {'project_id': project_id, **record['state']}

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, request: Request, req: DeleteProjectReq):
        user = current_user(request, users)
        if req.conversation_action != 'delete':
            raise HTTPException(409, {'code': 'PROJECT_DELETE_ACTION_CHANGED', 'message': '请刷新页面后确认删除项目及全部内容'})
        project = projects.get(project_id, user.user_id)
        if project is None:
            if controls_enabled():
                return await project_deletion_status(project_id, request)
            raise HTTPException(404, '项目不存在')
        if projects.is_legacy_default(project):
            raise HTTPException(409, {'code': 'GENERAL_MIGRATION_REQUIRED'})
        if not controls_enabled():
            # Compatibility deployments may delete only idle sessions. Production
            # uses the durable cancellation coordinator below.
            if not os.getenv('IGRAPH_CONTROL_SERVICE_KEY'):
                raise HTTPException(409, {'code': 'PROJECT_CANCEL_UNAVAILABLE'})
            contained = [row for row in await user_sessions(request, user.user_id)
                         if session_project_id(row, user.user_id) == project_id]
            for row in contained:
                runs = await get_session_runs(row['session_id'], request, user.user_id)
                if _active_run_record(runs):
                    raise HTTPException(409, {'code': 'PROJECT_CANCEL_UNAVAILABLE'})
            for row in contained:
                await delete_owned_session(row['session_id'], request, user.user_id, verify=False)
            await graph.request('DELETE', f'/internal/projects/{project_id}/artifacts',
                headers=service_headers(user.user_id),
                json_body={'session_ids': [row['session_id'] for row in contained]})
            await delete_project_constructions(project_id, user.user_id)
            project_datasets.delete_project(project_id, user.user_id)
            projects.delete(project_id, user.user_id)
            return {'success': True, 'project_id': project_id, 'delete_status': 'deleted'}
        projects.mark_deleting(project_id, user.user_id)
        # Signal BFF preparations immediately; Runtime delivers all remaining
        # cancellations and confirms actual exit independently of this request.
        for task in tuple(project_requests.get((user.user_id, project_id), set())):
            if task is not asyncio.current_task():
                task.cancel()
        try:
            record = await submit_project_deletion(project)
            status = record['state']['delete_status']
        except Exception:
            status = 'requested'
        return JSONResponse({'success': True, 'project_id': project_id, 'delete_status': status},
                            status_code=200 if status == 'deleted' else 202)

    @app.get("/api/sessions")
    async def list_sessions(request: Request, limit: int = Query(default=50, ge=1, le=200),
                            cursor: str | None = Query(default=None, max_length=1024),
                            ids: list[str] = Query(default=[], max_length=50)):
        user = current_user(request, users)
        next_cursor = None
        if ids:
            if any(not re.fullmatch(r'[\w.-]{1,256}', sid) or sid in {'.', '..'} for sid in ids):
                raise HTTPException(400, 'Invalid session ID')
            semaphore = asyncio.Semaphore(4)

            async def lookup(sid):
                async with semaphore:
                    try:
                        return await get_session(sid, request, user.user_id)
                    except HTTPException as exc:
                        if exc.status_code == 404:
                            return None
                        raise

            rows = [row for row in await asyncio.gather(*(lookup(sid) for sid in dict.fromkeys(ids))) if row]
            if controls_enabled():
                deleting = await runtime.request('GET', '/internal/sessions/deleting', headers=forwarded_headers(request, user.user_id))
                rows = [row for row in rows if row.get('session_id') not in set(deleting.get('session_ids', []))]
        else:
            rows, next_cursor = session_page(await user_sessions(request, user.user_id),
                                            limit=limit, cursor=cursor, activity=_session_activity)
        available_models = await available_models_for(user)
        default_model = available_models[0] if available_models else "qwen3.6-plus"
        return {"success": True, "sessions": [_session_row(row, default_model) for row in rows],
                "next_cursor": next_cursor, "has_more": next_cursor is not None}

    @app.post("/api/sessions")
    async def create_session(req: CreateSessionReq, request: Request):
        user = current_user(request, users)
        # Reject invalid/deleting project scopes before depending on model services.
        project = project_or_404(req.project_id, user.user_id) if req.project_id else None
        available_models = await available_models_for(user)
        default_model = (await model_catalog_for(user))['default_model'] if controls_enabled() else (available_models[0] if available_models else '')
        model = req.model or default_model
        if model not in available_models:
            raise HTTPException(status_code=403, detail=f"用户无权使用模型 {model}")
        project_id = project.project_id if project else None
        default_dataset_id = project.default_dataset_id if project else None
        client_session_id = str(req.client_session_id or "").strip()
        if client_session_id:
            try:
                session_id = str(uuid.UUID(client_session_id))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="client_session_id 必须是有效 UUID") from exc
        else:
            session_id = str(uuid.uuid4())

        active_dataset_id = req.active_dataset_id if req.active_dataset_id is not None else default_dataset_id
        active_dataset = None
        if active_dataset_id:
            active_dataset = project_datasets.get(active_dataset_id, user.user_id, project_id)
            if active_dataset is None or active_dataset.status != "ready":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "DRAFT_DATASET_STALE",
                        "message": "新会话选择的数据集已失效，请重新选择",
                        "project_id": project_id,
                        "active_dataset_id": default_dataset_id,
                    },
                )

        preview = " ".join(req.initial_message_preview.split())[:120]
        title = preview[:36] or "新会话"
        headers = forwarded_headers(request, user.user_id)

        async def create_or_reuse() -> tuple[dict, bool]:
            if client_session_id:
                try:
                    existing = await get_session(session_id, request, user.user_id)
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                else:
                    metadata = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
                    if str(metadata.get("client_session_id") or "") != session_id:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "SESSION_CREATION_CONFLICT",
                                "message": "该客户端 Session ID 已被其他创建请求占用",
                            },
                        )
                    existing_state = _state(existing)
                    if (existing_state.get('project_id') or None) != project_id:
                        raise HTTPException(409, {'code': 'SESSION_CREATION_CONFLICT',
                                                  'message': '该草稿已在其他范围创建会话'})
                    existing_model = str(existing_state.get("model") or model)
                    try:
                        await graph.request("GET", f"/sessions/{session_id}/graph/info", headers=headers)
                    except HTTPException as exc:
                        if exc.status_code != 404:
                            raise
                        await graph.request("POST", f"/sessions/{session_id}", headers=headers, json_body={"project_id": project_id})
                    await graph.request(
                        "PATCH",
                        f"/sessions/{session_id}/config",
                        headers=headers,
                        json_body={"llm_model": existing_model},
                    )
                    return existing, True

            state = {
                "model": model,
                "project_id": project_id,
                "active_dataset_id": active_dataset_id,
                "reasoning_mode": req.reasoning_mode,
                "planning_mode": req.planning_mode,
                "last_message_preview": preview,
            }
            try:
                await runtime.request(
                    "POST",
                    "/sessions",
                    headers=headers,
                    params={"type": "agent"},
                    json_body={
                        "session_id": session_id,
                        "session_name": title,
                        "session_state": state,
                        "metadata": {"owner": user.user_id, "client_session_id": session_id if client_session_id else ""},
                        "user_id": user.user_id,
                        "agent_id": settings.agent_id,
                    },
                )
            except HTTPException as exc:
                if client_session_id and exc.status_code == 409:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "SESSION_CREATION_CONFLICT",
                            "message": "该客户端 Session ID 已被其他创建请求占用",
                        },
                    ) from exc
                raise
            try:
                await graph.request("POST", f"/sessions/{session_id}", headers=headers, json_body={"project_id": project_id})
                await graph.request(
                    "PATCH",
                    f"/sessions/{session_id}/config",
                    headers=headers,
                    json_body={"llm_model": model},
                )
            except Exception:
                # Session creation is a single product operation. Avoid exposing a
                # half-created AgentOS session when Graph Engine initialization fails.
                try:
                    await graph.request("DELETE", f"/sessions/{session_id}", headers=headers)
                except HTTPException:
                    pass
                try:
                    await runtime.request(
                        "DELETE",
                        f"/sessions/{session_id}",
                        headers=headers,
                        params={"type": "agent", "user_id": user.user_id},
                    )
                except Exception:
                    pass
                raise
            return {
                "session_id": session_id,
                "session_name": title,
                "session_state": state,
                "metadata": {"owner": user.user_id, "client_session_id": session_id if client_session_id else ""},
            }, False

        if client_session_id:
            key = (user.user_id, session_id)
            async with session_creation_locks_guard:
                creation_lock = session_creation_locks.setdefault(key, asyncio.Lock())
                session_creation_lock_users[key] = session_creation_lock_users.get(key, 0) + 1
            try:
                async with creation_lock:
                    session, reused = await create_or_reuse()
            finally:
                async with session_creation_locks_guard:
                    remaining = session_creation_lock_users.get(key, 1) - 1
                    if remaining <= 0:
                        session_creation_lock_users.pop(key, None)
                        if session_creation_locks.get(key) is creation_lock:
                            session_creation_locks.pop(key, None)
                    else:
                        session_creation_lock_users[key] = remaining
        else:
            session, reused = await create_or_reuse()

        state = _state(session)
        response_project_id = state.get("project_id") or None
        response_dataset_id = state.get("active_dataset_id")
        response_dataset = (
            project_datasets.get(str(response_dataset_id), user.user_id, response_project_id)
            if response_dataset_id
            else None
        )
        selected_model = str(state.get("model") or model)
        return {
            "success": True,
            "session_id": session_id,
            "reused": reused,
            "project_id": response_project_id,
            "active_dataset_id": response_dataset_id,
            "dataset_view": _session_dataset_view(
                response_dataset,
                state,
                session_id=session_id,
            ),
            "provider": "openai-compatible",
            **state,
            "model": selected_model,
            "title": str(session.get("session_name") or title),
            "context_usage": {"used_tokens": 0, "limit_tokens": 0, "ratio": 0},
            "available_models": available_models,
            "default_model": default_model,
            "model_revision": state.get('model_revision', 0),
        }

    @app.post("/api/sessions/{session_id}/activate")
    async def activate_session(session_id: str, request: Request):
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        has_graph = await ensure_active_dataset_loaded(session_id, session, request, user.user_id)
        state = _state(session)
        active_dataset_id = str(state.get("active_dataset_id") or "").strip()
        active_dataset = (
            project_datasets.get(active_dataset_id, user.user_id, session_project_id(session, user.user_id))
            if active_dataset_id
            else None
        )
        runs = await get_session_runs(session_id, request, user.user_id)
        restored = _messages_from_runs(runs) or _messages(session)
        active_run, pending, _ = _active_run_snapshot(runs)
        control = await runtime.request('GET', f'/internal/sessions/{session_id}/execution',
            headers=forwarded_headers(request, user.user_id)) if controls_enabled() else None
        return {
            "success": True,
            "session_id": session_id,
            "restored": True,
            'control': control,
            "requires_graph_reload": False,
            "messages": restored,
            "has_graph": has_graph,
            "dataset_view": _session_dataset_view(
                active_dataset,
                state,
                session_id=session_id,
            ),
            "pending_feedback": pending,
            "active_run": active_run,
            "project_id": session_project_id(session, user.user_id),
            "active_dataset_id": state.get("active_dataset_id"),
            "large_graph_prepare": (
                _normalize_large_graph_prepare_context(
                    state.get("large_graph_prepare"),
                    dataset_id=active_dataset_id,
                    session_id=session_id,
                )
                if active_dataset_id
                else None
            ),
            "provider": state.get("provider", "openai-compatible"),
            "model": state.get("model", ""),
            "model_revision": state.get("model_revision", 0),
            "model_unavailable_reason": state.get("model_unavailable_reason"),
            "reasoning_mode": state.get("reasoning_mode", "direct"),
            "planning_mode": state.get("planning_mode", "auto"),
            "context_usage": session.get("metrics") or {},
            "available_models": await available_models_for(user),
            "default_model": (await model_catalog_for(user))['default_model'] if controls_enabled() else '',
        }

    @app.get("/api/sessions/{session_id}/messages")
    async def session_messages(session_id: str, request: Request):
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        return {
            "success": True,
            "session_id": session_id,
            "messages": await restored_messages(session, session_id, request, user.user_id),
        }

    @app.get("/api/sessions/{session_id}/info")
    async def session_info(session_id: str, request: Request):
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        has_graph = await ensure_active_dataset_loaded(session_id, session, request, user.user_id)
        state = _state(session)
        active_dataset_id = str(state.get("active_dataset_id") or "").strip()
        active_dataset = (
            project_datasets.get(active_dataset_id, user.user_id, session_project_id(session, user.user_id))
            if active_dataset_id
            else None
        )
        runs = await get_session_runs(session_id, request, user.user_id)
        active_run, pending, _ = _active_run_snapshot(runs)
        return {
            "session_id": session_id,
            "has_graph": has_graph,
            "dataset_view": _session_dataset_view(
                active_dataset,
                state,
                session_id=session_id,
            ),
            "pending_feedback": pending,
            "active_run": active_run,
            "project_id": session_project_id(session, user.user_id),
            "active_dataset_id": state.get("active_dataset_id"),
            "large_graph_prepare": (
                _normalize_large_graph_prepare_context(
                    state.get("large_graph_prepare"),
                    dataset_id=active_dataset_id,
                    session_id=session_id,
                )
                if active_dataset_id
                else None
            ),
            "provider": state.get("provider", "openai-compatible"),
            "model": state.get("model", ""),
            "model_revision": state.get("model_revision", 0),
            "model_unavailable_reason": state.get("model_unavailable_reason"),
            "reasoning_mode": state.get("reasoning_mode", "direct"),
            "planning_mode": state.get("planning_mode", "auto"),
            "context_usage": session.get("metrics") or {},
            "available_models": await available_models_for(user),
            "default_model": (await model_catalog_for(user))['default_model'] if controls_enabled() else '',
        }

    @app.get("/api/sessions/{session_id}/context-usage")
    async def context_usage(session_id: str, request: Request):
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        return {"success": True, "session_id": session_id, "context_usage": session.get("metrics") or {}}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request):
        user = current_user(request, users)
        if controls_enabled():
            # Runtime authenticates ownership and durably fences/cancels the
            # session before acknowledgement; its coordinator finishes cleanup.
            result = await runtime.request('DELETE', f'/internal/sessions/{session_id}',
                headers=forwarded_headers(request, user.user_id))
            return JSONResponse(result, status_code=200 if result['delete_status'] == 'deleted' else 202)
        await delete_owned_session(session_id, request, user.user_id)
        return {"success": True}

    async def patch_setting(session_id: str, request: Request, patch: dict) -> dict:
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        state = {**_state(session), **patch}
        if "model" in patch and state.get("model") and state["model"] not in await available_models_for(user):
            raise HTTPException(status_code=403, detail=f"用户无权使用模型 {state['model']}")
        await write_session_state(session_id, session, request, user.user_id, patch)
        if "model" in patch and patch.get("model"):
            await graph.request(
                "PATCH",
                f"/sessions/{session_id}/config",
                headers=forwarded_headers(request, user.user_id),
                json_body={"llm_model": patch["model"]},
            )
        return {"success": True, "session_id": session_id, **patch}

    @app.patch("/api/sessions/{session_id}/model")
    async def set_model(session_id: str, req: SessionSettingReq, request: Request):
        if controls_enabled():
            user = current_user(request, users)
            await get_session(session_id, request, user.user_id)
            if req.model_revision is None:
                raise HTTPException(409, {'code': 'MODEL_SELECTION_CHANGED'})
            result = await runtime.request('PATCH', f'/internal/sessions/{session_id}/model-selection',
                headers=service_headers(user.user_id), json_body={'model': req.model, 'model_revision': req.model_revision})
            return {'success': True, 'session_id': session_id, **result, 'available_models': await available_models_for(user)}
        result = await patch_setting(session_id, request, {"model": req.model})
        result["provider"] = "openai-compatible"
        return result

    @app.get('/api/sessions/{session_id}/model')
    async def session_model(session_id: str, request: Request):
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        state = _state(session)
        return {key: state.get(key) for key in ('model', 'model_revision', 'model_unavailable_reason')}

    @app.patch("/api/sessions/{session_id}/reasoning-mode")
    async def set_reasoning(session_id: str, req: SessionSettingReq, request: Request):
        return await patch_setting(session_id, request, {"reasoning_mode": req.reasoning_mode})

    @app.patch("/api/sessions/{session_id}/planning-mode")
    async def set_planning(session_id: str, req: SessionSettingReq, request: Request):
        return await patch_setting(session_id, request, {"planning_mode": req.planning_mode})

    @app.patch("/api/sessions/{session_id}")
    async def move_session(session_id: str, req: MoveSessionReq, request: Request):
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        if session_project_id(session, user.user_id) != req.project_id:
            raise HTTPException(409, {'code': 'SESSION_MOVE_UNSUPPORTED', 'message': '请在目标项目中新建会话'})
        return {'success': True, 'session_id': session_id, 'project_id': req.project_id}

    @app.patch("/api/sessions/{session_id}/dataset")
    async def set_session_dataset(session_id: str, req: SessionDatasetReq, request: Request):
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        project_id = session_project_id(session, user.user_id)
        dataset = None
        if req.active_dataset_id:
            dataset = project_dataset_or_404(req.active_dataset_id, project_id, user.user_id)
            if dataset.status != "ready":
                raise HTTPException(status_code=409, detail="数据集尚未准备完成")
        load_result, dataset_view = await switch_session_dataset(
            session_id,
            dataset,
            request,
            user.user_id,
        )
        return {
            "success": True,
            "session_id": session_id,
            "project_id": project_id,
            "active_dataset_id": dataset.dataset_id if dataset else None,
            "load_result": load_result,
            "dataset_view": dataset_view,
        }

    async def run_form(
        session_id: str, req: ChatReq, request: Request
    ) -> tuple[dict, dict[str, str], str, bool]:
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        req = req.model_copy(update={"attachments": _restore_attachment_tokens(
            req.attachments, user_id=user.user_id, session_id=session_id,
        ) or None})
        if req.replace_run_id and not controls_enabled():
            raise HTTPException(409, {"code": "EDIT_UNAVAILABLE", "message": "当前服务不支持替换对话，请更新服务后重试"})
        if req.replace_run_id and (req.regenerate or req.regenerate_of):
            raise HTTPException(422, {"code": "INVALID_EDIT_REQUEST"})
        regeneration = None
        if req.regenerate or req.regenerate_of:
            history = _ordered_runs(await get_session_runs(session_id, request, user.user_id))
            latest = history[-1] if history else None
            latest_id = str((latest or {}).get("run_id") or (latest or {}).get("id") or "")
            if not latest_id or (req.regenerate_of and req.regenerate_of != latest_id):
                raise HTTPException(409, {"code": "REGENERATE_TARGET_CHANGED", "message": "最新回复已变化，请刷新后重试"})
            if str(latest.get("status") or "").lower() not in {"completed", "error", "failed", "cancelled", "canceled"}:
                raise HTTPException(409, {"code": "RUN_ACTIVE", "message": "请等待当前任务结束"})
            regeneration = latest_id
        dataset_ready = await ensure_active_dataset_loaded(
            session_id, session, request, user.user_id
        )
        state = _state(session)
        active_dataset_id = str(state.get("active_dataset_id") or "").strip()
        active_dataset = (
            project_datasets.get(
                active_dataset_id,
                user.user_id,
                session_project_id(session, user.user_id),
            )
            if active_dataset_id
            else None
        )
        pending_large_graph = (
            _normalize_large_graph_prepare_context(
                state.get("large_graph_prepare"),
                dataset_id=active_dataset_id,
                session_id=session_id,
            )
            if active_dataset_id
            else None
        )
        if (
            not dataset_ready
            and active_dataset is not None
            and _is_partitioned_graph_metadata(active_dataset.metadata)
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "LARGE_GRAPH_DATASET_PREPARING",
                    "message": "项目大图仍在准备中，请稍后重试；不会重复提交该作业",
                    "job_id": (
                        pending_large_graph.get("job_id")
                        if pending_large_graph is not None
                        else None
                    ),
                    "status": (
                        pending_large_graph.get("status")
                        if pending_large_graph is not None
                        else str(
                            (active_dataset.metadata or {}).get(
                                "preparation_status", "queued"
                            )
                        )
                    ),
                    "dataset_view": _session_dataset_view(
                        active_dataset,
                        state,
                        session_id=session_id,
                    ),
                },
            )
        options = {
            "model": state.get("model", "qwen3.6-plus"),
            "reasoning_mode": req.reasoning_mode or state.get("reasoning_mode", "direct"),
            "planning_mode": req.planning_mode or state.get("planning_mode", "auto"),
            "output_format": req.output_format,
        }
        factory_options = {
            **options,
            "response_language": resolve_response_language(req.message),
        }
        project_id = session_project_id(session, user.user_id)
        training_payload = await project_training_runs(project_id, request, current_session_id=session_id)
        training_summary = _project_training_summary(training_payload.get("training_runs") or [])
        if not controls_enabled():
            await sync_custom_model(user, str(options["model"]), request)
        data_state = dataset_runtime_state(project_datasets.get(str(state.get("active_dataset_id")),
            user.user_id, project_id) if state.get("active_dataset_id") else None)
        attachment_tokens = {item.file_token for item in (req.attachments or [])}
        scoped_datasets = project_datasets.list(project_id, user.user_id)
        data_state["available_construction_sources"] = [
            {"dataset_id": item.dataset_id} for item in scoped_datasets
            if item.file_type in {"sql", "csv", "txt"}
        ]
        data_state["construction_sources"] = [
            {"dataset_id": item.dataset_id, "name": item.name, "source_filename": item.original_filename,
             "source_format": item.file_type, "status": item.metadata.get("construction", {}).get("status", "not_built")}
            for item in scoped_datasets
            if item.metadata.get("attachment_file_token") in attachment_tokens
        ]
        data_state["available_constructed_datasets"] = [
            {"dataset_id": item.dataset_id, "name": item.name, "source_format": item.file_type,
             "revision": item.metadata["construction"].get("revision")}
            for item in scoped_datasets
            if item.status == "ready" and item.metadata.get("construction", {}).get("status") == "ready"
        ]
        next_state = {
            **state,
            **options,
            "last_message_preview": req.message.strip()[:120],
            "project_training_summary": training_summary,
            "data_state": data_state,
        }
        session_name = str(session.get("session_name") or "").strip()
        session_patch: dict[str, Any] = {"session_state": next_state}
        if not session_name or session_name == "新会话":
            session_patch["session_name"] = req.message.strip().replace("\n", " ")[:36] or "新会话"
        await runtime.request(
            "PATCH",
            f"/sessions/{session_id}",
            headers=forwarded_headers(request, user.user_id),
            params={"type": "agent", "user_id": user.user_id},
            json_body=session_patch,
        )
        form = {
            "message": _message_with_attachments(req),
            "session_id": session_id,
            "user_id": user.user_id,
            "factory_input": json.dumps(factory_options, ensure_ascii=False),
            "metadata": json.dumps({"igraph_turn": {"user_message": req.message,
                "attachments": [item.model_dump() for item in req.attachments or []],
                "regenerate_of": regeneration}}, ensure_ascii=False),
        }
        return form, forwarded_headers(request, user.user_id), user.user_id, dataset_ready

    async def controlled_run_form(session_id: str, req: ChatReq, request: Request):
        user = current_user(request, users)
        req = req.model_copy(update={'start_request_id': req.start_request_id or uuid.uuid4()})
        start_key = (user.user_id, session_id, str(req.start_request_id))
        task = asyncio.current_task()
        starting_tasks.setdefault(start_key, set()).add(task)
        failed = True
        try:
            result = await controlled_run_form_body(session_id, req, request)
            failed = False
            return result
        finally:
            tasks = starting_tasks[start_key]
            tasks.discard(task)
            if not tasks:
                starting_tasks.pop(start_key, None)
                starting_finished[start_key] = {'generation': 1, 'failed': failed}

    async def controlled_run_form_body(session_id: str, req: ChatReq, request: Request):
        if req.replace_run_id and (not req.message.strip() or req.regenerate or req.regenerate_of):
            raise HTTPException(422, {"code": "INVALID_EDIT_REQUEST", "message": "修改后的提示词不能为空"})
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        req = req.model_copy(update={"attachments": _restore_attachment_tokens(
            req.attachments, user_id=user.user_id, session_id=session_id,
        ) or None})
        start_id = str(req.start_request_id or uuid.uuid4())
        state = _state(session)
        content = {"request": req.model_dump(mode='json', exclude={'start_request_id'}),
                   "model": state.get('model'), "reasoning_mode": req.reasoning_mode or state.get('reasoning_mode'),
                   "planning_mode": req.planning_mode or state.get('planning_mode'), "active_dataset_id": state.get('active_dataset_id')}
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        registration = await runtime.request('POST', f'/internal/sessions/{session_id}/runs/register',
                                             headers=forwarded_headers(request, user.user_id),
                                             json_body={"start_request_id": start_id, "fingerprint": digest, 'preparation_owner': preparation_instance,
                                                        "replace_run_id": str(req.replace_run_id) if req.replace_run_id else None,
                                                        "model_revision": req.model_revision})
        if not registration.get('preparation_claimed'):
            # Stable identity is enough for the client's normal resume/snapshot
            # path. Never resend a user question on an idempotent duplicate.
            return None, registration
        rid, generation = registration['run_id'], registration['execution_generation']
        key = (user.user_id, session_id, rid)
        token = execution_headers.set({**service_headers(user.user_id), 'X-Run-ID': rid,
                                       'X-Execution-Generation': str(generation)})
        try:
            task = asyncio.create_task(run_form(session_id, req, request))
        finally:
            execution_headers.reset(token)
        preparations[key] = task
        failed = True
        try:
            result = await task
            failed = False
            result[0]['run_id'] = rid
            return result, registration
        except asyncio.CancelledError:
            raise HTTPException(409, {"code": "RUN_CANCELLED", "run_id": rid, "start_request_id": start_id})
        finally:
            preparations.pop(key, None)
            preparation_finished[key] = {'generation': generation, 'failed': failed}
            # The coordinator can recover this acknowledgement by querying the
            # BFF even if this response/ack is lost. Browser disconnect is separate.
            ack = asyncio.create_task(runtime.request('POST', f'/internal/sessions/{session_id}/runs/{rid}/preparation-finished',
                       headers=forwarded_headers(request, user.user_id), json_body={"generation": generation, "failed": failed}))
            try:
                await asyncio.shield(ack)
            except (Exception, asyncio.CancelledError):
                # Consume detached errors without claiming durable confirmation.
                ack.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    def duplicate_start(registration, streaming: bool):
        if not streaming:
            return JSONResponse({"success": True, "control": registration, "run_id": registration['run_id']}, status_code=202)
        async def events():
            yield _sse({"type": "stream_recovery_required", "run_id": registration['run_id'],
                        "reason": "start_already_registered"})
        return StreamingResponse(events(), media_type='text/event-stream')

    async def failed_native_handoff(session_id, prepared, registration, request):
        _, _, uid, _ = prepared
        rid, generation = registration['run_id'], registration['execution_generation']
        preparation_finished[(uid, session_id, rid)] = {'generation': generation, 'failed': True}
        # Closing an unclaimed start is atomic against the native claim. An
        # already claimed producer retains its identity and normal recovery.
        try:
            await asyncio.wait_for(runtime.request('POST', f'/internal/sessions/{session_id}/runs/{rid}/preparation-finished',
                headers=forwarded_headers(request, uid), json_body={'generation': generation, 'failed': True}), 5)
        except Exception:
            pass  # The coordinator can query this instance's evidence after loss.

    @app.post("/api/sessions/{session_id}/chat")
    async def chat(session_id: str, req: ChatReq, request: Request):
        if controls_enabled():
            async def launch():
                prepared, registration = await controlled_run_form(session_id, req, request)
                if prepared is None:
                    return None, registration, None
                form, headers, _, _ = prepared
                form.update({'stream': 'false', 'background': 'true'})
                try:
                    payload = await runtime.request('POST', f'/agents/{settings.agent_id}/runs', headers=headers, data=form)
                except BaseException:
                    await failed_native_handoff(session_id, prepared, registration, request)
                    raise
                return prepared, registration, payload
            prepared, registration, payload = await asyncio.shield(own_launch(launch()))
            if prepared is None:
                return duplicate_start(registration, False)
            form, headers, user_id, dataset_ready = prepared
            # Preserve the existing non-stream response contract while the
            # producer remains independently owned by Agno.
            while str(payload.get('status') or '').lower() not in {'completed', 'cancelled', 'failed', 'error', 'paused'}:
                await asyncio.sleep(.25)
                runs = await runtime.request('GET', f'/sessions/{session_id}/runs', headers=headers,
                                             params={'type': 'agent', 'user_id': user_id})
                payload = next((run for run in _data_rows(runs) if run.get('run_id') == registration['run_id']), payload)
            return {"success": True, "run_id": registration['run_id'], "start_request_id": registration['start_request_id'],
                    "response": str(payload.get('content') or ''), 'context_usage': payload.get('metrics') or {},
                    'has_graph': dataset_ready or await graph_loaded(session_id, request, user_id),
                    'reasoning_mode': json.loads(form['factory_input'])['reasoning_mode']}
        _, reservation = await reserve_new_run(session_id, request)
        try:
            form, headers, user_id, dataset_ready = await run_form(
                session_id, req, request
            )
            form.update({"stream": "false", "background": "false"})
            payload = await runtime.request("POST", f"/agents/{settings.agent_id}/runs", headers=headers, data=form)
            return {
                "success": True,
                "response": str(payload.get("content") or ""),
                "has_graph": dataset_ready
                or await graph_loaded(session_id, request, user_id),
                "reasoning_mode": json.loads(form["factory_input"])["reasoning_mode"],
                "context_usage": payload.get("metrics") or {},
                "run_id": payload.get("run_id"),
            }
        finally:
            await release_run_reservation(reservation)

    @app.post("/api/sessions/{session_id}/chat/stream")
    async def chat_stream(session_id: str, req: ChatReq, request: Request):
        reservation = None
        if controls_enabled():
            async def launch():
                prepared, registration = await controlled_run_form(session_id, req, request)
                if prepared is None:
                    return None, None, registration
                form, headers, _, _ = prepared
                form.update({'stream': 'true', 'background': 'true'})
                owned_stack = AsyncExitStack()
                try:
                    upstream = await owned_stack.enter_async_context(runtime.stream('POST', f'/agents/{settings.agent_id}/runs', headers=headers, data=form))
                    return owned_stack, upstream, registration
                except BaseException:
                    await failed_native_handoff(session_id, prepared, registration, request)
                    await close_observation(owned_stack)
                    raise
            task = own_launch(launch())
            try:
                stack, response, registration = await asyncio.shield(task)
            except asyncio.CancelledError:
                async def disconnect():
                    try:
                        owned_stack, _, _ = await task
                        if owned_stack:
                            await close_observation(owned_stack)
                    except Exception:
                        pass  # Owned task's error is observable through control.
                own_launch(disconnect())
                raise
            if stack is None:
                return duplicate_start(registration, True)
        else:
            _, reservation = await reserve_new_run(session_id, request)
        try:
            if reservation:
                form, headers, _, _ = await run_form(session_id, req, request)
                form.update({"stream": "true", "background": "true"})
        except Exception:
            if reservation:
                await release_run_reservation(reservation)
            raise
        protocol_version = 2 if request.headers.get("X-iGraph-Stream-Version") == "2" else 1
        if reservation:
            stack = AsyncExitStack()
            try:
                response = await stack.enter_async_context(runtime.stream("POST", f"/agents/{settings.agent_id}/runs", headers=headers, data=form))
            except BaseException:
                await close_observation(stack)
                await release_run_reservation(reservation)
                raise

        async def events():
            try:
                async for chunk in observation_events(response, protocol_version=protocol_version, session_id=session_id):
                    yield chunk
            finally:
                await cleanup()

        async def cleanup():
            try:
                await close_observation(stack)
            finally:
                if reservation:
                    await release_run_reservation(reservation)

        return StreamingResponse(
            events(),
            background=BackgroundTask(cleanup),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-iGraph-Stream-Version": str(protocol_version),
            },
        )

    @app.get("/api/sessions/{session_id}/runs/{run_id}/snapshot")
    async def run_snapshot(session_id: str, run_id: str, request: Request):
        user = current_user(request, users)
        await get_session(session_id, request, user.user_id)
        runs = await get_session_runs(session_id, request, user.user_id)
        target = next((run for run in runs if str(run.get('run_id')) == run_id), None)
        if target is None:
            raise HTTPException(404, 'Run not found')
        status = str(target.get('status') or '').lower()
        phase = await runtime.request('GET', f'/internal/sessions/{session_id}/runs/{run_id}/recovery-status',
                                      headers=forwarded_headers(request, user.user_id))
        active_run, pending, _ = _active_run_snapshot(runs)
        summary = None
        if controls_enabled():
            try:
                summary = await runtime.request('GET', f'/internal/sessions/{session_id}/runs/control',
                                                headers=forwarded_headers(request, user.user_id), params={'run_id': run_id})
            except HTTPException as exc:
                # Persisted history predating the control ledger still has a
                # valid, owned native snapshot. It grants no execution rights
                # and must not be turned into a fabricated control record.
                if exc.status_code != 404 or not isinstance(exc.detail, dict) or exc.detail.get('code') != 'RUN_NOT_FOUND':
                    raise
        return {
            'session_id': session_id, 'run_id': run_id, 'status': status,
            'control': summary,
            'messages': _messages_from_runs(runs), 'active_run': active_run, 'pending_feedback': pending,
            'snapshot_complete': status in {'completed', 'error', 'failed', 'cancelled', 'paused'} and phase.get('status') == status,
            # Agno's persisted events omit content deltas and their original indices.
            # A DB last_event_index is not an atomic snapshot watermark. Fail closed.
            'resume_cursor': None,
        }

    @app.post("/api/sessions/{session_id}/runs/{run_id}/resume")
    async def resume_stream(session_id: str, run_id: str, request: Request, last_event_index: int = -1):
        user = current_user(request, users)
        await get_session(session_id, request, user.user_id)
        headers = forwarded_headers(request, user.user_id)
        data = {"last_event_index": str(last_event_index)}
        protocol_version = 2 if request.headers.get("X-iGraph-Stream-Version") == "2" else 1
        stack = AsyncExitStack()
        try:
            response = await stack.enter_async_context(runtime.stream(
                "POST", f"/internal/sessions/{session_id}/runs/{run_id}/resume", headers=headers, data=data))
        except BaseException:
            await close_observation(stack)
            raise

        async def events():
            try:
                async for chunk in observation_events(response, protocol_version=protocol_version, session_id=session_id, run_id=run_id):
                    yield chunk
            finally:
                await close_observation(stack)

        return StreamingResponse(
            events(),
            background=BackgroundTask(close_observation, stack),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-iGraph-Stream-Version": str(protocol_version),
            },
        )

    @app.post("/api/sessions/{session_id}/feedback/stream")
    async def feedback(session_id: str, req: FeedbackReq, request: Request):
        user = current_user(request, users)
        await get_session(session_id, request, user.user_id)
        run_id = (req.run_id or req.request_id).strip()
        decision_id = req.decision_id.strip() or f"legacy:{uuid.uuid4()}"
        if not run_id or (req.run_id and req.request_id and req.run_id != req.request_id):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "STALE_FEEDBACK",
                    "message": "确认请求缺少有效 Run ID",
                    "active_run": None,
                    "pending_feedback": None,
                },
            )

        lock = await run_lock(user.user_id, session_id)
        async with lock:
            now = asyncio.get_running_loop().time()
            for key, attempt in list(feedback_attempts.items()):
                if now - float(attempt.get("updated_at", now)) > feedback_attempt_ttl_seconds:
                    feedback_attempts.pop(key, None)

            runs = await get_session_runs(session_id, request, user.user_id)
            active_run, pending, active_record = _active_run_snapshot(runs)

            def conflict(code: str, message: str) -> HTTPException:
                return HTTPException(
                    status_code=409,
                    detail={
                        "code": code,
                        "message": message,
                        "active_run": active_run,
                        "pending_feedback": pending,
                    },
                )

            requested_call_id = req.tool_call_id.strip()
            prior = next(
                (
                    attempt
                    for key, attempt in feedback_attempts.items()
                    if key[:3] == (user.user_id, session_id, run_id)
                    and (not requested_call_id or key[3] == requested_call_id)
                ),
                None,
            )
            if prior is not None:
                code = "FEEDBACK_ALREADY_ACCEPTED" if prior.get("state") == "accepted" else "FEEDBACK_IN_PROGRESS"
                message = "该确认已经提交，正在核对 Run 状态" if code == "FEEDBACK_ALREADY_ACCEPTED" else "该确认正在提交，请勿重复操作"
                raise conflict(code, message)

            if active_run is None:
                raise conflict("NO_PENDING_FEEDBACK", "当前会话没有待确认的活动 Run")
            if active_run.get("run_id") != run_id:
                raise conflict("STALE_FEEDBACK", "确认请求已过期，请刷新当前会话状态")
            if active_run.get("status") != "paused" or active_record is None or pending is None:
                raise conflict("NO_PENDING_FEEDBACK", "当前 Run 已不再等待确认")

            authoritative = feedback_request_from_payload(active_record)
            requirements = authoritative.get("pending_requirements") or []
            if isinstance(requirements, list) and len(requirements) > 1:
                raise conflict("MULTIPLE_FEEDBACK_UNSUPPORTED", "当前 Run 存在多个待确认操作，已安全停止提交")
            call_id = str(authoritative.get("call_id") or "").strip()
            tool_name = str(authoritative.get("tool_name") or "").strip()
            if not call_id or not tool_name:
                raise conflict("STALE_FEEDBACK", "AgentOS 暂停记录缺少工具身份，无法安全继续")
            if requested_call_id and requested_call_id != call_id:
                raise conflict("STALE_FEEDBACK", "待确认工具已经变化，请重新核对确认卡片")
            if req.tool_name.strip() and req.tool_name.strip() != tool_name:
                raise conflict("STALE_FEEDBACK", "待确认工具名称已经变化，请重新核对确认卡片")

            attempt_key = (user.user_id, session_id, run_id, call_id)
            feedback_attempts[attempt_key] = {
                "decision_id": decision_id,
                "state": "submitting",
                "updated_at": now,
            }

        confirmed = req.decision in {"approve", "modify"}
        authoritative_args = authoritative.get("proposed_args")
        tool_args = req.patched_args if req.decision == "modify" else authoritative_args
        decision_fields = {
            "tool_call_id": call_id,
            "tool_name": tool_name,
            "tool_args": tool_args if isinstance(tool_args, dict) else {},
            # Agno's continuation dispatch executes/rejects an approved tool
            # only when its original confirmation requirement is preserved.
            "requires_confirmation": True,
            "confirmed": confirmed,
            "confirmation_note": req.comment,
        }
        # Agno replaces run_response.tools with the submitted list. Preserve
        # completed tools/artifacts and the target's original execution metadata.
        stored_tools = [dict(tool) for tool in (active_record.get("tools") or []) if isinstance(tool, dict)]
        tools = [{**tool, **decision_fields} if str(tool.get("tool_call_id")) == call_id else tool for tool in stored_tools]
        if not any(str(tool.get("tool_call_id")) == call_id for tool in tools):
            requirement = next((item.get("tool_execution") for item in requirements
                                if isinstance(item.get("tool_execution"), dict)
                                and str(item["tool_execution"].get("tool_call_id")) == call_id), {})
            tools.append({**requirement, **decision_fields})
        data = {"tools": json.dumps(tools), "session_id": session_id, "user_id": user.user_id, "stream": "true", "background": "true"}
        protocol_version = 2 if request.headers.get("X-iGraph-Stream-Version") == "2" else 1
        stack = AsyncExitStack()
        try:
            response = await stack.enter_async_context(runtime.stream(
                "POST", f"/agents/{settings.agent_id}/runs/{run_id}/continue",
                headers=forwarded_headers(request, user.user_id), data=data))
        except BaseException as exc:
            await close_observation(stack)
            # Only an explicit upstream HTTP rejection proves non-acceptance.
            # A transport failure may have lost an accepted response.
            if isinstance(exc, HTTPException):
                attempt = feedback_attempts.get(attempt_key)
                if attempt and attempt.get("decision_id") == decision_id:
                    feedback_attempts.pop(attempt_key, None)
                exc.headers = {**(exc.headers or {}), "X-iGraph-Feedback-Accepted": "false"}
            raise
        attempt = feedback_attempts.get(attempt_key)
        if attempt and attempt.get("decision_id") == decision_id:
            attempt.update({"state": "accepted", "updated_at": asyncio.get_running_loop().time()})

        async def events():
            try:
                yield _stream_event_frame(
                    {"type": "feedback_accepted", "session_id": session_id, "run_id": run_id, "decision_id": decision_id},
                    protocol_version=protocol_version, session_id=session_id, run_id=run_id)
                async for chunk in observation_events(response, protocol_version=protocol_version, session_id=session_id, run_id=run_id):
                    yield chunk
            finally:
                await close_observation(stack)

        return StreamingResponse(
            events(),
            background=BackgroundTask(close_observation, stack),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-iGraph-Stream-Version": str(protocol_version),
            },
        )

    async def learned_memory_items(request: Request, user_id: str) -> list[dict]:
        payload = await runtime.request("GET", "/internal/memory", headers={**forwarded_headers(request, user_id), **service_headers(user_id)})
        return payload["items"]

    @app.get("/api/memory")
    async def list_memory(request: Request, scope: str = "long_term", query: str = "", limit: int = Query(100, ge=1, le=1000)):
        user = current_user(request, users)
        payload = await runtime.request(
            "GET", "/memories", headers=forwarded_headers(request, user.user_id),
            params={"user_id": user.user_id, "topics": scope, "search_content": query or None, "limit": limit},
        )
        rows = _data_rows(payload)
        items = []
        for row in rows:
            topics = row.get("topics") or []
            category = topics[1] if len(topics) > 1 else "fact"
            key = topics[2] if len(topics) > 2 else "memory"
            item = {
                "memory_id": row.get("memory_id"),
                "scope": scope,
                "category": category,
                "key": key,
                "value": row.get("memory", ""),
                "confidence": row.get("confidence"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
            if scope == "playbook":
                item.update({
                    "playbook_id": row.get("memory_id"),
                    "problem_type": key,
                    "approach": row.get("memory", ""),
                    "lifecycle_status": category if category in {"candidate", "validated", "active", "promoted_to_skill"} else "candidate",
                })
            elif scope == "user_profile":
                item["profile_id"] = row.get("memory_id")
            items.append(item)
        if scope == "long_term":
            learned = await learned_memory_items(request, user.user_id)
            search = query.strip().casefold()
            if search:
                learned = [item for item in learned if search in " ".join(
                    str(item.get(field) or "") for field in ("key", "value", "category")
                ).casefold()]
            items = learned + items
        return {"success": True, "scope": scope, "items": items[:limit]}

    @app.post("/api/memory")
    async def write_memory(req: MemoryWriteReq, request: Request):
        user = current_user(request, users)
        payload = await runtime.request(
            "POST", "/memories", headers=forwarded_headers(request, user.user_id),
            json_body={"memory": req.value, "user_id": user.user_id, "topics": [req.scope, req.category, req.key]},
        )
        return {"success": True, "memory_id": payload.get("memory_id"), "item": payload}

    @app.patch("/api/memory/{memory_id}")
    async def update_memory(memory_id: str, req: MemoryPatchReq, request: Request):
        user = current_user(request, users)
        if memory_id.startswith("learning:"):
            return await runtime.request(
                "PATCH", f"/internal/memory/{memory_id}", headers={**forwarded_headers(request, user.user_id), **service_headers(user.user_id)},
                json_body={"value": req.value, "key": req.key},
            )
        payload = await runtime.request(
            "PATCH", f"/memories/{memory_id}", headers=forwarded_headers(request, user.user_id),
            json_body={"memory": req.value, "user_id": user.user_id, "topics": [req.scope, "fact", req.key]},
        )
        return {"success": True, "item": payload}

    @app.delete("/api/memory/{memory_id}")
    async def delete_memory(memory_id: str, request: Request, scope: str = "long_term", reason: str = ""):
        user = current_user(request, users)
        if memory_id.startswith("learning:"):
            await runtime.request(
                "DELETE", f"/internal/memory/{memory_id}", headers={**forwarded_headers(request, user.user_id), **service_headers(user.user_id)},
            )
            return {"success": True, "memory_id": memory_id, "scope": "long_term", "reason": reason}
        await runtime.request(
            "DELETE", f"/memories/{memory_id}", headers=forwarded_headers(request, user.user_id), params={"user_id": user.user_id}
        )
        return {"success": True, "memory_id": memory_id, "scope": scope, "reason": reason}

    @app.get("/api/memory/events")
    async def memory_events(request: Request, limit: int = 200):
        user = current_user(request, users)
        headers = forwarded_headers(request, user.user_id)
        sessions_payload = await runtime.request(
            "GET", "/sessions", headers=headers,
            params={"type": "agent", "component_id": settings.agent_id, "user_id": user.user_id, "limit": 200},
        )
        events: list[dict] = []
        for session in _data_rows(sessions_payload):
            session_id = str(session.get("session_id") or "")
            if not session_id:
                continue
            runs_payload = await runtime.request(
                "GET", f"/sessions/{session_id}/runs", headers=headers,
                params={"type": "agent", "user_id": user.user_id},
            )
            for run in _data_rows(runs_payload):
                for event in run.get("events") or []:
                    if not isinstance(event, dict):
                        continue
                    event_name = str(event.get("event") or "")
                    if not any(token in event_name.lower() for token in ("memory", "learning", "knowledge")):
                        continue
                    events.append({
                        "type": event_name,
                        "session_id": session_id,
                        "run_id": run.get("run_id"),
                        "ts": event.get("created_at") or run.get("created_at"),
                    })
        events.sort(key=lambda item: str(item.get("ts") or ""))
        return {"success": True, "events": events[-max(1, limit):], "source": "agentos-run-events"}

    @app.get("/api/memory/usage")
    async def memory_usage(request: Request):
        user = current_user(request, users)
        payload = await runtime.request("GET", "/memories", headers=forwarded_headers(request, user.user_id), params={"user_id": user.user_id, "limit": 1000})
        meta = (payload.get("meta") or {}) if isinstance(payload, dict) else {}
        rows = _data_rows(payload)
        learned = await learned_memory_items(request, user.user_id)
        included_ids = [str(item.get("memory_id")) for item in [*learned, *rows] if item.get("memory_id")]
        return {
            "success": True,
            "count": meta.get("total_count", len(rows)) + len(learned),
            "snapshot_char_count": sum(len(str(item.get("memory") or "")) for item in rows)
                + sum(len(item["value"]) for item in learned),
            "snapshot": "",
            "frozen": False,
            "included_item_ids": included_ids,
            "excluded_counts": {
                "archived": 0,
                "candidate": 0,
                "promoted_to_skill": 0,
                "char_budget": 0,
                "wrong_scope": 0,
            },
            "scope": user.user_id,
            "source": "agentos",
        }

    @app.get("/api/metrics/knowledge")
    async def knowledge_metrics(request: Request):
        """Compatibility metrics backed by AgentOS-owned resources."""

        user = current_user(request, users)
        headers = forwarded_headers(request, user.user_id)
        memories = await runtime.request(
            "GET", "/memories", headers=headers,
            params={"user_id": user.user_id, "limit": 1},
        )
        skills = await runtime.request("GET", "/internal/skills", headers=headers)
        memory_meta = memories.get("meta") if isinstance(memories, dict) else {}
        memory_count = (memory_meta or {}).get("total_count", len(_data_rows(memories)))
        memory_count += len(await learned_memory_items(request, user.user_id))
        skill_rows = skills.get("skills", []) if isinstance(skills, dict) else []
        skill_counts: dict[str, int] = {}
        for item in skill_rows if isinstance(skill_rows, list) else []:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "unknown")
            skill_counts[source] = skill_counts.get(source, 0) + 1
        return {
            "counters": {
                "agentos_memories": int(memory_count or 0),
                "skills_total": sum(skill_counts.values()),
                **{f"skills_{key}": value for key, value in skill_counts.items()},
            },
            "source": "agentos",
        }

    @app.get("/api/export/sft")
    async def export_sft(
        request: Request,
        format: str = "alpaca",
        min_turns: int = 1,
        require_tool_calls: bool = False,
    ):
        """Export AgentOS runs using the former alpaca/sharegpt/raw contract."""

        user = current_user(request, users)
        output_format = str(format or "alpaca").strip().lower()
        headers = forwarded_headers(request, user.user_id)
        sessions_payload = await runtime.request(
            "GET", "/sessions", headers=headers,
            params={"type": "agent", "component_id": settings.agent_id, "user_id": user.user_id, "limit": 1000},
        )
        items: list[dict] = []
        for session in _data_rows(sessions_payload):
            session_id = str(session.get("session_id") or "")
            if not session_id:
                continue
            runs_payload = await runtime.request(
                "GET", f"/sessions/{session_id}/runs", headers=headers,
                params={"user_id": user.user_id},
            )
            runs = _ordered_runs(_data_rows(runs_payload) or (
                [item for item in runs_payload if isinstance(item, dict)]
                if isinstance(runs_payload, list) else []
            ))
            completed = [
                run for run in runs
                if _run_input_text(run).strip() and _run_output_text(run).strip()
            ]
            if len(completed) < max(int(min_turns), 1):
                continue
            for run in completed:
                tools = run.get("tools") or []
                if require_tool_calls and not (isinstance(tools, list) and tools):
                    continue
                row = _sft_row(run, output_format)
                if row is not None:
                    items.append(row)
        return {"success": True, "format": output_format, "items": items, "source": "agentos-runs"}

    @app.post("/api/skills/generate-from-playbook")
    async def generate_skill_from_playbook(req: GenerateSkillReq, request: Request):
        user = current_user(request, users)
        playbook = await get_playbook_memory(req.playbook_id, request, user.user_id)
        playbook_content = str(playbook.get("memory") or "")
        session_id = str(uuid.uuid4())
        if req.session_id.strip():
            session = await get_session(req.session_id.strip(), request, user.user_id)
            state = _state(session)
        else:
            state = {}
        available_models = await available_models_for(user)
        model = state.get("model", available_models[0] if available_models else "qwen3.6-plus")
        await sync_custom_model(user, str(model), request)
        prompt = (
            f"请把下面来自当前用户 AgentOS Memory/Learned Knowledge 的 Playbook 生成一个可执行 Skill。\n\n"
            f"Playbook ID: {req.playbook_id}\nPlaybook 内容:\n{playbook_content[:12000]}\n\n"
            "只输出完整 SKILL.md，不要解释；必须包含 YAML frontmatter（name、description）以及适用场景、操作步骤、"
            "输入输出、失败处理和安全边界。name 必须使用小写字母、数字和连字符。不要调用任何工具。"
        )
        data = {
            "message": prompt,
            "session_id": session_id,
            "user_id": user.user_id,
            "factory_input": json.dumps({
                "model": model,
                "reasoning_mode": state.get("reasoning_mode", "direct"),
                "planning_mode": state.get("planning_mode", "auto"),
                "output_format": "chat",
            }, ensure_ascii=False),
            "stream": "true",
            "background": "false",
        }
        headers = forwarded_headers(request, user.user_id)

        async def events():
            full: list[str] = []
            completed = False
            try:
                async with runtime.stream("POST", f"/agents/{settings.agent_id}/runs", headers=headers, data=data) as response:
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw:
                            continue
                        payload = json.loads(raw)
                        event = str(payload.get("event") or payload.get("type") or "")
                        if event in {"RunContent", "TeamRunContent"}:
                            token = str(payload.get("content") or "")
                            if token:
                                full.append(token)
                                yield _sse({"type": "token", "content": token})
                        elif event in {"RunCompleted", "TeamRunCompleted"}:
                            if not full and payload.get("content"):
                                full.append(str(payload["content"]))
                            completed = True
                        elif event in {"RunError", "RunCancelled", "TeamRunError", "TeamRunCancelled"}:
                            yield _sse({"type": "error", "error": str(payload.get("content") or event)})
                            return
                if not completed:
                    yield _sse({"type": "error", "error": "AgentOS Skill 生成流未正常完成"})
                    return
                content = "".join(full)
                suffix = re.sub(r"[^a-z0-9]", "", req.playbook_id.lower())[-8:] or "skill"
                suggested = _canonical_skill_name(f"playbook-{suffix}")
                yield _sse({"type": "done", "suggested_name": suggested, "full_content": content, "completed": completed})
            except Exception as exc:
                yield _sse({"type": "error", "error": str(exc)})
            finally:
                try:
                    await runtime.request(
                        "DELETE",
                        f"/sessions/{session_id}",
                        headers=headers,
                        params={"type": "agent", "user_id": user.user_id},
                    )
                except Exception:
                    pass

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/skills/promote-playbook")
    async def promote_playbook(req: PromotePlaybookReq, request: Request):
        user = current_user(request, users)
        playbook = await get_playbook_memory(req.playbook_id, request, user.user_id)
        playbook_content = str(playbook.get("memory") or "").strip()
        name = _canonical_skill_name(req.skill_name)
        description = req.description or f"从 AgentOS Learned Knowledge Playbook {req.playbook_id} 升级"
        body = (
            "---\n"
            f"name: {name}\n"
            f"description: {json.dumps(description, ensure_ascii=False)}\n"
            f"domain: {req.domain}\n"
            f"risk_level: {req.risk_level}\n"
            "version: 0.1.0\n"
            "---\n\n"
            f"# {name}\n\n"
            f"本 Skill 从当前用户的 AgentOS Learned Knowledge Playbook `{req.playbook_id}` 升级。\n\n"
            "## 原始 Playbook\n\n"
            f"{playbook_content}\n\n"
            "## 执行约束\n\n"
            "1. 校验 Playbook 的适用条件与当前任务一致。\n"
            "2. 按记录的步骤执行，并在关键写操作前请求确认。\n"
            "3. 汇总工具结果、失败原因与可复现信息。\n"
        )
        payload = await runtime.request(
            "POST",
            "/internal/skills",
            headers={**forwarded_headers(request, user.user_id), "Content-Type": "application/json"},
            json_body={
                "name": name,
                "body": body,
                "metadata": {
                    "source": "manual_promote",
                    "playbook_id": req.playbook_id,
                    "domain": req.domain,
                    "risk_level": req.risk_level,
                },
                "destination": "review_queue",
            },
        )
        return {**payload, "playbook_id": req.playbook_id}

    @app.api_route("/api/skills", methods=["GET", "POST"])
    async def skills_root(request: Request):
        user = current_user(request, users)
        body = await request.body()
        return await runtime.request(
            request.method, "/internal/skills", headers={**forwarded_headers(request, user.user_id), "Content-Type": "application/json"},
            params=dict(request.query_params), json_body=json.loads(body) if body else None,
        )

    @app.post("/api/skills/upload")
    async def upload_skill(request: Request, file: UploadFile = File(...)):
        user = current_user(request, users)
        content = await file.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise HTTPException(413, "Skill 文件不能超过 2MB")
        return await runtime.request("POST", "/internal/skills/upload",
            headers=forwarded_headers(request, user.user_id),
            files={"file": (file.filename or "", content, file.content_type or "application/octet-stream")})

    @app.api_route("/api/skills/{name}", methods=["GET", "PUT", "DELETE"])
    async def skill_item(name: str, request: Request):
        user = current_user(request, users)
        body = await request.body()
        return await runtime.request(
            request.method, f"/internal/skills/{name}", headers={**forwarded_headers(request, user.user_id), "Content-Type": "application/json"},
            params=dict(request.query_params), json_body=json.loads(body) if body else None,
        )

    @app.post("/api/skills/{name}/{decision}")
    async def skill_decision(name: str, decision: Literal["approve", "reject"], request: Request):
        user = current_user(request, users)
        body = await request.body()
        return await runtime.request(
            "POST", f"/internal/skills/{name}/{decision}", headers={**forwarded_headers(request, user.user_id), "Content-Type": "application/json"},
            json_body=json.loads(body) if body else {},
        )

    async def graph_tool(session_id: str, tool_name: str, arguments: dict, request: Request):
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        await ensure_active_dataset_loaded(session_id, session, request, user.user_id)
        return await graph.request(
            "POST", f"/sessions/{session_id}/tools/{tool_name}", headers={**forwarded_headers(request, user.user_id), **service_headers(user.user_id)},
            json_body={"arguments": arguments},
        )

    @app.get(MIGRATION_GUIDE_PATH)
    async def dataset_api_migration(request: Request):
        current_user(request, users)
        return DATASET_API_MIGRATION

    @app.get("/api/datasets", deprecated=True, dependencies=[Depends(legacy_dataset_api_notice)])
    async def datasets(request: Request):
        user = current_user(request, users)
        return await graph.request("GET", "/datasets", headers={**forwarded_headers(request, user.user_id),
            **service_headers(user.user_id), **scope_headers(None)})

    @app.delete("/api/datasets/{dataset_id}", deprecated=True, dependencies=[Depends(legacy_dataset_api_notice)])
    async def delete_dataset(dataset_id: str, request: Request):
        user = current_user(request, users)
        return await graph.request("DELETE", f"/datasets/{dataset_id}", headers=forwarded_headers(request, user.user_id))

    @app.post("/api/sessions/{session_id}/load-dataset", deprecated=True, dependencies=[Depends(legacy_dataset_api_notice)])
    async def load_dataset(session_id: str, request: Request):
        return await graph_tool(session_id, "load_dataset", await request.json(), request)

    @app.post("/api/sessions/{session_id}/save-dataset", deprecated=True, dependencies=[Depends(legacy_dataset_api_notice)])
    async def save_dataset(session_id: str, request: Request):
        return await graph_tool(session_id, "save_as_dataset", await request.json(), request)

    @app.post("/api/sessions/{session_id}/graph/clear")
    async def clear_graph(session_id: str, request: Request):
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        result = await graph.request("POST", f"/sessions/{session_id}/graph/clear", headers=forwarded_headers(request, user.user_id), json_body={})
        await write_session_state(
            session_id,
            session,
            request,
            user.user_id,
            {"active_dataset_id": None},
        )
        return result

    @app.get(
        "/api/sessions/{session_id}/large-graph/artifacts/"
        "{artifact_id}/partitions/{partition}"
    )
    async def download_large_graph_artifact_partition(
        session_id: str, artifact_id: str, partition: int, request: Request
    ):
        """Authenticated streaming proxy for private Graph Engine artifacts."""
        if partition < 0:
            raise HTTPException(status_code=404, detail="Artifact 分区不存在")
        user = current_user(request, users)
        await get_session(session_id, request, user.user_id)
        upstream_client, upstream = await graph.open_stream(
            "GET",
            (
                f"/sessions/{session_id}/large-graph/artifacts/"
                f"{artifact_id}/partitions/{partition}"
            ),
            headers=forwarded_headers(request, user.user_id),
        )

        async def body():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await upstream_client.aclose()

        response_headers = {}
        disposition = upstream.headers.get("content-disposition", "").strip()
        if disposition:
            response_headers["Content-Disposition"] = disposition
        return StreamingResponse(
            body(),
            media_type=upstream.headers.get(
                "content-type", "application/x-ndjson"
            ).split(";", 1)[0],
            headers=response_headers,
        )

    @app.post("/api/sessions/{session_id}/graph/visualize")
    async def visualize_graph(session_id: str, request: Request):
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        state = _state(session)
        project_id = session_project_id(session, user.user_id)
        active_dataset_id = str(state.get("active_dataset_id") or "").strip()
        active_dataset = (
            project_datasets.get(active_dataset_id, user.user_id, project_id)
            if active_dataset_id
            else None
        )
        view = _session_dataset_view(
            active_dataset,
            state,
            session_id=session_id,
        )
        visualization = (
            view.get("visualization")
            if isinstance(view, dict) and isinstance(view.get("visualization"), dict)
            else {}
        )
        if view and visualization.get("supported") is False:
            partitioned = view.get("kind") == _PARTITIONED_GRAPH_KIND
            raise HTTPException(
                status_code=409,
                detail={
                    "code": (
                        "PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE"
                        if partitioned
                        else "GRAPH_NOT_VISUALIZABLE"
                    ),
                    "message": (
                        "分区大图不能直接全量可视化；请先运行有界子图提取，再可视化结果"
                        if partitioned
                        else "非独立图，无法直接可视化"
                    ),
                    "dataset_view": view,
                },
            )
        if not await ensure_active_dataset_loaded(session_id, session, request, user.user_id):
            active_dataset = (
                project_datasets.get(active_dataset_id, user.user_id, project_id)
                if active_dataset_id
                else None
            )
            view = _session_dataset_view(
                active_dataset,
                _state(session),
                session_id=session_id,
            )
            visualization = (
                view.get("visualization")
                if isinstance(view, dict)
                and isinstance(view.get("visualization"), dict)
                else {}
            )
            if view and visualization.get("supported") is False:
                partitioned = view.get("kind") == _PARTITIONED_GRAPH_KIND
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": (
                            "PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE"
                            if partitioned
                            else "GRAPH_NOT_VISUALIZABLE"
                        ),
                        "message": (
                            "分区大图不能直接全量可视化；请先运行有界子图提取，再可视化结果"
                            if partitioned
                            else "非独立图，无法直接可视化"
                        ),
                        "dataset_view": view,
                    },
                )
            raise HTTPException(
                status_code=409,
                detail={"code": "GRAPH_NOT_LOADED", "message": "尚未加载可视化的独立图"},
            )
        body = await request.json()
        result = await graph.request(
            "POST", f"/sessions/{session_id}/graph/visualize", headers=forwarded_headers(request, user.user_id),
            json_body={"arguments": body or {}},
        )
        if not isinstance(result, dict):
            raise HTTPException(
                status_code=502,
                detail={"code": "INVALID_GRAPH_RESPONSE", "message": "Graph Engine 返回了无效的可视化响应"},
            )
        if result.get("success") is False:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "GRAPH_VISUALIZATION_FAILED",
                    "message": str(result.get("error") or "图谱可视化失败"),
                },
            )
        if not isinstance(result.get("nodes"), list) or not isinstance(result.get("edges"), list):
            raise HTTPException(
                status_code=502,
                detail={"code": "INVALID_GRAPH_RESPONSE", "message": "Graph Engine 返回的图结构不完整"},
            )
        return result

    @app.get("/api/sessions/{session_id}/workspace/files")
    async def workspace_files(session_id: str, request: Request):
        user = current_user(request, users)
        await get_session(session_id, request, user.user_id)
        return {"success": True, "files": await asyncio.to_thread(visible_files, user.user_id, session_id)}

    @app.get("/api/sessions/{session_id}/workspace/derived/{artifact_path:path}")
    async def download_derived(session_id: str, artifact_path: str, request: Request):
        user = current_user(request, users)
        await get_session(session_id, request, user.user_id)
        try:
            target = derived_file(user.user_id, session_id, artifact_path)
        except ValueError:
            raise HTTPException(404, {"code": "ARTIFACT_NOT_FOUND", "message": "文件不存在"})
        return FileResponse(target, filename=target.name, media_type="application/octet-stream",
                            headers={"X-Content-Type-Options": "nosniff"})

    @app.post("/api/sessions/{session_id}/upload")
    async def upload(session_id: str, request: Request, file: UploadFile = File(...)):
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        file_token = uuid.uuid4().hex
        max_bytes = max(1, int(os.getenv("IGRAPH_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))))
        content = await file.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise HTTPException(413, {"code": "UPLOAD_TOO_LARGE", "max_bytes": max_bytes})
        if not content:
            raise HTTPException(400, {"code": "EMPTY_UPLOAD", "message": "上传文件不能为空"})
        filename = file.filename or f"upload-{file_token}.bin"
        payload = await graph.request(
            "POST", f"/sessions/{session_id}/uploads/ingest", headers=forwarded_headers(request, user.user_id),
            data={"file_token": file_token, "original_filename": filename},
            files={"file": (filename, content, file.content_type or "application/octet-stream")},
        )
        dataset = None
        if Path(filename).suffix.lower() in {".sql", ".csv", ".txt"}:
            if not content:
                raise HTTPException(400, "数据集文件不能为空")
            project_id = session_project_id(session, user.user_id)
            if project_id:
                project_or_404(project_id, user.user_id)
            dataset = project_datasets.create(project_id=project_id, user_id=user.user_id,
                name=Path(filename).stem, original_filename=filename, content=content)
            dataset = project_datasets.update(dataset.dataset_id, user.user_id, project_id=project_id,
                metadata={**dataset.metadata, "attachment_file_token": file_token}) or dataset
        # Identical bytes with different extensions still need independent graph registration.
        _, workspace_path = save_upload(user.user_id, session_id, file_token, filename, content)
        return {"success": True, "workspace_path": workspace_path, "filename": filename, "file_token": file_token, "graph_engine": payload,
                "dataset": _project_dataset_row(dataset) if dataset else None,
                "construction_required": dataset is not None}

    install_constructed_import_routes(app, graph=graph, directory=project_datasets, projects=projects,
        user_for=lambda request: current_user(request, users), project_for=project_or_404,
        dataset_row=_project_dataset_row)

    install_construction_access(app, directory=project_datasets, projects=projects,
        session_for=get_session, state_for=_state)

    install_construction_routes(app, graph=graph, directory=project_datasets,
        user_for=lambda request: current_user(request, users), project_for=project_or_404,
        dataset_for=project_dataset_or_404)

    async def delete_project_constructions(project_id: str, user_id: str):
        for item in project_datasets.list(project_id, user_id):
            if "construction" in item.metadata:
                await graph.request("DELETE", f"/construction/{item.dataset_id}", headers={**service_headers(user_id), **scope_headers(project_id)})

    return app


app = create_app()

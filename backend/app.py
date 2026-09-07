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
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from backend.general_migration import GeneralMigration
from backend.config import BackendConfig
from backend.custom_models import CustomModelDirectory, CustomModelRecord, public_custom_model
from backend.event_mapper import feedback_request_from_payload, translate_sse
from backend.identity import current_user, public_user, require_admin
from backend.project_datasets import ProjectDatasetDirectory, ProjectDatasetRecord
from backend.projects import ProjectDirectory, ProjectRecord
from backend.service_client import ServiceClient, forwarded_headers
from backend.stream_recovery import close_observation, observation_events
from backend.users import UserDirectory, UserRecord
from execution_protocol import (CancelRequest, JobCancelRequest, Target, controls_enabled,
                                execution_headers, require_service, service_headers)
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
    name: str = Field(min_length=1, max_length=120)


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
    start_request_id: uuid.UUID | None = None


class SessionSettingReq(BaseModel):
    model: str | None = None
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


def _run_input_text(run: dict) -> str:
    value = run.get("run_input")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("input_content", "message", "content"):
            if value.get(key) is not None:
                return str(value[key])
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
        if isinstance(item, dict)
    )
    detail = job.get("detail") if isinstance(job.get("detail"), dict) else {}
    if detail.get("epoch") is not None:
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
            result = tool.get("result")
            if isinstance(result, dict) and (result.get("success") is False or (result.get("error") and not result.get("async"))):
                continue
            if isinstance(result, str) and re.search(
                r"validation errors?|error (?:from|executing) .*train|invalid (?:argument|input)",
                result,
                re.IGNORECASE,
            ):
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
            "status": "done" if card.get("done") else "running",
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
        if user_text:
            messages.append({"id": f"user:{run_id}", "run_id": run_id, "role": "user", "content": user_text})

        tool_cards: list[dict] = []
        graphs: list[dict] = []
        reports: list[dict] = []
        tools = run.get("tools") or []
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
                "tools": tool_cards,
                "graphs": graphs,
                "reports": reports,
                "thinking": ([{"phase": "reasoning", "text": reasoning_text, "streaming": False}] if reasoning_text else []),
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
    return {
        "kind": "single_graph",
        "visualization": {"supported": True, "code": None},
        "summary": {
            key: metadata[key]
            for key in ("num_nodes", "num_edges", "directed", "has_node_features", "has_node_labels")
            if metadata.get(key) is not None
        },
    }


def _message_with_attachments(req: ChatReq) -> str:
    names = [item.filename for item in (req.attachments or []) if item.filename]
    if not names:
        return req.message
    hints: list[str] = []
    for name in names:
        suffix = Path(name).suffix.lower()
        if suffix == ".pt":
            hint = "多图分类数据集使用 load_graph_classification_dataset；普通单图使用 load_graph_from_upload"
        elif suffix in {".npz", ".json", ".csv", ".tsv", ".txt", ".edgelist", ".dat"}:
            hint = "使用 load_graph_from_upload 自动检测并加载"
        else:
            hint = "根据文件格式选择上传文件加载工具"
        hints.append(f"- {name}（{hint}）")
    return (
        req.message
        + "\n\n[附件文件]\n以下文件已上传并注册到当前会话：\n"
        + "\n".join(hints)
        + "\n用户说‘这个文件/图数据’时即指上述附件；不要要求用户再次上传。"
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
    async def lifespan(_app):
        nonlocal project_reconciler
        if controls_enabled():
            project_reconciler = asyncio.create_task(reconcile_project_deletions())
        try:
            yield
        finally:
            tasks = set(launch_tasks) | set(preparations.values())
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
            return {"run_cancel_v1": bool(ready), "job_cancel_v1": bool(ready)}
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
        result = await runtime.request('POST', f'/internal/sessions/{session_id}/jobs/{job_id}/cancel',
                                       headers=forwarded_headers(request, user.user_id), json_body=req.model_dump(mode='json'))
        return JSONResponse(result, status_code=200 if result.get('execution_stopped') else 202)

    @app.get('/api/sessions/{session_id}/jobs/{job_id}/control')
    async def job_control(session_id: str, job_id: str, request: Request):
        user = current_user(request, users)
        return await runtime.request('GET', f'/internal/sessions/{session_id}/jobs/{job_id}/summary',
                                     headers=forwarded_headers(request, user.user_id))

    def available_models_for(user: UserRecord) -> list[str]:
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
    def validate(request: Request):
        user_id = request.headers.get("X-User-ID", "").strip()
        record = users.get(user_id)
        if not record or not record.enabled:
            return {"valid": False, "reason": "user_not_supported" if user_id else "missing_user_id"}
        models = available_models_for(record)
        return {
            "valid": True,
            "user_id": record.user_id,
            "is_admin": "admin" in record.roles,
            "available_models": models,
            "default_model": models[0] if models else "qwen3.6-plus",
        }

    @app.get("/api/models/custom")
    def list_custom_models(request: Request):
        user = current_user(request, users)
        return {"success": True, "models": [public_custom_model(item) for item in custom_models.list(user.user_id)]}

    @app.post("/api/models/custom")
    async def save_custom_model(req: CustomModelReq, request: Request):
        user = current_user(request, users)
        try:
            record = custom_models.save(CustomModelRecord(user_id=user.user_id, **req.model_dump()))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await sync_custom_model(user, record.model_id, request)
        return {"success": True, "model": public_custom_model(record)}

    @app.delete("/api/models/custom/{model_id:path}")
    async def delete_custom_model(model_id: str, request: Request):
        user = current_user(request, users)
        if not custom_models.delete(user.user_id, model_id):
            raise HTTPException(status_code=404, detail="自定义模型不存在")
        await runtime.request(
            "DELETE",
            f"/internal/models/custom/{user.user_id}/{model_id}",
            headers=forwarded_headers(request, user.user_id),
        )
        return {"success": True, "model_id": model_id}

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
        for item in custom_models.list(user_id):
            custom_models.delete(user_id, item.model_id)
        project_datasets.delete_user(user_id)
        projects.delete_user(user_id)
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
        return _ordered_runs(_data_rows(payload))

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

    async def write_session_state(
        session_id: str,
        session: dict,
        request: Request,
        user_id: str,
        patch: dict[str, Any],
    ) -> dict:
        state = {**_state(session), **patch}
        await runtime.request(
            "PATCH",
            f"/sessions/{session_id}",
            headers=forwarded_headers(request, user_id),
            params={"type": "agent", "user_id": user_id},
            json_body={"session_state": state},
        )
        return state

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
        source_path = Path(dataset.storage_path)
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
        await graph.request(
            "POST",
            f"/sessions/{session_id}/uploads/register",
            headers=headers,
            json_body={
                "file_token": f"project-{dataset.dataset_id}",
                "original_filename": dataset.original_filename,
                "saved_path": dataset.storage_path,
            },
        )
        result = await graph.request(
            "POST",
            f"/sessions/{session_id}/graph/load_upload",
            headers=headers,
            json_body={"filename": dataset.original_filename, "append": False},
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
                    completed = job.get("result") if isinstance(job.get("result"), dict) else result
                    result = {**load_context, **completed}
                    break
                if status == 'cancelled':
                    # User cancellation leaves the validated project source
                    # available for the next Run and other conversations.
                    raise HTTPException(409, {'code': 'RUN_CANCELLED'})
                if status == "failed":
                    project_datasets.update(
                        dataset.dataset_id,
                        user_id,
                        project_id=project_id,
                        status="failed",
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
            updated = project_datasets.update(
                dataset.dataset_id,
                user_id,
                project_id=project_id,
                metadata={
                    **(dataset.metadata if isinstance(dataset.metadata, dict) else {}),
                    **summary,
                    "dataset_kind": "graph_collection",
                    "format_detected": _GRAPH_COLLECTION_FORMAT,
                    "dataset_profile": result.get("dataset_profile") or dataset_summary.get("name"),
                },
            )
            if updated is not None:
                result["dataset_view"] = _dataset_view(updated)
        else:
            try:
                info = await graph.request(
                    "GET",
                    f"/sessions/{session_id}/graph/info",
                    headers=headers,
                )
                metadata = {
                    **(dataset.metadata if isinstance(dataset.metadata, dict) else {}),
                    "dataset_kind": "single_graph",
                    "format_detected": result.get("format_detected") or "single_graph",
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
                    metadata={key: value for key, value in metadata.items() if value is not None},
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
                if previous is not None and previous.status == "ready":
                    await load_project_dataset_into_session(
                        session_id,
                        session,
                        previous,
                        request,
                        user_id,
                    )
                await write_session_state(
                    session_id,
                    session,
                    request,
                    user_id,
                    {"active_dataset_id": previous.dataset_id if previous else None},
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
                await write_session_state(
                    session_id,
                    session,
                    request,
                    user_id,
                    {"active_dataset_id": dataset.dataset_id if dataset else None},
                )
            except asyncio.CancelledError:
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
            return load_result, _dataset_view(refreshed)

    async def ensure_active_dataset_loaded(
        session_id: str,
        session: dict,
        request: Request,
        user_id: str,
    ) -> bool:
        if await graph_loaded(session_id, request, user_id):
            return True
        state = _state(session)
        dataset_id = str(state.get("active_dataset_id") or "").strip()
        if not dataset_id:
            return False
        project_id = session_project_id(session, user_id)
        dataset = project_dataset_or_404(dataset_id, project_id, user_id)
        await load_project_dataset_into_session(
            session_id,
            session,
            dataset,
            request,
            user_id,
        )
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

    @app.post("/api/projects/{project_id}/datasets")
    async def create_project_dataset(
        request: Request,
        project_id: str | None = None,
        file: UploadFile = File(...),
        name: str = Form(""),
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
        filename = file.filename or "dataset.bin"
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="数据集文件不能为空")
        if project_id:
            project_or_404(project_id, user.user_id)
        dataset = project_datasets.create(
            project_id=project_id,
            user_id=user.user_id,
            name=name or Path(filename).stem,
            original_filename=filename,
            content=content,
        )
        previous_default_dataset_id = default_dataset_id
        if project and (project.default_dataset_id is None or set_as_default):
            project = projects.set_default_dataset(project_id, user.user_id, dataset.dataset_id) or project
        activated_session_id = None
        dataset_view = None
        if target_session is not None:
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
    async def create_personal_dataset(request: Request, file: UploadFile = File(...),
                                      name: str = Form(""), session_id: str = Form("")):
        return await create_project_dataset(request, file=file, name=name, session_id=session_id,
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
        available_models = available_models_for(user)
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
    async def list_sessions(request: Request):
        user = current_user(request, users)
        rows = await user_sessions(request, user.user_id, limit=200)
        available_models = available_models_for(user)
        default_model = available_models[0] if available_models else "qwen3.6-plus"
        return {"success": True, "sessions": [_session_row(row, default_model) for row in rows]}

    @app.post("/api/sessions")
    async def create_session(req: CreateSessionReq, request: Request):
        user = current_user(request, users)
        available_models = available_models_for(user)
        model = req.model or (available_models[0] if available_models else "qwen3.6-plus")
        if model not in available_models:
            raise HTTPException(status_code=403, detail=f"用户无权使用模型 {model}")
        project = project_or_404(req.project_id, user.user_id) if req.project_id else None
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
            "dataset_view": _dataset_view(response_dataset),
            "provider": "openai-compatible",
            **state,
            "model": selected_model,
            "title": str(session.get("session_name") or title),
            "context_usage": {"used_tokens": 0, "limit_tokens": 0, "ratio": 0},
            "available_models": available_models,
            "default_model": available_models[0] if available_models else selected_model,
        }

    @app.post("/api/sessions/{session_id}/activate")
    async def activate_session(session_id: str, request: Request):
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        state = _state(session)
        has_graph = await ensure_active_dataset_loaded(session_id, session, request, user.user_id)
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
            "dataset_view": _dataset_view(active_dataset),
            "pending_feedback": pending,
            "active_run": active_run,
            "project_id": session_project_id(session, user.user_id),
            "active_dataset_id": state.get("active_dataset_id"),
            "provider": state.get("provider", "openai-compatible"),
            "model": state.get("model", available_models_for(user)[0] if available_models_for(user) else "qwen3.6-plus"),
            "reasoning_mode": state.get("reasoning_mode", "direct"),
            "planning_mode": state.get("planning_mode", "auto"),
            "context_usage": session.get("metrics") or {},
            "available_models": available_models_for(user),
            "default_model": available_models_for(user)[0] if available_models_for(user) else "qwen3.6-plus",
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
        state = _state(session)
        has_graph = await ensure_active_dataset_loaded(session_id, session, request, user.user_id)
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
            "dataset_view": _dataset_view(active_dataset),
            "pending_feedback": pending,
            "active_run": active_run,
            "project_id": session_project_id(session, user.user_id),
            "active_dataset_id": state.get("active_dataset_id"),
            "provider": state.get("provider", "openai-compatible"),
            "model": state.get("model", available_models_for(user)[0] if available_models_for(user) else "qwen3.6-plus"),
            "reasoning_mode": state.get("reasoning_mode", "direct"),
            "planning_mode": state.get("planning_mode", "auto"),
            "context_usage": session.get("metrics") or {},
            "available_models": available_models_for(user),
            "default_model": available_models_for(user)[0] if available_models_for(user) else "qwen3.6-plus",
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
        if state.get("model") and state["model"] not in available_models_for(user):
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
        result = await patch_setting(session_id, request, {"model": req.model})
        result["provider"] = "openai-compatible"
        return result

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

    async def run_form(session_id: str, req: ChatReq, request: Request) -> tuple[dict, dict[str, str], str]:
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        await ensure_active_dataset_loaded(session_id, session, request, user.user_id)
        state = _state(session)
        options = {
            "model": state.get("model", available_models_for(user)[0] if available_models_for(user) else "qwen3.6-plus"),
            "reasoning_mode": req.reasoning_mode or state.get("reasoning_mode", "direct"),
            "planning_mode": req.planning_mode or state.get("planning_mode", "auto"),
            "output_format": req.output_format,
        }
        project_id = session_project_id(session, user.user_id)
        training_payload = await project_training_runs(project_id, request, current_session_id=session_id)
        training_summary = _project_training_summary(training_payload.get("training_runs") or [])
        await sync_custom_model(user, str(options["model"]), request)
        next_state = {
            **state,
            **options,
            "last_message_preview": req.message.strip()[:120],
            "project_training_summary": training_summary,
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
            "factory_input": json.dumps(options, ensure_ascii=False),
        }
        return form, forwarded_headers(request, user.user_id), user.user_id

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
        user = current_user(request, users)
        session = await get_session(session_id, request, user.user_id)
        start_id = str(req.start_request_id or uuid.uuid4())
        state = _state(session)
        content = {"request": req.model_dump(mode='json', exclude={'start_request_id'}),
                   "model": state.get('model'), "reasoning_mode": req.reasoning_mode or state.get('reasoning_mode'),
                   "planning_mode": req.planning_mode or state.get('planning_mode'), "active_dataset_id": state.get('active_dataset_id')}
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        registration = await runtime.request('POST', f'/internal/sessions/{session_id}/runs/register',
                                             headers=forwarded_headers(request, user.user_id),
                                             json_body={"start_request_id": start_id, "fingerprint": digest, 'preparation_owner': preparation_instance})
        if not registration.get('preparation_claimed'):
            # Stable identity is enough for the client's normal resume/snapshot
            # path. Never resend a user question on an idempotent duplicate.
            return None, registration
        rid, generation = registration['run_id'], registration['execution_generation']
        key = (user.user_id, session_id, rid)
        token = execution_headers.set({**service_headers(user.user_id), 'X-Run-ID': rid,
                                       'X-Execution-Generation': str(generation), 'X-Operation-ID': str(uuid.uuid4())})
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
        _, _, uid = prepared
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
                form, headers, _ = prepared
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
            form, headers, user_id = prepared
            # Preserve the existing non-stream response contract while the
            # producer remains independently owned by Agno.
            while str(payload.get('status') or '').lower() not in {'completed', 'cancelled', 'failed', 'error', 'paused'}:
                await asyncio.sleep(.25)
                runs = await runtime.request('GET', f'/sessions/{session_id}/runs', headers=headers,
                                             params={'type': 'agent', 'user_id': user_id})
                payload = next((run for run in _data_rows(runs) if run.get('run_id') == registration['run_id']), payload)
            return {"success": True, "run_id": registration['run_id'], "start_request_id": registration['start_request_id'],
                    "response": str(payload.get('content') or ''), 'context_usage': payload.get('metrics') or {},
                    'has_graph': await graph_loaded(session_id, request, user_id),
                    'reasoning_mode': json.loads(form['factory_input'])['reasoning_mode']}
        _, reservation = await reserve_new_run(session_id, request)
        try:
            form, headers, user_id = await run_form(session_id, req, request)
            form.update({"stream": "false", "background": "false"})
            payload = await runtime.request("POST", f"/agents/{settings.agent_id}/runs", headers=headers, data=form)
            return {
                "success": True,
                "response": str(payload.get("content") or ""),
                "has_graph": await graph_loaded(session_id, request, user_id),
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
                form, headers, _ = prepared
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
                form, headers, _ = await run_form(session_id, req, request)
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

    @app.get("/api/memory")
    async def list_memory(request: Request, scope: str = "long_term", query: str = "", limit: int = 100):
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
        return {"success": True, "scope": scope, "items": items}

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
        payload = await runtime.request(
            "PATCH", f"/memories/{memory_id}", headers=forwarded_headers(request, user.user_id),
            json_body={"memory": req.value, "user_id": user.user_id, "topics": [req.scope, "fact", req.key]},
        )
        return {"success": True, "item": payload}

    @app.delete("/api/memory/{memory_id}")
    async def delete_memory(memory_id: str, request: Request, scope: str = "long_term", reason: str = ""):
        user = current_user(request, users)
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
        included_ids = [str(item.get("memory_id")) for item in rows if item.get("memory_id")]
        return {
            "success": True,
            "count": meta.get("total_count", len(rows)),
            "snapshot_char_count": sum(len(str(item.get("memory") or "")) for item in rows),
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
        available_models = available_models_for(user)
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
            "POST", f"/sessions/{session_id}/tools/{tool_name}", headers=forwarded_headers(request, user.user_id),
            json_body={"arguments": arguments},
        )

    @app.get("/api/datasets")
    async def datasets(request: Request):
        user = current_user(request, users)
        return await graph.request("GET", "/datasets", headers=forwarded_headers(request, user.user_id))

    @app.delete("/api/datasets/{dataset_id}")
    async def delete_dataset(dataset_id: str, request: Request):
        user = current_user(request, users)
        return await graph.request("DELETE", f"/datasets/{dataset_id}", headers=forwarded_headers(request, user.user_id))

    @app.post("/api/sessions/{session_id}/load-dataset")
    async def load_dataset(session_id: str, request: Request):
        return await graph_tool(session_id, "load_dataset", await request.json(), request)

    @app.post("/api/sessions/{session_id}/save-dataset")
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
        view = _dataset_view(active_dataset)
        if view and view.get("kind") == "graph_collection":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "GRAPH_NOT_VISUALIZABLE",
                    "message": "非独立图，无法直接可视化",
                    "dataset_view": view,
                },
            )
        if not await ensure_active_dataset_loaded(session_id, session, request, user.user_id):
            active_dataset = (
                project_datasets.get(active_dataset_id, user.user_id, project_id)
                if active_dataset_id
                else None
            )
            view = _dataset_view(active_dataset)
            if view and view.get("kind") == "graph_collection":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "GRAPH_NOT_VISUALIZABLE",
                        "message": "非独立图，无法直接可视化",
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

    @app.post("/api/sessions/{session_id}/upload")
    async def upload(session_id: str, request: Request, file: UploadFile = File(...)):
        user = current_user(request, users)
        await get_session(session_id, request, user.user_id)
        file_token = uuid.uuid4().hex
        content = await file.read()
        filename = file.filename or f"upload-{file_token}.bin"
        payload = await graph.request(
            "POST", f"/sessions/{session_id}/uploads/ingest", headers=forwarded_headers(request, user.user_id),
            data={"file_token": file_token, "original_filename": filename},
            files={"file": (filename, content, file.content_type or "application/octet-stream")},
        )
        return {"success": True, "filename": filename, "file_token": file_token, "graph_engine": payload}

    return app


app = create_app()

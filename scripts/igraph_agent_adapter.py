#!/usr/bin/env python3
"""Bridge one benchmark task to the project's own headless iGraph Agent.

This adapter intentionally imports :mod:`agent_runtime` from a separately
configured source checkout.  The standalone benchmark bundle remains free of
the product frontend, authentication, and session services, while the actual
planner/tool-selection loop is still ``agent_runtime.agent.igraph.IGraph`` --
never ``CodexAgent``.

Before the first Agent turn, the adapter replaces the product MCP client with
the benchmark's attempt-local canonical skill backend.  The Agent therefore
selects and executes the real ``graph_analysis_skills`` actions while the
runner remains authoritative for the virtual graph-ML broker and trajectory.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping, Sequence


_RELEASE_ROOT = Path(__file__).resolve().parents[1]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))

from benchmark_platform.igraph_skill_backend import install_benchmark_skill_client
from benchmark_platform.tool_names import CANONICAL_TOOL_NAMES

try:
    from .zhizengzeng_config import load_codex_zhizengzeng_config
except ImportError:  # Direct ``python scripts/...py`` execution.
    from scripts.zhizengzeng_config import load_codex_zhizengzeng_config


_FAILURE_STATUSES = {"error", "failed", "failure", "timeout", "cancelled", "canceled"}
_REDACTED = "[REDACTED]"


def _strict_envelope(stream: Any = None) -> dict[str, Any]:
    source = sys.stdin if stream is None else stream
    payload = json.loads(source.read())
    if not isinstance(payload, dict):
        raise ValueError("benchmark task envelope must be a JSON object")
    if payload.get("schema") != "igraph.benchmark.agent_task.v1":
        raise ValueError("unsupported benchmark task envelope schema")
    if not isinstance(payload.get("question"), str) or not payload["question"].strip():
        raise ValueError("benchmark task envelope has no question")
    context = payload.get("context", {})
    if context is not None and not isinstance(context, dict):
        raise ValueError("benchmark task context must be a JSON object")
    return payload


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("positive number expected") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("positive number expected")
    return parsed


def _redact(value: Any, secrets: Iterable[str]) -> Any:
    exact = tuple(sorted({item for item in secrets if item}, key=len, reverse=True))

    def visit(current: Any) -> Any:
        if isinstance(current, str):
            for secret in exact:
                current = current.replace(secret, _REDACTED)
            return current
        if isinstance(current, dict):
            return {str(visit(key)): visit(nested) for key, nested in current.items()}
        if isinstance(current, (list, tuple)):
            return [visit(nested) for nested in current]
        return current

    return visit(value)


def _emit(payload: Mapping[str, Any], *, secrets: Iterable[str] = ()) -> None:
    sanitized = _redact(dict(payload), secrets)
    sys.stdout.write(
        json.dumps(
            sanitized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


@contextmanager
def _temporary_environment(updates: Mapping[str, str]):
    previous = {name: os.environ.get(name) for name in updates}
    try:
        for name, value in updates.items():
            os.environ[str(name)] = str(value)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _session_id(envelope: Mapping[str, Any]) -> str:
    identity = f"{envelope.get('question_id')}#{envelope.get('attempt')}#{os.getpid()}"
    return "benchmark-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _prompt(envelope: Mapping[str, Any]) -> str:
    public_context = envelope.get("context")
    metadata = {
        "category": envelope.get("category"),
        "dataset": envelope.get("dataset"),
        "task_type": envelope.get("task_type"),
        "context": public_context if isinstance(public_context, dict) else {},
    }
    return (
        "当前 benchmark attempt 已绑定所选图数据；canonical iGraph 工具会默认使用该数据。"
        "图规模与属性尚未预读；任何 0/unknown 元数据只是占位，不代表空图，必须以工具结果为准。"
        "请使用这些工具解决下面的问题。"
        "工具结果是结论的唯一计算依据；不要读取题库、ground truth、答案文件或父目录。"
        "图机器学习任务必须先调用匹配的 train 工具，再调用 infer 工具。"
        "最后直接给出原问题的清晰答案。\n\n"
        f"任务元数据：{json.dumps(metadata, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"原始问题：\n{envelope['question']}"
    )


def _result_success(value: Any) -> bool:
    parsed = value
    if isinstance(value, str):
        stripped = value.strip()
        try:
            parsed = json.loads(stripped)
        except (TypeError, json.JSONDecodeError):
            lowered = stripped.lower()
            return not (
                lowered.startswith(("error", "failed", "错误", "工具执行错误"))
                or "执行错误" in lowered
            )
    if isinstance(parsed, Mapping):
        if parsed.get("success") is False or parsed.get("ok") is False:
            return False
        if parsed.get("isError") is True or parsed.get("is_error") is True:
            return False
        if str(parsed.get("status") or "").strip().lower() in _FAILURE_STATUSES:
            return False
        if parsed.get("error"):
            return False
    return True


def _trace_results(runtime: Any) -> list[tuple[str, Any]]:
    agent = getattr(runtime, "_agent", runtime)
    logger = getattr(agent, "logger", None)
    store = getattr(logger, "_store", None)
    loader = getattr(store, "load_trace_records", None)
    if not callable(loader):
        return []
    rows: list[tuple[str, Any]] = []
    for record in loader(runtime.session_id):
        if not isinstance(record, dict) or record.get("event") != "TOOL_RESULT":
            continue
        name = str(record.get("tool") or "").strip()
        if name:
            rows.append((name, record.get("result")))
    return rows


def _merge_tool_results(
    calls: list[dict[str, Any]],
    results: Sequence[tuple[str, Any]],
) -> list[dict[str, Any]]:
    unmatched = set(range(len(calls)))
    for name, result in results:
        match = next(
            (index for index in sorted(unmatched) if calls[index].get("name") == name),
            None,
        )
        if match is None:
            continue
        calls[match]["result"] = result
        calls[match]["success"] = _result_success(result)
        unmatched.remove(match)
    for index in unmatched:
        # A request event without a persisted result is an incomplete call,
        # never an invented success.
        calls[index]["result"] = None
        calls[index]["success"] = False
    return calls


async def _collect_events(
    runtime: Any,
    prompt: str,
) -> tuple[list[dict[str, Any]], str, Any, str | None, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    answer_chunks: list[str] = []
    reasoning: list[dict[str, Any]] = []
    terminal_error: str | None = None
    saw_done = False

    async for raw_event in runtime.chat_stream(prompt):
        if not isinstance(raw_event, Mapping):
            continue
        event = dict(raw_event)
        events.append(event)
        event_type = str(event.get("type") or "")
        if event_type == "token" and isinstance(event.get("content"), str):
            answer_chunks.append(event["content"])
        elif event_type in {"thinking", "solve_thinking"}:
            content = event.get("content")
            if isinstance(content, str) and content:
                reasoning.append(
                    {
                        "type": event_type,
                        "phase": event.get("phase"),
                        "step_id": event.get("step_id"),
                        "content": content,
                    }
                )
        elif event_type in {"tool_call", "solve_tool_call"}:
            name = str(event.get("tool") or event.get("name") or "").strip()
            # Product-internal actions such as tool_select remain in the raw
            # transcript, but only benchmark-canonical actions enter the
            # scored tool_calls channel.
            if name in CANONICAL_TOOL_NAMES:
                calls.append(
                    {
                        "call_id": event.get("call_id") or event.get("id"),
                        "name": name,
                        "arguments": event.get("args", event.get("arguments", {})),
                    }
                )
        elif event_type == "await_user_feedback":
            terminal_error = "iGraph Agent requested interactive feedback in a headless benchmark"
        elif event_type == "error":
            terminal_error = str(event.get("error") or "iGraph Agent failed")
        elif event_type == "done":
            saw_done = True

    if not saw_done and terminal_error is None:
        terminal_error = "iGraph Agent stream ended without a done event"
    final_answer = "".join(answer_chunks).strip()
    if not final_answer and terminal_error is None:
        terminal_error = "iGraph Agent produced no final answer"
    return events, final_answer, {"blocks": reasoning}, terminal_error, calls


def _load_runtime(
    runtime_root: Path,
    *,
    session_id: str,
    user_id: str,
    model: str,
    args: argparse.Namespace,
):
    required = runtime_root / "agent_runtime" / "agent" / "igraph.py"
    if not required.is_file():
        raise FileNotFoundError("iGraph runtime root does not contain agent_runtime/agent/igraph.py")
    root_text = str(runtime_root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from agent_runtime import adapt_legacy_agent  # type: ignore[import-not-found]
    from agent_runtime.agent.igraph import IGraph  # type: ignore[import-not-found]
    from backend.logging.logger import Logger  # type: ignore[import-not-found]

    logger = Logger()
    session_logger = logger.create_session_logger(
        session_id,
        provider="transparent_openai_sdk",
        model=model,
        reasoning_mode=args.reasoning_mode,
        user_id=user_id,
    )
    agent = IGraph(
        session_logger=session_logger,
        model=model,
        reasoning_mode=args.reasoning_mode,
        planning_mode=args.planning_mode,
        # IGraph requires a non-empty MCP setting during construction.  The
        # inert value is never opened: install_benchmark_skill_client replaces
        # the client before the first chat_stream call.
        mcp_url="benchmark://canonical-skills",
        restored_has_graph=True,
        memory_manager=None,
        user_id=user_id,
    )
    return adapt_legacy_agent(agent)


def _run(args: argparse.Namespace, envelope: Mapping[str, Any]) -> dict[str, Any]:
    runtime_root = args.runtime_root.expanduser().resolve()
    skills_dir = args.skills_dir.expanduser().resolve()
    if not skills_dir.is_dir():
        raise FileNotFoundError("graph analysis skill directory is unavailable")
    dataset_path = Path(str(envelope.get("dataset_path") or "")).expanduser().resolve()
    if not dataset_path.is_file():
        raise FileNotFoundError("benchmark dataset path is unavailable")
    provider = load_codex_zhizengzeng_config(args.provider_profile)
    session_id = _session_id(envelope)
    user_id = args.user_id
    state_root = (Path.cwd() / "igraph-agent-state").resolve()
    state_root.mkdir(parents=True, exist_ok=False)
    inherited_python_path = os.environ.get("PYTHONPATH", "")
    runtime_python_path = os.pathsep.join(
        part
        for part in (str(runtime_root), inherited_python_path)
        if part
    )
    environment = {
        "LLM_API_KEY": provider.api_key,
        "LLM_BASE_URL": provider.base_url,
        "OPENAI_API_KEY": provider.api_key,
        "OPENAI_BASE_URL": provider.base_url,
        "LLM_WIRE_API": "responses" if args.model == "gpt-5.6-sol" else "chat_completions",
        "LLM_TOOL_PROTOCOL": "text",
        "ENABLE_MEMORY_UPDATE": "0",
        "IGRAPH_DATA_ROOT": str(state_root),
        "PYTHONPATH": runtime_python_path,
    }
    runtime = None
    skill_client = None
    try:
        # Some legacy runtime constructors print endpoint diagnostics (one old
        # branch even prints an API key).  They are deliberately discarded so
        # stdout remains the strict benchmark JSON channel and secrets cannot
        # enter either transcript artifacts or runner logs.
        with (
            _temporary_environment(environment),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            runtime = _load_runtime(
                runtime_root,
                session_id=session_id,
                user_id=user_id,
                model=args.model,
                args=args,
            )
            skill_client = install_benchmark_skill_client(
                runtime,
                skills_dir=skills_dir,
                dataset_path=dataset_path,
                environment=os.environ,
                work_dir=Path.cwd(),
                timeout_seconds=args.tool_timeout_seconds,
            )
            events, final_answer, reasoning, terminal_error, calls = asyncio.run(
                _collect_events(runtime, _prompt(envelope))
            )
            calls = _merge_tool_results(calls, _trace_results(runtime))
        common = {
            "final_answer": final_answer or None,
            "reasoning": reasoning,
            "tool_calls": calls,
            "transcript": events,
        }
        if terminal_error is not None:
            return {
                "status": "failed",
                "error": {"type": "IGraphAgentError", "message": terminal_error},
                **common,
            }
        return {"status": "success", **common}
    finally:
        if runtime is not None:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                try:
                    runtime.cleanup()
                except Exception:
                    pass
        if skill_client is not None:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                try:
                    asyncio.run(skill_client.close())
                except Exception:
                    pass


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one benchmark task with the iGraph Agent")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--skills-dir", type=Path, required=True)
    parser.add_argument("--provider-profile", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--user-id", default="benchmark")
    parser.add_argument("--reasoning-mode", choices=("direct", "staged"), default="direct")
    parser.add_argument("--planning-mode", choices=("never", "auto", "always"), default="auto")
    parser.add_argument("--tool-timeout-seconds", type=_positive_float, default=900.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    secrets: set[str] = set()
    try:
        provider = load_codex_zhizengzeng_config(args.provider_profile)
        secrets.update((provider.api_key, provider.base_url))
        envelope = _strict_envelope()
        _emit(_run(args, envelope), secrets=secrets)
    except Exception as exc:
        _emit(
            {
                "status": "failed",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            },
            secrets=secrets,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

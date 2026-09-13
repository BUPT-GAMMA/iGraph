"""Per-Run progress guard for the native AgentOS tool loop."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from hashlib import sha256
from inspect import isawaitable
import json
import logging
from math import isfinite
from typing import Any

from agno.exceptions import RunCancelledException, StopAgentRun
from pydantic import BaseModel

from graph_engine.tool_execution import resources_overlap, tool_execution_policy

_LOG = logging.getLogger("igraph.agent_loop")


TOOL_CALL_LIMIT = 32
MAX_SAME_CALL = 3
MAX_CONSECUTIVE_FAILURES = 5
_RUN_GUARD_ATTRIBUTE = "_igraph_tool_loop_guard"
_JOB_STATUS_TOOL = "get_job_status"
_ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
_MUTATING_TOOLS = frozenset(
    {
        "load_graph",
        "load_graph_from_upload",
        "clear_graph",
        "undo_graph",
        "to_undirected",
        "load_graph_version",
        "discard_graph_version",
        "workspace_bash",
        "workspace_publish",
    }
)


def normalize_tool_arguments(value: object) -> object:
    """Return a deterministic JSON-compatible representation of tool arguments."""

    if isinstance(value, BaseModel):
        return normalize_tool_arguments(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return normalize_tool_arguments(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): normalize_tool_arguments(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [normalize_tool_arguments(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [normalize_tool_arguments(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
        )
    if isinstance(value, Enum):
        return normalize_tool_arguments(value.value)
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, float) and not isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def tool_call_fingerprint(tool_name: str, arguments: object) -> str:
    """Fingerprint a tool call using its name and canonical arguments."""

    payload = [str(tool_name).strip(), normalize_tool_arguments(arguments)]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _job_payload(result: object) -> dict[str, Any] | None:
    if not isinstance(result, dict) or result.get("success", True) is False:
        return None
    job = result.get("job")
    if isinstance(job, dict):
        payload = dict(job)
        for key in ("job_id", "status", "result", "artifact", "artifacts", "detail", "progress", "message"):
            if key in result and key not in payload:
                payload[key] = result[key]
        return payload
    return result


def _job_id(arguments: object, result: object = None) -> str | None:
    if isinstance(arguments, Mapping) and arguments.get("job_id") is not None:
        return str(arguments["job_id"])
    payload = _job_payload(result)
    if payload and payload.get("job_id") is not None:
        return str(payload["job_id"])
    return None


def _job_status(result: object) -> str | None:
    payload = _job_payload(result)
    if payload is None or payload.get("status") is None:
        return None
    return str(payload["status"]).strip().lower()


def _progress_projection(tool_name: str, arguments: object, result: object) -> object:
    """Keep only observable state; changing arguments alone is not progress."""

    if not isinstance(result, dict):
        return normalize_tool_arguments(result)
    normalized_name = str(tool_name).strip()
    if normalized_name == _JOB_STATUS_TOOL:
        payload = _job_payload(result) or {}
        return normalize_tool_arguments(
            {
                "job_id": _job_id(arguments, result),
                "status": payload.get("status"),
                "artifact": payload.get("artifact"),
                "artifacts": payload.get("artifacts"),
                "result": payload.get("result"),
                "detail": payload.get("detail"),
                "progress": payload.get("progress"),
                "progress_updates": result.get("progress_updates"),
                "graph_state": payload.get("graph_state") or result.get("graph_state"),
            }
        )
    if normalized_name == "workspace_bash":
        return normalize_tool_arguments(
            {
                "exit_code": result.get("exit_code"),
                "output": result.get("output"),
                "output_truncated": result.get("output_truncated"),
                "artifact": result.get("artifact"),
                "artifacts": result.get("artifacts"),
                "graph_state": result.get("graph_state"),
            }
        )
    return normalize_tool_arguments(result)


def has_real_progress(
    tool_name: str,
    arguments: object,
    result: object,
    previous_result: object,
) -> bool:
    """Return whether a tool result changes observable work state."""

    if isinstance(result, dict) and result.get("success", True) is False:
        return False
    if previous_result is None:
        return True
    return _progress_projection(tool_name, arguments, result) != _progress_projection(
        tool_name, arguments, previous_result
    )


@dataclass
class ToolLoopGuard:
    """Stop repeated calls and failure streaks without owning the model loop."""

    max_calls: int = TOOL_CALL_LIMIT
    max_same_call: int = MAX_SAME_CALL
    max_failures: int = MAX_CONSECUTIVE_FAILURES
    calls: int = 0
    failures: int = 0
    _fingerprint_counts: dict[str, int] = field(default_factory=dict)
    _progress_results: dict[str, object] = field(default_factory=dict)
    _fingerprint_results: dict[str, object] = field(default_factory=dict)
    _read_calls: dict[str, tuple[str, frozenset[str]]] = field(default_factory=dict)
    _stagnant_counts: dict[str, int] = field(default_factory=dict)
    _job_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    _last_block_reason: str | None = None
    stopped: bool = False
    _mutation_lock: asyncio.Lock | None = field(default=None, repr=False)

    def _block(self, reason: str, message: str) -> str:
        self._last_block_reason = reason
        self.stopped = True
        return message

    def before_call(self, tool_name: str, arguments: object) -> str | None:
        if self.stopped:
            reason = self._last_block_reason or "guard_stopped"
            messages = {
                "consecutive_failures": "工具连续失败次数过多，已停止本轮；请调整输入或向用户说明失败原因。",
                "duplicate_call": "检测到重复工具调用，已停止本轮以避免死循环。",
                "no_real_progress": "工具返回内容没有带来新的可观察进展，已停止本轮以避免无效循环。",
                "total_call_limit": "本轮工具调用已达到上限，请根据已有结果结束本轮或向用户说明阻塞原因。",
                "guard_stopped": "本轮工具调用已停止，不再执行后续工具。",
            }
            return self._block(reason, messages[reason])
        self._last_block_reason = None
        if self.calls >= self.max_calls:
            _LOG.warning("agent.loop.limit_reached calls=%s tool=%s", self.calls, tool_name)
            return self._block(
                "total_call_limit", "本轮工具调用已达到上限，请根据已有结果结束本轮或向用户说明阻塞原因。"
            )
        if self.failures >= self.max_failures:
            return self._block(
                "consecutive_failures", "工具连续失败次数过多，已停止本轮；请调整输入或向用户说明失败原因。"
            )

        normalized_name = str(tool_name).strip()
        fingerprint = tool_call_fingerprint(normalized_name, arguments)
        job_state = self._job_states.get(_job_id(arguments)) if normalized_name == _JOB_STATUS_TOOL else None
        active_poll = bool(job_state and job_state.get("status") in _ACTIVE_JOB_STATUSES)
        if normalized_name == _JOB_STATUS_TOOL and job_state is None:
            # Before the first result arrives the guard cannot know whether the
            # job is still active, so let AgentOS perform the initial poll.
            pass
        elif normalized_name == _JOB_STATUS_TOOL and not active_poll:
            count = self._fingerprint_counts.get(fingerprint, 0) + 1
            self._fingerprint_counts[fingerprint] = count
            if count > self.max_same_call:
                return self._block(
                    "duplicate_call", f"检测到工具 {normalized_name} 的相同参数重复调用，已停止本轮以避免死循环。"
                )
        elif normalized_name != _JOB_STATUS_TOOL:
            count = self._fingerprint_counts.get(fingerprint, 0) + 1
            self._fingerprint_counts[fingerprint] = count
            if count > self.max_same_call:
                return self._block(
                    "duplicate_call", f"检测到工具 {normalized_name} 的相同参数重复调用，已停止本轮以避免死循环。"
                )

        if normalized_name != _JOB_STATUS_TOOL and self._stagnant_counts.get(normalized_name, 0) >= self.max_same_call:
            return self._block("no_real_progress", "工具返回内容没有带来新的可观察进展，已停止本轮以避免无效循环。")

        policy = tool_execution_policy(normalized_name, arguments)
        if policy.read_only and normalized_name != _JOB_STATUS_TOOL:
            self._read_calls[fingerprint] = (normalized_name, policy.reads)
        self.calls += 1
        _LOG.info("agent.tool.started tool=%s call_count=%s fingerprint=%s", normalized_name, self.calls, fingerprint)
        return None

    def after_result(
        self,
        success: bool,
        *,
        tool_name: str | None = None,
        arguments: object = None,
        result: object = None,
    ) -> str | None:
        # A result from an already running parallel tool cannot undo a stop.
        if self.stopped:
            return None
        self._last_block_reason = None
        if success:
            self.failures = 0
        else:
            self.failures += 1
            if self.failures >= self.max_failures:
                self.stopped = True
                self._last_block_reason = "consecutive_failures"
                return "工具连续失败次数过多，已停止本轮；请调整输入或向用户说明失败原因。"

        if tool_name is not None:
            normalized_name = str(tool_name).strip()
            progress_key = f"{normalized_name}:{_job_id(arguments)}" if normalized_name == _JOB_STATUS_TOOL else normalized_name
            previous = self._progress_results.get(progress_key)
            progressed = success and has_real_progress(normalized_name, arguments, result, previous)
            if progressed:
                self._stagnant_counts[normalized_name] = 0
            elif success and normalized_name != _JOB_STATUS_TOOL:
                self._stagnant_counts[normalized_name] = self._stagnant_counts.get(normalized_name, 0) + 1
            if success:
                self._progress_results[progress_key] = result
                fingerprint = tool_call_fingerprint(normalized_name, arguments)
                # Compare the same call, not a different query's result. Count
                # the fresh observation as one, preserving the duplicate limit.
                if normalized_name != _JOB_STATUS_TOOL:
                    if fingerprint in self._fingerprint_results and has_real_progress(
                        normalized_name, arguments, result, self._fingerprint_results[fingerprint]
                    ):
                        self._fingerprint_counts[fingerprint] = 1
                    self._fingerprint_results[fingerprint] = result

                # Invalidate only observations of resources actually written by
                # a successful, progressing tool. Reads and unrelated writes
                # must not reset each other's loop counters. A queued job is
                # not evidence that its resource mutation has completed.
                writes = tool_execution_policy(normalized_name, arguments).writes
                if progressed and writes and _job_status(result) not in _ACTIVE_JOB_STATUSES:
                    for read_fingerprint, (reader, reads) in self._read_calls.items():
                        if any(resources_overlap(write, read) for write in writes for read in reads):
                            self._fingerprint_counts.pop(read_fingerprint, None)
                            self._fingerprint_results.pop(read_fingerprint, None)
                            self._stagnant_counts.pop(reader, None)
                            self._progress_results.pop(reader, None)
            _LOG.info("agent.tool.completed tool=%s success=%s progressed=%s calls=%s failures=%s", normalized_name, success, progressed, self.calls, self.failures)

            if normalized_name == _JOB_STATUS_TOOL:
                job_id = _job_id(arguments, result)
                if job_id:
                    status = _job_status(result)
                    self._job_states[job_id] = {"status": status, "progressed": progressed}
                    if status not in _ACTIVE_JOB_STATUSES:
                        fingerprint = tool_call_fingerprint(normalized_name, arguments)
                        self._fingerprint_counts.setdefault(fingerprint, 1)

        return None


def _guard_for_run(run_context: object) -> ToolLoopGuard:
    from agent_runtime.execution_scope import current_scope
    owner = current_scope.get() or run_context
    guard = getattr(owner, _RUN_GUARD_ATTRIBUTE, None)
    if not isinstance(guard, ToolLoopGuard):
        guard = ToolLoopGuard()
        setattr(owner, _RUN_GUARD_ATTRIBUTE, guard)
    return guard


def _guard_failure(
    guard: ToolLoopGuard,
    *,
    tool_name: str,
    arguments: object,
    message: str,
    cause: object | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": "IGRAPH_TOOL_LOOP_GUARD",
        "reason": guard._last_block_reason or "guard_limit",
        "message": message,
        "tool_name": str(tool_name),
        "fingerprint": tool_call_fingerprint(tool_name, arguments),
        "call_count": guard.calls,
        "failure_count": guard.failures,
    }
    if cause is not None:
        error["cause"] = cause
    return {"success": False, "error": error, "terminal": True}


def _stop_agent_run(failure: dict[str, Any]) -> None:
    """Use AgentOS's native stop-after-tool-call path for guard failures.

    AgentOS treats a returned ``terminal`` field as ordinary tool content. The
    public ``StopAgentRun`` exception is the control signal that marks the
    tool result as ``stop_after_tool_call`` and exits the native model loop.
    """

    raise StopAgentRun(
        json.dumps(failure, ensure_ascii=False, sort_keys=True),
        agent_message=json.dumps(failure, ensure_ascii=False, sort_keys=True),
    )


async def progress_guard_hook(*, run_context, name, args, func):
    """Wrap one AgentOS tool call with guard state owned by its RunContext."""

    guard = _guard_for_run(run_context)
    blocked = guard.before_call(name, args)
    if blocked:
        _stop_agent_run(_guard_failure(
            guard,
            tool_name=name,
            arguments=args,
            message=blocked,
        ))

    async def execute_tool():
        result = func(**args)
        if isawaitable(result):
            return await result
        return result

    try:
        if str(name).strip() in _MUTATING_TOOLS:
            if guard._mutation_lock is None:
                guard._mutation_lock = asyncio.Lock()
            async with guard._mutation_lock:
                result = await execute_tool()
        else:
            result = await execute_tool()
    except (asyncio.CancelledError, RunCancelledException):
        raise
    except Exception as exc:
        failure = guard.after_result(
            False,
            tool_name=name,
            arguments=args,
            result={"success": False, "error": str(exc)},
        )
        if failure:
            _stop_agent_run(_guard_failure(
                guard,
                tool_name=name,
                arguments=args,
                message=failure,
                cause=str(exc),
            ))
        return {
            "success": False,
            "error": {
                "code": "TOOL_EXECUTION_ERROR",
                "message": str(exc),
                "tool_name": str(name),
            },
            "terminal": False,
        }

    from agent_runtime.construction_state import _result_payload
    payload = _result_payload(result)
    success = not (isinstance(payload, dict) and (payload.get("success") is False or payload.get("status") in {"failed", "cancelled", "canceled"}))
    success = success and not bool(getattr(result, "isError", False))
    failure = guard.after_result(success, tool_name=name, arguments=args, result=payload)
    if failure:
        cause = result.get("error") if isinstance(result, dict) else None
        _stop_agent_run(_guard_failure(
            guard,
            tool_name=name,
            arguments=args,
            message=failure,
            cause=cause,
        ))
    return result


async def agentos_tool_hook(*, run_context, name, args, func):
    """Guard structured results before serializing them for AgentOS storage/streaming.

    AgentOS stringifies tool output; a bare dict would become Python repr, which
    cannot be decoded by the Backend's JSON event and history projections.
    """
    result = await progress_guard_hook(run_context=run_context, name=name, args=args, func=func)
    return json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else result


async def loop_guard_tool_hook(function_name, function_call, arguments, run_context):
    """Use the public Agno hook signature while retaining structured MCP results."""
    return await agentos_tool_hook(run_context=run_context, name=function_name, args=arguments, func=function_call)


async def loop_guard_post_hook(run_context, run_output):
    """Persist guard termination as a native error, including the durable controller path."""
    from agno.exceptions import OutputCheckError
    guard = _guard_for_run(run_context)
    if guard.stopped:
        raise OutputCheckError(
            "工具调用已停止：" + str(guard._last_block_reason or "guard_limit"),
            additional_data={"code": "IGRAPH_TOOL_LOOP_GUARD", "reason": guard._last_block_reason},
        )

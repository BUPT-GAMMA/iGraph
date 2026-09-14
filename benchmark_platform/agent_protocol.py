"""Agent command/replay protocol and transcript normalization for benchmarks."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


_ERROR_MARKERS = (
    "error:",
    "exception:",
    "traceback (most recent call last)",
    "command not found",
    "no such file or directory",
    '"ok": false',
    "unknown action",
)

_SUCCESS_STATUSES = {
    "success",
    "succeeded",
    "ok",
    "complete",
    "completed",
    "pass",
    "passed",
    "done",
}
_FAILURE_STATUSES = {
    "error",
    "failed",
    "failure",
    "cancelled",
    "canceled",
    "timeout",
    "timed_out",
    "rejected",
}


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


@dataclass
class AgentExecution:
    status: str
    final_answer: Any = None
    reasoning: Any = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[Any] = field(default_factory=list)
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    stdout_log_path: str | None = None
    stderr_log_path: str | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error: dict[str, str] | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    try:
        return json.loads(stripped, parse_constant=_reject_nonfinite_json)
    except (TypeError, ValueError):
        return value


def _content_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping):
            text = item.get("text") or item.get("output_text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts) or None


def _exit_code_success(value: Any) -> bool | None:
    """Return an explicit exit-code signal without coercing arbitrary values."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value == 0
    if isinstance(value, str):
        normalized = value.strip()
        if re.fullmatch(r"(?:0|-[1-9][0-9]*|[1-9][0-9]*)", normalized):
            return int(normalized) == 0
    return None


def _structured_result_success(value: Any, *, _depth: int = 0) -> bool | None:
    """Extract an authoritative success/failure signal from a tool result.

    Failure signals take precedence over success signals when a malformed result
    contradicts itself.  Nested ``output``/``result`` values are considered only
    when their enclosing object has no explicit signal, so an outer exit code of
    zero remains authoritative even if captured stdout contains error-like text.
    """

    parsed = _json_value(value)
    if not isinstance(parsed, Mapping):
        return None

    metadata = parsed.get("metadata")
    records = [parsed]
    if isinstance(metadata, Mapping):
        records.append(metadata)

    saw_success = False
    for record in records:
        status = str(record.get("status", "")).strip().lower().replace("-", "_")
        if status in _FAILURE_STATUSES:
            return False
        if (
            record.get("is_error") is True
            or record.get("isError") is True
            or record.get("ok") is False
            or record.get("success") is False
            or record.get("timed_out") is True
            or record.get("timedOut") is True
        ):
            return False

        if "exit_code" in record:
            exit_success = _exit_code_success(record.get("exit_code"))
            if exit_success is False:
                return False
            if exit_success is True:
                saw_success = True

        if (
            status in _SUCCESS_STATUSES
            or record.get("is_error") is False
            or record.get("isError") is False
            or record.get("ok") is True
            or record.get("success") is True
        ):
            saw_success = True

    if saw_success:
        return True

    if _depth < 2:
        for key in ("result", "output"):
            if key not in parsed or parsed[key] is parsed:
                continue
            nested_signal = _structured_result_success(parsed[key], _depth=_depth + 1)
            if nested_signal is not None:
                return nested_signal
    return None


def _result_success(value: Any) -> bool:
    parsed = _json_value(value)
    structured_success = _structured_result_success(parsed)
    if structured_success is not None:
        return structured_success
    output = parsed.get("output", parsed) if isinstance(parsed, Mapping) else parsed
    lowered = str(output).lower()
    return not any(marker in lowered for marker in _ERROR_MARKERS)


def _execution_status(value: Any, *, has_error: bool = False) -> str:
    normalized = str(value or "success").strip().lower().replace("-", "_")
    if has_error or normalized in _FAILURE_STATUSES:
        return "failed" if normalized != "timeout" else "timeout"
    if normalized in _SUCCESS_STATUSES:
        return "success"
    return normalized or "failed"


def _direct_tool_call(value: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    function = value.get("function")
    if isinstance(function, Mapping):
        raw_name = function.get("name", value.get("name", "unknown"))
        arguments = function.get("arguments", value.get("arguments", {}))
    else:
        raw_name = value.get("name", value.get("tool_name", "unknown"))
        arguments = value.get("arguments", value.get("args", {}))
    has_result = "result" in value or "output" in value
    result = value.get("result", value.get("output"))
    explicit_success = value.get("success")
    explicit_status = str(value.get("status", "")).strip().lower()
    if isinstance(explicit_success, bool):
        success = explicit_success
    elif explicit_status in _SUCCESS_STATUSES:
        success = True
    elif explicit_status in _FAILURE_STATUSES:
        success = False
    else:
        success = _result_success(result) if has_result else False
    return {
        "index": index,
        "call_id": value.get("id", value.get("call_id")),
        "name": str(raw_name or "unknown"),
        "raw_name": str(raw_name or "unknown"),
        "arguments": _json_value(arguments),
        "result": _json_value(result),
        "success": success,
    }


def _transcript_tool_calls(messages: Sequence[Any]) -> tuple[list[dict[str, Any]], Any]:
    pending: dict[str, dict[str, Any]] = {}
    calls: list[dict[str, Any]] = []
    final_answer: Any = None

    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = message.get("role")
        event_type = message.get("type")
        payload = message.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        payload_type = payload.get("type")

        request_items: Any = None
        if role == "assistant" and message.get("tool_calls"):
            request_items = message.get("tool_calls")
        elif event_type == "response_item" and payload_type == "function_call":
            request_items = [payload]

        if isinstance(request_items, list):
            for request in request_items:
                if not isinstance(request, Mapping):
                    continue
                function = request.get("function")
                if isinstance(function, Mapping):
                    raw_name = function.get("name", request.get("name", "unknown"))
                    arguments = function.get("arguments", request.get("arguments", {}))
                else:
                    raw_name = request.get("name", "unknown")
                    arguments = request.get("arguments", {})
                call_id = str(
                    request.get("id", request.get("call_id", f"pending-{len(pending)}"))
                )
                pending[call_id] = {
                    "index": len(calls) + len(pending),
                    "call_id": call_id,
                    "name": str(raw_name or "unknown"),
                    "raw_name": str(raw_name or "unknown"),
                    "arguments": _json_value(arguments),
                }
            continue

        call_id: str | None = None
        output: Any = None
        if role == "tool":
            call_id = str(message.get("tool_call_id", ""))
            output = message.get("content")
        elif event_type == "response_item" and payload_type == "function_call_output":
            call_id = str(payload.get("call_id", ""))
            output = payload.get("output")
        if call_id and call_id in pending:
            request = pending.pop(call_id)
            request["result"] = _json_value(output)
            request["success"] = _result_success(output)
            calls.append(request)
            continue

        message_role = payload.get("role") if event_type == "response_item" else role
        message_content = payload.get("content") if event_type == "response_item" else message.get("content")
        if message_role == "assistant":
            text = _content_text(message_content)
            if text:
                final_answer = text

    for request in pending.values():
        request["result"] = None
        request["success"] = False
        calls.append(request)
    calls.sort(key=lambda item: int(item.get("index", 0)))
    for index, call in enumerate(calls):
        call["index"] = index
    return calls, final_answer


def normalize_agent_payload(payload: Any) -> AgentExecution:
    """Normalize direct JSON responses and OpenAI/Codex transcript shapes."""

    if isinstance(payload, list):
        transcript = list(payload)
        calls, final_answer = _transcript_tool_calls(transcript)
        return AgentExecution(
            status="success" if final_answer is not None else "invalid_output",
            final_answer=_json_value(final_answer),
            tool_calls=calls,
            transcript=transcript,
        )
    if not isinstance(payload, Mapping):
        return AgentExecution(
            status="invalid_output" if payload is None else "success",
            final_answer=_json_value(payload),
        )

    transcript_value = payload.get("transcript", payload.get("messages", payload.get("chat", [])))
    transcript = list(transcript_value) if isinstance(transcript_value, list) else []
    transcript_calls, transcript_answer = _transcript_tool_calls(transcript)

    direct_calls: list[dict[str, Any]] = []
    raw_direct = payload.get("tool_calls")
    if isinstance(raw_direct, list):
        for index, value in enumerate(raw_direct):
            call = _direct_tool_call(value, index)
            if call is not None:
                direct_calls.append(call)

    answer = payload.get(
        "final_answer",
        payload.get("answer", payload.get("response", payload.get("output", transcript_answer))),
    )
    reasoning = payload.get("reasoning", payload.get("solution", payload.get("analysis")))
    error_value = payload.get("error")
    error = None
    if isinstance(error_value, Mapping):
        error = {
            "type": str(error_value.get("type") or "AgentError"),
            "message": str(error_value.get("message") or error_value),
        }
    elif error_value:
        error = {
            "type": type(error_value).__name__,
            "message": str(error_value),
        }
    warnings: list[dict[str, str]] = []
    warning_values = payload.get("warnings", [])
    if isinstance(warning_values, list):
        for warning in warning_values:
            if not isinstance(warning, Mapping):
                continue
            message = warning.get("message")
            if message is None:
                continue
            warnings.append(
                {
                    "type": str(warning.get("type") or "AgentWarning"),
                    "message": str(message),
                }
            )
    status = _execution_status(payload.get("status", "success"), has_error=bool(error))
    if answer is None and status == "success":
        status = "invalid_output"
    return AgentExecution(
        status=status,
        final_answer=_json_value(answer),
        reasoning=reasoning,
        tool_calls=_merge_tool_calls(direct_calls, transcript_calls),
        transcript=transcript,
        error=error,
        warnings=warnings,
    )


def parse_agent_stdout(stdout: str) -> Any:
    """Parse one JSON document or a complete JSONL transcript.

    Non-JSON diagnostic lines are ignored only when at least one valid JSON
    line exists.  Returning every JSONL event is important for preserving the
    agent's full tool trajectory instead of scoring only its final event.
    """

    stripped = stdout.strip()
    if not stripped:
        raise ValueError("agent command produced empty stdout")
    try:
        return json.loads(stripped, parse_constant=_reject_nonfinite_json)
    except json.JSONDecodeError as original:
        parsed_lines: list[Any] = []
        for line in stripped.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            try:
                parsed_lines.append(
                    json.loads(candidate, parse_constant=_reject_nonfinite_json)
                )
            except json.JSONDecodeError:
                continue
        if len(parsed_lines) == 1:
            return parsed_lines[0]
        if parsed_lines:
            return parsed_lines
        raise ValueError("agent command stdout does not contain valid JSON") from original


def _merge_tool_calls(
    direct_calls: Sequence[dict[str, Any]],
    transcript_calls: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not direct_calls:
        return [dict(call) for call in transcript_calls]
    if not transcript_calls:
        return [dict(call) for call in direct_calls]

    merged = [dict(call) for call in transcript_calls]
    positions = {
        str(call.get("call_id")): index
        for index, call in enumerate(merged)
        if call.get("call_id") is not None
    }
    for call in direct_calls:
        value = dict(call)
        call_id = value.get("call_id")
        if call_id is not None and str(call_id) in positions:
            merged[positions[str(call_id)]] = value
        else:
            merged.append(value)
    for index, call in enumerate(merged):
        call["index"] = index
    return merged


def _spool_tail(stream: Any, maximum_bytes: int) -> tuple[str, int, bool]:
    stream.flush()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - maximum_bytes), os.SEEK_SET)
    payload = stream.read(maximum_bytes)
    if not isinstance(payload, bytes):
        payload = str(payload).encode("utf-8", errors="replace")
    return payload.decode("utf-8", errors="replace"), size, size > maximum_bytes


def _spool_full_text(stream: Any) -> str:
    """Read a completed spool for normalization without replacing its log file."""

    stream.flush()
    stream.seek(0, os.SEEK_SET)
    payload = stream.read()
    if not isinstance(payload, bytes):
        payload = str(payload).encode("utf-8", errors="replace")
    return payload.decode("utf-8", errors="replace")


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    # Give adapters a bounded opportunity to run their own cleanup handlers
    # (notably, to remove daemon-managed containers) before escalating.  Docker
    # containers are not members of this process group, so an immediate
    # SIGKILL would prevent the adapter's ``finally`` blocks from cleaning them
    # up.  Windows has no POSIX process groups here, but terminate-then-kill
    # provides the equivalent bounded graceful shutdown for the child process.
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=30)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _execution_from_capture(
    full_stdout: str,
    stdout_tail: str,
    stderr_tail: str,
    *,
    duration_seconds: float,
    stdout_log_path: Path,
    stderr_log_path: Path,
    stdout_bytes: int,
    stderr_bytes: int,
    stdout_truncated: bool,
    stderr_truncated: bool,
    forced_status: str | None = None,
    error: dict[str, str] | None = None,
) -> AgentExecution:
    try:
        execution = normalize_agent_payload(parse_agent_stdout(full_stdout))
    except (TypeError, ValueError) as exc:
        execution = AgentExecution(
            status=forced_status or "failed",
            error={"type": type(exc).__name__, "message": str(exc)},
        )
    if forced_status is not None:
        execution.status = forced_status
    execution.duration_seconds = duration_seconds
    # stdout/stderr are display fields only.  Full byte streams remain in the
    # attempt workspace logs and are deliberately not copied into result JSON.
    execution.stdout = stdout_tail
    execution.stderr = stderr_tail
    execution.stdout_log_path = str(stdout_log_path)
    execution.stderr_log_path = str(stderr_log_path)
    execution.stdout_bytes = stdout_bytes
    execution.stderr_bytes = stderr_bytes
    execution.stdout_truncated = stdout_truncated
    execution.stderr_truncated = stderr_truncated
    if error is not None:
        execution.error = error
    return execution


def execute_agent_command(
    command: Sequence[str],
    envelope: Mapping[str, Any],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: float = 600.0,
    max_capture_bytes: int = 1_000_000,
    inherit_environment: bool = True,
) -> AgentExecution:
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("agent command must be a non-empty string array")
    if (
        isinstance(max_capture_bytes, bool)
        or not isinstance(max_capture_bytes, int)
        or max_capture_bytes < 1
    ):
        raise ValueError("max_capture_bytes must be a positive integer")
    merged_environment = os.environ.copy() if inherit_environment else {}
    if environment:
        merged_environment.update({str(key): str(value) for key, value in environment.items()})

    workspace = Path(cwd).resolve()
    stdout_log_path = (workspace / "agent.stdout.log").resolve()
    stderr_log_path = (workspace / "agent.stderr.log").resolve()

    started = time.perf_counter()
    popen_options: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": None,
        "stderr": None,
        "cwd": workspace,
        "env": merged_environment,
        "text": True,
        "encoding": "utf-8",
    }
    if os.name == "posix":
        popen_options["start_new_session"] = True

    with stdout_log_path.open(mode="w+b") as stdout_stream, stderr_log_path.open(
        mode="w+b"
    ) as stderr_stream:
        popen_options["stdout"] = stdout_stream
        popen_options["stderr"] = stderr_stream
        try:
            process = subprocess.Popen(list(command), **popen_options)
        except OSError as exc:
            stdout, stdout_bytes, stdout_truncated = _spool_tail(
                stdout_stream, max_capture_bytes
            )
            stderr, stderr_bytes, stderr_truncated = _spool_tail(
                stderr_stream, max_capture_bytes
            )
            return AgentExecution(
                status="failed",
                duration_seconds=time.perf_counter() - started,
                stdout=stdout,
                stderr=stderr,
                stdout_log_path=str(stdout_log_path),
                stderr_log_path=str(stderr_log_path),
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                error={"type": type(exc).__name__, "message": str(exc)},
            )

        timed_out: subprocess.TimeoutExpired | None = None
        try:
            process.communicate(
                input=json.dumps(envelope, ensure_ascii=False, allow_nan=False),
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = exc
            _terminate_process_group(process)

        duration = time.perf_counter() - started
        stdout, stdout_bytes, stdout_truncated = _spool_tail(
            stdout_stream, max_capture_bytes
        )
        stderr, stderr_bytes, stderr_truncated = _spool_tail(
            stderr_stream, max_capture_bytes
        )
        full_stdout = _spool_full_text(stdout_stream)
        if timed_out is not None:
            return _execution_from_capture(
                full_stdout,
                stdout,
                stderr,
                duration_seconds=duration,
                stdout_log_path=stdout_log_path,
                stderr_log_path=stderr_log_path,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                forced_status="timeout",
                error={"type": type(timed_out).__name__, "message": str(timed_out)},
            )
        if process.returncode != 0:
            return _execution_from_capture(
                full_stdout,
                stdout,
                stderr,
                duration_seconds=duration,
                stdout_log_path=stdout_log_path,
                stderr_log_path=stderr_log_path,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                forced_status="failed",
                error={
                    "type": "AgentCommandError",
                    "message": f"agent command exited with status {process.returncode}",
                },
            )
        return _execution_from_capture(
            full_stdout,
            stdout,
            stderr,
            duration_seconds=duration,
            stdout_log_path=stdout_log_path,
            stderr_log_path=stderr_log_path,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

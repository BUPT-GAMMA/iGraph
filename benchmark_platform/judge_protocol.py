"""Strict subprocess protocol for binary LLM-as-a-Judge evaluation.

The judge is an external argv command.  It receives exactly one JSON object on
stdin and must return exactly one JSON object on stdout.  Raw output is kept in
anonymous temporary files only; the returned execution record contains hashes
and structural metadata so benchmark artifacts need not retain judge rationale
or echoed ground truth.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


_PROTOCOL = "igraph-benchmark.judge/v1"


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is not allowed: {value}")
    return parsed


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is not allowed: {key!r}")
        result[key] = value
    return result


@dataclass
class JudgeExecution:
    """Result of one judge command invocation.

    ``reason`` is populated only when the caller opts in with
    ``include_reason=True``.  ``raw_summary`` deliberately contains no output
    excerpts: it records byte counts, SHA-256 digests, and response structure
    for diagnostics without persisting an explanation that may repeat the
    private ground truth.
    """

    status: str
    score: int | None = None
    reason: str | None = None
    duration_seconds: float = 0.0
    return_code: int | None = None
    protocol: str = _PROTOCOL
    raw_summary: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_command(command: Sequence[str]) -> list[str]:
    if isinstance(command, (str, bytes)) or not command:
        raise ValueError("judge command must be a non-empty string array")
    argv = list(command)
    if any(not isinstance(part, str) or not part or "\x00" in part for part in argv):
        raise ValueError("judge command must be a non-empty string array without NUL bytes")
    return argv


def _validate_positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return normalized


def _validate_maximum_bytes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_output_bytes must be a positive integer")
    return value


def _encode_envelope(envelope: Mapping[str, Any]) -> str:
    if not isinstance(envelope, Mapping):
        raise ValueError("judge envelope must be a JSON object")
    try:
        encoded = json.dumps(
            dict(envelope),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"judge envelope is not strict JSON: {exc}") from exc
    return encoded


def _parse_response(stdout: bytes) -> tuple[int, str | None, dict[str, Any]]:
    try:
        text = stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("judge stdout is not valid UTF-8") from exc
    if not text.strip():
        raise ValueError("judge command produced empty stdout")
    try:
        payload = json.loads(
            text,
            parse_constant=_reject_nonfinite_json,
            parse_float=_parse_finite_json_float,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"judge stdout must contain exactly one strict JSON object: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("judge stdout must contain a JSON object")
    if "score" not in payload:
        raise ValueError("judge response is missing required field 'score'")

    raw_score = payload["score"]
    if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
        raise ValueError("judge score must be numeric 0 or 1, not bool")
    if not math.isfinite(float(raw_score)) or raw_score not in (0, 1):
        raise ValueError("judge score must be numeric 0 or 1")

    raw_reason = payload.get("reason")
    if raw_reason is not None and not isinstance(raw_reason, str):
        raise ValueError("judge reason must be a string or null when provided")
    summary = {
        "response_keys": sorted(payload),
        "score_json_type": type(raw_score).__name__,
        "reason_present": raw_reason is not None,
    }
    return int(raw_score), raw_reason, summary


def _stream_digest(stream: Any) -> tuple[int, str]:
    stream.flush()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            chunk = str(chunk).encode("utf-8", errors="replace")
        digest.update(chunk)
    return size, digest.hexdigest()


def _read_limited(stream: Any, maximum_bytes: int) -> tuple[bytes, bool]:
    stream.flush()
    stream.seek(0, os.SEEK_SET)
    payload = stream.read(maximum_bytes + 1)
    if not isinstance(payload, bytes):
        payload = str(payload).encode("utf-8", errors="replace")
    return payload[:maximum_bytes], len(payload) > maximum_bytes


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
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


def _raw_summary(stdout_stream: Any, stderr_stream: Any) -> dict[str, Any]:
    stdout_bytes, stdout_sha256 = _stream_digest(stdout_stream)
    stderr_bytes, stderr_sha256 = _stream_digest(stderr_stream)
    return {
        "stdout_bytes": stdout_bytes,
        "stdout_sha256": stdout_sha256,
        "stderr_bytes": stderr_bytes,
        "stderr_sha256": stderr_sha256,
    }


def execute_judge_command(
    command: Sequence[str],
    envelope: Mapping[str, Any],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: float = 600.0,
    max_output_bytes: int = 1_000_000,
    inherit_environment: bool = True,
    include_reason: bool = False,
) -> JudgeExecution:
    """Run a binary judge adapter and validate its strict JSON response.

    The adapter is started with ``shell=False`` and receives the compact JSON
    envelope as its complete stdin.  It must emit an object shaped like
    ``{"score": 0 | 1, "reason": "optional text"}``.  Additional response
    fields are permitted and listed (by key only) in ``raw_summary``.
    """

    argv = _validate_command(command)
    timeout = _validate_positive_number(timeout_seconds, "timeout_seconds")
    maximum_bytes = _validate_maximum_bytes(max_output_bytes)
    stdin_text = _encode_envelope(envelope)

    workspace = Path(cwd).resolve()
    if not workspace.is_dir():
        raise ValueError(f"judge cwd is not a directory: {workspace}")

    merged_environment = os.environ.copy() if inherit_environment else {}
    if environment is not None:
        if not isinstance(environment, Mapping):
            raise ValueError("judge environment must be a string mapping")
        for key, value in environment.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("judge environment must be a string mapping")
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError("judge environment contains an invalid name or value")
            merged_environment[key] = value

    started = time.perf_counter()
    popen_options: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": None,
        "stderr": None,
        "cwd": workspace,
        "env": merged_environment,
        "text": True,
        "encoding": "utf-8",
        "shell": False,
    }
    if os.name == "posix":
        popen_options["start_new_session"] = True

    # Anonymous temporary streams bound memory use and disappear before this
    # function returns; unlike attempt logs, they do not persist judge rationale.
    with tempfile.TemporaryFile(mode="w+b") as stdout_stream, tempfile.TemporaryFile(
        mode="w+b"
    ) as stderr_stream:
        popen_options["stdout"] = stdout_stream
        popen_options["stderr"] = stderr_stream
        try:
            process = subprocess.Popen(argv, **popen_options)
        except OSError as exc:
            summary = _raw_summary(stdout_stream, stderr_stream)
            return JudgeExecution(
                status="failed",
                duration_seconds=time.perf_counter() - started,
                raw_summary=summary,
                error={"type": type(exc).__name__, "message": str(exc)},
            )

        try:
            process.communicate(input=stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            summary = _raw_summary(stdout_stream, stderr_stream)
            return JudgeExecution(
                status="timeout",
                duration_seconds=time.perf_counter() - started,
                return_code=process.returncode,
                raw_summary=summary,
                error={
                    "type": type(exc).__name__,
                    "message": f"judge command exceeded {timeout} seconds",
                },
            )

        duration = time.perf_counter() - started
        summary = _raw_summary(stdout_stream, stderr_stream)
        if process.returncode != 0:
            return JudgeExecution(
                status="failed",
                duration_seconds=duration,
                return_code=process.returncode,
                raw_summary=summary,
                error={
                    "type": "JudgeCommandError",
                    "message": f"judge command exited with status {process.returncode}",
                },
            )

        stdout, truncated = _read_limited(stdout_stream, maximum_bytes)
        if truncated:
            summary["stdout_truncated"] = True
            return JudgeExecution(
                status="invalid_output",
                duration_seconds=duration,
                return_code=process.returncode,
                raw_summary=summary,
                error={
                    "type": "JudgeOutputTooLarge",
                    "message": f"judge stdout exceeds {maximum_bytes} bytes",
                },
            )

        try:
            score, reason, response_summary = _parse_response(stdout)
        except (TypeError, ValueError) as exc:
            return JudgeExecution(
                status="invalid_output",
                duration_seconds=duration,
                return_code=process.returncode,
                raw_summary=summary,
                error={"type": type(exc).__name__, "message": str(exc)},
            )
        summary.update(response_summary)
        return JudgeExecution(
            status="success",
            score=score,
            reason=reason if include_reason else None,
            duration_seconds=duration,
            return_code=process.returncode,
            raw_summary=summary,
        )

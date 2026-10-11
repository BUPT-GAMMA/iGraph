"""HTTP boundary between the durable large-graph worker and GraphScope.

The graph-data image deliberately has no GraphScope Python dependency.  A
separate Python 3.11 service owns that runtime and both processes exchange only
bounded JSON control messages while reading/writing the shared immutable data
volume.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .contracts import BackendPlan
from .engine import LocalParallelGraphEngine, ProgressCallback


_RUN_ID = re.compile(r"[A-Za-z0-9_.-]{1,160}")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class GraphScopeProtocolError(RuntimeError):
    """The adapter was unreachable or returned an invalid control payload."""


def _bounded_float(
    value: Any, *, default: float, minimum: float, maximum: float
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _adapter_endpoint(base_url: str, path: str) -> str:
    parsed = urlsplit(str(base_url).strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GraphScopeProtocolError("GRAPHSCOPE_ADAPTER_URL 不是合法的 HTTP(S) 地址")
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


class GraphScopeAdapterClient:
    """Small, dependency-free client for one remote native-algorithm run."""

    def __init__(
        self,
        base_url: str,
        *,
        service_key: str | None = None,
        request_timeout: float | None = None,
        poll_seconds: float | None = None,
    ) -> None:
        self.base_url = str(base_url).strip().rstrip("/")
        _adapter_endpoint(self.base_url, "/health")
        self.service_key = (
            str(service_key)
            if service_key is not None
            else os.getenv("IGRAPH_CONTROL_SERVICE_KEY", "")
        )
        if not self.service_key:
            raise GraphScopeProtocolError("缺少 IGRAPH_CONTROL_SERVICE_KEY")
        self.request_timeout = _bounded_float(
            request_timeout
            if request_timeout is not None
            else os.getenv("GRAPHSCOPE_REQUEST_TIMEOUT_SECONDS", "15"),
            default=15.0,
            minimum=1.0,
            maximum=120.0,
        )
        self.poll_seconds = _bounded_float(
            poll_seconds
            if poll_seconds is not None
            else os.getenv("GRAPHSCOPE_POLL_SECONDS", "1"),
            default=1.0,
            minimum=0.1,
            maximum=10.0,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        missing_ok: bool = False,
    ) -> dict[str, Any]:
        encoded = None
        headers = {
            "Accept": "application/json",
            "X-iGraph-Service-Key": self.service_key,
        }
        if payload is not None:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            _adapter_endpoint(self.base_url, path),
            data=encoded,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and missing_ok:
                return {"status": "missing"}
            body = exc.read(min(_MAX_RESPONSE_BYTES, 64 * 1024))
            try:
                detail = json.loads(body.decode("utf-8")).get("detail")
            except (UnicodeError, json.JSONDecodeError, AttributeError):
                detail = body.decode("utf-8", errors="replace")[:1000]
            raise GraphScopeProtocolError(
                f"GraphScope 适配器请求失败({exc.code}): {detail or exc.reason}"
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise GraphScopeProtocolError(f"无法连接 GraphScope 适配器: {exc}") from exc
        if len(body) > _MAX_RESPONSE_BYTES:
            raise GraphScopeProtocolError("GraphScope 适配器响应超过控制面大小上限")
        try:
            decoded = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GraphScopeProtocolError("GraphScope 适配器返回了非法 JSON") from exc
        if not isinstance(decoded, dict):
            raise GraphScopeProtocolError("GraphScope 适配器响应必须是 JSON 对象")
        return decoded

    def cancel(self, run_id: str) -> bool:
        if _RUN_ID.fullmatch(run_id) is None:
            return False
        try:
            response = self._request(
                "DELETE", f"/v1/runs/{run_id}", missing_ok=True
            )
        except GraphScopeProtocolError:
            # The adapter lease is the authoritative orphan-reaper.  A best
            # effort cancellation must not hide the caller's terminal state.
            return False
        return str(response.get("status") or "").lower() in {
            "cancelled",
            "succeeded",
            "failed",
            "missing",
        }

    @staticmethod
    def _validated_result(
        response: dict[str, Any],
        *,
        result_root: Path,
        partitions: int,
        plan: BackendPlan,
    ) -> dict[str, Any]:
        raw = response.get("result")
        if not isinstance(raw, dict):
            raise GraphScopeProtocolError("GraphScope 完成响应缺少 result")
        summary = raw.get("summary")
        preview = raw.get("preview", [])
        result_files = raw.get("result_files")
        if not isinstance(summary, dict) or not isinstance(preview, list):
            raise GraphScopeProtocolError("GraphScope 结果摘要或预览类型错误")
        if not isinstance(result_files, list) or len(result_files) > partitions * 16:
            raise GraphScopeProtocolError("GraphScope 结果文件列表非法")
        root = result_root.resolve(strict=True)
        bound_files: list[str] = []
        for raw_relative in result_files:
            relative = str(raw_relative)
            pure = PurePosixPath(relative)
            if (
                pure.is_absolute()
                or len(pure.parts) != 1
                or any(part in {"", ".", ".."} for part in pure.parts)
                or pure.suffix != ".jsonl"
                or "\\" in relative
            ):
                raise GraphScopeProtocolError("GraphScope 返回了非法结果文件路径")
            candidate = result_root / relative
            if candidate.is_symlink():
                raise GraphScopeProtocolError("GraphScope 结果文件不允许符号链接")
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise GraphScopeProtocolError("GraphScope 结果文件不存在或越界") from exc
            if not resolved.is_file():
                raise GraphScopeProtocolError("GraphScope 结果路径不是常规文件")
            bound_files.append(f"results/{relative}")
        if len(set(bound_files)) != len(bound_files):
            raise GraphScopeProtocolError("GraphScope 返回了重复结果文件")
        metadata = dict(raw.get("backend_metadata") or {})
        metadata.update(
            {
                "backend": "graphscope",
                "protocol_version": plan.protocol_version,
                "native_app": plan.native_app,
                "semantic_profile": plan.semantic_profile,
                "topology_access": "graphscope-native-shared-snapshot-v1",
            }
        )
        return {
            "summary": summary,
            "preview": preview,
            "result_files": bound_files,
            "backend_metadata": metadata,
        }

    def run(
        self,
        *,
        run_id: str,
        payload: dict[str, Any],
        result_root: Path,
        partitions: int,
        plan: BackendPlan,
        progress: ProgressCallback | None,
    ) -> dict[str, Any]:
        if _RUN_ID.fullmatch(run_id) is None:
            raise GraphScopeProtocolError("GraphScope run_id 格式非法")
        self._request("POST", f"/v1/runs/{run_id}", payload)
        last_heartbeat = 0.0
        while True:
            now = time.monotonic()
            if now - last_heartbeat >= 5.0:
                self._request("POST", f"/v1/runs/{run_id}/heartbeat", {})
                last_heartbeat = now
            state = self._request("GET", f"/v1/runs/{run_id}")
            status = str(state.get("status") or "").lower()
            if progress is not None:
                value = _bounded_float(
                    state.get("progress"),
                    default=0.10,
                    minimum=0.10,
                    maximum=0.90,
                )
                progress(
                    "graphscope",
                    value,
                    str(state.get("message") or "GraphScope 原生算法执行中"),
                    {"run_id": run_id, "native_app": plan.native_app},
                )
            if status == "succeeded":
                return self._validated_result(
                    state,
                    result_root=result_root,
                    partitions=partitions,
                    plan=plan,
                )
            if status in {"failed", "cancelled"}:
                raise GraphScopeProtocolError(
                    str(state.get("error") or f"GraphScope 任务状态为 {status}")
                )
            if status not in {"queued", "running", "cancelling"}:
                raise GraphScopeProtocolError(f"未知 GraphScope 任务状态: {status!r}")
            time.sleep(self.poll_seconds)


def graphscope_run_id(job_id: str, lease_id: str | None) -> str:
    raw = f"{job_id}.{lease_id or 'unleased'}"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:160]
    if _RUN_ID.fullmatch(safe) is None:
        raise GraphScopeProtocolError("无法构造 GraphScope run_id")
    return safe


class GraphScopeBackend:
    """Execute one approved mode and retain the existing artifact publisher."""

    def __init__(
        self,
        artifact_root: str | Path,
        *,
        snapshot_root: str | Path,
        plan: BackendPlan,
        job_id: str,
        lease_id: str | None,
    ) -> None:
        self.local_engine = LocalParallelGraphEngine(
            artifact_root, snapshot_root=snapshot_root
        )
        self.plan = plan
        self.run_id = graphscope_run_id(job_id, lease_id)
        self.client = GraphScopeAdapterClient(str(plan.adapter_url))

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        def external_runner(**runner: Any) -> dict[str, Any]:
            result_root = Path(runner["result_root"])
            work_root = Path(runner["work_root"])
            result_root.mkdir(parents=True, exist_ok=True)
            work_root.mkdir(parents=True, exist_ok=True)
            payload = {
                "protocol_version": self.plan.protocol_version,
                "native_app": self.plan.native_app,
                "semantic_profile": self.plan.semantic_profile,
                "manifest_path": str(runner["manifest_path"]),
                "result_root": str(result_root),
                "work_root": str(work_root),
                "algorithm": runner["algorithm"],
                "parameters": dict(runner["parameters"]),
                "workers": int(runner["workers"]),
                "top_k": int(runner["top_k"]),
            }
            return self.client.run(
                run_id=self.run_id,
                payload=payload,
                result_root=result_root,
                partitions=int(runner["manifest"]["partitions"]),
                plan=self.plan,
                progress=runner.get("progress"),
            )

        return self.local_engine.execute(external_runner=external_runner, **kwargs)

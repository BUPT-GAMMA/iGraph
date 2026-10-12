"""Authenticated, lease-fenced process supervisor for GraphScope native apps."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import multiprocessing
import os
import queue
import re
import signal
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException

from . import ADAPTER_VERSION
from .algorithms import execute


_RUN_ID = re.compile(r"[A-Za-z0-9_.-]{1,160}")
_PROTOCOL = "igraph-graphscope-v1"
_MAX_CONTROL_BYTES = 2 * 1024 * 1024


def _worker(payload: dict[str, Any], messages: Any) -> None:
    try:
        if os.name == "posix":
            os.setsid()
        messages.put({"type": "running", "pid": os.getpid()})
        result = execute(payload)
        encoded = json.dumps(
            result, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_CONTROL_BYTES:
            raise ValueError("GraphScope 控制面结果超过大小上限")
        messages.put({"type": "result", "result": result})
    except BaseException as exc:
        detail = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )[-8000:]
        messages.put({"type": "error", "error": str(exc), "traceback": detail})


@dataclass
class Run:
    run_id: str
    digest: str
    process: multiprocessing.Process
    messages: Any
    status: str = "queued"
    progress: float = 0.12
    message: str = "GraphScope 任务已接收"
    error: str | None = None
    result: dict[str, Any] | None = None
    last_heartbeat: float = field(default_factory=time.monotonic)
    completed_at: float | None = None


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._lock = threading.RLock()
        self._context = multiprocessing.get_context("spawn")
        self.max_active = max(1, int(os.getenv("GRAPHSCOPE_MAX_ACTIVE_JOBS", "1")))
        self.lease_seconds = max(
            15.0, float(os.getenv("GRAPHSCOPE_REMOTE_LEASE_SECONDS", "30"))
        )
        self.retention_seconds = max(
            60.0, float(os.getenv("GRAPHSCOPE_RUN_RETENTION_SECONDS", "3600"))
        )

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_CONTROL_BYTES:
            raise HTTPException(status_code=413, detail="请求控制面负载超过大小上限")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _managed_path(raw: Any, root: Path, *, must_exist: bool) -> Path:
        try:
            configured = root.resolve(strict=True)
            candidate = Path(str(raw)).resolve(strict=must_exist)
            candidate.relative_to(configured)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="GraphScope 数据路径越出受管根目录") from exc
        cursor = configured
        try:
            relatives = candidate.relative_to(configured).parts
        except ValueError as exc:  # pragma: no cover - guarded above
            raise HTTPException(status_code=400, detail="GraphScope 数据路径非法") from exc
        for part in relatives:
            cursor = cursor / part
            if cursor.is_symlink():
                raise HTTPException(status_code=400, detail="GraphScope 数据路径不允许符号链接")
        return candidate

    def validate_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("protocol_version")) != _PROTOCOL:
            raise HTTPException(status_code=400, detail="GraphScope 协议版本不匹配")
        native_app = str(payload.get("native_app") or "")
        algorithm = str(payload.get("algorithm") or "")
        expected = {
            "wcc": {"graph_connectivity"},
            "pagerank_nx": {"node_centrality"},
            "bfs": {"shortest_path"},
            "sssp": {"shortest_path"},
            "cdlp": {"community_detection"},
            "louvain": {"community_detection"},
            "clustering": {"global_metrics"},
            "triangles": {"global_metrics"},
            "degree_assortativity_coefficient": {"global_metrics"},
            "average_degree_connectivity": {"distance_measures"},
            "average_shortest_path_length": {"path_analysis"},
            "k_core": {"subgraph_extraction"},
            "degree_centrality": {"degree_centrality", "node_centrality"},
            "eigenvector_centrality": {"node_centrality"},
            "katz_centrality": {"node_centrality"},
            "voterank": {"node_centrality"},
            "flash_betweenness": {"node_centrality"},
            "flash_closeness": {"node_centrality"},
            "flash_harmonic": {"node_centrality"},
            "flash_scc": {"strongly_connected_components"},
            "flash_bcc": {"biconnected_components"},
            "flash_bridges": {"bridges"},
            "flash_cut_point": {"articulation_points"},
            "flash_msf": {"minimum_spanning_forest"},
            "flash_color": {"graph_coloring"},
            "flash_mm": {"maximal_matching"},
            "hits": {"hits"},
            "k_shell": {"k_shell"},
            "flash_ppr": {"personalized_pagerank"},
            "flash_diameter_approximation": {"approximate_diameter"},
            "flash_fluid_community": {"fluid_community"},
            "flash_densest_subgraph": {"densest_subgraph"},
            "flash_maximal_independent_set": {"independent_set_cover"},
            "flash_minimal_dominating_set": {"independent_set_cover"},
            "flash_minimal_vertex_cover": {"independent_set_cover"},
            "flash_minimal_edge_cover": {"independent_set_cover"},
            "flash_triangle_counting": {"motif_counting"},
            "flash_rectangle_counting": {"motif_counting"},
            "flash_k_clique_counting": {"motif_counting"},
            "flash_diamond_counting": {"motif_counting"},
            "flash_tailed_triangle_counting": {"motif_counting"},
            "flash_three_path_counting": {"motif_counting"},
            "flash_cycle_plus_triangle_counting": {"motif_counting"},
            "flash_cyclic_triangle_counting": {"motif_counting"},
            "flash_acyclic_triangle_counting": {"motif_counting"},
            "flash_in_plus_triangle_counting": {"motif_counting"},
            "flash_out_plus_triangle_counting": {"motif_counting"},
        }
        if algorithm not in expected.get(native_app, set()):
            raise HTTPException(status_code=400, detail="GraphScope 原生算法与公开算法不匹配")
        snapshot_root = Path(
            os.getenv("GRAPHSCOPE_SNAPSHOT_ROOT", "/data/large_graph/snapshots")
        )
        artifact_root = Path(
            os.getenv("GRAPHSCOPE_ARTIFACT_ROOT", "/data/large_graph/artifacts")
        )
        manifest_path = self._managed_path(
            payload.get("manifest_path"), snapshot_root, must_exist=True
        )
        if manifest_path.name != "manifest.json" or not manifest_path.is_file():
            raise HTTPException(status_code=400, detail="GraphScope 快照清单路径非法")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="GraphScope 快照清单不可读") from exc
        if not isinstance(manifest, dict) or manifest.get("state") != "published":
            raise HTTPException(status_code=400, detail="GraphScope 快照尚未发布")
        parameters = payload.get("parameters")
        if not isinstance(parameters, dict):
            raise HTTPException(status_code=400, detail="GraphScope parameters 必须是对象")
        raw_rounds = parameters.get("rounds", 10)
        raw_motif_k = parameters.get("k", 0)
        try:
            densest_rounds = int(raw_rounds)
        except (TypeError, ValueError, OverflowError):
            densest_rounds = 0
        try:
            motif_k = int(raw_motif_k)
        except (TypeError, ValueError, OverflowError):
            motif_k = 0
        semantic_profile = str(payload.get("semantic_profile") or "")
        quality = manifest.get("quality") or {}
        has_self_loops = int(quality.get("self_loops_seen", 0)) > int(
            quality.get("self_loops_skipped", 0)
        )
        semantic_valid = False
        if native_app == "wcc":
            semantic_valid = (
                semantic_profile == "legacy_v1"
                and not bool(manifest.get("directed", True))
            )
        elif native_app == "pagerank_nx":
            semantic_valid = (
                semantic_profile == "legacy_v1"
                and str(parameters.get("centrality_type", "degree")).strip().lower()
                == "pagerank"
                and not bool(manifest.get("has_weights", False))
            )
        elif native_app == "bfs":
            semantic_valid = (
                semantic_profile == "distance_v1"
                and str(parameters.get("target", "")).strip().lower() == "all"
                and str(parameters.get("result_detail", "paths")).strip().lower()
                == "distances"
                and parameters.get("use_weight", True) is False
            )
        elif native_app == "sssp":
            semantic_valid = (
                semantic_profile == "distance_v1"
                and str(parameters.get("target", "")).strip().lower() == "all"
                and str(parameters.get("result_detail", "paths")).strip().lower()
                == "distances"
                and bool(parameters.get("use_weight", True))
            )
        elif native_app in {"cdlp", "louvain"}:
            requested = str(parameters.get("algorithm", "louvain")).strip().lower()
            semantic_valid = (
                semantic_profile == "graphscope_native_v1"
                and not bool(manifest.get("directed", True))
                and not bool(manifest.get("has_weights", False))
                and not has_self_loops
                and requested
                == ("label_propagation" if native_app == "cdlp" else "louvain")
            )
        elif native_app in {"clustering", "triangles"}:
            metric = str(parameters.get("metric", "")).strip().lower()
            semantic_valid = (
                semantic_profile == "legacy_v1"
                and not bool(manifest.get("directed", True))
                and not bool(manifest.get("has_weights", False))
                and not has_self_loops
                and metric
                == ("average_clustering" if native_app == "clustering" else "transitivity")
            )
        elif native_app == "degree_assortativity_coefficient":
            semantic_valid = (
                semantic_profile == "legacy_v1"
                and str(parameters.get("metric", "")).strip().lower()
                == "degree_assortativity"
                and not bool(manifest.get("directed", True))
                and not bool(manifest.get("has_weights", False))
                and not has_self_loops
            )
        elif native_app == "average_degree_connectivity":
            semantic_valid = (
                semantic_profile == "legacy_v1"
                and str(parameters.get("metric", "")).strip().lower()
                == "average_degree_connectivity"
                and not bool(manifest.get("has_weights", False))
                and not has_self_loops
            )
        elif native_app == "average_shortest_path_length":
            semantic_valid = (
                semantic_profile == "graphscope_native_v1"
                and str(parameters.get("method", "")).strip().lower()
                == "average_shortest_path_length"
                and not bool(manifest.get("has_weights", False))
                and not has_self_loops
                and not bool(parameters.get("strict_semantics", False))
            )
        elif native_app == "k_core":
            semantic_valid = (
                semantic_profile == "legacy_v1"
                and str(parameters.get("method", "")).strip().lower() == "k_core"
                and not bool(manifest.get("directed", True))
                and not has_self_loops
            )
        elif native_app in {
            "degree_centrality",
            "eigenvector_centrality",
            "katz_centrality",
            "voterank",
            "flash_betweenness",
            "flash_closeness",
            "flash_harmonic",
        }:
            centrality = str(parameters.get("centrality_type", "degree")).strip().lower()
            names = {
                "degree_centrality": "degree",
                "eigenvector_centrality": "eigenvector",
                "katz_centrality": "katz",
                "voterank": "voterank",
                "flash_betweenness": "betweenness",
                "flash_closeness": "closeness",
                "flash_harmonic": "harmonic",
            }
            semantic_valid = (
                semantic_profile
                == (
                    "graphscope_flash_approx_v1"
                    if native_app
                    in {"flash_betweenness", "flash_closeness", "flash_harmonic"}
                    else "legacy_v1"
                )
                and not bool(manifest.get("has_weights", False))
                and (
                    native_app in {"degree_centrality", "flash_betweenness"}
                    or not has_self_loops
                )
                and (
                    native_app not in {"flash_closeness", "flash_harmonic"}
                    or not bool(manifest.get("directed", True))
                )
                and (
                    native_app
                    not in {"flash_betweenness", "flash_closeness", "flash_harmonic"}
                    or not bool(parameters.get("strict_semantics", False))
                )
                and centrality == names[native_app]
            )
        elif native_app in {
            "flash_diameter_approximation",
            "flash_fluid_community",
            "flash_densest_subgraph",
        }:
            public_names = {
                "flash_diameter_approximation": "approximate_diameter",
                "flash_fluid_community": "fluid_community",
                "flash_densest_subgraph": "densest_subgraph",
            }
            semantic_valid = (
                semantic_profile == "graphscope_flash_approx_v1"
                and algorithm == public_names[native_app]
                and not bool(parameters.get("strict_semantics", False))
                and (
                    native_app != "flash_densest_subgraph"
                    or (
                        not isinstance(raw_rounds, bool)
                        and 1 <= densest_rounds <= 1000
                    )
                )
            )
        elif native_app in {
            "flash_maximal_independent_set",
            "flash_minimal_dominating_set",
            "flash_minimal_vertex_cover",
            "flash_minimal_edge_cover",
        }:
            operations = {
                "flash_maximal_independent_set": "maximal_independent_set",
                "flash_minimal_dominating_set": "minimal_dominating_set",
                "flash_minimal_vertex_cover": "minimal_vertex_cover",
                "flash_minimal_edge_cover": "minimal_edge_cover",
            }
            semantic_valid = (
                semantic_profile == "graphscope_native_v1"
                and algorithm == "independent_set_cover"
                and str(parameters.get("operation", "")).strip().lower()
                == operations[native_app]
            )
        elif native_app in {
            "flash_triangle_counting",
            "flash_rectangle_counting",
            "flash_k_clique_counting",
            "flash_diamond_counting",
            "flash_tailed_triangle_counting",
            "flash_three_path_counting",
            "flash_cycle_plus_triangle_counting",
            "flash_cyclic_triangle_counting",
            "flash_acyclic_triangle_counting",
            "flash_in_plus_triangle_counting",
            "flash_out_plus_triangle_counting",
        }:
            motifs = {
                "flash_triangle_counting": "triangle",
                "flash_rectangle_counting": "rectangle",
                "flash_k_clique_counting": "k_clique",
                "flash_diamond_counting": "diamond",
                "flash_tailed_triangle_counting": "tailed_triangle",
                "flash_three_path_counting": "three_path",
                "flash_cycle_plus_triangle_counting": "cycle_plus_triangle",
                "flash_cyclic_triangle_counting": "cyclic_triangle",
                "flash_acyclic_triangle_counting": "acyclic_triangle",
                "flash_in_plus_triangle_counting": "in_plus_triangle",
                "flash_out_plus_triangle_counting": "out_plus_triangle",
            }
            motif = str(parameters.get("motif", "")).strip().lower()
            semantic_valid = (
                semantic_profile == "graphscope_native_v1"
                and algorithm == "motif_counting"
                and bool(manifest.get("directed", True))
                and motif == motifs[native_app]
                and (
                    motif != "k_clique"
                    or (
                        not isinstance(raw_motif_k, bool)
                        and 2 <= motif_k <= 8
                    )
                )
            )
        elif native_app in {
            "flash_scc",
            "flash_bcc",
            "flash_bridges",
            "flash_cut_point",
            "flash_msf",
            "flash_color",
            "flash_mm",
            "hits",
            "k_shell",
            "flash_ppr",
        }:
            public_names = {
                "flash_scc": "strongly_connected_components",
                "flash_bcc": "biconnected_components",
                "flash_bridges": "bridges",
                "flash_cut_point": "articulation_points",
                "flash_msf": "minimum_spanning_forest",
                "flash_color": "graph_coloring",
                "flash_mm": "maximal_matching",
                "hits": "hits",
                "k_shell": "k_shell",
                "flash_ppr": "personalized_pagerank",
            }
            undirected_only = {
                "flash_bcc",
                "flash_bridges",
                "flash_cut_point",
                "flash_msf",
                "flash_color",
                "flash_mm",
                "k_shell",
            }
            unweighted_only = {"hits", "flash_ppr"}
            expected_profile = (
                "graphscope_flash_approx_v1"
                if native_app in {"flash_bcc", "flash_color", "flash_ppr"}
                else "graphscope_native_v1"
            )
            semantic_valid = (
                semantic_profile == expected_profile
                and algorithm == public_names[native_app]
                and (native_app in {"flash_scc", "hits"} or not has_self_loops)
                and (
                    native_app != "flash_scc"
                    or bool(manifest.get("directed", True))
                )
                and (
                    native_app not in undirected_only
                    or not bool(manifest.get("directed", True))
                )
                and (
                    native_app not in unweighted_only
                    or not bool(manifest.get("has_weights", False))
                )
                and (native_app != "k_shell" or int(parameters.get("k", 0)) >= 1)
                and (
                    native_app != "flash_ppr"
                    or bool(str(parameters.get("source") or "").strip())
                )
                and (
                    expected_profile != "graphscope_flash_approx_v1"
                    or not bool(parameters.get("strict_semantics", False))
                )
            )
        if not semantic_valid:
            raise HTTPException(status_code=400, detail="GraphScope 请求不满足已批准的语义配置")
        result_root = self._managed_path(
            payload.get("result_root"), artifact_root, must_exist=True
        )
        work_root = self._managed_path(
            payload.get("work_root"), artifact_root, must_exist=True
        )
        if not result_root.is_dir() or not work_root.is_dir():
            raise HTTPException(status_code=400, detail="GraphScope 输出目录不存在")
        checked = dict(payload)
        checked.update(
            {
                "manifest_path": str(manifest_path),
                "result_root": str(result_root),
                "work_root": str(work_root),
            }
        )
        return checked

    def create(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if _RUN_ID.fullmatch(run_id) is None:
            raise HTTPException(status_code=400, detail="run_id 格式非法")
        checked = self.validate_payload(payload)
        digest = self._digest(checked)
        with self._lock:
            self._refresh_locked()
            existing = self._runs.get(run_id)
            if existing is not None:
                if existing.digest != digest:
                    raise HTTPException(status_code=409, detail="run_id 已绑定其他请求")
                existing.last_heartbeat = time.monotonic()
                return self._view_locked(existing)
            active = sum(
                run.status in {"queued", "running", "cancelling"}
                for run in self._runs.values()
            )
            if active >= self.max_active:
                raise HTTPException(status_code=429, detail="GraphScope 适配器当前无空闲执行槽")
            messages = self._context.Queue()
            process = self._context.Process(
                target=_worker,
                args=(checked, messages),
                name=f"igraph-graphscope-{run_id[:48]}",
            )
            run = Run(run_id=run_id, digest=digest, process=process, messages=messages)
            self._runs[run_id] = run
            try:
                process.start()
            except BaseException:
                self._runs.pop(run_id, None)
                messages.close()
                raise
            return self._view_locked(run)

    def heartbeat(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._required_locked(run_id)
            run.last_heartbeat = time.monotonic()
            self._refresh_one_locked(run)
            return self._view_locked(run)

    def get(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._required_locked(run_id)
            self._refresh_one_locked(run)
            return self._view_locked(run)

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._required_locked(run_id)
            self._refresh_one_locked(run)
            if run.status not in {"succeeded", "failed", "cancelled"}:
                run.status = "cancelling"
                run.message = "正在取消 GraphScope 任务"
                self._terminate_locked(run)
                run.status = "cancelled"
                run.progress = min(run.progress, 0.90)
                run.message = "GraphScope 任务已取消"
                run.completed_at = time.monotonic()
            return self._view_locked(run)

    def sweep(self) -> None:
        with self._lock:
            self._refresh_locked()

    def _required_locked(self, run_id: str) -> Run:
        run = self._runs.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="GraphScope run_id 不存在")
        return run

    def _refresh_locked(self) -> None:
        now = time.monotonic()
        for run in list(self._runs.values()):
            self._refresh_one_locked(run)
            if (
                run.status in {"queued", "running", "cancelling"}
                and now - run.last_heartbeat > self.lease_seconds
            ):
                self._terminate_locked(run)
                run.status = "cancelled"
                run.message = "GraphScope 控制租约过期，任务已回收"
                run.error = "remote lease expired"
                run.completed_at = now
            if run.completed_at is not None and now - run.completed_at > self.retention_seconds:
                self._terminate_locked(run)
                run.messages.close()
                try:
                    run.process.close()
                except ValueError:
                    pass
                self._runs.pop(run.run_id, None)

    def _refresh_one_locked(self, run: Run) -> None:
        while True:
            try:
                item = run.messages.get_nowait()
            except queue.Empty:
                break
            kind = item.get("type")
            if kind == "running":
                run.status = "running"
                run.progress = 0.20
                run.message = "GraphScope 原生算法执行中"
            elif kind == "result":
                run.result = dict(item.get("result") or {})
                run.status = "succeeded"
                run.progress = 0.90
                run.message = "GraphScope 原生算法执行完成"
                run.completed_at = time.monotonic()
            elif kind == "error":
                run.error = str(item.get("error") or item.get("traceback") or "unknown error")
                run.status = "failed"
                run.message = "GraphScope 原生算法执行失败"
                run.completed_at = time.monotonic()
        if not run.process.is_alive() and run.status in {"queued", "running", "cancelling"}:
            run.process.join(timeout=0.1)
            # Give a just-flushed queue message one more opportunity.
            try:
                item = run.messages.get(timeout=0.05)
            except queue.Empty:
                item = None
            if item is not None:
                kind = item.get("type")
                if kind == "result":
                    run.result = dict(item.get("result") or {})
                    run.status = "succeeded"
                    run.progress = 0.90
                    run.message = "GraphScope 原生算法执行完成"
                elif kind == "error":
                    run.error = str(item.get("error") or "unknown error")
                    run.status = "failed"
                    run.message = "GraphScope 原生算法执行失败"
            if run.status in {"queued", "running", "cancelling"}:
                run.status = "failed"
                run.error = f"GraphScope 执行进程异常退出(exitcode={run.process.exitcode})"
                run.message = "GraphScope 原生算法执行失败"
            run.completed_at = time.monotonic()
        if not run.process.is_alive():
            self._terminate_locked(run)

    @staticmethod
    def _terminate_locked(run: Run) -> None:
        process = run.process
        pid = process.pid
        group_alive = False
        if os.name == "posix" and pid:
            try:
                group_alive = os.getpgid(pid) == pid
            except ProcessLookupError:
                try:
                    os.killpg(pid, 0)
                    group_alive = True
                except ProcessLookupError:
                    group_alive = False
                except PermissionError:
                    group_alive = True
        if group_alive and pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif process.is_alive():
            process.terminate()
        process.join(timeout=2)
        if group_alive and pid:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                group_alive = False
            except PermissionError:
                group_alive = True
        if group_alive and pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.is_alive():
            try:
                process.kill()
            except AttributeError:  # pragma: no cover - old Python fallback
                process.terminate()
        process.join(timeout=1)

    @staticmethod
    def _view_locked(run: Run) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": run.run_id,
            "status": run.status,
            "progress": run.progress,
            "message": run.message,
        }
        if run.error:
            payload["error"] = run.error
        if run.result is not None:
            payload["result"] = run.result
        return payload


registry = RunRegistry()
app = FastAPI(title="iGraph GraphScope Adapter", version=ADAPTER_VERSION)


def _authenticate(value: str | None) -> None:
    expected = os.getenv("IGRAPH_CONTROL_SERVICE_KEY", "")
    if not expected or value is None or not hmac.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="invalid service credential")


@app.get("/health")
def health() -> dict[str, Any]:
    registry.sweep()
    engine_path = Path(os.getenv("GRAPHSCOPE_HOME", "")) / "bin" / "grape_engine"
    return {
        "status": "ok",
        "adapter_version": ADAPTER_VERSION,
        "graphscope_available": importlib.util.find_spec("graphscope") is not None,
        "engine_available": engine_path.is_file() and os.access(engine_path, os.X_OK),
        "toolchain_available": all(shutil.which(tool) for tool in ("cmake", "g++")),
    }


@app.get("/v1/capabilities")
def capabilities(x_service_key: str | None = Header(None, alias="X-iGraph-Service-Key")) -> dict[str, Any]:
    _authenticate(x_service_key)
    return {
        "protocol_version": _PROTOCOL,
        "native_apps": [
            "wcc",
            "pagerank_nx",
            "bfs",
            "sssp",
            "cdlp",
            "louvain",
            "clustering",
            "triangles",
            "k_core",
            "eigenvector_centrality",
            "katz_centrality",
            "voterank",
            "degree_centrality",
            "degree_assortativity_coefficient",
            "average_degree_connectivity",
            "average_shortest_path_length",
            "flash_betweenness",
            "flash_closeness",
            "flash_harmonic",
            "flash_scc",
            "flash_bcc",
            "flash_bridges",
            "flash_cut_point",
            "flash_msf",
            "flash_color",
            "flash_mm",
            "hits",
            "k_shell",
            "flash_ppr",
            "flash_diameter_approximation",
            "flash_fluid_community",
            "flash_densest_subgraph",
            "flash_maximal_independent_set",
            "flash_minimal_dominating_set",
            "flash_minimal_vertex_cover",
            "flash_minimal_edge_cover",
            "flash_triangle_counting",
            "flash_rectangle_counting",
            "flash_k_clique_counting",
            "flash_diamond_counting",
            "flash_tailed_triangle_counting",
            "flash_three_path_counting",
            "flash_cycle_plus_triangle_counting",
            "flash_cyclic_triangle_counting",
            "flash_acyclic_triangle_counting",
            "flash_in_plus_triangle_counting",
            "flash_out_plus_triangle_counting",
        ],
        "max_active_jobs": registry.max_active,
    }


@app.post("/v1/runs/{run_id}")
def create_run(
    run_id: str,
    payload: dict[str, Any] = Body(...),
    x_service_key: str | None = Header(None, alias="X-iGraph-Service-Key"),
) -> dict[str, Any]:
    _authenticate(x_service_key)
    return registry.create(run_id, payload)


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str, x_service_key: str | None = Header(None, alias="X-iGraph-Service-Key")) -> dict[str, Any]:
    _authenticate(x_service_key)
    return registry.get(run_id)


@app.post("/v1/runs/{run_id}/heartbeat")
def heartbeat_run(run_id: str, x_service_key: str | None = Header(None, alias="X-iGraph-Service-Key")) -> dict[str, Any]:
    _authenticate(x_service_key)
    return registry.heartbeat(run_id)


@app.delete("/v1/runs/{run_id}")
def cancel_run(run_id: str, x_service_key: str | None = Header(None, alias="X-iGraph-Service-Key")) -> dict[str, Any]:
    _authenticate(x_service_key)
    return registry.cancel(run_id)


def _sweeper() -> None:
    while True:
        time.sleep(2)
        registry.sweep()


if multiprocessing.current_process().name == "MainProcess":
    threading.Thread(
        target=_sweeper, name="graphscope-run-sweeper", daemon=True
    ).start()


def main() -> None:
    uvicorn.run(
        app,
        host=os.getenv("GRAPHSCOPE_ADAPTER_HOST", "0.0.0.0"),
        port=int(os.getenv("GRAPHSCOPE_ADAPTER_PORT", "21113")),
        workers=1,
    )


if __name__ == "__main__":
    main()

"""Application-facing facade for the durable large-graph path."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import graph_engine.paths as data_paths

from .catalog import (
    LargeGraphCatalog,
    ScopeDeletionInProgress,
    SnapshotUnavailable,
    project_result_cache_key,
    validate_artifact_job_binding,
)
from .backend_planner import (
    BackendSelectionError,
    configured_graphscope_modes,
    resolve_backend_plan,
)
from .compatibility import GRAPHSCOPE_ONLY_ALGORITHM_NAMES
from .contracts import BackendPlan
from .contracts import DataQualityPolicy
from .durability import remove_tree_and_fsync
from .integrity import (
    ManifestBindingError,
    validate_artifact_manifest,
    validate_snapshot_manifest,
)
from .preflight import detect_delimiter
from .router import ComputeRouter, ResourceManager
from .runtime import DurableLargeGraphRuntime


_ARTIFACT_RESULT_FILE_PREVIEW_LIMIT = 20
_SERVER_DATASET_USER_ID = "__server_datasets__"


class LargeGraphService:
    """Validate API requests and keep worker internals out of the control plane."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        embedded_worker: bool | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else data_paths.data_root() / "large_graph"
        self.catalog = LargeGraphCatalog(self.root / "catalog.sqlite3")
        self.resources = ResourceManager(self.root)
        self.router = ComputeRouter(self.resources)
        self.runtime = DurableLargeGraphRuntime(
            self.root, self.catalog, resource_manager=self.resources
        )
        self._server_dataset_lock = threading.RLock()
        if embedded_worker is None:
            raw = os.getenv("LARGE_GRAPH_EMBEDDED_WORKER", "true").strip().lower()
            embedded_worker = raw not in {"0", "false", "no", "off"}
        self.embedded_worker = bool(embedded_worker)

    @staticmethod
    def _scope_component(user_id: str, session_id: str) -> str:
        # Sanitisation is lossy (and ``a__b,c`` used to collide with
        # ``a,b__c``), so retain a stable digest of the original tuple.  The
        # catalog still owns the exact tenant identity; this component only
        # makes its private filesystem namespace injective in practice.
        user = data_paths.safe_path_component(user_id, "_anonymous")
        session = data_paths.safe_path_component(session_id, "_session")
        identity = json.dumps(
            [str(user_id), str(session_id)],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()[:16]
        return f"{user}__{session}__{digest}"

    @staticmethod
    def _detect_delimiter(source: Path) -> str:
        return detect_delimiter(source)

    @staticmethod
    def _execution_engine(plan: BackendPlan) -> dict[str, Any]:
        if plan.selected_backend == "graphscope":
            return {
                "backend": "graphscope",
                "engine": "graphscope-analytical-engine",
                "engine_version": "0.29.0",
                "native_app": plan.native_app,
                "semantic_profile": plan.semantic_profile,
            }
        return {
            "backend": "legacy",
            "engine": ComputeRouter.ENGINE_NAME,
            "engine_version": ComputeRouter.ENGINE_VERSION,
            "native_app": None,
            "semantic_profile": plan.semantic_profile,
        }

    @staticmethod
    def _execution_binding(user_id: str, session_id: str) -> dict[str, Any]:
        from graph_data_service.execution_host import job_context

        ctx = job_context.get()
        if ctx is None or (ctx.user_id, ctx.session_id) != (user_id, session_id):
            return {}
        return {
            "job_id": ctx.job_id,
            "run_id": ctx.run_id,
            "generation": ctx.generation,
            "operation_id": ctx.operation_id,
        }

    def submit_preparation(
        self,
        *,
        user_id: str,
        session_id: str,
        graph_id: str,
        source_path: str,
        source_name: str,
        source_token: str,
        directed: bool,
        partitions: int | None,
        workers: int | None,
        delimiter: str,
        input_format: str,
        quality_policy: dict[str, Any] | None,
        graph_version: int | None = None,
        source_kind: str = "registered_upload",
        source_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = Path(source_path).resolve()
        if not source.is_file():
            return {"success": False, "code": "SOURCE_NOT_FOUND", "error": "上传文件不存在"}
        graph_id = data_paths.safe_path_component(graph_id or session_id, "graph")
        cpu_count = os.cpu_count() or 1
        partitions = max(
            1,
            min(
                int(
                    partitions
                    or os.getenv(
                        "LARGE_GRAPH_PARTITIONS", str(min(16, max(4, cpu_count * 2)))
                    )
                ),
                128,
            ),
        )
        workers = max(
            1,
            min(int(workers or min(8, cpu_count)), partitions, cpu_count),
        )
        delimiter = str(delimiter or "auto").strip().lower()
        if delimiter == "auto":
            delimiter = self._detect_delimiter(source)
        if delimiter not in {"whitespace", "comma", "tab"} and len(delimiter) != 1:
            raise ValueError("delimiter 必须是 auto、whitespace、comma、tab 或单字符")
        input_format = str(input_format or "edgelist").strip().lower()
        if input_format not in {"edgelist", "triples"}:
            raise ValueError("input_format 必须是 edgelist 或 triples")
        # The legacy edge-list loader preserves node spelling and NetworkX's
        # last duplicate edge value.  Keep the automatic large representation
        # topology-compatible; triple extraction intentionally keeps its
        # established NFKC/casefold + strongest-confidence defaults.
        if quality_policy is None and input_format == "edgelist":
            policy = DataQualityPolicy(
                node_normalization="exact",
                weight_conflict="last",
            )
        else:
            policy = DataQualityPolicy.from_dict(quality_policy)
        stat = source.stat()
        estimate = self.resources.estimate_preparation(
            stat.st_size, partitions, workers
        )
        admitted, reason, observed = self.resources.admit(estimate)
        if not admitted:
            return {
                "success": False,
                "code": "INSUFFICIENT_RESOURCES",
                "error": reason,
                "resource_estimate": observed,
            }
        source_kind = str(source_kind or "registered_upload").strip()
        if source_kind not in {"registered_upload", "server_dataset"}:
            raise ValueError("不支持的大图来源类型")
        source_ref = {
            "kind": source_kind,
            "filename": source_name,
            "file_token": source_token,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        if source_metadata:
            protected = {"kind", "filename", "file_token", "size", "mtime_ns"}
            source_ref.update(
                {
                    str(key): value
                    for key, value in source_metadata.items()
                    if str(key) not in protected
                }
            )
        request = {
            # source_path remains in the durable private job payload, but is
            # intentionally omitted from status/list API responses.
            "source_path": str(source),
            "source": source_ref,
            "user_scope": self._scope_component(user_id, session_id),
            "directed": bool(directed),
            "partitions": partitions,
            "workers": workers,
            "delimiter": delimiter,
            "input_format": input_format,
            "quality_policy": policy.to_dict(),
            "execution": self._execution_binding(user_id, session_id),
        }
        try:
            job_id, reserved_version = self.catalog.reserve_preparation(
                user_id=user_id,
                session_id=session_id,
                graph_id=graph_id,
                source=source_ref,
                policy_id=policy.policy_id,
                policy=policy.to_dict(),
                request=request,
                graph_version=graph_version,
            )
        except ScopeDeletionInProgress as exc:
            return {
                "success": False,
                "code": "LARGE_GRAPH_SESSION_DELETING",
                "error": str(exc),
            }
        except Exception as exc:
            if "UNIQUE constraint" in str(exc):
                return {
                    "success": False,
                    "code": "GRAPH_VERSION_EXISTS",
                    "error": "该 graph_version 已存在；图版本不可覆盖",
                }
            raise
        if self.embedded_worker:
            self.runtime.notify()
        return {
            "success": True,
            "async": True,
            "job_id": job_id,
            "status": "queued",
            "graph_id": graph_id,
            "graph_version": reserved_version,
            "data_quality_policy_id": policy.policy_id,
            "resource_estimate": observed,
            "message": "大图准备任务已持久化并进入串行队列。",
        }

    @staticmethod
    def _server_dataset_scope_ids(server_dataset_id: str) -> tuple[str, str]:
        digest = hashlib.sha256(str(server_dataset_id).encode("utf-8")).hexdigest()[:32]
        value = f"server-dataset-{digest}"
        return value, value

    def server_dataset_preparation_status(
        self, *, server_dataset_id: str, revision: str
    ) -> dict[str, Any]:
        """Return a bounded view of one server-owned cached snapshot."""

        source_session_id, source_graph_id = self._server_dataset_scope_ids(
            server_dataset_id
        )
        versions = self.catalog.list_versions(
            user_id=_SERVER_DATASET_USER_ID,
            session_id=source_session_id,
            graph_id=source_graph_id,
        )
        for version in reversed(versions):
            source = version.get("source")
            if not isinstance(source, dict) or (
                source.get("kind") != "server_dataset"
                or source.get("server_dataset_id") != server_dataset_id
                or source.get("revision") != revision
            ):
                continue
            graph_version = int(version["graph_version"])
            if version.get("status") == "published":
                snapshot = self.catalog.get_published_snapshot(
                    user_id=_SERVER_DATASET_USER_ID,
                    session_id=source_session_id,
                    graph_id=source_graph_id,
                    graph_version=graph_version,
                )
                if snapshot is None:
                    return {
                        "status": "failed",
                        "message": "服务器预处理目录记录缺少已发布快照",
                    }
                try:
                    _, manifest = validate_snapshot_manifest(
                        snapshot,
                        snapshot_root=self.root / "snapshots",
                        graph_id=source_graph_id,
                        graph_version=graph_version,
                        expected_user_id=_SERVER_DATASET_USER_ID,
                        expected_session_id=source_session_id,
                        expected_scope=self._scope_component(
                            _SERVER_DATASET_USER_ID, source_session_id
                        ),
                    )
                except ManifestBindingError:
                    return {
                        "status": "failed",
                        "message": "服务器预处理快照完整性校验失败",
                    }
                return {
                    "status": "ready",
                    "progress": 1.0,
                    "message": "服务器数据源已完成预处理",
                    "graph_version": graph_version,
                    "snapshot_id": snapshot["snapshot_id"],
                    "num_nodes": int(snapshot["node_count"]),
                    "num_edges": int(snapshot["edge_count"]),
                    "partitions": int(snapshot["partitions"]),
                    "directed": bool(snapshot["directed"]),
                    "has_weights": bool(snapshot["has_weights"]),
                    "storage_bytes": manifest.get("storage_bytes"),
                }
            job = self.catalog.get_prepare_job_for_version(
                user_id=_SERVER_DATASET_USER_ID,
                session_id=source_session_id,
                graph_id=source_graph_id,
                graph_version=graph_version,
            )
            if job is None:
                return {
                    "status": str(version.get("status") or "failed"),
                    "message": "服务器预处理版本缺少任务记录",
                }
            status = str(job.get("status") or "failed")
            return {
                "status": status,
                "progress": float(job.get("progress") or 0.0),
                "message": str(job.get("message") or ""),
                "job_id": str(job.get("job_id") or ""),
                "graph_version": graph_version,
                **(
                    {"error": str(job.get("error") or job.get("message") or "预处理失败")}
                    if status in {"failed", "cancelled"}
                    else {}
                ),
            }
        return {
            "status": "not_prepared",
            "progress": 0.0,
            "message": "等待服务器预处理",
        }

    def ensure_server_dataset_prepared(
        self,
        *,
        server_dataset_id: str,
        revision: str,
        source_path: str,
        source_name: str,
        relative_path: str,
        directed: bool = True,
    ) -> dict[str, Any]:
        """Idempotently enqueue one immutable server-level preparation."""

        with self._server_dataset_lock:
            current = self.server_dataset_preparation_status(
                server_dataset_id=server_dataset_id,
                revision=revision,
            )
            if current["status"] != "not_prepared":
                return {
                    "success": current["status"] not in {"failed", "cancelled"},
                    **current,
                }
            source_session_id, source_graph_id = self._server_dataset_scope_ids(
                server_dataset_id
            )
            submitted = self.submit_preparation(
                user_id=_SERVER_DATASET_USER_ID,
                session_id=source_session_id,
                graph_id=source_graph_id,
                source_path=source_path,
                source_name=source_name,
                source_token=f"{server_dataset_id}:{revision}",
                directed=directed,
                partitions=None,
                workers=None,
                delimiter="auto",
                input_format="edgelist",
                quality_policy=None,
                source_kind="server_dataset",
                source_metadata={
                    "server_dataset_id": server_dataset_id,
                    "revision": revision,
                    "relative_path": relative_path,
                    "directed": bool(directed),
                },
            )
            return submitted

    def attach_server_dataset_snapshot(
        self,
        *,
        user_id: str,
        session_id: str,
        graph_id: str,
        graph_version: int,
        server_dataset_id: str,
        revision: str,
    ) -> dict[str, Any]:
        """Bind a ready server snapshot as a session-local copy-on-write base."""

        status = self.server_dataset_preparation_status(
            server_dataset_id=server_dataset_id,
            revision=revision,
        )
        if status.get("status") != "ready":
            return {
                "success": False,
                "code": "SERVER_DATASET_NOT_READY",
                "error": status.get("error") or status.get("message") or "服务器数据源尚未完成预处理",
                "preparation": status,
            }
        source_session_id, source_graph_id = self._server_dataset_scope_ids(
            server_dataset_id
        )
        try:
            attached = self.catalog.attach_published_server_snapshot(
                user_id=user_id,
                session_id=session_id,
                graph_id=graph_id,
                graph_version=int(graph_version),
                source_user_id=_SERVER_DATASET_USER_ID,
                source_session_id=source_session_id,
                source_graph_id=source_graph_id,
                source_graph_version=int(status["graph_version"]),
                server_dataset_id=server_dataset_id,
                revision=revision,
            )
        except ScopeDeletionInProgress as exc:
            return {
                "success": False,
                "code": "LARGE_GRAPH_SESSION_DELETING",
                "error": str(exc),
            }
        except SnapshotUnavailable as exc:
            return {
                "success": False,
                "code": "SERVER_DATASET_NOT_READY",
                "error": str(exc),
            }
        return {
            "success": True,
            "async": False,
            "already_prepared": True,
            "server_snapshot_reused": True,
            "representation": "partitioned_snapshot",
            "format_detected": "edgelist",
            "graph_id": graph_id,
            "graph_version": int(attached["target_graph_version"]),
            "num_nodes": int(attached["node_count"]),
            "num_edges": int(attached["edge_count"]),
            "summary": {
                "node_count": int(attached["node_count"]),
                "edge_count": int(attached["edge_count"]),
                "partitions": int(attached["partitions"]),
                "directed": bool(attached["directed"]),
                "has_weights": bool(attached["has_weights"]),
                "storage_bytes": status.get("storage_bytes"),
                "server_snapshot_reused": True,
            },
            "message": "已为当前会话绑定服务器预处理快照，无需重新解析和分区。",
        }

    def submit_compute(
        self,
        *,
        user_id: str,
        session_id: str,
        graph_id: str,
        graph_version: int,
        algorithm: str,
        parameters: dict[str, Any] | None,
        backend: str | None = None,
        workers: int | None = None,
        minimum_derived_version: int | None = None,
    ) -> dict[str, Any]:
        graph_id = data_paths.safe_path_component(graph_id or session_id, "graph")
        snapshot = self.catalog.get_published_snapshot(
            user_id=user_id,
            session_id=session_id,
            graph_id=graph_id,
            graph_version=int(graph_version),
        )
        algorithm = str(algorithm or "").strip().lower()
        params = dict(parameters or {})
        manifest_payload: dict[str, Any] | None = None
        snapshot_scope: str | None = None
        shared_snapshot_binding: dict[str, Any] | None = None
        if snapshot is not None:
            # The catalog lookup scopes the row to the caller. Independently
            # bind that row to the immutable manifest and all of its shards
            # before a durable job can capture the snapshot reference.
            owner_user_id = str(snapshot.get("user_id") or "")
            owner_session_id = str(snapshot.get("session_id") or "")
            owner_graph_id = str(snapshot.get("graph_id") or "")
            owner_graph_version = int(snapshot.get("graph_version") or 0)
            version_source = snapshot.get("version_source")
            shared_server_snapshot = (
                isinstance(version_source, dict)
                and version_source.get("kind") == "server_dataset_snapshot"
                and version_source.get("source_snapshot_id")
                == snapshot.get("snapshot_id")
                and version_source.get("source_user_id") == owner_user_id
                and version_source.get("source_session_id") == owner_session_id
                and version_source.get("source_graph_id") == owner_graph_id
                and int(version_source.get("source_graph_version") or 0)
                == owner_graph_version
                and owner_user_id == _SERVER_DATASET_USER_ID
            )
            locally_owned = (
                owner_user_id == user_id and owner_session_id == session_id
            )
            if not locally_owned and not shared_server_snapshot:
                return {
                    "success": False,
                    "code": "SNAPSHOT_MANIFEST_INVALID",
                    "error": "快照目录身份与当前会话不一致，请重新准备该图版本。",
                }
            if shared_server_snapshot:
                shared_snapshot_binding = dict(version_source)
            snapshot_scope = self._scope_component(
                owner_user_id, owner_session_id
            )
            try:
                _, manifest_payload = validate_snapshot_manifest(
                    snapshot,
                    snapshot_root=self.root / "snapshots",
                    graph_id=owner_graph_id,
                    graph_version=owner_graph_version,
                    expected_user_id=owner_user_id,
                    expected_session_id=owner_session_id,
                    expected_scope=snapshot_scope,
                )
            except ManifestBindingError:
                return {
                    "success": False,
                    "code": "SNAPSHOT_MANIFEST_INVALID",
                    "error": "快照清单与目录或分区文件不一致，请重新准备该图版本。",
                }
        if snapshot is not None and str(
            (snapshot.get("policy") or {}).get("edge_key", "endpoints")
        ) != "endpoints":
            return {
                "success": False,
                "code": "MULTIGRAPH_SEMANTICS_UNSUPPORTED",
                "error": (
                    "当前算法目录遵循 GraphData/NetworkX 简单图语义；"
                    "请以 quality_policy.edge_key=endpoints 重新准备快照。"
                ),
            }
        degree_fast_paths = {"degree", "in_degree", "out_degree", "degree_centrality"}
        node_count = (
            int(snapshot.get("node_count", 0))
            if snapshot
            else self.router.large_node_threshold
        )
        edge_count = (
            int(snapshot.get("edge_count", 0))
            if snapshot
            else self.router.large_edge_threshold
        )
        centrality_type = str(params.get("centrality_type", "degree")).strip().lower()
        if (
            snapshot is not None
            and algorithm == "node_centrality"
            and centrality_type in {"betweenness", "load", "percolation"}
            and "sample_sources" not in params
        ):
            params["sample_sources"] = self.resources.default_brandes_sample_sources(
                node_count, edge_count
            )
        partitions = int(snapshot.get("partitions", 1)) if snapshot else 1
        cpu_count = os.cpu_count() or 1
        workers = max(
            1,
            min(int(workers or min(8, cpu_count)), partitions, cpu_count, 8),
        )
        decision = self.router.route(
            operation_scope="global",
            node_count=node_count,
            edge_count=edge_count,
            algorithm=algorithm,
            directed=bool(snapshot.get("directed", True)) if snapshot else True,
            weighted=bool(params.get("use_weight", False)),
            snapshot_available=snapshot is not None,
            partitions=partitions,
            workers=workers,
            force_large=True,
            parameters=params,
            snapshot_storage_bytes=(manifest_payload or {}).get("storage_bytes"),
        )
        if not decision.accepted or decision.path != "large_graph":
            return {"success": False, "error": decision.reason, **decision.to_dict()}
        assert snapshot is not None
        assert manifest_payload is not None
        try:
            backend_plan = resolve_backend_plan(
                requested_backend=backend,
                algorithm=algorithm,
                parameters=params,
                directed=bool(snapshot.get("directed", True)),
                has_weights=bool(snapshot.get("has_weights", False)),
                has_self_loops=(
                    int((manifest_payload.get("quality") or {}).get("self_loops_seen", 0))
                    > int((manifest_payload.get("quality") or {}).get("self_loops_skipped", 0))
                ),
            )
        except BackendSelectionError as exc:
            return {
                "success": False,
                "code": exc.code,
                "error": exc.message,
                "requested_backend": str(backend or "").strip().lower() or None,
            }
        route_decision = decision.to_dict()
        route_decision["execution_engine"] = self._execution_engine(backend_plan)
        if (
            backend_plan.selected_backend == "legacy"
            and algorithm not in degree_fast_paths
            and manifest_payload.get("adjacency_format")
            != "igraph-sharded-sqlite-adjacency-v1"
        ):
            return {
                "success": False,
                "code": "SNAPSHOT_ADJACENCY_REQUIRED",
                "error": (
                    "该快照采用 GraphScope 紧凑存储，未预建本地 SQLite 邻接索引；"
                    "请改用已适配的 GraphScope 模式，或设置 "
                    "LARGE_GRAPH_PRECOMPUTE_ADJACENCY=true 后重新准备该图版本。"
                ),
                "backend_plan": backend_plan.to_dict(),
            }
        # The catalog schema intentionally keeps snapshot rows compact. Carry
        # the integrity-checked byte accounting in the immutable durable job
        # request so dequeue-time admission uses the same real shard sizes.
        request_snapshot = {
            **snapshot,
            "storage_bytes": manifest_payload["storage_bytes"],
        }
        request = {
            "user_scope": self._scope_component(user_id, session_id),
            "snapshot": request_snapshot,
            "snapshot_scope": snapshot_scope,
            "snapshot_binding": shared_snapshot_binding,
            "algorithm": algorithm,
            "parameters": params,
            "backend_plan": backend_plan.to_dict(),
            "workers": workers,
            "route_decision": route_decision,
            "execution": self._execution_binding(user_id, session_id),
        }
        creates_derived_version = bool(
            algorithm == "subgraph_extraction"
            and params.get("create_version", True)
        )
        project_id = self.catalog.get_session_project(
            user_id=user_id, session_id=session_id
        )
        if project_id and not creates_derived_version:
            cache_key = project_result_cache_key(request)
            if cache_key:
                request["project_result"] = {
                    "project_id": project_id,
                    "cache_key": cache_key,
                }
        try:
            job_id, derived_version, reused = self.catalog.enqueue_compute(
                user_id=user_id,
                session_id=session_id,
                graph_id=graph_id,
                graph_version=int(graph_version),
                request=request,
                minimum_derived_version=minimum_derived_version,
            )
        except ScopeDeletionInProgress as exc:
            return {
                "success": False,
                "code": "LARGE_GRAPH_SESSION_DELETING",
                "error": str(exc),
            }
        except SnapshotUnavailable as exc:
            return {
                "success": False,
                "code": "LARGE_GRAPH_SNAPSHOT_REQUIRED",
                "error": str(exc),
            }
        reused_job = self.catalog.get_job_for_session(
            job_id, user_id=user_id, session_id=session_id
        )
        reused_status = str((reused_job or {}).get("status") or "queued")
        reused_from_session = str(
            (reused_job or {}).get("session_id") or ""
        )
        if reused and reused_status == "done":
            cached = self.catalog.get_job_result_for_session(
                job_id, user_id=user_id, session_id=session_id
            )
            if cached is not None:
                cached.pop("job_id", None)
                source_graph_id = cached.get("graph_id")
                source_graph_version = cached.get("graph_version")
                return {
                    **cached,
                    "success": True,
                    "async": False,
                    "graph_id": graph_id,
                    "graph_version": int(graph_version),
                    "source_job_id": job_id,
                    "source_graph_id": source_graph_id,
                    "source_graph_version": source_graph_version,
                    "project_result_reused": True,
                    "source_session_id": reused_from_session,
                    "backend_plan": backend_plan.to_dict(),
                    "route_decision": route_decision,
                    "message": "已复用同项目、同数据版本和同参数的已有计算结果。",
                }
        if self.embedded_worker and not reused:
            self.runtime.notify()
        return {
            "success": True,
            "async": True,
            "job_id": job_id,
            "status": reused_status,
            "graph_id": graph_id,
            "graph_version": int(graph_version),
            **(
                {"new_graph_version": int(derived_version)}
                if derived_version is not None
                else {}
            ),
            "algorithm": algorithm,
            "project_result_reused": reused,
            **(
                {"source_session_id": reused_from_session}
                if reused and reused_from_session != session_id
                else {}
            ),
            "backend_plan": backend_plan.to_dict(),
            "route_decision": route_decision,
            "message": (
                "已复用同项目中正在执行的等价计算作业。"
                if reused
                else "大图全局算法已持久化并进入串行队列。"
            ),
        }

    def job_status(self, job_id: str, *, user_id: str, session_id: str) -> dict[str, Any] | None:
        job = self.catalog.get_job_for_session(
            job_id, user_id=user_id, session_id=session_id
        )
        if job is None:
            return None
        # Status is a bounded control-plane view: no source path, request body,
        # full result partitions or artifact contents are embedded here.
        result = {
            key: job.get(key)
            for key in (
                "job_id", "graph_id", "graph_version", "kind", "status", "stage",
                "progress", "message", "summary", "artifact_id", "error",
                "created_at", "updated_at", "started_at", "finished_at",
            )
        } | {
            # Preserve the existing progress-event contract consumed by the
            # Agent/backend event mapper without exposing private job input.
            "tool_name": (
                "prepare_large_graph" if job["kind"] == "prepare"
                else "run_large_graph_algorithm"
            ),
            # Runtime progress is durably stored in the bounded summary_json
            # column.  Expose the same object under the event contract's
            # ``detail`` name so polling clients do not silently lose it.
            "detail": job.get("summary") or {},
        }
        if str(job.get("session_id") or "") != session_id:
            result["project_result_reused"] = True
            result["source_session_id"] = str(job.get("session_id") or "")
        request = job.get("request")
        raw_plan = request.get("backend_plan") if isinstance(request, dict) else None
        try:
            result["execution_engine"] = self._execution_engine(
                BackendPlan.from_dict(raw_plan if isinstance(raw_plan, dict) else None)
            )
        except (TypeError, ValueError):
            pass
        return result

    def job_result(self, job_id: str, *, user_id: str, session_id: str) -> dict[str, Any] | None:
        return self.catalog.get_job_result_for_session(
            job_id, user_id=user_id, session_id=session_id
        )

    def cancel(self, job_id: str, *, user_id: str, session_id: str) -> bool:
        return self.runtime.cancel(job_id, user_id=user_id, session_id=session_id)

    def job_control(self, job_id: str, *, user_id: str, session_id: str) -> dict[str, Any] | None:
        job = self.catalog.get_job(job_id, user_id=user_id, session_id=session_id)
        if job is None:
            return None
        terminal = job['status'] in {'done', 'failed', 'cancelled'}
        return {
            'job_id': job_id,
            'run_id': (job['request'].get('execution') or {}).get('run_id'),
            'run_status': job['status'],
            'execution_stopped': terminal,
            'terminal_persisted': terminal,
            'cancel_status': ('confirmed' if terminal else 'stopping') if job['cancel_requested'] else 'none',
            'pending_components': [] if terminal else [job_id],
        }

    def delete_session(
        self,
        *,
        user_id: str,
        session_id: str,
        wait_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Cancel a session's real workers, then purge its durable state.

        Catalog rows are retained if a worker does not acknowledge
        cancellation in time. This is deliberate: deleting its lease first
        would allow an untracked process to keep writing after session delete.
        """
        requested = self.catalog.request_cancel_scope(
            user_id=user_id, session_id=session_id
        )
        if requested and self.embedded_worker:
            self.runtime.notify()
        if wait_seconds is None:
            try:
                wait_seconds = float(
                    os.getenv("LARGE_GRAPH_DELETE_WAIT_SECONDS", "10")
                )
            except (TypeError, ValueError):
                wait_seconds = 10.0
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        active = self.catalog.active_scope_jobs(
            user_id=user_id, session_id=session_id
        )
        while active and time.monotonic() < deadline:
            time.sleep(0.05)
            active = self.catalog.active_scope_jobs(
                user_id=user_id, session_id=session_id
            )
        if active:
            return {
                "success": False,
                "code": "LARGE_GRAPH_SESSION_DELETE_PENDING",
                "error": "大图底层执行进程仍在终止，请稍后重试删除会话。",
                "active_job_ids": [str(row["job_id"]) for row in active],
            }

        # Keep the durable deletion fence and catalog references until managed
        # files are gone.  Otherwise another API replica can enqueue while this
        # method is still deleting files, and a crash leaves untraceable orphan
        # directories that a retry can no longer discover.
        records = self.catalog.scope_objects_for_deletion(
            user_id=user_id, session_id=session_id
        )
        removed = 0
        for kind, id_key, path_key, root_name, prefix in (
            ("snapshots", "snapshot_id", "manifest_path", "snapshots", "snap_"),
            ("artifacts", "artifact_id", "path", "artifacts", "artifact_"),
        ):
            allowed_root = (self.root / root_name).resolve()
            for record in records[kind]:
                object_id = str(record.get(id_key) or "")
                try:
                    manifest = Path(str(record.get(path_key) or "")).resolve()
                except (OSError, RuntimeError):
                    continue
                generated_dir = manifest.parent
                if (
                    object_id.startswith(prefix)
                    and generated_dir.name == object_id
                    and manifest.name == "manifest.json"
                    and generated_dir.is_relative_to(allowed_root)
                ):
                    try:
                        existed = os.path.lexists(generated_dir)
                        remove_tree_and_fsync(generated_dir)
                        if existed:
                            removed += 1
                    except (OSError, ValueError, shutil.Error) as exc:
                        return {
                            "success": False,
                            "code": "LARGE_GRAPH_SESSION_DELETE_FAILED",
                            "error": f"删除大图管理目录失败: {exc}",
                            "removed_objects": removed,
                        }
        # Artifacts are deleted before their parent jobs to satisfy the foreign
        # key, and the tombstone is released in the same final transaction.
        self.catalog.purge_scope(user_id=user_id, session_id=session_id)
        return {
            "success": True,
            "cancelled_jobs": len(requested),
            "removed_objects": removed,
        }

    def list_versions(self, *, user_id: str, session_id: str, graph_id: str) -> list[dict[str, Any]]:
        graph_id = data_paths.safe_path_component(graph_id or session_id, "graph")
        versions = self.catalog.list_versions(
            user_id=user_id, session_id=session_id, graph_id=graph_id
        )
        # Internal manifest paths are available through the snapshot/artifact
        # reference APIs, not the version listing shown to an Agent.
        for version in versions:
            version.pop("manifest_path", None)
        return versions

    def list_session_scopes(self) -> list[dict[str, Any]]:
        """List durable session identities known to the large-graph catalog."""
        return self.catalog.list_session_scopes()

    def bind_session_project(
        self,
        *,
        user_id: str,
        session_id: str,
        project_id: str | None,
    ) -> None:
        """Bind a Graph Engine session to its trusted Backend project."""
        self.catalog.bind_session_project(
            user_id=user_id,
            session_id=session_id,
            project_id=project_id,
        )

    def get_session_scope(
        self, *, user_id: str, session_id: str
    ) -> dict[str, Any] | None:
        """Return a durable session identity known to the catalog."""
        return self.catalog.get_session_scope(
            user_id=user_id, session_id=session_id
        )

    def version_matches_small_binding(
        self,
        *,
        user_id: str,
        session_id: str,
        graph_id: str,
        graph_version: int,
        small_source: dict[str, Any],
        directed: bool,
    ) -> bool:
        """Verify that a large representation is the same logical graph."""
        expected_token = str(small_source.get("file_token") or "")
        expected_digest = str(small_source.get("content_sha256") or "")
        if not expected_token or not expected_digest:
            return False
        for version in self.list_versions(
            user_id=user_id, session_id=session_id, graph_id=graph_id
        ):
            if int(version.get("graph_version", -1)) != int(graph_version):
                continue
            source = version.get("source") if isinstance(version.get("source"), dict) else {}
            quality = version.get("quality") if isinstance(version.get("quality"), dict) else {}
            policy = version.get("policy") if isinstance(version.get("policy"), dict) else {}
            return bool(
                version.get("status") == "published"
                and str(source.get("file_token") or "") == expected_token
                and str(quality.get("source_content_fingerprint") or "")
                == expected_digest
                and bool(version.get("directed")) == bool(directed)
                and str(policy.get("edge_key", "endpoints")) == "endpoints"
                and str(policy.get("node_normalization", "")) == "exact"
                and str(policy.get("weight_conflict", "")) == "last"
            )
        return False

    def get_artifact(
        self,
        artifact_id: str,
        *,
        user_id: str,
        session_id: str,
        include_preview: bool = True,
        include_path: bool = False,
    ) -> dict[str, Any] | None:
        validated = self._validated_artifact(
            artifact_id, user_id=user_id, session_id=session_id
        )
        if validated is None:
            return None
        artifact, manifest_path, manifest = validated
        # Return the canonical path which was actually validated, rather than
        # echoing a non-normalized catalog spelling to an internal caller.
        artifact["path"] = str(manifest_path)
        # The complete file list remains in the immutable manifest.  A small,
        # indexed window makes multi-dataset artifacts discoverable without
        # copying a potentially large partition list into the control plane.
        result_files = manifest["result_files"]
        result_file_count = len(result_files)
        artifact["result_file_count"] = result_file_count
        artifact["result_files_preview"] = [
            {"index": index, "path": relative}
            for index, relative in enumerate(
                result_files[:_ARTIFACT_RESULT_FILE_PREVIEW_LIMIT]
            )
        ]
        artifact["result_files_truncated"] = (
            result_file_count > _ARTIFACT_RESULT_FILE_PREVIEW_LIMIT
        )
        artifact["download_index_range"] = (
            {"start": 0, "end": result_file_count - 1}
            if result_file_count
            else None
        )
        if not include_preview:
            artifact.pop("preview", None)
        if not include_path:
            # Filesystem layout is private worker state. Public callers use
            # the authenticated partition download endpoint instead.
            artifact.pop("path", None)
        return artifact

    def artifact_partition_path(
        self,
        artifact_id: str,
        partition: int,
        *,
        user_id: str,
        session_id: str,
    ) -> Path | None:
        """Resolve one result shard from the same manifest validation pass.

        Keeping lookup, completed-job binding and partition selection together
        prevents the HTTP layer from reparsing a manifest after it was checked.
        """
        if isinstance(partition, bool) or not isinstance(partition, int) or partition < 0:
            return None
        validated = self._validated_artifact(
            artifact_id, user_id=user_id, session_id=session_id
        )
        if validated is None:
            return None
        _, manifest_path, manifest = validated
        result_files = manifest.get("result_files")
        if not isinstance(result_files, list) or partition >= len(result_files):
            return None
        relative = result_files[partition]
        if not isinstance(relative, str):  # already enforced by manifest validation
            return None
        candidate = manifest_path.parent / relative
        cursor = manifest_path.parent
        try:
            for part in Path(relative).parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    return None
            result_path = candidate.resolve(strict=True)
            result_path.relative_to(manifest_path.parent)
        except (OSError, RuntimeError, ValueError):
            return None
        return result_path if result_path.is_file() else None

    def _validated_artifact(
        self,
        artifact_id: str,
        *,
        user_id: str,
        session_id: str,
    ) -> tuple[dict[str, Any], Path, dict[str, Any]] | None:
        artifact = self.catalog.get_artifact_for_session(
            artifact_id, user_id=user_id, session_id=session_id
        )
        if artifact is None:
            return None
        job = self.catalog.get_job(
            str(artifact.get("job_id") or ""),
            user_id=str(artifact.get("user_id") or user_id),
            session_id=str(artifact.get("session_id") or session_id),
        )
        if job is None or job.get("status") != "done":
            return None
        request = job.get("request")
        base_scope = request.get("user_scope") if isinstance(request, dict) else None
        lease_id = job.get("lease_id")
        if (
            not isinstance(base_scope, str)
            or not base_scope
            or not isinstance(lease_id, str)
            or not lease_id
        ):
            return None
        try:
            validate_artifact_job_binding(job, artifact)
            manifest_path, manifest = validate_artifact_manifest(
                artifact,
                artifact_root=self.root / "artifacts",
                expected_scope=f"{base_scope}__{lease_id}",
            )
        except (ManifestBindingError, TypeError, ValueError):
            # A catalog row is not sufficient authority to disclose or
            # download a filesystem object whose immutable manifest has been
            # moved, rebound or modified.
            return None
        return artifact, manifest_path, manifest

    def capabilities(self) -> list[dict[str, Any]]:
        graphscope_modes = configured_graphscope_modes()
        graphscope_only = set(GRAPHSCOPE_ONLY_ALGORITHM_NAMES)
        configured_backend = os.getenv("LARGE_GRAPH_BACKEND", "legacy").strip().lower()
        rows = self.router.capability_catalog()
        for row in rows:
            algorithm = str(row["algorithm"])
            native_modes = list(graphscope_modes.get(algorithm, ()))
            backend_routes: list[dict[str, Any]] = []
            if native_modes:
                backend_routes.append(
                    {
                        "backend": "graphscope",
                        "engine": "graphscope-analytical-engine",
                        "engine_version": "0.29.0",
                        "modes": native_modes,
                        "parallel_strategy": "graphscope-native-fragment-parallel",
                    }
                )
            if algorithm not in graphscope_only:
                backend_routes.append(
                    {
                        "backend": "legacy",
                        "engine": row["engine"],
                        "engine_version": row["engine_version"],
                        "modes": list(row.get("modes") or ()),
                        "parallel_strategy": row["parallel_strategy"],
                    }
                )
            row["available_backends"] = [route["backend"] for route in backend_routes]
            row["backend_routes"] = backend_routes
            if native_modes and configured_backend in {"auto", "graphscope"}:
                row["preferred_backend"] = "graphscope"
            elif algorithm not in graphscope_only:
                row["preferred_backend"] = "legacy"
            else:
                row["preferred_backend"] = None
        return rows

    def close(self) -> None:
        self.runtime.close()

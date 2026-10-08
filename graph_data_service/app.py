"""
GraphData FastAPI app factory.
"""

from __future__ import annotations

import asyncio
import anyio
import httpx
import hashlib
import json
import os
import shutil
import threading
import time
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import numpy as np
from pydantic import BaseModel, Field, ConfigDict

import graph_engine.paths as _dp
from graph_engine.observability import close_service_loggers, get_service_logger
from graph_engine.tool_execution import execution_policy
from graph_engine.model_support import DEFAULT_MODEL, ProviderGateway, normalize_model_name, provider_for_model
from graph_engine.extraction import FileFormatPreCheckSubAgent, TextGraphExtractionSubAgent
from graph_data_service.data_pipeline import GraphBuilder
from graph_data_service.dataset_registry import DatasetRegistry
from graph_data_service.reporting import build_analysis_report
from graph_data_service.tool_catalog import GRAPH_ML_TOOLS, REPORTING_TOOLS, get_all_public_tools
from graph_data_service.codec import (
    edge_type_values_for_graph,
    graph_ml_readiness_for_graph,
    graph_data_to_document,
    node_label_class_mapping_for_graph,
    node_label_values_for_graph,
    node_label_stats_for_graph,
    node_type_values_for_graph,
)
import concurrent.futures
from graph_data_service.compute import GraphCompute, NX_TOOL_NAMES, run_nx_tool
from graph_engine.finance_catalog import FINANCE_TOOL_NAMES
from graph_data_service.embedding import default_provider
from graph_data_service.graph_data import GraphData
from graph_data_service.graph_ml import GraphMLModule
from graph_data_service.graph_ml.datasets import graph_classification_sample_to_graph_data
from graph_data_service.graph_ml.schemas import LoadGraphClassificationGraphRequest
from graph_data_service.job_manager import JobManager
from graph_data_service.execution_host import GraphExecutionHost, JobContext, job_context
from graph_data_service.worker_contract import ISOLATED_DATA_TOOLS
from graph_data_service.async_work import run_sync
from graph_data_service.tool_scheduler import ToolResourceScheduler
from execution_protocol import controls_enabled, require_service, service_headers, validate_service_configuration
from graph_data_service.large_graph import LargeGraphService
from graph_data_service.large_graph.preflight import (
    probe_binary_archive,
    probe_structured_source,
)
from graph_data_service.rag import LightweightRAG
from graph_data_service.property_graph import UnsupportedPropertyGraphAppend, require_lossless_append
from graph_data_service.search import EdgeSearchRequest, EntitySearchIndex, NodeSearchRequest
from graph_data_service.session import GraphSession, UploadFileRecord
from construction_access import DATASET_TOOLS, SCOPE_HEADER
from graph_data_service.construction_access import ConstructionAccessClient
from graph_data_service.construction import ConstructionStore, ConstructionError, to_graph_data
from graph_data_service.storage.nebula_backend import (
    NebulaGraphRepository,
    NebulaRepositoryError,
)

# 数据操作工具名集合（写入 graph_data.log）
_DATA_TOOLS = {
    "load_graph", "load_graph_from_upload", "clear_graph", "undo_graph",
    "list_graph_versions", "load_graph_version", "discard_graph_version",
    "to_undirected", "get_graph_info", "list_datasets", "load_dataset",
    "save_as_dataset", "load_graph_from_text", "load_graph_from_triples",
    "upload_document", "nebula_status", "nebula_sync_graph",
    # Large-graph data preparation stays separate from GraphData/Nebula import.
    "prepare_large_graph", "list_large_graph_versions",
}

_GRAPH_ML_TOOL_NAMES = {spec.get("name", "").strip() for spec in GRAPH_ML_TOOLS}
_GRAPH_ML_HEAVY_TOOLS = {
    "load_graph_classification_dataset",
    "train",
    "infer",
}
_REPORTING_TOOL_NAMES = {spec.get("name", "").strip() for spec in REPORTING_TOOLS}
_NEBULA_QUERY_TOOLS = {
    "nebula_get_node_info",
    "nebula_get_neighbors",
    "nebula_shortest_path",
}


class LoadGraphTextReq(BaseModel):
    edge_text: str
    append: bool = False


class RegisterUploadReq(BaseModel):
    file_token: str
    original_filename: str
    saved_path: str


class LoadGraphUploadReq(BaseModel):
    filename: str
    append: bool = False
    directed: bool = True


class ServerDatasetPrepareReq(BaseModel):
    server_dataset_id: str = Field(min_length=1, max_length=128)
    revision: str = Field(min_length=64, max_length=64)
    relative_path: str = Field(min_length=1, max_length=1024)
    original_filename: str = Field(min_length=1, max_length=255)
    source_path: str = Field(min_length=1, max_length=4096)
    directed: bool = True


class ServerDatasetAttachReq(BaseModel):
    server_dataset_id: str = Field(min_length=1, max_length=128)
    revision: str = Field(min_length=64, max_length=64)


class ToolInvokeReq(BaseModel):
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ConstructionBuildReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_path: str | None = None
    source_filename: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class ConstructionSourceReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_path: str
    source_filename: str


class ConstructionOptimizeReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision: int = Field(strict=True, ge=1)
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=100)


class ConstructionSaveReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision: int = Field(strict=True, ge=1)
    operations: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class ConstructionPresetReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: Literal["finance", "tianlongbabu"]


class ConstructionImportReq(BaseModel):
    source_dataset_id: str = Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")


class ConstructionLoadReq(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(min_length=1)


class _NullTraceLogger:
    def log_trace_event(self, *_args, **_kwargs) -> None:
        return None


class _GraphMLSessionView:
    """Read-only session view used for bounded Nebula materialisation."""

    def __init__(self, graph_version: int, graph: GraphData) -> None:
        self.graph_version = int(graph_version)
        self._graph = graph

    def get_graph(self) -> GraphData:
        return self._graph


def _service_logger(session_id: str, service: str, user_id: str = ""):
    logs_dir = str(_dp.user_sessions_dir(user_id)) if user_id else None
    return get_service_logger(session_id, service, logs_dir=logs_dir)


def _sanitize_filename(filename: str) -> str:
    base = os.path.basename(filename or "upload.txt")
    cleaned = "".join(ch if ch.isalnum() or ch in {".", "_", "-"} else "_" for ch in base)
    return cleaned or "upload.txt"


def _graph_data_upload_root() -> str:
    root = os.getenv("GRAPH_DATA_UPLOAD_DIR", "uploads/graph_data")
    return os.path.abspath(root)


def _regular_file_bytes(root: Path) -> int:
    """Return managed upload usage without following directory symlinks."""
    if not root.exists():
        return 0
    total = 0
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = [
            name for name in directory_names
            if not (Path(current_root) / name).is_symlink()
        ]
        for name in file_names:
            path = Path(current_root) / name
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


def _registered_upload_path(saved_path: str, user_id: str = "") -> Path | None:
    """Resolve an internal upload reference without allowing arbitrary file reads.

    ``/uploads/register`` is used by the backend to hand project-owned files to
    Graph Engine. Treating ``saved_path`` as an unrestricted server path would
    let a caller feed configuration/source files into the graph pipeline.
    """
    try:
        resolved = Path(saved_path).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file():
        return None
    # Never grant a caller the whole shared data root: it contains every
    # user's project/dataset/session files. Authenticated uploads belong only
    # to that user's Graph Engine-managed upload tree; legacy anonymous mode
    # has its own isolated root.
    allowed_roots = (
        {_dp.user_uploads_dir(user_id).resolve()}
        if user_id
        else {Path(_graph_data_upload_root()).resolve()}
    )
    return resolved if any(resolved.is_relative_to(root) for root in allowed_roots) else None


def _registered_server_dataset_path(
    saved_path: str,
    *,
    relative_path: str,
    server_dataset_id: str,
    revision: str,
    directed: bool = True,
) -> Path | None:
    """Resolve a Backend-owned public source without granting broad file reads."""

    try:
        root = Path(
            os.getenv(
                "GRAPH_ENGINE_SERVER_DATASETS_DIR",
                str(_dp.data_root() / "server_datasets"),
            )
        ).resolve(strict=True)
        candidate = root.joinpath(*Path(relative_path).parts)
        if candidate.is_symlink():
            return None
        resolved = Path(saved_path).resolve(strict=True)
        file_stat = resolved.stat()
        actual_relative = resolved.relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file() or resolved != candidate.resolve():
        return None
    expected_id = "server-" + hashlib.sha256(
        actual_relative.encode("utf-8")
    ).hexdigest()
    revision_identity = (
        f"{actual_relative}\0{int(file_stat.st_size)}\0{int(file_stat.st_mtime_ns)}"
    )
    if not directed:
        revision_identity += "\0directed=false"
    expected_revision = hashlib.sha256(revision_identity.encode("utf-8")).hexdigest()
    if (
        actual_relative != relative_path
        or expected_id != server_dataset_id
        or expected_revision != revision
        or resolved.suffix.lower()
        not in {".txt", ".csv", ".tsv", ".edgelist", ".dat"}
    ):
        return None
    return resolved


def _version_matches_upload(
    session: GraphSession, graph_version: int, upload: UploadFileRecord
) -> bool:
    """Only reuse a logical version when its recorded source is identical."""
    source = session.version_source(graph_version)
    if not source or str(source.get("file_token") or "") != str(upload.file_token):
        return False
    try:
        path = Path(upload.saved_path)
        stat = path.stat()
    except OSError:
        return False
    if source.get("size") is not None and int(source["size"]) != stat.st_size:
        return False
    if source.get("mtime_ns") is not None and int(source["mtime_ns"]) != stat.st_mtime_ns:
        return False
    expected_digest = str(source.get("content_sha256") or "")
    if expected_digest:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source_file:
                while True:
                    block = source_file.read(8 * 1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
        except OSError:
            return False
        if digest.hexdigest() != expected_digest:
            return False
    return True


def _graph_classification_profile_hint(filename: str) -> str | None:
    stem = os.path.splitext(os.path.basename(filename or ""))[0].lower()
    if "pcba" in stem:
        return "pcba"
    if "hiv" in stem:
        return "hiv"
    return None


def _required_str(arguments: Dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"参数 `{key}` 缺失或格式错误。")
    return value


def _optional_bool(arguments: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = arguments.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"参数 `{key}` 需要为布尔值。")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _is_global_nx_operation(
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    remote_primary: bool = False,
) -> bool:
    """Classify legacy NetworkX tools before they can materialise a large graph."""
    if tool_name in {
        "maximum_flow", "minimum_cut", "global_minimum_cut",
        "minimum_spanning_tree", "community_detection", "node_centrality",
        "graph_connectivity", "find_cycles", "maximum_cycle", "topological_sort",
        "clique_analysis", "global_metrics", "all_shortest_paths",
    }:
        return True
    if tool_name == "degree_analysis":
        # ``node`` is a native point-query optimization only for the Nebula
        # primary representation.  The local GraphCompute implementation still
        # scans every node, so the argument must not bypass the large-graph
        # guard for a local graph.
        return not (remote_primary and bool(arguments.get("node")))
    if tool_name == "reachability":
        return str(arguments.get("method") or "") == "transitive_closure"
    if tool_name == "path_analysis":
        return str(arguments.get("method") or "") == "average_shortest_path_length"
    if tool_name == "link_prediction_score":
        return not (
            arguments.get("source") is not None
            and arguments.get("target") is not None
        )
    if tool_name == "subgraph_extraction":
        return str(arguments.get("method") or "") in {
            "k_core", "k_truss", "k_corona"
        }
    if tool_name == "shortest_path":
        return str(arguments.get("target") or "") == "all" or str(
            arguments.get("algorithm") or ""
        ) == "yen_k_shortest"
    return False


def _large_algorithm_for_legacy_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Preserve the public tool contract when entering the large-graph path.

    Modified after the complete catalogue was implemented: aliases such as
    degree_analysis -> degree changed the result schema and discarded modes.
    The router now owns an explicit capability for every legacy tool name.
    """
    return tool_name


def _detect_text_format_verbose(
    text: str,
) -> tuple[Literal["edgelist", "triples", "freetext"], dict]:
    """判断文本格式，同时返回判断依据供日志记录。

    Returns (format, detail_dict)，detail_dict 包含：
      lines_sampled, nl_lines, long_lines, edgelist_votes, triple_votes
    """
    import re as _re
    _NL_PUNCT = _re.compile(r"[。！？!?；;，,、…—''""]")
    _CJK = _re.compile(r"[一-鿿]")

    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    detail: dict = {"lines_sampled": 0, "nl_lines": 0, "long_lines": 0,
                    "edgelist_votes": 0, "triple_votes": 0}
    if not lines:
        return "freetext", detail

    sample = lines[:30]
    detail["lines_sampled"] = len(sample)

    nl_lines = sum(1 for l in sample if _NL_PUNCT.search(l) or _CJK.search(l))
    long_lines = sum(1 for l in sample if len(l) > 80)
    detail["nl_lines"] = nl_lines
    detail["long_lines"] = long_lines

    edgelist_votes = triple_votes = 0
    for line in sample:
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t", 2)]
        elif "," in line:
            parts = [p.strip() for p in line.split(",", 2)]
        else:
            parts = line.split(None, 2)
        if len(parts) == 2:
            edgelist_votes += 1
        elif len(parts) == 3:
            try:
                float(parts[2])
                edgelist_votes += 1
            except ValueError:
                triple_votes += 1
    detail["edgelist_votes"] = edgelist_votes
    detail["triple_votes"] = triple_votes

    total = len(sample)
    if triple_votes >= total * 0.6:
        return "triples", detail
    if edgelist_votes >= total * 0.6:
        return "edgelist", detail
    # CJK characters alone do not make a file natural language: Chinese node
    # identifiers and relation names are valid structured fields. Prefer a
    # consistent 2/3-column shape above, then use prose signals as fallback.
    if nl_lines > len(sample) * 0.2:
        return "freetext", detail
    if long_lines > len(sample) * 0.2:
        return "freetext", detail
    return "freetext", detail


def _detect_text_format(text: str) -> Literal["edgelist", "triples", "freetext"]:
    fmt, _ = _detect_text_format_verbose(text)
    return fmt


def _parse_triples_from_text(text: str) -> list[dict]:
    """将三元组格式文本（space/tab/comma 分隔）解析为 triples 列表。"""
    triples: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t", 2)]
        elif "," in line:
            parts = [p.strip() for p in line.split(",", 2)]
        else:
            parts = line.split(None, 2)
        if len(parts) >= 3 and all(parts[:3]):
            triples.append({"source": parts[0], "relation": parts[1], "target": parts[2]})
    return triples


class GraphDataService:
    """图数据服务层：业务逻辑集中于此，路由和工具分发都调用这里的方法。"""

    def __init__(
        self,
        *,
        text_graph_llm_factory: Callable[[str], Awaitable[str]] | None = None,
        nebula_repository: NebulaGraphRepository | None = None,
        storage_backend: str | None = None,
        migrate_storage: bool = True,
        construction_store: ConstructionStore | None = None,
        construction_access: ConstructionAccessClient | None = None,
    ) -> None:
        self._sessions: Dict[str, GraphSession] = {}
        if construction_store is None:
            from graph_engine.construction import register_builtin_algorithms
            self._construction = register_builtin_algorithms(ConstructionStore())
        else:
            self._construction = construction_store
        from graph_data_service.construction_coordinator import ConstructionCoordinator
        self._construction_coordinator = ConstructionCoordinator(self._construction)
        self._construction_access = construction_access or ConstructionAccessClient()
        self._deleted_sessions: set[str] = set()
        self._session_lifecycle_lock = threading.RLock()
        self._compute = GraphCompute()
        self._graph_ml = GraphMLModule(get_session=self._get_graph_ml_session)
        # Copy legacy datasets and per-user graph snapshots into the
        # Graph Engine-owned layout. Sources are deliberately retained.
        try:
            from graph_engine.migration import migrate_legacy_graph_storage
            if migrate_storage:
                migrate_legacy_graph_storage()
        except Exception:
            pass
        self._dataset_registry = DatasetRegistry(str(_dp.global_datasets_dir()))
        # Per-user dataset registries, keyed by user_id.
        self._user_dataset_registries: Dict[str, DatasetRegistry] = {}
        _pg = None if text_graph_llm_factory else self._create_text_graph_provider_gateway()
        self._text_graph_extractor = TextGraphExtractionSubAgent(
            llm_factory=text_graph_llm_factory,
            provider_gateway=_pg,
        )
        self._file_format_classifier = FileFormatPreCheckSubAgent(
            llm_factory=text_graph_llm_factory,
            provider_gateway=_pg,
        )
        self._job_manager = JobManager(progress_root=_dp.data_root() / "graph_engine" / "job_progress")
        self._execution_host = GraphExecutionHost(self._job_manager) if controls_enabled() else None
        # New large-graph path: durable catalog/worker state is intentionally
        # independent from the in-memory JobManager used by legacy operations.
        self._large_graph = LargeGraphService()
        if self._execution_host:
            self._execution_host.large_graph = self._large_graph
        self._nebula = nebula_repository or NebulaGraphRepository()
        backend = str(storage_backend or os.getenv("GRAPH_STORAGE_BACKEND", "local")).strip().lower()
        if backend not in {"local", "nebula"}:
            raise ValueError("GRAPH_STORAGE_BACKEND 必须是 local 或 nebula")
        self._storage_backend = backend
        self._embedding_provider: Any = None  # lazy-initialized on first search/rag use
        _workers = max(1, int(os.getenv("GRAPH_COMPUTE_CONCURRENCY", str(min(4, (os.cpu_count() or 2))))))
        self._process_pool = concurrent.futures.ProcessPoolExecutor(max_workers=_workers)
        self._rag_cache: Dict[str, LightweightRAG] = {}
        self._search_index_cache: Dict[str, EntitySearchIndex] = {}
        self._graph_ml_inflight: Dict[tuple[str, str, str], asyncio.Task] = {}
        self._graph_ml_inflight_lock = asyncio.Lock()
        self._graph_write_locks: dict[str, threading.RLock] = {}
        self._graph_write_locks_guard = threading.Lock()
        self._compute_global_sem = asyncio.Semaphore(_env_int("GRAPH_COMPUTE_GLOBAL_CONCURRENCY", _workers))
        self._compute_per_user_limit = _env_int("GRAPH_COMPUTE_PER_USER_CONCURRENCY", min(4, _workers))
        self._compute_user_sems: dict[str, asyncio.Semaphore] = {}
        self._heavy_global_sem = asyncio.Semaphore(_env_int("GRAPH_HEAVY_GLOBAL_CONCURRENCY", max(1, min(2, _workers))))
        self._heavy_per_user_limit = _env_int("GRAPH_HEAVY_PER_USER_CONCURRENCY", 1)
        self._heavy_user_sems: dict[str, asyncio.Semaphore] = {}
        self._tool_scheduler = ToolResourceScheduler()
        self._tool_reservations: set[asyncio.Task] = set()
        self._restore_sessions_from_disk()

    @staticmethod
    def _create_text_graph_provider_gateway(model_override: str = "") -> ProviderGateway | None:
        model_raw = model_override or os.getenv("TEXT_GRAPH_EXTRACTOR_MODEL") or os.getenv("LLM_MODEL") or os.getenv("LLM_DEFAULT_MODEL") or DEFAULT_MODEL
        try:
            model = normalize_model_name(model_raw)
            provider = provider_for_model(model)
        except ValueError:
            return None

        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
            base_url = os.getenv("OPENAI_BASE_URL") or os.getenv("LLM_BASE_URL")
        elif provider == "anthropic":
            api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY")
            base_url = os.getenv("CLAUDE_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL") or os.getenv("LLM_BASE_URL")
        elif provider == "qwen":
            api_key = os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or os.getenv("LLM_API_KEY")
            base_url = os.getenv("QWEN_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL") or os.getenv("LLM_BASE_URL")
        elif provider == "deepseek":
            api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY")
            base_url = os.getenv("DEEPSEEK_BASE_URL") or os.getenv("LLM_BASE_URL")
        elif provider == "doubao":
            api_key = os.getenv("ARK_API_KEY") or os.getenv("DOUBAO_API_KEY") or os.getenv("LLM_API_KEY")
            base_url = os.getenv("ARK_BASE_URL") or os.getenv("DOUBAO_BASE_URL") or os.getenv("LLM_BASE_URL")
        else:
            return None
        if not api_key or not base_url:
            return None
        try:
            return ProviderGateway(
                model=model,
                api_key=api_key,
                base_url=base_url,
                temperature=0.0,
                logger=_NullTraceLogger(),
            )
        except (ImportError, ValueError):
            return None

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    @staticmethod
    def _session_key(session_id: str, user_id: str = "") -> str:
        sid = str(session_id or "").strip()
        uid = str(user_id or "").strip()
        return f"{uid}:{sid}" if uid else sid

    def _cache_key(self, session_id: str, user_id: str = "") -> str:
        return self._session_key(session_id, user_id)

    def _get_user_sem(
        self,
        sems: dict[str, asyncio.Semaphore],
        *,
        user_id: str,
        limit: int,
    ) -> asyncio.Semaphore:
        key = str(user_id or "_anonymous")
        if key not in sems:
            sems[key] = asyncio.Semaphore(max(1, limit))
        return sems[key]

    async def _run_with_compute_limits(self, user_id: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        user_sem = self._get_user_sem(
            self._compute_user_sems,
            user_id=user_id,
            limit=self._compute_per_user_limit,
        )
        async with user_sem:
            async with self._compute_global_sem:
                return await fn()

    async def _run_with_heavy_limits(self, user_id: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        user_sem = self._get_user_sem(
            self._heavy_user_sems,
            user_id=user_id,
            limit=self._heavy_per_user_limit,
        )
        async with user_sem:
            async with self._heavy_global_sem:
                return await fn()

    async def _wait_tool_job(self, job_id: str) -> None:
        """A submitted child can itself return a background job handle."""
        seen = set()
        while job_id and job_id not in seen:
            seen.add(job_id)
            await self._job_manager.wait(job_id)
            job = self._job_manager.status(job_id)
            result = job.result if job else None
            job_id = str(result.get("job_id") or "") if isinstance(result, dict) and result.get("async") else ""

    async def _run_with_tool_policy(self, session_id, tool_name, arguments, user_id, invoke):
        reservation = await self._tool_scheduler.acquire(
            (str(user_id or "").strip(), str(session_id or "").strip()),
            execution_policy(tool_name, arguments),
        )
        retained = False
        try:
            result = await invoke()
            if isinstance(result, dict) and result.get("async") and result.get("job_id"):
                # The worker does not re-enter dispatch_tool. Transfer this
                # reservation to its actual lifetime without blocking the
                # submitting request or making the worker wait on itself.
                async def finish():
                    try:
                        await self._wait_tool_job(str(result["job_id"]))
                    finally:
                        self._tool_scheduler.release(reservation)

                task = asyncio.create_task(finish())
                self._tool_reservations.add(task)
                task.add_done_callback(self._tool_reservations.discard)
                retained = True
            return result
        except asyncio.CancelledError:
            # Registration deliberately survives cancellation. Drain any
            # pending child handoff before releasing its parent's resources.
            context = job_context.get()
            if context and self._execution_host:
                pending = [task for key, (_, task) in self._execution_host.launches.items()
                           if key[:4] == (context.user_id, context.session_id, context.run_id, context.generation)
                           and not task.done()]
                for task in pending:
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                children = [job.job_id for job in self._job_manager.list_jobs(session_id)
                            if job.user_id == user_id and job.parent_job_id == context.job_id]
                for job_id in children:
                    task = asyncio.create_task(self._wait_tool_job(job_id))
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
            raise
        finally:
            if not retained:
                self._tool_scheduler.release(reservation)

    async def _run_nx_in_pool(self, tool_name, nx_graph, arguments):
        future = asyncio.get_running_loop().run_in_executor(
            self._process_pool, run_nx_tool, tool_name, nx_graph, arguments
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # ProcessPool cannot stop a running function. Retain both capacity
            # and its graph reservation until the process really completes.
            with anyio.CancelScope(shield=True):
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
            if not future.cancelled():
                future.exception()
            raise

    def _get_user_dataset_registry(self, user_id: str) -> DatasetRegistry:
        if user_id not in self._user_dataset_registries:
            user_dir = str(_dp.user_datasets_dir(user_id))
            self._user_dataset_registries[user_id] = DatasetRegistry(user_dir)
        return self._user_dataset_registries[user_id]

    def get_or_create(self, session_id: str, user_id: str = "") -> GraphSession:
        key = self._session_key(session_id, user_id)
        with self._session_lifecycle_lock:
            if key in self._deleted_sessions:
                raise HTTPException(409, {'code': 'SESSION_DELETING'})
            if key not in self._sessions:
                self._sessions[key] = GraphSession(session_id=session_id, user_id=user_id)
                self._sessions[key].load_snapshot()
                # Keep GraphData/Nebula versions above durable large-graph
                # reservations after an API restart.
                large_versions = self._large_graph.list_versions(
                    user_id=user_id, session_id=session_id, graph_id=session_id
                )
                if large_versions:
                    self._sessions[key].reserve_version_floor(
                        max(int(row.get("graph_version", 0)) for row in large_versions)
                    )
            return self._sessions[key]

    def get_required(self, session_id: str, user_id: str = "") -> GraphSession:
        key = self._session_key(session_id, user_id)
        session = self._sessions.get(key)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return session

    def delete_session(self, session_id: str, user_id: str = "", *, permanent: bool = False) -> None:
        with self._session_lifecycle_lock:
            self._delete_session(session_id, user_id, permanent=permanent)

    def _delete_session(self, session_id: str, user_id: str, *, permanent: bool) -> None:
        if self._execution_host and any(job.user_id == user_id and job.status not in {'done', 'failed', 'cancelled'}
                                       for job in self._job_manager.list_jobs(session_id)):
            raise HTTPException(409, {'code': 'SESSION_EXECUTION_ACTIVE'})
        key = self._session_key(session_id, user_id)
        with self._graph_write_lock(session_id, user_id):
            large_cleanup = self._large_graph.delete_session(
                user_id=user_id, session_id=session_id
            )
            if not large_cleanup.get("success"):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": large_cleanup.get(
                            "code", "LARGE_GRAPH_SESSION_DELETE_PENDING"
                        ),
                        "error": large_cleanup.get("error"),
                        "active_job_ids": large_cleanup.get("active_job_ids", []),
                    },
                )
            if permanent:
                # Runtime supplies the durable fence after restart. This local
                # fence rejects requests admitted remotely just before deletion.
                self._deleted_sessions.add(key)
                self._job_manager.delete_progress(session_id, user_id)
            self._graph_ml.release_session(key)
            session = self._sessions.pop(key, None)
            if session is None:
                session = GraphSession(session_id=session_id, user_id=user_id)
            session.delete_storage()
            self._rag_cache.pop(key, None)
            self._search_index_cache.pop(key, None)

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
        return self._large_graph.ensure_server_dataset_prepared(
            server_dataset_id=server_dataset_id,
            revision=revision,
            source_path=source_path,
            source_name=source_name,
            relative_path=relative_path,
            directed=directed,
        )

    def attach_server_dataset(
        self,
        session_id: str,
        *,
        user_id: str,
        server_dataset_id: str,
        revision: str,
    ) -> dict[str, Any]:
        session = self.get_or_create(session_id, user_id=user_id)
        with self._graph_write_lock(session_id, user_id):
            existing = self._large_graph.list_versions(
                user_id=user_id,
                session_id=session_id,
                graph_id=session_id,
            )
            graph_version = max(
                [session.graph_version]
                + [int(row.get("graph_version", 0)) for row in existing]
            ) + 1
            result = self._large_graph.attach_server_dataset_snapshot(
                user_id=user_id,
                session_id=session_id,
                graph_id=session_id,
                graph_version=graph_version,
                server_dataset_id=server_dataset_id,
                revision=revision,
            )
            if result.get("success"):
                session.reserve_version_floor(int(result["graph_version"]))
            return result

    def _get_graph_ml_session(self, runtime_session_id: str):
        session = self._sessions.get(str(runtime_session_id))
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        graph = session.get_graph()
        if graph is not None:
            return session
        if not session.is_remote:
            return session
        max_nodes, max_edges = self._primary_materialize_limits()
        graph = self._nebula.materialize_subgraph(
            user_id=session.user_id,
            session_id=session.session_id,
            graph_version=session.graph_version,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
        return _GraphMLSessionView(session.graph_version, graph)

    def _get_embedding_provider(self):
        if self._embedding_provider is None:
            self._embedding_provider = default_provider()
        return self._embedding_provider

    def _get_rag(self, session_id: str, user_id: str = "") -> LightweightRAG:
        key = self._cache_key(session_id, user_id)
        if key not in self._rag_cache:
            session = self.get_or_create(session_id, user_id=user_id)
            self._rag_cache[key] = LightweightRAG(
                str(session._session_dir),
                provider=self._get_embedding_provider(),
            )
        return self._rag_cache[key]

    def _get_search_index(self, session_id: str, user_id: str = "") -> EntitySearchIndex:
        key = self._cache_key(session_id, user_id)
        session = self.get_required(session_id, user_id=user_id)
        gd = session.get_graph()
        if gd is None:
            raise ValueError("尚未加载图数据。")
        existing = self._search_index_cache.get(key)
        if existing is None or existing.is_stale(session.graph_version):
            existing = EntitySearchIndex(gd, session.graph_version, self._get_embedding_provider())
            self._search_index_cache[key] = existing
        return existing

    def _restore_sessions_from_disk(self) -> None:
        def restore_one(session_id: str, user_id: str = "") -> None:
            key = self._session_key(session_id, user_id)
            if key in self._sessions:
                return
            try:
                session = GraphSession(session_id=session_id, user_id=user_id)
                if session.load_snapshot():
                    large_versions = self._large_graph.list_versions(
                        user_id=user_id, session_id=session_id, graph_id=session_id
                    )
                    if large_versions:
                        session.reserve_version_floor(
                            max(
                                int(row.get("graph_version", 0))
                                for row in large_versions
                            )
                        )
                    self._sessions[key] = session
            except Exception:
                pass

        for session_id in GraphSession.discover_restorable_sessions():
            restore_one(session_id)
        users_root = _dp.data_root() / "users"
        if not users_root.exists():
            return
        for user_dir in users_root.iterdir():
            if not user_dir.is_dir():
                continue
            for root_name in ("graph_sessions",):
                root = user_dir / root_name
                if not root.exists():
                    continue
                for session_dir in root.iterdir():
                    if not session_dir.is_dir():
                        continue
                    has_local = (session_dir / "graph_current.npz").exists() or (session_dir / "graph_versions.json").exists()
                    has_remote = False
                    meta_path = session_dir / "session_meta.json"
                    if meta_path.exists():
                        try:
                            meta = json.loads(meta_path.read_text(encoding="utf-8"))
                            remote = meta.get("remote_graph") if isinstance(meta, dict) else None
                            has_remote = isinstance(remote, dict) and bool(remote.get("graph_key"))
                        except Exception:
                            has_remote = False
                    if not has_local and not has_remote:
                        continue
                    restore_one(session_dir.name, user_id=user_dir.name)

    @property
    def nebula_primary(self) -> bool:
        return self._storage_backend == "nebula"

    @staticmethod
    def _primary_materialize_limits() -> tuple[int, int]:
        return (
            _env_int("NEBULA_MAX_MATERIALIZE_NODES", 50_000),
            _env_int("NEBULA_MAX_MATERIALIZE_EDGES", 200_000),
        )

    def _require_graph(self, session_id: str, user_id: str = ""):
        # Build each version's cached NetworkX snapshot once, off the event
        # loop. Dispatch reservations keep the selected version stable until
        # its readers finish; this lock also protects simultaneous cache fills.
        with self._graph_write_lock(session_id, user_id):
            return self._resolve_graph_snapshot(session_id, user_id)

    def _resolve_graph_snapshot(self, session_id: str, user_id: str):
        session = self.get_required(session_id, user_id=user_id)
        gd = session.get_graph()
        if gd is None and session.is_remote:
            max_nodes, max_edges = self._primary_materialize_limits()
            gd = self._nebula.materialize_subgraph(
                user_id=user_id,
                session_id=session_id,
                graph_version=session.graph_version,
                max_nodes=max_nodes,
                max_edges=max_edges,
            )
        if gd is None:
            raise ValueError("尚未加载任何图数据。")
        nx_graph = self._compute.resolve_graph(self._cache_key(session_id, user_id), session.graph_version, gd)
        return session, gd, nx_graph

    def _graph_write_lock(self, session_id: str, user_id: str) -> threading.RLock:
        key = self._cache_key(session_id, user_id)
        with self._graph_write_locks_guard:
            lock = self._graph_write_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._graph_write_locks[key] = lock
            return lock

    def _commit_graph(
        self,
        session: GraphSession,
        graph: GraphData,
        *,
        user_id: str,
        description: str,
        progress_callback=None,
    ) -> dict[str, Any]:
        """Commit a graph to the configured source of truth."""
        cache_key = self._cache_key(session.session_id, user_id)
        with self._graph_write_lock(session.session_id, user_id):
            if not self.nebula_primary:
                session.set_graph(graph, description=description)
                self._compute.invalidate(cache_key)
                self._search_index_cache.pop(cache_key, None)
                return {
                    "backend": "local",
                    "graph_version": session.graph_version,
                    "graph_key": "",
                    "features_omitted": [],
                    "warnings": [],
                }
            if not self._nebula.enabled:
                raise NebulaRepositoryError(
                    "NEBULA_DISABLED",
                    "GRAPH_STORAGE_BACKEND=nebula 时必须设置 NEBULA_ENABLED=1",
                )
            graph.ensure_node_names()
            base_version = session.graph_version
            version = session.next_graph_version()
            result = self._nebula.sync_graph(
                graph,
                user_id=user_id,
                session_id=session.session_id,
                graph_version=version,
                current_version_getter=lambda: session.graph_version,
                expected_current_version=base_version,
                progress_callback=progress_callback,
                retain_previous=True,
            )
            session.set_remote_graph(
                graph_key=str(result.get("graph_key") or ""),
                graph_version=version,
                node_count=graph.num_nodes,
                edge_count=graph.num_edges,
                directed=graph.directed,
                graph_name=graph.name,
                graph_desc=graph.graph_desc or "",
                has_weights=graph.has_edge_weight,
                description=description,
            )
            self._compute.invalidate(cache_key)
            self._search_index_cache.pop(cache_key, None)
            return result

    def _activate_remote_session(
        self,
        session: GraphSession,
        meta: dict[str, Any],
        *,
        description: str = "",
        record_version: bool = False,
    ) -> None:
        session.set_remote_graph(
            graph_key=str(meta.get("graph_key") or ""),
            graph_version=int(meta.get("graph_version", 0) or 0),
            node_count=int(meta.get("node_count", 0) or 0),
            edge_count=int(meta.get("edge_count", 0) or 0),
            directed=bool(meta.get("directed", True)),
            graph_name=str(meta.get("graph_name") or ""),
            graph_desc=str(meta.get("graph_desc") or ""),
            has_weights=True,
            description=description,
            record_version=record_version,
        )
        cache_key = self._cache_key(session.session_id, session.user_id)
        self._compute.invalidate(cache_key)
        self._search_index_cache.pop(cache_key, None)

    def _current_graph_for_update(self, session: GraphSession, user_id: str) -> Optional[GraphData]:
        graph = session.get_graph()
        if graph is not None:
            return graph
        if not session.is_remote:
            return None
        max_nodes, max_edges = self._primary_materialize_limits()
        return self._nebula.materialize_subgraph(
            user_id=user_id,
            session_id=session.session_id,
            graph_version=session.graph_version,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )

    @staticmethod
    def _graph_from_edgelist(
        text: str,
        existing: Optional[GraphData] = None,
        *,
        directed: bool = True,
    ) -> GraphData:
        require_lossless_append(existing)
        if existing is not None and existing.directed != directed:
            raise ValueError("追加边列表的有向/无向语义必须与当前图一致")
        node2id: dict[str, int] = dict(existing.node2id) if existing is not None else {}
        id2node: dict[int, str] = dict(existing.id2node) if existing is not None else {}
        pairs: list[tuple[int, int]] = list(existing.all_edges()) if existing is not None else []
        weights: list[float] = []
        if existing is not None:
            weights = [
                float(existing.edge_weight[i]) if existing.edge_weight is not None else 1.0
                for i in range(existing.num_edges)
            ]
        next_id = len(id2node)
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace("\t", " ").replace(",", " ").split()
            if len(parts) < 2:
                continue
            src, dst = parts[0], parts[1]
            try:
                weight = float(parts[2]) if len(parts) >= 3 else 1.0
            except ValueError:
                weight = 1.0
            for name in (src, dst):
                if name not in node2id:
                    node2id[name] = next_id
                    id2node[next_id] = name
                    next_id += 1
            pairs.append((node2id[src], node2id[dst]))
            weights.append(weight)
        edge_index = np.asarray(pairs, dtype=np.int64).T if pairs else np.empty((2, 0), dtype=np.int64)
        return GraphData(
            num_nodes=next_id,
            edge_index=edge_index,
            id2node=id2node or None,
            node2id=node2id or None,
            edge_weight=np.asarray(weights, dtype=np.float32) if weights else None,
            directed=directed,
        )

    # ── NebulaGraph 混合/主存储 ──────────────────────────────────────────────────────────

    @staticmethod
    def _nebula_error_result(exc: NebulaRepositoryError) -> dict:
        return exc.to_dict()

    def do_nebula_status(self, session_id: str, user_id: str = "") -> dict:
        session = self.get_or_create(session_id, user_id=user_id)
        return self._nebula.session_status(user_id, session_id, session.graph_version)

    def _do_nebula_sync_job(
        self,
        session: GraphSession,
        graph: GraphData,
        graph_version: int,
        user_id: str,
        progress_callback=None,
    ) -> dict:
        try:
            return self._nebula.sync_graph(
                graph,
                user_id=user_id,
                session_id=session.session_id,
                graph_version=graph_version,
                current_version_getter=lambda: session.graph_version,
                progress_callback=progress_callback,
            )
        except NebulaRepositoryError as exc:
            return self._nebula_error_result(exc)

    async def submit_nebula_sync(self, session_id: str, user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
        graph = session.get_graph()
        if graph is None and session.is_remote:
            return {
                "success": True,
                "backend": "nebula",
                "already_primary": True,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "graph_key": (session.remote_graph or {}).get("graph_key", ""),
                "message": "当前图已经以 NebulaGraph 为主存储，无需再次同步。",
            }
        if graph is None:
            return {
                "success": False,
                "backend": "nebula",
                "code": "NOT_SYNCED",
                "error": "尚未加载任何图数据，无法同步到 NebulaGraph。",
            }
        if not self._nebula.enabled:
            return NebulaRepositoryError(
                "NEBULA_DISABLED", "NebulaGraph 未启用，请设置 NEBULA_ENABLED=1"
            ).to_dict()
        graph_version = session.graph_version
        job_id = await self._job_manager.submit(
            session_id,
            "nebula_sync_graph",
            self._do_nebula_sync_job,
            session,
            graph,
            graph_version,
            user_id,
            _progress_kwarg="progress_callback",
            _identity={"user_id": user_id},
        )
        return {
            "success": True,
            "backend": "nebula",
            "async": True,
            "job_id": job_id,
            "status": "queued",
            "session_id": session_id,
            "graph_version": graph_version,
            "message": "NebulaGraph 同步任务已提交。",
        }

    def _dispatch_nebula_query(
        self,
        session_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        user_id: str = "",
    ) -> dict:
        session = self.get_required(session_id, user_id=user_id)
        graph = session.get_graph()
        if graph is None and not session.is_remote:
            return {
                "success": False,
                "backend": "nebula",
                "code": "NOT_SYNCED",
                "error": "尚未加载任何图数据。",
            }
        try:
            if session.is_remote:
                if tool_name == "nebula_get_node_info":
                    return self._nebula.get_node_info_primary(
                        user_id=user_id, session_id=session_id,
                        graph_version=session.graph_version,
                        node_name=_required_str(arguments, "node_name"),
                    )
                if tool_name == "nebula_get_neighbors":
                    return self._nebula.get_neighbors_primary(
                        user_id=user_id, session_id=session_id,
                        graph_version=session.graph_version,
                        node_name=_required_str(arguments, "node_name"),
                        direction=str(arguments.get("direction") or "out"),
                        limit=int(arguments.get("limit", 20)),
                    )
                if tool_name == "nebula_shortest_path":
                    return self._nebula.shortest_path_primary(
                        user_id=user_id, session_id=session_id,
                        graph_version=session.graph_version,
                        source=_required_str(arguments, "source"),
                        target=_required_str(arguments, "target"),
                        direction=str(arguments.get("direction") or "out"),
                        max_steps=int(arguments.get("max_steps", 10)),
                    )
            if tool_name == "nebula_get_node_info":
                return self._nebula.get_node_info(
                    graph,
                    user_id=user_id,
                    session_id=session_id,
                    graph_version=session.graph_version,
                    node_name=_required_str(arguments, "node_name"),
                )
            if tool_name == "nebula_get_neighbors":
                return self._nebula.get_neighbors(
                    graph,
                    user_id=user_id,
                    session_id=session_id,
                    graph_version=session.graph_version,
                    node_name=_required_str(arguments, "node_name"),
                    direction=str(arguments.get("direction") or "out"),
                    limit=int(arguments.get("limit", 20)),
                )
            if tool_name == "nebula_shortest_path":
                return self._nebula.shortest_path(
                    graph,
                    user_id=user_id,
                    session_id=session_id,
                    graph_version=session.graph_version,
                    source=_required_str(arguments, "source"),
                    target=_required_str(arguments, "target"),
                    direction=str(arguments.get("direction") or "out"),
                    max_steps=int(arguments.get("max_steps", 10)),
                )
            return {
                "success": False,
                "backend": "nebula",
                "code": "QUERY_FAILED",
                "error": f"未知 NebulaGraph 工具: {tool_name}",
            }
        except NebulaRepositoryError as exc:
            return self._nebula_error_result(exc)
        except (TypeError, ValueError) as exc:
            return NebulaRepositoryError(
                "QUERY_FAILED", f"NebulaGraph 查询参数无效: {exc}"
            ).to_dict()

    # ── 数据操作 ──────────────────────────────────────────────────

    def do_load_graph_text(self, session_id: str, edge_text: str, append: bool, user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
        existing = self._current_graph_for_update(session, user_id) if append else None
        graph = self._graph_from_edgelist(edge_text, existing=existing)
        commit = self._commit_graph(
            session,
            graph,
            user_id=user_id,
            description="从边列表文本加载图" + ("（追加）" if append else ""),
        )
        return {
            "success": True,
            "backend": commit.get("backend", self._storage_backend),
            "session_id": session_id,
            "graph_version": session.graph_version,
            "num_nodes": graph.num_nodes,
            "num_edges": graph.num_edges,
            "features_omitted": commit.get("features_omitted", []),
            "warnings": commit.get("warnings", []),
            "message": (
                f"图加载成功（已{('追加' if append else '覆盖')}当前图）。"
                " 如需查看图规模与结构摘要，请调用 get_graph_info。"
            ),
        }

    def _do_load_graph_from_upload_binary_job(
        self,
        session: GraphSession,
        saved_path: str,
        original_filename: str,
        file_token: str,
        append: bool,
        user_id: str,
        progress_callback=None,
    ) -> dict:
        svc_log = _service_logger(session.session_id, "graph_data", user_id=user_id)
        try:
            if progress_callback:
                progress_callback(
                    {"progress": 0.01, "message": "正在解析上传的图文件"}
                )
            loaded_graph = GraphData.load(saved_path)
            if progress_callback:
                progress_callback(
                    {
                        "progress": 0.04,
                        "message": (
                            f"图文件解析完成：{loaded_graph.num_nodes} 个节点，"
                            f"{loaded_graph.num_edges} 条边"
                        ),
                    }
                )
            existing = self._current_graph_for_update(session, user_id) if append else None
            if existing is not None:
                loaded_graph = self._merge_graphs(
                    existing, loaded_graph, directed=loaded_graph.directed
                )
            commit = self._commit_graph(
                session,
                loaded_graph,
                user_id=user_id,
                description=f"从上传文件加载图: {os.path.basename(saved_path)}",
                progress_callback=progress_callback,
            )
            session.mark_version_upload_source(
                session.graph_version,
                UploadFileRecord(
                    file_token=file_token,
                    original_filename=original_filename,
                    saved_path=saved_path,
                ),
            )
            svc_log.event(
                "UPLOAD_LOAD_SUCCESS",
                filename=original_filename,
                format="binary_json",
                graph_version=session.graph_version,
                import_method=commit.get("import_method", "client"),
            )
            return {
                "success": True,
                "session_id": session.session_id,
                "graph_version": session.graph_version,
                "filename": original_filename,
                "backend": commit.get("backend", self._storage_backend),
                "import_method": commit.get("import_method", "client"),
                "num_nodes": loaded_graph.num_nodes,
                "num_edges": loaded_graph.num_edges,
                "features_omitted": commit.get("features_omitted", []),
                "warnings": commit.get("warnings", []),
                "message": (
                    f"从上传文件 '{original_filename}' 加载成功。"
                    " 如需查看图规模与结构摘要，请调用 get_graph_info。"
                ),
            }
        except NebulaRepositoryError as exc:
            svc_log.event(
                "UPLOAD_LOAD_ERROR",
                filename=original_filename,
                format="binary_json",
                error=exc.message,
                code=exc.code,
            )
            return {
                **exc.to_dict(),
                "session_id": session.session_id,
                "graph_version": session.graph_version,
                "filename": original_filename,
            }
        except UnsupportedPropertyGraphAppend as exc:
            return {**exc.to_dict(), "session_id": session.session_id, "graph_version": session.graph_version}
        except Exception as exc:
            svc_log.event(
                "UPLOAD_LOAD_ERROR",
                filename=original_filename,
                format="binary_json",
                error=str(exc),
            )
            return {
                "success": False,
                "backend": self._storage_backend,
                "code": "QUERY_FAILED",
                "session_id": session.session_id,
                "graph_version": session.graph_version,
                "filename": original_filename,
                "error": f"加载上传文件失败: {exc}",
            }

    async def do_load_graph_from_upload(
        self,
        session_id: str,
        filename: str,
        append: bool,
        user_id: str = "",
        *,
        directed: bool = True,
    ) -> dict:
        svc_log = _service_logger(session_id, "graph_data", user_id=user_id)
        session = self.get_required(session_id, user_id=user_id)
        record = session.resolve_upload_by_name(filename)
        if record is None:
            svc_log.event("UPLOAD_LOAD_ERROR", filename=filename, error="file_not_found")
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "error": f"未找到上传文件: {filename}",
            }

        svc_log.event(
            "UPLOAD_LOAD_START",
            filename=record.original_filename,
            saved_path=record.saved_path,
            append=append,
        )

        lower_path = record.saved_path.lower()
        lower_filename = str(record.original_filename or filename).lower()
        graph_classification_profile = _graph_classification_profile_hint(
            record.original_filename or filename
        )
        if (
            (lower_path.endswith(".pt") or lower_filename.endswith(".pt"))
            and graph_classification_profile is not None
        ):
            if append:
                return {
                    "success": False,
                    "session_id": session_id,
                    "graph_version": session.graph_version,
                    "filename": record.original_filename,
                    "format_detected": "graph_classification_dataset",
                    "error": (
                        "HIV/PCBA multi-graph .pt datasets cannot be appended to the "
                        "current single-graph state; load with append=false."
                    ),
                }
            svc_log.event(
                "UPLOAD_FORMAT_DETECTED",
                filename=record.original_filename,
                format="graph_classification_dataset",
                dataset_profile=graph_classification_profile,
            )
            result = await self._submit_graph_ml_tool_job(
                session_id,
                "load_graph_classification_dataset",
                {
                    "filename": filename,
                    "dataset_profile": graph_classification_profile,
                },
                user_id=user_id,
            )
            result["filename"] = record.original_filename
            result["format_detected"] = "graph_classification_dataset"
            result["dataset_profile"] = graph_classification_profile
            return result

        # Route oversized uploads before any read_text/GraphData conversion.
        # File bytes are only a conservative proxy until validation produces
        # exact counts, but this guard prevents the API process from becoming
        # the first component to fully materialise an obviously large source.
        try:
            source_bytes = os.path.getsize(record.saved_path)
        except OSError as exc:
            return {
                "success": False,
                "code": "SOURCE_NOT_FOUND",
                "error": f"读取上传文件元数据失败: {exc}",
            }
        large_upload_threshold = _env_int(
            "LARGE_GRAPH_UPLOAD_BYTES_THRESHOLD", 256 * 1024 * 1024
        )
        binary_upload_threshold = _env_int(
            "LARGE_GRAPH_BINARY_UPLOAD_BYTES_THRESHOLD", 64 * 1024 * 1024
        )
        binary_suffix = lower_path.endswith((".npz", ".pt", ".json"))
        archive_uncompressed_bytes = source_bytes
        archive_entries = 0
        binary_expansion_threshold_reached = False
        if binary_suffix and lower_path.endswith((".npz", ".pt")):
            try:
                archive_probe = probe_binary_archive(record.saved_path)
            except OSError as exc:
                return {"success": False, "code": "SOURCE_NOT_FOUND", "error": str(exc)}
            archive_uncompressed_bytes = archive_probe.uncompressed_bytes
            archive_entries = archive_probe.entries
            binary_expansion_threshold_reached = (
                archive_probe.is_archive
                and archive_uncompressed_bytes >= binary_upload_threshold
            )
        detected_stream_format: Literal["edgelist", "triples", "freetext"] | None = None
        sample = ""
        if not binary_suffix:
            try:
                with open(
                    record.saved_path, "r", encoding="utf-8", errors="replace"
                ) as sample_file:
                    sample = sample_file.read(64 * 1024)
            except OSError as exc:
                return {"success": False, "code": "SOURCE_NOT_FOUND", "error": str(exc)}
            detected_stream_format, _detail = _detect_text_format_verbose(sample)

        # Compact numeric edge lists can contain millions of edges while still
        # being far below the byte threshold. Count only until the routing
        # threshold is reached; this remains streaming and never constructs a
        # GraphData/NX object in the API process.
        edge_threshold = max(
            1, _env_int("LARGE_GRAPH_EDGE_THRESHOLD", 5_000_000)
        )
        edge_threshold_reached = False
        node_threshold_reached = False
        estimated_nodes = 0
        structured_rows = 0
        probe_estimator = "none"
        if detected_stream_format in {"edgelist", "triples"}:
            node_threshold = max(
                1, _env_int("LARGE_GRAPH_NODE_THRESHOLD", 1_000_000)
            )
            try:
                probe = probe_structured_source(
                    record.saved_path,
                    input_format=detected_stream_format,
                    edge_threshold=edge_threshold,
                    node_threshold=node_threshold,
                )
                structured_rows = probe.rows_scanned
                estimated_nodes = probe.estimated_nodes
                edge_threshold_reached = probe.edge_threshold_reached
                node_threshold_reached = probe.node_threshold_reached
                probe_estimator = probe.estimator
            except OSError as exc:
                return {"success": False, "code": "SOURCE_NOT_FOUND", "error": str(exc)}
        else:
            node_threshold = max(
                1, _env_int("LARGE_GRAPH_NODE_THRESHOLD", 1_000_000)
            )

        if (
            source_bytes >= large_upload_threshold
            or edge_threshold_reached
            or node_threshold_reached
            or (
                binary_suffix
                and (
                    source_bytes >= binary_upload_threshold
                    or binary_expansion_threshold_reached
                )
            )
        ):
            if append:
                return {
                    "success": False,
                    "code": "LARGE_GRAPH_APPEND_UNSUPPORTED",
                    "error": "超大图快照是不可变版本，不能使用 append；请准备一个新版本。",
                }
            if binary_suffix:
                return {
                    "success": False,
                    "code": "LARGE_GRAPH_FORMAT_UNSUPPORTED",
                    "error": (
                        "当前大图准备器只接受流式边列表或三元组文件；"
                        "禁止将大型二进制/JSON 文件回退到 GraphData 全量加载。"
                    ),
                    "source_bytes": source_bytes,
                    "archive_uncompressed_bytes": archive_uncompressed_bytes,
                    "archive_entries": archive_entries,
                    "binary_threshold_bytes": binary_upload_threshold,
                }
            detected = detected_stream_format or _detect_text_format_verbose(sample)[0]
            if detected == "freetext":
                return {
                    "success": False,
                    "code": "LARGE_GRAPH_FORMAT_UNSUPPORTED",
                    "error": (
                        "大型自然语言文件不能直接作为图快照；"
                        "请先使用可流式处理的外部抽取流程生成边列表/三元组。"
                    ),
                }
            with self._graph_write_lock(session_id, user_id):
                existing_large_versions = self._large_graph.list_versions(
                    user_id=user_id, session_id=session_id, graph_id=session_id
                )
                next_graph_version = max(
                    [session.graph_version]
                    + [
                        int(row.get("graph_version", 0))
                        for row in existing_large_versions
                    ]
                ) + 1
                # Reserve before catalog submission while holding the same
                # lock used by small graph commits. A failed submission may
                # leave a harmless gap but can never create a collision.
                session.reserve_version_floor(next_graph_version)
                result = self._large_graph.submit_preparation(
                    user_id=user_id,
                    session_id=session_id,
                    graph_id=session_id,
                    source_path=record.saved_path,
                    source_name=record.original_filename,
                    source_token=record.file_token,
                    directed=directed,
                    partitions=None,
                    workers=None,
                    delimiter="auto",
                    input_format="triples" if detected == "triples" else "edgelist",
                    quality_policy=None,
                    graph_version=next_graph_version,
                )
            result["route_decision"] = {
                "path": "large_graph",
                "code": "LARGE_UPLOAD_STREAMING_PREPARATION",
                "reason": (
                    "结构化记录数达到大图边阈值"
                    if edge_threshold_reached
                    else (
                        "流式节点基数估算达到大图节点阈值"
                        if node_threshold_reached
                        else "上传文件超过 API 全量读取阈值"
                    )
                ),
                "source_bytes": source_bytes,
                "threshold_bytes": large_upload_threshold,
                "binary_threshold_bytes": binary_upload_threshold,
                "edge_threshold": edge_threshold,
                "node_threshold": node_threshold,
                "structured_rows_scanned": structured_rows,
                "estimated_nodes": estimated_nodes,
                "node_estimator": probe_estimator,
            }
            return result

        # Controlled uploads must pass the streaming guard too. Only small
        # sources are delegated to the existing cancellable isolated loader.
        if job_context.get() and self._execution_host and not session.is_remote:
            from graph_data_service.isolated_compute import run_isolated
            return await self._run_with_compute_limits(
                user_id,
                lambda: run_isolated(self, session_id, user_id, 'load_graph_from_upload',
                                     {'filename': filename, 'append': append, 'directed': directed}),
            )

        # Nebula 主存储下，二进制图的解析、CSV 生成和导入都在后台线程执行。
        if lower_path.endswith((".npz", ".pt", ".json")) and self.nebula_primary:
            svc_log.event(
                "UPLOAD_FORMAT_DETECTED",
                filename=record.original_filename,
                format="binary_json",
            )
            if not append:
                for loaded in session.nebula_versions_for_upload(record):
                    try:
                        active = self._nebula.activate_graph_key(
                            user_id,
                            session_id,
                            str(loaded["graph_key"]),
                            graph_version_override=int(loaded["graph_version"]),
                        )
                    except NebulaRepositoryError as exc:
                        svc_log.event(
                            "UPLOAD_REUSE_SKIPPED",
                            filename=record.original_filename,
                            graph_version=loaded.get("graph_version"),
                            code=exc.code,
                            error=exc.message,
                        )
                        continue
                    self._activate_remote_session(session, active)
                    svc_log.event(
                        "UPLOAD_LOAD_REUSED",
                        filename=record.original_filename,
                        graph_version=session.graph_version,
                        graph_key=active.get("graph_key"),
                    )
                    return {
                        "success": True,
                        "backend": "nebula",
                        "async": False,
                        "already_loaded": True,
                        "reused_graph_version": int(loaded["graph_version"]),
                        "session_id": session_id,
                        "graph_version": session.graph_version,
                        "filename": record.original_filename,
                        "num_nodes": int(active.get("node_count", 0) or 0),
                        "num_edges": int(active.get("edge_count", 0) or 0),
                        "directed": bool(active.get("directed", True)),
                        "has_edge_weight": bool(loaded.get("has_weights", False)),
                        "warnings": [],
                        "message": (
                            "该上传文件已经加载，已直接切换到已有 "
                            "NebulaGraph 图版本。"
                        ),
                    }
            job_id = await self._job_manager.submit(
                session_id,
                "load_graph_from_upload",
                self._do_load_graph_from_upload_binary_job,
                session,
                record.saved_path,
                record.original_filename,
                record.file_token,
                append,
                user_id,
                _progress_kwarg="progress_callback",
                _identity={"user_id": user_id},
            )
            return {
                "success": True,
                "backend": "nebula",
                "async": True,
                "job_id": job_id,
                "status": "queued",
                "session_id": session_id,
                "graph_version": session.graph_version,
                "filename": record.original_filename,
                "message": "图文件加载与 NebulaGraph 导入任务已提交。",
            }

        # 本地存储仍保持二进制文件的同步加载行为。
        if lower_path.endswith((".npz", ".pt", ".json")):
            svc_log.event(
                "UPLOAD_FORMAT_DETECTED",
                filename=record.original_filename,
                format="binary_json",
            )
            try:
                session.load_graph_from_upload_path(record.saved_path, append=append)
                session.mark_version_upload_source(session.graph_version, record)
                self._compute.invalidate(self._cache_key(session_id, user_id))
            except FileNotFoundError as exc:
                svc_log.event(
                    "UPLOAD_LOAD_ERROR",
                    filename=record.original_filename,
                    format="binary_json",
                    error=str(exc),
                )
                return {
                    "success": False,
                    "session_id": session_id,
                    "graph_version": session.graph_version,
                    "filename": record.original_filename,
                    "error": str(exc),
                }
            except UnsupportedPropertyGraphAppend:
                raise
            except Exception as exc:
                svc_log.event(
                    "UPLOAD_LOAD_ERROR",
                    filename=record.original_filename,
                    format="binary_json",
                    error=str(exc),
                )
                raise HTTPException(status_code=500, detail=f"加载上传文件失败: {exc}")
            svc_log.event(
                "UPLOAD_LOAD_SUCCESS",
                filename=record.original_filename,
                format="binary_json",
                graph_version=session.graph_version,
            )
            return {
                "success": True,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "filename": record.original_filename,
                "backend": "local",
                "features_omitted": [],
                "warnings": [],
                "message": (
                    f"从上传文件 '{record.original_filename}' 加载成功。"
                    " 如需查看图规模与结构摘要，请调用 get_graph_info。"
                ),
            }

        # 文本文件：读取后检测格式并路由
        try:
            with open(record.saved_path, "r", encoding="utf-8") as _f:
                text = _f.read()
        except OSError as e:
            svc_log.event("UPLOAD_LOAD_ERROR", filename=record.original_filename, error=f"read_failed: {e}")
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "filename": record.original_filename,
                "error": f"读取文件失败: {e}",
            }

        # 格式检测：优先用 LLM pre-check，不可用或调用失败时退回启发式
        # 与 TextGraphExtractor 一致：优先用 session model 对应的 gateway
        classifier = self._file_format_classifier
        fmt_raw, fmt_detail = _detect_text_format_verbose(text)
        session_model = session.llm_model
        from graph_data_service.run_model import resolve_run_gateway
        if (job_context.get() or controls_enabled()) and fmt_raw == 'freetext':
            classifier = FileFormatPreCheckSubAgent(provider_gateway=await resolve_run_gateway(_NullTraceLogger(), session_id, user_id))
        elif session_model:
            session_gateway = self._create_text_graph_provider_gateway(session_model)
            if session_gateway is not None:
                classifier = FileFormatPreCheckSubAgent(provider_gateway=session_gateway)
        # Obvious row-structured files are deterministic and should not be
        # reclassified by an LLM. Only ambiguous/free text needs pre-check.
        use_heuristic = fmt_raw != "freetext" or not classifier.available()
        if not use_heuristic:
            svc_log.event("PRE_CHECK_START", filename=record.original_filename)
            precheck = await classifier.classify(
                text, filename=record.original_filename
            )
            fmt_source = precheck["source"]
            fmt_reason = precheck["reason"]
            fmt_extraction_goal = precheck.get("extraction_goal", "")
            svc_log.event(
                "PRE_CHECK_DONE",
                filename=record.original_filename,
                format=precheck["format"],
                source=fmt_source,
                extraction_goal=fmt_extraction_goal,
                reason=fmt_reason,
            )
            if fmt_source == "fallback":
                use_heuristic = True

        if use_heuristic:
            fmt = {"edgelist": "edge_list", "triples": "triple"}.get(fmt_raw, "freetext")
            fmt_source = "heuristic"
            fmt_reason = str(fmt_detail)
            fmt_extraction_goal = ""
        else:
            fmt = precheck["format"]  # "edge_list" | "triple" | "freetext"

        svc_log.event(
            "UPLOAD_FORMAT_DETECTED",
            filename=record.original_filename,
            format=fmt,
            source=fmt_source,
            reason=fmt_reason,
        )

        if fmt == "edge_list":
            try:
                existing = self._current_graph_for_update(session, user_id) if append else None
                graph = self._graph_from_edgelist(
                    text, existing=existing, directed=directed
                )
                commit = self._commit_graph(
                    session,
                    graph,
                    user_id=user_id,
                    description=f"从上传边列表加载图: {record.original_filename}",
                )
                session.mark_version_upload_source(session.graph_version, record)
                n, e = graph.num_nodes, graph.num_edges
            except UnsupportedPropertyGraphAppend:
                raise
            except Exception as exc:
                svc_log.event("UPLOAD_LOAD_ERROR", filename=record.original_filename, format="edge_list", error=str(exc))
                raise HTTPException(status_code=500, detail=f"边列表解析失败: {exc}")
            svc_log.event(
                "UPLOAD_LOAD_SUCCESS",
                filename=record.original_filename,
                format="edge_list",
                nodes=n,
                edges=e,
                graph_version=session.graph_version,
            )
            return {
                "success": True,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "filename": record.original_filename,
                "format_detected": "edge_list",
                "backend": commit.get("backend", self._storage_backend),
                "features_omitted": commit.get("features_omitted", []),
                "warnings": commit.get("warnings", []),
                "message": (
                    f"从上传文件 '{record.original_filename}' 以边列表格式加载成功。"
                    " 如需查看图规模与结构摘要，请调用 get_graph_info。"
                ),
            }

        if fmt == "triple":
            triples = _parse_triples_from_text(text)
            svc_log.event(
                "UPLOAD_TRIPLES_PARSED",
                filename=record.original_filename,
                triple_count=len(triples),
            )
            result = self.do_load_graph_from_triples(
                session_id,
                triples,
                directed=directed,
                append=append,
                user_id=user_id,
            )
            if result.get("success"):
                session.mark_version_upload_source(session.graph_version, record)
            result["filename"] = record.original_filename
            result["format_detected"] = "triple"
            svc_log.event(
                "UPLOAD_LOAD_SUCCESS",
                filename=record.original_filename,
                format="triple",
                triple_count=len(triples),
                graph_version=session.graph_version,
            )
            return result

        # freetext：提交 job，调用方（igraph）轮询进度，避免阻塞 MCP 连接
        svc_log.event(
            "UPLOAD_FORMAT_FREETEXT",
            filename=record.original_filename,
            source=fmt_source,
            reason=fmt_reason,
            extraction_goal=fmt_extraction_goal,
        )
        job_id = await self._job_manager.submit(
            session_id,
            "load_graph_from_upload/text",
            self._do_load_graph_from_text_limited,
            session_id,
            user_id,
            text,
            directed,
            _progress_kwarg="progress_callback",
            _identity={"user_id": user_id},
            append=append,
            extraction_goal=fmt_extraction_goal,
        )
        return {
            "success": True,
            "async": True,
            "job_id": job_id,
            "status": "queued",
            "format_detected": "freetext",
            "filename": record.original_filename,
            "message": "文件内容识别为自然语言，text2graph 已作为异步作业启动。",
        }

    def do_clear_graph(self, session_id: str, user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
        if session.is_remote or self.nebula_primary:
            self._nebula.clear_active(user_id, session_id)
            session.clear_remote_graph()
        else:
            session.clear_graph()
        cache_key = self._cache_key(session_id, user_id)
        self._compute.invalidate(cache_key)
        self._search_index_cache.pop(cache_key, None)
        return {
            "success": True,
            "session_id": session_id,
            "graph_version": session.graph_version,
            "message": "图已清空。",
        }

    def do_undo_graph(self, session_id: str, user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
        if session.is_remote:
            candidates = [
                row for row in session.list_versions()
                if row.get("loaded")
                and row.get("backend") == "nebula"
                and int(row.get("graph_version", 0)) < session.graph_version
                and row.get("graph_key")
            ]
            if not candidates:
                return {
                    "success": False,
                    "session_id": session_id,
                    "graph_version": session.graph_version,
                    "error": "没有可回退的 NebulaGraph 历史版本。",
                }
            target = candidates[-1]
            try:
                meta = self._nebula.activate_graph_key(
                    user_id,
                    session_id,
                    str(target["graph_key"]),
                    graph_version_override=int(target["graph_version"]),
                )
            except NebulaRepositoryError as exc:
                return exc.to_dict()
            self._activate_remote_session(session, meta)
            return {
                "success": True,
                "backend": "nebula",
                "session_id": session_id,
                "graph_version": session.graph_version,
                "num_nodes": int(meta.get("node_count", 0) or 0),
                "num_edges": int(meta.get("edge_count", 0) or 0),
                "message": "已将 NebulaGraph 活动指针回滚到上一版本。",
            }
        if not session.undo():
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "error": "没有可回退的历史版本。",
            }
        self._compute.invalidate(self._cache_key(session_id, user_id))
        gd = session.get_graph()
        return {
            "success": True,
            "session_id": session_id,
            "graph_version": session.graph_version,
            **self._graph_counts_payload(gd),
            "message": "图已回滚到上一版本。",
        }

    def do_to_undirected(self, session_id: str, user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
        gd = self._current_graph_for_update(session, user_id)
        if gd is None:
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "error": "尚未加载任何图数据。",
            }
        if gd.semantic_type == "undirected" and gd.storage_type == "bidirectional" and not gd.has_duplicate_entries:
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "message": "当前图已经是无向图，无需转换。",
            }
        new_gd = gd.to_undirected()
        commit = self._commit_graph(
            session,
            new_gd,
            user_id=user_id,
            description="转换为无向图",
        )
        return {
            "success": True,
            "backend": commit.get("backend", self._storage_backend),
            "session_id": session_id,
            "graph_version": session.graph_version,
            "features_omitted": commit.get("features_omitted", []),
            "warnings": commit.get("warnings", []),
            "message": "图已转换为无向图。如需查看结构摘要，请调用 get_graph_info。",
            **self._graph_counts_payload(new_gd),
        }

    def do_get_graph_construction_status(self, dataset_id: str, user_id: str = "") -> dict:
        return self._construction.status(user_id, dataset_id)

    def do_get_constructed_graph(self, dataset_id: str, user_id: str = "") -> dict:
        return self._construction.graph(user_id, dataset_id)

    def do_get_constructed_schema(self, dataset_id: str, user_id: str = "") -> dict:
        return self._construction.schema(user_id, dataset_id)

    def do_preview_graph_optimization(self, dataset_id: str, base_revision: int, operations: list, user_id: str = "") -> dict:
        return self._construction.optimize(user_id, dataset_id, base_revision, operations)

    def do_commit_graph_optimization(self, dataset_id: str, base_revision: int, operations: list, user_id: str = "") -> dict:
        context = job_context.get()
        def before_commit():
            if context:
                signal = self._job_manager._cancel_signals.get(context.job_id)
                if signal and signal.is_set():
                    raise InterruptedError("JOB_CANCELLED")
        receipt = None
        if context and self._execution_host:
            receipt = {"run_id": context.run_id, "job_id": context.job_id, "generation": context.generation}
            proof = self._construction._directory(user_id, dataset_id) / "record.json"
            self._execution_host.prepare_commit(context, proof)
        result = self._construction.optimize(user_id, dataset_id, base_revision, operations, commit=True,
                                              before_commit=before_commit, receipt=receipt)
        if context:
            job = self._job_manager.status(context.job_id)
            if job:
                job.result = dict(result)
        return result

    def do_load_constructed_graph(self, session_id: str, dataset_id: str, user_id: str = "") -> dict:
        """Activate an immutable dataset revision, preserving the property document.

        Property graph snapshots stay local even when a deployment also exposes
        Nebula: its current projection cannot retain arbitrary JSON properties.
        Use the existing atomic session version manifest as the commit boundary.
        """
        from graph_data_service.graph_snapshot import snapshot_summary

        document = self._construction.graph(user_id, dataset_id, saved_only=True)
        graph = to_graph_data(document)
        session = self.get_or_create(session_id, user_id=user_id)
        context = job_context.get()
        version = session.graph_version
        result = {"success": True, "session_id": session_id, "dataset_id": dataset_id,
                  "construction_dataset_id": dataset_id, "construction_revision": document["revision"],
                  "dataset_version": str(document["revision"]), "backend": "local", **self._graph_counts_payload(graph)}
        receipt = {"result": result}
        if context:
            receipt.update(run_id=context.run_id, job_id=context.job_id, generation=context.generation)
        def commit_allowed():
            if context:
                signal = self._job_manager._cancel_signals.get(context.job_id)
                if signal and signal.is_set():
                    raise InterruptedError("JOB_CANCELLED")
            if self.get_required(session_id, user_id) is not session or session.graph_version != version:
                raise ConstructionError("graph_version_changed", "Session graph changed while loading the dataset.", 409)
        descriptor, staging = tempfile.mkstemp(prefix=".construction-load-", suffix=".npz", dir=session._session_dir)
        os.close(descriptor)
        try:
            graph.save(staging)
            with self._session_lifecycle_lock, self._graph_write_lock(session_id, user_id):
                commit_allowed()
                if context and self._execution_host:
                    self._execution_host.prepare_commit(context, session._versions_path())
                result["graph_version"] = session.publish_snapshot(
                    staging, snapshot_summary(graph), expected_version=version, receipt=receipt,
                    description=f"加载构图数据集 {dataset_id}@{document['revision']}", before_commit=commit_allowed)
                self._compute.invalidate(self._cache_key(session_id, user_id))
                self._search_index_cache.pop(self._cache_key(session_id, user_id), None)
                session.bind_dataset(dataset_id, str(document["revision"]))
                if context:
                    job = self._job_manager.status(context.job_id)
                    if job:
                        job.result = dict(result)
            return result
        finally:
            Path(staging).unlink(missing_ok=True)

    def do_list_datasets(self, user_id: str = "", construction_ids: tuple[str, ...] = ()) -> dict:
        global_metas = self._dataset_registry.list_datasets()
        global_dicts = [dict(m.to_dict(), scope="global") for m in global_metas]
        if user_id:
            user_metas = self._get_user_dataset_registry(user_id).list_datasets()
            user_dicts = [dict(m.to_dict(), scope="user") for m in user_metas]
            existing = {row["dataset_id"] for row in user_dicts}
            user_dicts.extend({**row, "name": row["source_filename"] or row["dataset_id"], "scope": "user",
                               "source_type": "construction", "is_directed": row["directed"],
                               "versions": [str(row["revision"])], "storage_backend": "construction",
                               "load_tool": "load_constructed_graph"}
                              for row in (self._construction.status(user_id, dataset_id) for dataset_id in construction_ids)
                              if row["status"] == "ready" and row["dataset_id"] not in existing)
        else:
            user_dicts = []
        return {
            "success": True,
            "datasets": user_dicts + global_dicts,
        }

    def do_load_dataset(self, session_id: str, dataset_id: str, version: str = "latest", user_id: str = "") -> dict:
        session = self.get_or_create(session_id, user_id=user_id)
        # Check user's registry first, then global.
        registry = None
        if user_id:
            user_reg = self._get_user_dataset_registry(user_id)
            if user_reg.get(dataset_id, required=False) is not None:
                registry = user_reg
        if registry is None:
            registry = self._dataset_registry
        meta = registry.get(dataset_id)
        actual_version = version if version != "latest" else meta.versions[-1]
        if meta.storage_backend == "nebula":
            if not self.nebula_primary:
                return {
                    "success": False,
                    "code": "NEBULA_PRIMARY_REQUIRED",
                    "error": "该数据集存储在 NebulaGraph 中，请设置 GRAPH_STORAGE_BACKEND=nebula。",
                }
            graph_ref = meta.graph_ref or {}
            graph_key = str(graph_ref.get("graph_key") or "")
            if not graph_key:
                return {"success": False, "code": "QUERY_FAILED", "error": "数据集缺少 NebulaGraph 图引用。"}
            new_version = session.next_graph_version()
            try:
                active = self._nebula.activate_graph_key(
                    user_id,
                    session_id,
                    graph_key,
                    graph_version_override=new_version,
                )
            except NebulaRepositoryError as exc:
                return exc.to_dict()
            self._activate_remote_session(
                session,
                active,
                description=f"加载 Nebula 数据集 {dataset_id}@{actual_version}",
                record_version=True,
            )
            session.bind_dataset(dataset_id, actual_version)
            return {
                "success": True,
                "backend": "nebula",
                "session_id": session_id,
                "dataset_id": dataset_id,
                "dataset_version": actual_version,
                "graph_version": session.graph_version,
                "num_nodes": int(active.get("node_count", 0) or 0),
                "num_edges": int(active.get("edge_count", 0) or 0),
            }

        graph = registry.load_graph(dataset_id, version=version)
        commit = self._commit_graph(
            session,
            graph,
            user_id=user_id,
            description=f"加载数据集 {dataset_id}@{actual_version}",
        )
        session.bind_dataset(dataset_id, actual_version)
        return {
            "success": True,
            "backend": commit.get("backend", self._storage_backend),
            "session_id": session_id,
            "dataset_id": dataset_id,
            "dataset_version": actual_version,
            "graph_version": session.graph_version,
            **self._graph_counts_payload(graph),
            "features_omitted": commit.get("features_omitted", []),
            "warnings": commit.get("warnings", []),
        }

    def do_load_graph_classification_graph(
        self,
        session_id: str,
        arguments: Dict[str, Any],
        user_id: str = "",
    ) -> dict:
        req = LoadGraphClassificationGraphRequest.model_validate(arguments or {})
        session = self.get_required(session_id, user_id=user_id)
        runtime_session_id = self._cache_key(session_id, user_id)
        bundle = self._graph_ml.resolve_graph_classification_dataset(
            runtime_session_id,
            req.dataset_id,
        )
        graph = graph_classification_sample_to_graph_data(bundle, req.graph_index)
        session.set_graph(
            graph,
            description=(
                f"从图分类数据集 {bundle.dataset_id} 导出 graph_index={req.graph_index}"
            ),
        )
        self._compute.invalidate(runtime_session_id)
        split_name = next(
            (
                split
                for split, indices in bundle.split_indices.items()
                if req.graph_index in indices
            ),
            None,
        )
        label_stats = node_label_stats_for_graph(graph)
        label_values = node_label_values_for_graph(graph)
        label_classes = node_label_class_mapping_for_graph(graph)
        node_types = node_type_values_for_graph(graph)
        edge_types = edge_type_values_for_graph(graph)
        preview_payload = self._graph_preview(graph)
        ml_readiness = graph_ml_readiness_for_graph(graph)
        graph_label = self._json_safe_value(graph.graph_label)
        graph_info_payload = {
            "num_nodes": graph.num_nodes,
            "num_edges": graph.num_edges,
            "num_edge_entries": graph.num_edge_entries,
            "num_unique_edges": graph.num_unique_edges,
            "num_unique_directed_edges": graph.num_unique_directed_edges,
            "num_unique_undirected_edges": graph.num_unique_undirected_edges,
            "semantic_type": graph.semantic_type,
            "storage_type": graph.storage_type,
            "is_symmetric": graph.is_symmetric,
            "has_self_loops": graph.has_self_loops,
            "has_duplicate_entries": graph.has_duplicate_entries,
            "directed": graph.directed,
            "has_node_names": graph.has_node_names,
            "has_edge_weight": graph.has_edge_weight,
            "has_node_feat": graph.has_node_feat,
            "node_feat_dim": graph.node_feat_dim,
            "has_edge_feat": graph.has_edge_feat,
            "edge_feat_dim": graph.edge_feat_dim,
            "has_node_label": graph.has_node_label,
            "node_label_stats": label_stats,
            "node_label_values": label_values,
            "node_label_classes": label_classes,
            "has_node_type": graph.has_node_type,
            "node_type_values": node_types,
            "has_edge_type": graph.has_edge_type,
            "edge_type_values": edge_types,
            "has_graph_label": graph.has_graph_label,
            "graph_label": graph_label,
            "has_node_text": graph.has_node_text,
            "has_split_masks": graph.has_split_masks,
            "split_stats": None,
            "label_names": graph.label_names,
            "graph_desc": graph.graph_desc,
        }
        return {
            "success": True,
            "session_id": session_id,
            "dataset_id": bundle.dataset_id,
            "dataset_name": bundle.name,
            "graph_index": req.graph_index,
            "split": split_name,
            "graph_version": session.graph_version,
            "num_nodes": graph.num_nodes,
            "num_edges": graph.num_edges,
            "num_edge_entries": graph.num_edge_entries,
            "num_unique_edges": graph.num_unique_edges,
            "num_unique_directed_edges": graph.num_unique_directed_edges,
            "num_unique_undirected_edges": graph.num_unique_undirected_edges,
            "semantic_type": graph.semantic_type,
            "storage_type": graph.storage_type,
            "is_symmetric": graph.is_symmetric,
            "has_self_loops": graph.has_self_loops,
            "has_duplicate_entries": graph.has_duplicate_entries,
            "directed": graph.directed,
            "has_node_names": graph.has_node_names,
            "has_edge_weight": graph.has_edge_weight,
            "node_label_stats": label_stats,
            "nodes": preview_payload["nodes"],
            "edges": preview_payload["edges"],
            "preview": preview_payload["preview"],
            "graph_info": graph_info_payload,
            "graph_ml_readiness": ml_readiness,
            "message": (
                f"已将图分类数据集 {bundle.dataset_id} 的 graph_index={req.graph_index} "
                "导出为当前会话图。现在可以继续使用 get_graph_info、get_node_info、"
                "visualize_subgraph 和 subgraph_extraction。"
            ),
        }

    def do_save_as_dataset(self, session_id: str, name: str, description: str, user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
        registry = self._get_user_dataset_registry(user_id) if user_id else self._dataset_registry
        if session.is_remote:
            graph_ref = session.remote_graph
            if graph_ref is None:
                return {"success": False, "error": "当前 NebulaGraph 图引用不可用。"}
            meta = registry.create_remote(name, description, graph_ref=graph_ref)
        else:
            graph = session.get_graph()
            if graph is None:
                return {"success": False, "error": "尚未加载任何图数据。"}
            meta = registry.create(name, description, graph)
        session.bind_dataset(meta.dataset_id, meta.versions[-1])
        return {"success": True, "dataset": dict(meta.to_dict(), scope="user" if user_id else "global")}

    def do_list_graph_versions(self, session_id: str, user_id: str = "") -> dict:
        session = self.get_or_create(session_id, user_id=user_id)
        versions = session.list_versions()
        return {
            "success": True,
            "session_id": session_id,
            "current_graph_version": session.graph_version,
            "versions": versions,
        }

    def do_load_graph_version(self, session_id: str, graph_version: int, user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
        if session.is_remote or self.nebula_primary:
            loaded = next(
                (
                    row for row in session.list_versions()
                    if int(row.get("graph_version", -1)) == int(graph_version)
                    and row.get("backend") == "nebula"
                    and row.get("graph_key")
                ),
                None,
            )
            if loaded is not None:
                try:
                    meta = self._nebula.activate_graph_key(
                        user_id,
                        session_id,
                        str(loaded["graph_key"]),
                        graph_version_override=int(graph_version),
                    )
                except NebulaRepositoryError as exc:
                    return exc.to_dict()
                self._activate_remote_session(session, meta)
                return {
                    "success": True,
                    "backend": "nebula",
                    "session_id": session_id,
                    "graph_version": session.graph_version,
                    "loaded_version": int(graph_version),
                    "num_nodes": int(meta.get("node_count", 0) or 0),
                    "num_edges": int(meta.get("edge_count", 0) or 0),
                    "description": loaded.get("description", ""),
                    "message": f"已切换到 NebulaGraph 图版本 {graph_version}。",
                }
        loaded = session.load_version(int(graph_version))
        if loaded is None:
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "error": f"图版本 {graph_version} 不存在或不可加载。",
                "versions": session.list_versions(),
            }
        self._compute.invalidate(self._cache_key(session_id, user_id))
        self._search_index_cache.pop(self._cache_key(session_id, user_id), None)
        return {
            "success": True,
            "session_id": session_id,
            "graph_version": session.graph_version,
            "loaded_version": int(graph_version),
            **self._graph_counts_payload(session.get_graph()),
            "description": loaded.get("description", ""),
            "message": f"已切换到图版本 {graph_version}。",
        }

    def do_discard_graph_version(
        self,
        session_id: str,
        graph_version: int,
        fallback_graph_version: Optional[int] = None,
        user_id: str = "",
    ) -> dict:
        session = self.get_required(session_id, user_id=user_id)
        remote_target = next(
            (
                row for row in session.list_versions()
                if int(row.get("graph_version", -1)) == int(graph_version)
                and row.get("backend") == "nebula"
                and row.get("graph_key")
            ),
            None,
        )
        if remote_target is not None:
            if int(graph_version) == session.graph_version:
                candidates = [
                    row for row in session.list_versions()
                    if row.get("backend") == "nebula"
                    and row.get("graph_key")
                    and int(row.get("graph_version", -1)) != int(graph_version)
                ]
                if fallback_graph_version is not None:
                    candidates = [
                        row for row in candidates
                        if int(row.get("graph_version", -1)) == int(fallback_graph_version)
                    ]
                if not candidates:
                    return {"success": False, "error": "删除当前远程版本前需要可用回退版本"}
                fallback = candidates[-1]
                meta = self._nebula.activate_graph_key(
                    user_id,
                    session_id,
                    str(fallback["graph_key"]),
                    graph_version_override=int(fallback["graph_version"]),
                )
                self._activate_remote_session(session, meta)
            self._nebula.delete_graph_key(str(remote_target["graph_key"]))
            records = [
                row for row in session._load_version_records()
                if int(row.get("graph_version", -1)) != int(graph_version)
            ]
            session._write_version_records(records)
            return {
                "success": True,
                "backend": "nebula",
                "session_id": session_id,
                "discarded_graph_version": int(graph_version),
                "graph_version": session.graph_version,
                "versions": session.list_versions(),
                "message": f"已删除 NebulaGraph 图版本 {graph_version}。",
            }
        discarded = session.discard_version(
            int(graph_version),
            int(fallback_graph_version) if fallback_graph_version is not None else None,
        )
        if discarded is None:
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "error": f"图版本 {graph_version} 不存在，或无法找到可回退版本。",
                "versions": session.list_versions(),
            }
        cache_key = self._cache_key(session_id, user_id)
        self._compute.invalidate(cache_key)
        self._search_index_cache.pop(cache_key, None)
        return {
            "success": True,
            "session_id": session_id,
            **discarded,
            "versions": session.list_versions(),
            "message": f"已删除图版本 {graph_version} 并回退到版本 {session.graph_version}。",
        }

    def do_delete_dataset(self, dataset_id: str, user_id: str = "") -> dict:
        if user_id:
            user_reg = self._get_user_dataset_registry(user_id)
            if user_reg.delete(dataset_id):
                return {"success": True, "dataset_id": dataset_id, "message": "数据集已删除"}
            if user_id != "admin":
                return {"success": False, "error": f"用户数据集 '{dataset_id}' 不存在，或无权删除全局数据集"}
        ok = self._dataset_registry.delete(dataset_id)
        if not ok:
            return {"success": False, "error": f"数据集 '{dataset_id}' 不存在"}
        return {"success": True, "dataset_id": dataset_id, "message": "数据集已删除"}

    def do_load_graph_from_triples(
        self,
        session_id: str,
        triples: list[dict[str, Any]],
        *,
        directed: bool = True,
        append: bool = False,
        user_id: str = "",
    ) -> dict:
        session = self.get_or_create(session_id, user_id=user_id)
        if not isinstance(triples, list) or not triples:
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "error": "triples 不能为空。",
            }
        max_control_rows = max(
            1, _env_int("GRAPH_SMALL_MAX_STRUCTURED_ROWS", 1_000_000)
        )
        if len(triples) > max_control_rows:
            return {
                "success": False,
                "code": "STRUCTURED_INPUT_TOO_LARGE",
                "session_id": session_id,
                "graph_version": session.graph_version,
                "error": (
                    f"结构化三元组数量 {len(triples)} 超过控制面上限 "
                    f"{max_control_rows}；请上传流式三元组文件并调用 prepare_large_graph。"
                ),
            }
        valid_count = self._count_valid_triples(triples)
        skipped = len(triples) - valid_count
        graph = GraphBuilder.build_from_triples(triples, directed=directed)
        if graph.num_edges == 0:
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "raw_triples": len(triples),
                "skipped": skipped,
                "error": "没有可用于建图的有效三元组。",
            }
        if append:
            existing = self._current_graph_for_update(session, user_id)
            if existing is not None:
                graph = self._merge_graphs(existing, graph, directed=directed)
        commit = self._commit_graph(
            session,
            graph,
            user_id=user_id,
            description="从结构化三元组构建图" + ("（追加）" if append else ""),
        )
        return {
            "success": True,
            "backend": commit.get("backend", self._storage_backend),
            "session_id": session_id,
            "graph_version": session.graph_version,
            "features_omitted": commit.get("features_omitted", []),
            "warnings": commit.get("warnings", []),
            "nodes": graph.num_nodes,
            "edges": graph.num_edges,
            **self._graph_counts_payload(graph),
            "raw_triples": len(triples),
            "valid_triples": valid_count,
            "materialized_edges": graph.num_edges,
            "skipped": skipped,
            "sample_triples": self._sample_triples(triples),
            "message": "已从结构化三元组构建图。relation 写入 edge_type，evidence 写入 edge_text，confidence 写入 edge_feat。",
        }

    async def do_load_graph_from_text(
        self,
        session_id: str,
        user_id: str = "",
        text: str = "",
        directed: bool = True,
        *,
        filename: str = "",
        append: bool = False,
        extraction_goal: str = "",
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict:
        session = self.get_or_create(session_id, user_id=user_id)
        max_text_bytes = max(
            1, _env_int("TEXT2GRAPH_MAX_INPUT_BYTES", 16 * 1024 * 1024)
        )
        if text and len(text.encode("utf-8")) > max_text_bytes:
            return {
                "success": False,
                "code": "TEXT_INPUT_TOO_LARGE",
                "session_id": session_id,
                "graph_version": session.graph_version,
                "error": (
                    f"文本请求超过 {max_text_bytes} 字节上限；"
                    "请先上传文件，结构化大图请调用 prepare_large_graph。"
                ),
            }
        if filename and not text:
            record = session.resolve_upload_by_name(filename)
            if record is None:
                return {
                    "success": False,
                    "session_id": session_id,
                    "graph_version": session.graph_version,
                    "error": f"未找到上传文件: {filename}",
                }
            try:
                if Path(record.saved_path).stat().st_size > max_text_bytes:
                    return {
                        "success": False,
                        "code": "TEXT_INPUT_TOO_LARGE",
                        "session_id": session_id,
                        "graph_version": session.graph_version,
                        "error": (
                            f"文本文件超过 {max_text_bytes} 字节上限；"
                            "结构化大图请调用 prepare_large_graph。"
                        ),
                    }
                with open(record.saved_path, "r", encoding="utf-8") as _f:
                    text = _f.read()
            except OSError as _e:
                return {
                    "success": False,
                    "session_id": session_id,
                    "graph_version": session.graph_version,
                    "error": f"读取文件失败: {_e}",
                }
        if not text or not text.strip():
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "error": "text 和 filename 不能同时为空。",
            }
        # Use the session's configured model when it differs from the
        # service-level default, so extraction follows the calling session's LLM.
        extractor = self._text_graph_extractor
        session_model = session.llm_model
        from graph_data_service.run_model import resolve_run_gateway
        if job_context.get() or controls_enabled():
            from graph_engine.extraction import TextGraphExtractionSubAgent
            call_gateway = await resolve_run_gateway(_NullTraceLogger(), session_id, user_id)
            session_model = call_gateway.model
            extractor = TextGraphExtractionSubAgent(provider_gateway=call_gateway)
        elif session_model:
            call_gateway = self._create_text_graph_provider_gateway(session_model)
            if call_gateway is not None:
                from graph_engine.extraction import TextGraphExtractionSubAgent
                extractor = TextGraphExtractionSubAgent(provider_gateway=call_gateway)
        svc_log = _service_logger(session_id, "graph_data", user_id=user_id)
        svc_log.event(
            "TEXT_EXTRACT_START",
            text_len=len(text),
            extraction_goal=extraction_goal,
            model=session_model or "default",
        )
        _chunk_size = int(os.getenv("TEXT2GRAPH_CHUNK_SIZE", "1000"))
        _overlap = int(os.getenv("TEXT2GRAPH_OVERLAP_TOKENS", "150"))
        _parallel = int(os.getenv("TEXT2GRAPH_PARALLEL_NUMBER", "8"))
        try:
            extraction = await extractor.extract(
                text,
                directed=directed,
                extraction_goal=extraction_goal,
                progress_callback=progress_callback,
                max_chunk_tokens=_chunk_size,
                overlap_tokens=_overlap,
                max_concurrency=_parallel,
            )
        except RuntimeError as exc:
            if str(exc) == "text_graph_extractor_llm_unavailable":
                return {
                    "success": False,
                    "session_id": session_id,
                    "graph_version": session.graph_version,
                    "error": "文本图抽取需要可用的 LLM 配置，请设置 LLM_API_KEY/LLM_BASE_URL 或注入 text_graph_llm_factory。",
                }
            raise

        svc_log.event(
            "TEXT_EXTRACT_DONE",
            chunk_count=extraction.chunk_count,
            raw_triples=extraction.raw_triple_count,
            deduped_triples=extraction.deduped_triple_count,
            skipped=extraction.skipped_count,
            warnings=len(extraction.warnings),
        )
        if not extraction.triples:
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "chunks_processed": extraction.chunk_count,
                "raw_triples": extraction.raw_triple_count,
                "deduped_triples": 0,
                "skipped": extraction.skipped_count,
                "warnings": extraction.warnings,
                "sample_triples": [],
                "error": "没有从文本中抽取到足够明确的实体关系，因此没有创建图。",
            }

        graph = GraphBuilder.build_from_triples(extraction.triples, directed=directed)
        if append:
            existing = self._current_graph_for_update(session, user_id)
            if existing is not None:
                graph = self._merge_graphs(existing, graph, directed=directed)
        commit = self._commit_graph(
            session,
            graph,
            user_id=user_id,
            description="从文本构建图" + (f": {filename}" if filename else ""),
        )
        return {
            "success": True,
            "backend": commit.get("backend", self._storage_backend),
            "session_id": session_id,
            "graph_version": session.graph_version,
            "features_omitted": commit.get("features_omitted", []),
            "storage_warnings": commit.get("warnings", []),
            "nodes": graph.num_nodes,
            "edges": graph.num_edges,
            **self._graph_counts_payload(graph),
            "chunks_processed": extraction.chunk_count,
            "raw_triples": extraction.raw_triple_count,
            "deduped_triples": extraction.deduped_triple_count,
            "skipped": extraction.skipped_count,
            "warnings": extraction.warnings,
            "sample_triples": self._sample_triples(extraction.triples),
            "message": (
                "已从文本构建图。关系已写入 edge_type，证据写入 edge_text，"
                "置信度写入 edge_feat[:,0]。"
            ),
        }

    async def _do_load_graph_from_text_limited(
        self,
        session_id: str,
        user_id: str,
        text: str = "",
        directed: bool = True,
        **kwargs,
    ) -> dict:
        return await self._run_with_heavy_limits(
            user_id,
            lambda: self.do_load_graph_from_text(
                session_id,
                user_id,
                text,
                directed,
                **kwargs,
            ),
        )

    @staticmethod
    def _count_valid_triples(triples: list[dict[str, Any]]) -> int:
        count = 0
        for item in triples:
            if not isinstance(item, dict):
                continue
            if str(item.get("source") or "").strip() and str(item.get("target") or "").strip():
                count += 1
        return count

    @staticmethod
    def _sample_triples(triples: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in triples:
            if not isinstance(item, dict):
                continue
            src = str(item.get("source") or "").strip()
            dst = str(item.get("target") or "").strip()
            if not src or not dst:
                continue
            try:
                confidence = float(item.get("confidence", 1.0) or 1.0)
            except (TypeError, ValueError):
                confidence = 1.0
            rows.append(
                {
                    "source": src,
                    "relation": str(item.get("relation") or "related_to").strip() or "related_to",
                    "target": dst,
                    "confidence": confidence,
                }
            )
            if len(rows) >= limit:
                break
        return rows

    @classmethod
    def _merge_graphs(cls, existing: GraphData, incoming: GraphData, *, directed: bool) -> GraphData:
        require_lossless_append(existing, incoming)
        triples = cls._graph_to_triples(existing) + cls._graph_to_triples(incoming)
        if not directed:
            triples = cls._collapse_undirected_triples(triples)
        return GraphBuilder.build_from_triples(triples, directed=directed)

    @staticmethod
    def _collapse_undirected_triples(triples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in triples:
            source = str(item.get("source") or "").strip()
            target = str(item.get("target") or "").strip()
            relation = str(item.get("relation") or "related_to").strip() or "related_to"
            if not source or not target:
                continue
            a, b = sorted((source, target))
            key = (a, relation, b)
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
        return rows

    @staticmethod
    def _graph_to_triples(graph: GraphData) -> list[dict[str, Any]]:
        id2node = graph.id2node
        src_arr, dst_arr = graph.edge_index
        triples: list[dict[str, Any]] = []
        for idx in range(graph.num_edges):
            src = int(src_arr[idx])
            dst = int(dst_arr[idx])
            confidence = 1.0
            if graph.edge_feat is not None and graph.edge_feat.shape[0] > idx:
                try:
                    confidence = float(np.asarray(graph.edge_feat[idx]).ravel()[0])
                except Exception:
                    confidence = 1.0
            triples.append(
                {
                    "source": id2node.get(src, str(src)),
                    "relation": (
                        str(graph.edge_type[idx])
                        if graph.edge_type is not None and len(graph.edge_type) > idx
                        else "related_to"
                    ),
                    "target": id2node.get(dst, str(dst)),
                    "confidence": confidence,
                    "evidence": (
                        str(graph.edge_text[idx])
                        if graph.edge_text is not None and len(graph.edge_text) > idx
                        else ""
                    ),
                }
            )
        return triples

    def do_upload_document(self, session_id: str, filename: str | None = None, content: str | None = None, user_id: str = "") -> dict:
        session = self.get_or_create(session_id, user_id=user_id)
        rag = self._get_rag(session_id, user_id=user_id)
        if filename:
            record = session.resolve_upload_by_name(filename)
            if record is None:
                return {"success": False, "error": f"未找到上传文件: {filename}"}
            with open(record.saved_path, "rb") as f:
                raw = f.read()
            content = raw.decode("utf-8", errors="ignore")
            doc_id = os.path.splitext(os.path.basename(filename))[0]
        else:
            content = str(content or "")
            doc_id = f"doc_{int(time.time())}"
        return rag.ingest_document(doc_id, content)

    def do_retrieve_context(self, session_id: str, query: str, top_k: int = 5, user_id: str = "") -> dict:
        rag = self._get_rag(session_id, user_id=user_id)
        return rag.retrieve(query, top_k=top_k)

    def _primary_search_graph(
        self,
        session: GraphSession,
        *,
        user_id: str,
        seed_names: Optional[list[str]] = None,
    ) -> GraphData:
        candidate_nodes = max(1, _env_int("NEBULA_SEARCH_CANDIDATE_NODES", 2_000))
        candidate_edges = max(1, _env_int("NEBULA_SEARCH_CANDIDATE_EDGES", 10_000))
        if seed_names:
            try:
                return self._nebula.materialize_subgraph(
                    user_id=user_id,
                    session_id=session.session_id,
                    graph_version=session.graph_version,
                    seed_names=seed_names,
                    hops=1,
                    direction="both",
                    max_nodes=candidate_nodes,
                    max_edges=candidate_edges,
                )
            except NebulaRepositoryError as exc:
                if exc.code not in {"QUERY_FAILED"}:
                    raise
        return self._nebula.materialize_subgraph(
            user_id=user_id,
            session_id=session.session_id,
            graph_version=session.graph_version,
            max_nodes=candidate_nodes,
            max_edges=candidate_edges,
            allow_truncate=True,
        )

    def do_search_node(self, session_id: str, arguments: Dict[str, Any], user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
        req = NodeSearchRequest(
            query=str(arguments.get("query") or ""),
            top_k=int(arguments.get("top_k", 8)),
            filters=arguments.get("filters") if isinstance(arguments.get("filters"), dict) else {},
            sort=arguments.get("sort") if isinstance(arguments.get("sort"), dict) else None,
            rerank=_optional_bool(arguments, "rerank", True),
        )
        if session.is_remote:
            graph = self._primary_search_graph(
                session,
                user_id=user_id,
                seed_names=[req.query] if req.query else None,
            )
            index = EntitySearchIndex(graph, session.graph_version, self._get_embedding_provider())
            result = index.search_nodes(req)
            result.update(
                {
                    "backend": "nebula",
                    "candidate_node_count": graph.num_nodes,
                    "candidate_scope": "exact_seed_neighborhood_or_bounded_scan",
                }
            )
            return result
        return self._get_search_index(session_id, user_id=user_id).search_nodes(req)

    def do_search_edge(self, session_id: str, arguments: Dict[str, Any], user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
        req = EdgeSearchRequest(
            query=str(arguments.get("query") or ""),
            src_query=arguments.get("src_query"),
            dst_query=arguments.get("dst_query"),
            top_k=int(arguments.get("top_k", 8)),
            filters=arguments.get("filters") if isinstance(arguments.get("filters"), dict) else {},
            sort=arguments.get("sort") if isinstance(arguments.get("sort"), dict) else None,
            rerank=_optional_bool(arguments, "rerank", True),
        )
        if session.is_remote:
            seeds = [
                str(value) for value in (req.src_query, req.dst_query)
                if isinstance(value, str) and value.strip()
            ]
            graph = self._primary_search_graph(session, user_id=user_id, seed_names=seeds or None)
            index = EntitySearchIndex(graph, session.graph_version, self._get_embedding_provider())
            result = index.search_edges(req)
            result.update(
                {
                    "backend": "nebula",
                    "candidate_node_count": graph.num_nodes,
                    "candidate_edge_count": graph.num_edges,
                    "candidate_scope": "exact_endpoint_neighborhood_or_bounded_scan",
                }
            )
            return result
        return self._get_search_index(session_id, user_id=user_id).search_edges(req)

    def do_get_graph_info(self, session_id: str, user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
        if session.is_remote:
            return self._nebula.graph_info_primary(
                user_id=user_id,
                session_id=session_id,
                graph_version=session.graph_version,
            )
        summary = session.graph_summary()
        gd = session.get_graph()
        if not summary.loaded:
            return {
                "loaded": False,
                "session_id": session_id,
                "graph_version": summary.graph_version,
                "message": summary.message,
            }
        label_stats = node_label_stats_for_graph(gd)
        label_values = node_label_values_for_graph(gd)
        label_classes = node_label_class_mapping_for_graph(gd)
        node_types = node_type_values_for_graph(gd)
        edge_types = edge_type_values_for_graph(gd)
        preview_payload = self._graph_preview(gd)
        ml_readiness = graph_ml_readiness_for_graph(gd)
        # An imported NPZ/JSON may contain the ID used to create its snapshot.
        # That provenance is not a construction dataset in the current project.
        # Use the persisted version source so restored sessions behave the same.
        imported_snapshot = session.version_source(summary.graph_version).get("kind") == "upload"
        construction_dataset_id = None if imported_snapshot else gd.graph_metadata.get("construction_dataset_id")
        construction_revision = None if imported_snapshot else gd.graph_metadata.get("construction_revision")
        construction_state = {
            "construction_dataset_id": construction_dataset_id,
            "construction_revision": construction_revision,
            "construction_applicable": bool(construction_dataset_id),
        }
        graph_info_payload = {
            **construction_state,
            "num_nodes": summary.num_nodes,
            "num_edges": summary.num_edges,
            "num_edge_entries": summary.num_edge_entries,
            "num_unique_edges": summary.num_unique_edges,
            "num_unique_directed_edges": summary.num_unique_directed_edges,
            "num_unique_undirected_edges": summary.num_unique_undirected_edges,
            "semantic_type": summary.semantic_type,
            "storage_type": summary.storage_type,
            "is_symmetric": summary.is_symmetric,
            "has_self_loops": summary.has_self_loops,
            "has_duplicate_entries": summary.has_duplicate_entries,
            "directed": summary.directed,
            "has_node_names": summary.has_node_names,
            "has_edge_weight": summary.has_edge_weight,
            "has_node_feat": gd.has_node_feat,
            "node_feat_dim": gd.node_feat_dim,
            "has_edge_feat": gd.has_edge_feat,
            "edge_feat_dim": gd.edge_feat_dim,
            "has_node_label": gd.has_node_label,
            "node_label_stats": label_stats,
            "node_label_values": label_values,
            "node_label_classes": label_classes,
            "has_node_type": gd.has_node_type,
            "node_type_values": node_types,
            "has_edge_type": gd.has_edge_type,
            "edge_type_values": edge_types,
            "has_graph_label": gd.has_graph_label,
            "graph_label": self._json_safe_value(gd.graph_label),
            "has_node_text": gd.has_node_text,
            "has_split_masks": gd.has_split_masks,
            "split_stats": {
                "train": int(gd.train_mask.sum()) if gd.train_mask is not None else None,
                "val": int(gd.val_mask.sum()) if gd.val_mask is not None else None,
                "test": int(gd.test_mask.sum()) if gd.test_mask is not None else None,
            } if gd.has_split_masks else None,
            "label_names": gd.label_names,
        }
        return {
            "loaded": True,
            "session_id": session_id,
            "graph_version": summary.graph_version,
            **construction_state,
            "num_nodes": summary.num_nodes,
            "num_edges": summary.num_edges,
            "num_edge_entries": summary.num_edge_entries,
            "num_unique_edges": summary.num_unique_edges,
            "num_unique_directed_edges": summary.num_unique_directed_edges,
            "num_unique_undirected_edges": summary.num_unique_undirected_edges,
            "semantic_type": summary.semantic_type,
            "storage_type": summary.storage_type,
            "is_symmetric": summary.is_symmetric,
            "has_self_loops": summary.has_self_loops,
            "has_duplicate_entries": summary.has_duplicate_entries,
            "directed": summary.directed,
            "has_node_names": summary.has_node_names,
            "has_edge_weight": summary.has_edge_weight,
            "node_label_stats": label_stats,
            "nodes": preview_payload["nodes"],
            "edges": preview_payload["edges"],
            "preview": preview_payload["preview"],
            "graph_info": graph_info_payload,
            "graph_ml_readiness": ml_readiness,
        }

    def do_create_analysis_report(self, session_id: str, arguments: Dict[str, Any], user_id: str = "") -> dict:
        source_arg = arguments.get("source_results")
        source_results_valid = isinstance(source_arg, dict)
        source_results = dict(source_arg) if source_results_valid else {}
        if source_results_valid and "get_graph_info" not in source_results and "graph_info" not in source_results:
            try:
                session = self.get_required(session_id, user_id=user_id)
                if session.get_graph() is not None:
                    source_results["get_graph_info"] = self.do_get_graph_info(session_id, user_id=user_id)
            except HTTPException:
                pass

        options_arg = arguments.get("options")
        return build_analysis_report(
            report_intent=str(arguments.get("report_intent") or ""),
            analysis_goal=str(arguments.get("analysis_goal") or ""),
            title=str(arguments.get("title") or ""),
            source_results=source_results,
            options=options_arg if isinstance(options_arg, dict) else None,
        )

    @staticmethod
    def _json_safe_value(value: Any) -> Any:
        if isinstance(value, np.ndarray):
            if value.ndim == 0:
                return GraphDataService._json_safe_value(value.item())
            return [GraphDataService._json_safe_value(item) for item in value.tolist()]
        if isinstance(value, np.generic):
            return GraphDataService._json_safe_value(value.item())
        if isinstance(value, float) and not np.isfinite(value):
            return None
        return value

    @staticmethod
    def _graph_counts_payload(graph: GraphData | None) -> Dict[str, Any]:
        if graph is None:
            return {
                "num_nodes": 0,
                "num_edges": 0,
                "num_edge_entries": 0,
                "num_unique_edges": 0,
                "num_unique_directed_edges": 0,
                "num_unique_undirected_edges": 0,
                "semantic_type": "unknown",
                "storage_type": "mixed",
                "is_symmetric": False,
                "has_self_loops": False,
                "has_duplicate_entries": False,
                "directed": False,
            }
        return {
            "num_nodes": graph.num_nodes,
            "num_edges": graph.num_edges,
            "num_edge_entries": graph.num_edge_entries,
            "num_unique_edges": graph.num_unique_edges,
            "num_unique_directed_edges": graph.num_unique_directed_edges,
            "num_unique_undirected_edges": graph.num_unique_undirected_edges,
            "semantic_type": graph.semantic_type,
            "storage_type": graph.storage_type,
            "is_symmetric": graph.is_symmetric,
            "has_self_loops": graph.has_self_loops,
            "has_duplicate_entries": graph.has_duplicate_entries,
            "directed": graph.directed,
        }

    @staticmethod
    def _graph_preview(gd, node_limit: int = 10, edge_limit: int = 30) -> Dict[str, Any]:
        id2node = gd.id2node
        sampled_ids = list(range(min(node_limit, gd.num_nodes)))
        sampled_set = set(sampled_ids)
        sampled_nodes = [id2node.get(i, str(i)) for i in sampled_ids]
        src_arr, dst_arr = gd.edge_index
        internal_edges: list[dict] = []
        for i in range(gd.num_edges):
            src = int(src_arr[i])
            dst = int(dst_arr[i])
            if src not in sampled_set or dst not in sampled_set:
                continue
            internal_edges.append({
                "src": id2node.get(src, str(src)),
                "dst": id2node.get(dst, str(dst)),
                "weight": float(gd.edge_weight[i]) if gd.edge_weight is not None else 1.0,
            })
        candidate_edges = internal_edges
        if not candidate_edges and sampled_set:
            incident_edges: list[dict] = []
            for i in range(gd.num_edges):
                src = int(src_arr[i])
                dst = int(dst_arr[i])
                if src not in sampled_set and dst not in sampled_set:
                    continue
                incident_edges.append({
                    "src": id2node.get(src, str(src)),
                    "dst": id2node.get(dst, str(dst)),
                    "weight": float(gd.edge_weight[i]) if gd.edge_weight is not None else 1.0,
                })
            candidate_edges = incident_edges
        preview_edges = candidate_edges[:edge_limit]
        return {
            "nodes": sampled_nodes,
            "edges": preview_edges,
            "preview": {
                "sample_node_count": len(sampled_nodes),
                "sample_edge_count": len(preview_edges),
                "node_truncated": gd.num_nodes > len(sampled_nodes),
                "edge_truncated": len(candidate_edges) > len(preview_edges),
                "sample_strategy": "first_n_node_ids",
            },
        }

    @staticmethod
    def _subgraph_payload(graph: GraphData, method: str, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        names = [graph.id2node.get(i, str(i)) for i in range(graph.num_nodes)]
        src, dst = graph.edge_index
        edges = [
            {
                "from": graph.id2node.get(int(src[i]), str(int(src[i]))),
                "to": graph.id2node.get(int(dst[i]), str(int(dst[i]))),
                "weight": float(graph.edge_weight[i]) if graph.edge_weight is not None else 1.0,
            }
            for i in range(min(graph.num_edges, limit))
        ]
        return {
            "success": True,
            "backend": "nebula",
            "method": method,
            "num_nodes": graph.num_nodes,
            "num_edges": graph.num_edges,
            "nodes_preview": names[:limit],
            "nodes_truncated": graph.num_nodes > limit,
            "edges_preview": edges,
            "edges_truncated": graph.num_edges > limit,
        }

    def _do_primary_subgraph_extraction(
        self,
        session_id: str,
        arguments: Dict[str, Any],
        user_id: str,
    ) -> dict:
        session = self.get_required(session_id, user_id=user_id)
        method = str(arguments.get("method") or "")
        k = max(1, int(arguments.get("k", 2)))
        direction = str(arguments.get("direction") or "out")
        max_nodes = int(arguments.get("max_nodes", _env_int("NEBULA_MAX_MATERIALIZE_NODES", 50_000)))
        max_edges = int(arguments.get("max_edges", _env_int("NEBULA_MAX_MATERIALIZE_EDGES", 200_000)))
        limit_raw = arguments.get("limit", 20)
        limit = 200 if str(limit_raw).lower() == "all" else max(1, min(int(limit_raw), 200))
        try:
            if method == "ego_graph":
                node = _required_str(arguments, "node")
                subgraph = self._nebula.materialize_subgraph(
                    user_id=user_id,
                    session_id=session_id,
                    graph_version=session.graph_version,
                    seed_names=[node],
                    hops=k,
                    direction=direction,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                )
                result = self._subgraph_payload(subgraph, method, limit)
            elif method == "node_subset":
                nodes = arguments.get("nodes")
                if not isinstance(nodes, list) or not nodes:
                    return {"success": False, "error": "node_subset 需要非空 nodes"}
                subgraph = self._nebula.materialize_subgraph(
                    user_id=user_id,
                    session_id=session_id,
                    graph_version=session.graph_version,
                    seed_names=[str(name) for name in nodes],
                    hops=0,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                )
                result = self._subgraph_payload(subgraph, method, limit)
            elif method in {"k_core", "k_truss", "k_corona"}:
                source = self._nebula.materialize_subgraph(
                    user_id=user_id,
                    session_id=session_id,
                    graph_version=session.graph_version,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                )
                computed = self._compute.subgraph_extraction(
                    source.to_networkx(),
                    method,
                    k=k,
                    limit=limit,
                    include_full_nodes=True,
                )
                full_nodes = computed.pop("_full_nodes", [])
                if not computed.get("success"):
                    computed["current_graph_replaced"] = False
                    return computed
                subgraph = source.subgraph_by_names([str(name) for name in full_nodes])
                result = {**computed, "backend": "nebula"}
            else:
                return {"success": False, "error": f"未知方法 '{method}'"}
        except NebulaRepositoryError as exc:
            return exc.to_dict()

        create_version = _optional_bool(arguments, "create_version", True)
        if not create_version:
            result["current_graph_replaced"] = False
            result["graph_version"] = session.graph_version
            return result
        previous_version = session.graph_version
        description = str(arguments.get("description") or "").strip() or (
            f"Nebula 子图版本: {method} ({subgraph.num_nodes} nodes, {subgraph.num_edges} edges)"
        )
        commit = self._commit_graph(
            session,
            subgraph,
            user_id=user_id,
            description=description,
        )
        result.update(
            {
                "current_graph_replaced": True,
                "previous_graph_version": previous_version,
                "graph_version": session.graph_version,
                "version_description": description,
                "features_omitted": commit.get("features_omitted", []),
                "warnings": commit.get("warnings", []),
                "message": "已在 NebulaGraph 中创建子图版本并切换为当前图。",
            }
        )
        return result

    def _primary_bounded_nx_graph(
        self,
        session: GraphSession,
        tool_name: str,
        arguments: Dict[str, Any],
        user_id: str,
    ) -> tuple[Any, dict[str, Any]]:
        """Materialise only the traversal scope required by a P1 NX tool."""
        max_nodes = int(arguments.get("max_nodes", _env_int("NEBULA_MAX_MATERIALIZE_NODES", 50_000)))
        max_edges = int(arguments.get("max_edges", _env_int("NEBULA_MAX_MATERIALIZE_EDGES", 200_000)))
        seed_names: Optional[list[str]] = None
        hops = 0
        direction = "both"
        scope = "bounded_full_graph"

        if tool_name == "all_shortest_paths":
            seed_names = [_required_str(arguments, "source")]
            hops = max(1, min(int(arguments.get("max_steps", 10)), 20))
            direction = str(arguments.get("direction") or "out")
            scope = "source_hop_neighborhood"
        elif tool_name == "path_analysis":
            method = str(arguments.get("method") or "")
            if method != "average_shortest_path_length":
                seed_names = [_required_str(arguments, "source")]
                default_steps = int(arguments.get("cutoff", 5)) if method == "all_simple_paths" else 10
                hops = max(1, min(int(arguments.get("max_steps", default_steps)), 20))
                direction = str(arguments.get("direction") or "out")
                scope = "source_hop_neighborhood"
        elif tool_name == "reachability":
            method = str(arguments.get("method") or "")
            if method != "transitive_closure":
                seed_names = [_required_str(arguments, "node")]
                hops = max(1, min(int(arguments.get("max_steps", 10)), 20))
                direction = "in" if method == "ancestors" else "out"
                scope = "node_hop_neighborhood"

        graph = self._nebula.materialize_subgraph(
            user_id=user_id,
            session_id=session.session_id,
            graph_version=session.graph_version,
            seed_names=seed_names,
            hops=hops,
            direction=direction,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
        nx_graph = graph.to_networkx()
        target = str(arguments.get("target") or "").strip()
        target_in_scope: Optional[bool] = None
        if target and scope != "bounded_full_graph":
            target_in_scope = target in nx_graph
            if not target_in_scope:
                # Validate that the target exists; absence from this materialised
                # scope then means only "not reachable within max_steps".
                self._nebula.materialize_subgraph(
                    user_id=user_id,
                    session_id=session.session_id,
                    graph_version=session.graph_version,
                    seed_names=[target],
                    hops=0,
                    max_nodes=1,
                    max_edges=1,
                )
        return nx_graph, {
            "backend": "nebula",
            "materialized_scope": scope,
            "materialized_nodes": graph.num_nodes,
            "materialized_edges": graph.num_edges,
            "max_steps": hops or None,
            "target_in_scope": target_in_scope,
            "complete_for_query": scope == "bounded_full_graph",
        }

    def _dispatch_graph_tool(self, session_id: str, tool_name: str, arguments: Dict[str, Any], user_id: str = "") -> dict:
        """图数据/图计算工具分发。"""
        if tool_name in FINANCE_TOOL_NAMES:
            session = self.get_required(session_id, user_id=user_id)
            result = self._compute.finance_analysis(session.get_graph(), tool_name, arguments)
            return {**result, "graph_version": session.graph_version}
        if tool_name == "load_graph":
            edge_text = _required_str(arguments, "edge_text")
            append = _optional_bool(arguments, "append", False)
            return self.do_load_graph_text(session_id, edge_text, append, user_id=user_id)

        if tool_name == "clear_graph":
            return self.do_clear_graph(session_id, user_id=user_id)

        if tool_name == "undo_graph":
            return self.do_undo_graph(session_id, user_id=user_id)

        if tool_name == "list_graph_versions":
            return self.do_list_graph_versions(session_id, user_id=user_id)

        if tool_name == "load_graph_version":
            return self.do_load_graph_version(
                session_id,
                int(arguments.get("graph_version", 0)),
                user_id=user_id,
            )

        if tool_name == "discard_graph_version":
            fallback = arguments.get("fallback_graph_version")
            return self.do_discard_graph_version(
                session_id,
                int(arguments.get("graph_version", 0)),
                int(fallback) if fallback is not None else None,
                user_id=user_id,
            )

        if tool_name == "to_undirected":
            return self.do_to_undirected(session_id, user_id=user_id)

        if tool_name == "get_graph_info":
            return self.do_get_graph_info(session_id, user_id=user_id)

        if tool_name == "list_datasets":
            return self.do_list_datasets(user_id=user_id)

        if tool_name == "load_dataset":
            dataset_id = _required_str(arguments, "dataset_id")
            version = str(arguments.get("version", "latest"))
            return self.do_load_dataset(session_id, dataset_id, version, user_id=user_id)

        if tool_name == "save_as_dataset":
            name = _required_str(arguments, "name")
            description = str(arguments.get("description") or "")
            return self.do_save_as_dataset(session_id, name, description, user_id=user_id)

        if tool_name == "load_graph_from_text":
            return {"success": False, "error": "load_graph_from_text 应通过异步分发调用"}

        if tool_name == "load_graph_from_triples":
            triples = arguments.get("triples")
            if not isinstance(triples, list):
                raise ValueError("参数 `triples` 需要为数组。")
            directed = _optional_bool(arguments, "directed", True)
            append = _optional_bool(arguments, "append", False)
            return self.do_load_graph_from_triples(
                session_id,
                triples,
                directed=directed,
                append=append,
                user_id=user_id,
            )

        if tool_name == "upload_document":
            filename = str(arguments.get("filename") or "").strip() or None
            content = str(arguments.get("content") or "").strip() or None
            return self.do_upload_document(session_id, filename, content, user_id=user_id)

        if tool_name == "retrieve_context":
            query = _required_str(arguments, "query")
            top_k = int(arguments.get("top_k", 5))
            return self.do_retrieve_context(session_id, query, top_k=top_k, user_id=user_id)

        if tool_name == "get_job_status":
            job_id = _required_str(arguments, "job_id")
            if job_id.startswith("lg_"):
                job = self._large_graph.job_status(
                    job_id, user_id=user_id, session_id=session_id
                )
                return {"success": bool(job), "job": job, "progress_updates": []}
            job = self._job_manager.owned(job_id, session_id, user_id)
            if job is None:
                return {"success": False, "job": None, "progress_updates": []}
            try:
                limit = arguments.get("progress_limit")
                return self._job_manager.progress_status(job, after_seq=arguments.get("after_seq"),
                                                         limit=200 if limit is None else limit)
            except ValueError as exc:
                return {"success": False, "code": "INVALID_PROGRESS_CURSOR", "error": str(exc)}

        if tool_name == "get_job_result":
            job_id = _required_str(arguments, "job_id")
            if job_id.startswith("lg_"):
                result = self._large_graph.job_result(
                    job_id, user_id=user_id, session_id=session_id
                )
                status = str((result or {}).get("status") or "").lower()
                # A failed/cancelled large job still has a useful, non-empty
                # bounded result payload (status, summary and error).  Do not
                # mistake that payload's truthiness for successful execution.
                # Queued/running mirrors the legacy branch below: lookup was
                # successful, but the caller must keep polling until terminal.
                return {
                    "success": bool(result) and status not in {"failed", "cancelled"},
                    "result": result,
                }
            job = self._job_manager.owned(job_id, session_id, user_id)
            if job is None:
                return {"success": False, "result": None}
            if job.status not in {"done", "failed", "cancelled"}:
                return {
                    "success": True,
                    "result": {"job_id": job_id, "status": job.status},
                }
            return {
                "success": job.status == "done",
                "result": {
                    "job_id": job_id,
                    "status": job.status,
                    "result": job.result,
                    "error": job.message if job.status == "failed" else None,
                },
            }

        if tool_name == "cancel_job":
            job_id = _required_str(arguments, "job_id")
            if job_id.startswith("lg_"):
                return {
                    "success": self._large_graph.cancel(
                        job_id, user_id=user_id, session_id=session_id
                    ),
                    "job_id": job_id,
                }
            if not self._job_manager.owned(job_id, session_id, user_id):
                return {"success": False, "error": "作业不存在", "job_id": job_id}
            return {"success": self._job_manager.cancel(job_id), "job_id": job_id}

        if tool_name == "prepare_large_graph":
            session = self.get_required(session_id, user_id=user_id)
            filename = _required_str(arguments, "filename")
            upload = session.resolve_upload_by_name(filename)
            if upload is None:
                return {
                    "success": False,
                    "code": "SOURCE_NOT_FOUND",
                    "error": f"未找到已注册上传文件: {filename}",
                }
            graph_id = str(arguments.get("graph_id") or session_id)
            requested_version = arguments.get("graph_version")
            directed = _optional_bool(arguments, "directed", True)
            input_format = str(arguments.get("input_format") or "edgelist")
            quality_policy = (
                arguments.get("quality_policy")
                if isinstance(arguments.get("quality_policy"), dict)
                else None
            )
            with self._graph_write_lock(session_id, user_id):
                existing_large_versions = self._large_graph.list_versions(
                    user_id=user_id, session_id=session_id, graph_id=graph_id
                )
                if requested_version is None and graph_id == session_id:
                    if (
                        session.has_graph()
                        and session.graph_version > 0
                        and _version_matches_upload(
                            session, session.graph_version, upload
                        )
                        and session.graph_summary().directed == directed
                    ):
                        # Representations may share a version only when their
                        # source fingerprint and graph orientation agree.
                        requested_version = session.graph_version
                    else:
                        requested_version = max(
                            [session.graph_version]
                            + [
                                int(row.get("graph_version", 0))
                                for row in existing_large_versions
                            ]
                        ) + 1

                requested_int = (
                    int(requested_version) if requested_version is not None else None
                )
                if graph_id == session_id and requested_int is not None:
                    small_versions = {
                        int(row.get("graph_version", -1)): row
                        for row in session.list_versions()
                    }
                    if requested_int in small_versions:
                        if not _version_matches_upload(session, requested_int, upload):
                            return {
                                "success": False,
                                "code": "GRAPH_VERSION_SOURCE_MISMATCH",
                                "error": (
                                    f"graph_version={requested_int} 已绑定另一数据源；"
                                    "同一逻辑版本不能被不同图覆盖。"
                                ),
                            }
                        if bool(small_versions[requested_int].get("is_directed", True)) != directed:
                            return {
                                "success": False,
                                "code": "GRAPH_VERSION_SEMANTICS_MISMATCH",
                                "error": "同一 graph_version 的有向/无向语义必须一致。",
                            }
                    # Reserve before catalog submission under the shared graph
                    # write lock. Gaps after a rejected job are intentional.
                    session.reserve_version_floor(requested_int)

                result = self._large_graph.submit_preparation(
                    user_id=user_id,
                    session_id=session_id,
                    graph_id=graph_id,
                    source_path=upload.saved_path,
                    source_name=upload.original_filename,
                    source_token=upload.file_token,
                    directed=directed,
                    partitions=arguments.get("partitions"),
                    workers=arguments.get("workers"),
                    delimiter=str(arguments.get("delimiter") or "auto"),
                    input_format=input_format,
                    quality_policy=quality_policy,
                    graph_version=requested_int,
                )
            return result

        if tool_name == "run_large_graph_algorithm":
            session = self.get_required(session_id, user_id=user_id)
            graph_id = str(arguments.get("graph_id") or session_id)
            parameters = (
                arguments.get("parameters")
                if isinstance(arguments.get("parameters"), dict)
                else {}
            )
            creates_derived_version = bool(
                _required_str(arguments, "algorithm").strip().lower()
                == "subgraph_extraction"
                and _optional_bool(parameters, "create_version", True)
            )
            with self._graph_write_lock(session_id, user_id):
                result = self._large_graph.submit_compute(
                    user_id=user_id,
                    session_id=session_id,
                    graph_id=graph_id,
                    graph_version=int(arguments.get("graph_version", 0)),
                    algorithm=_required_str(arguments, "algorithm"),
                    parameters=parameters,
                    workers=arguments.get("workers"),
                    minimum_derived_version=(
                        session.next_graph_version()
                        if graph_id == session_id and creates_derived_version
                        else None
                    ),
                )
                new_version = result.get("new_graph_version")
                if (
                    result.get("success")
                    and graph_id == session_id
                    and new_version is not None
                ):
                    session.reserve_version_floor(int(new_version))
                return result

        if tool_name == "list_large_graph_versions":
            return {
                "success": True,
                "graph_id": str(arguments.get("graph_id") or session_id),
                "versions": self._large_graph.list_versions(
                    user_id=user_id,
                    session_id=session_id,
                    graph_id=str(arguments.get("graph_id") or session_id),
                ),
            }

        if tool_name == "get_large_graph_artifact":
            artifact_id = _required_str(arguments, "artifact_id")
            artifact = self._large_graph.get_artifact(
                artifact_id,
                user_id=user_id,
                session_id=session_id,
                include_preview=_optional_bool(arguments, "include_preview", True),
            )
            return {
                "success": bool(artifact),
                "artifact": artifact,
                "download_url_template": (
                    f"/api/sessions/{session_id}/large-graph/artifacts/"
                    f"{artifact_id}/partitions/{{partition}}"
                    if artifact else None
                ),
            }

        if tool_name == "get_large_graph_capabilities":
            return {"success": True, "capabilities": self._large_graph.capabilities()}

        if tool_name == "create_analysis_report":
            return self.do_create_analysis_report(session_id, arguments, user_id=user_id)

        session = self.get_required(session_id, user_id=user_id)
        if session.is_remote:
            try:
                if tool_name == "get_node_info":
                    return self._nebula.get_node_info_primary(
                        user_id=user_id,
                        session_id=session_id,
                        graph_version=session.graph_version,
                        node_name=_required_str(arguments, "node_name"),
                    )
                if tool_name == "get_edge_info":
                    return self._nebula.get_edge_info_primary(
                        user_id=user_id,
                        session_id=session_id,
                        graph_version=session.graph_version,
                        src_name=_required_str(arguments, "src_name"),
                        dst_name=_required_str(arguments, "dst_name"),
                    )
                if tool_name == "check_edge_exists":
                    return self._nebula.check_edge_exists_primary(
                        user_id=user_id,
                        session_id=session_id,
                        graph_version=session.graph_version,
                        src_name=_required_str(arguments, "src_name"),
                        dst_name=_required_str(arguments, "dst_name"),
                    )
                if tool_name == "get_neighbors":
                    return self._nebula.get_neighbors_primary(
                        user_id=user_id,
                        session_id=session_id,
                        graph_version=session.graph_version,
                        node_name=_required_str(arguments, "node_name"),
                        direction=str(arguments.get("direction") or "out"),
                        limit=int(arguments.get("limit", 20)),
                    )
                if tool_name == "subgraph_extraction":
                    return self._do_primary_subgraph_extraction(session_id, arguments, user_id)
                if tool_name == "visualize_subgraph":
                    node_names = arguments.get("node_names")
                    return self.do_visualize_subgraph(
                        session_id,
                        list(node_names) if isinstance(node_names, list) else None,
                        bool(arguments.get("include_community", True)),
                        arguments.get("highlights"),
                        max_nodes=arguments.get("max_nodes"),
                        user_id=user_id,
                    )
                if tool_name == "shortest_path" and not _optional_bool(arguments, "use_weight", True):
                    return self._nebula.shortest_path_primary(
                        user_id=user_id,
                        session_id=session_id,
                        graph_version=session.graph_version,
                        source=_required_str(arguments, "source"),
                        target=_required_str(arguments, "target"),
                        direction=str(arguments.get("direction") or "out"),
                        max_steps=int(arguments.get("max_steps", 10)),
                    )
                if tool_name == "degree_analysis" and arguments.get("node"):
                    info = self._nebula.get_node_info_primary(
                        user_id=user_id,
                        session_id=session_id,
                        graph_version=session.graph_version,
                        node_name=_required_str(arguments, "node"),
                    )
                    return {
                        "success": True,
                        "backend": "nebula",
                        "mode": "single_node",
                        "node": info.get("node_name"),
                        "in_degree": info.get("in_degree"),
                        "out_degree": info.get("out_degree"),
                        "total_degree": info.get("degree"),
                        "degree_truncated": info.get("degree_truncated", False),
                    }
            except NebulaRepositoryError as exc:
                return exc.to_dict()

        _, gd, nx_graph = self._require_graph(session_id, user_id=user_id)

        if tool_name == "get_node_info":
            node_name = _required_str(arguments, "node_name")
            return self._compute.get_node_info(gd, node_name)

        if tool_name == "get_edge_info":
            src_name = _required_str(arguments, "src_name")
            dst_name = _required_str(arguments, "dst_name")
            return self._compute.get_edge_info(gd, src_name, dst_name)

        if tool_name == "check_edge_exists":
            src_name = _required_str(arguments, "src_name")
            dst_name = _required_str(arguments, "dst_name")
            return self._compute.check_edge_exists(gd, src_name, dst_name)

        if tool_name == "get_neighbors":
            node_name = _required_str(arguments, "node_name")
            direction = str(arguments.get("direction") or "out").lower()
            if direction not in {"out", "in", "both"}:
                return {"success": False, "error": "direction 必须是 out、in 或 both"}
            limit = max(1, min(int(arguments.get("limit", 20)), 200))
            if not gd.has_node_name(node_name):
                return {"success": False, "error": f"节点 '{node_name}' 不存在"}
            outgoing = gd.neighbors_by_name(node_name) if direction in {"out", "both"} else []
            incoming = gd.predecessors_by_name(node_name) if direction in {"in", "both"} else []
            names = list(dict.fromkeys(outgoing + incoming))[:limit]
            return {
                "success": True,
                "backend": "local",
                "session_id": session_id,
                "graph_version": session.graph_version,
                "node_name": node_name,
                "direction": direction,
                "neighbors": [{"node_name": name} for name in names],
                "count": len(names),
                "limit": limit,
            }

        if tool_name == "search_node":
            return self.do_search_node(session_id, arguments, user_id=user_id)

        if tool_name == "search_edge":
            return self.do_search_edge(session_id, arguments, user_id=user_id)

        if tool_name == "shortest_path":
            source = _required_str(arguments, "source")
            target = _required_str(arguments, "target")
            use_weight = _optional_bool(arguments, "use_weight", True)
            algorithm = str(arguments.get("algorithm", "default"))
            k = int(arguments.get("k", 3))
            return self._compute.shortest_path(nx_graph, source, target, use_weight, algorithm, k)

        if tool_name == "all_shortest_paths":
            source = _required_str(arguments, "source")
            target = _required_str(arguments, "target")
            return self._compute.all_shortest_paths(nx_graph, source, target)

        if tool_name == "maximum_flow":
            source = _required_str(arguments, "source")
            sink = _required_str(arguments, "sink")
            return self._compute.maximum_flow(nx_graph, source, sink)

        if tool_name == "minimum_cut":
            source = _required_str(arguments, "source")
            sink = _required_str(arguments, "sink")
            return self._compute.minimum_cut(nx_graph, source, sink)

        if tool_name == "global_minimum_cut":
            return self._compute.global_minimum_cut(
                nx_graph,
                treat_as_undirected=_optional_bool(arguments, "treat_as_undirected", True),
            )

        if tool_name == "minimum_spanning_tree":
            return self._compute.minimum_spanning_tree(nx_graph)

        if tool_name == "community_detection":
            algorithm = str(arguments.get("algorithm", "louvain"))
            return self._compute.community_detection(
                nx_graph,
                algorithm,
                focus_nodes=(
                    [str(node) for node in arguments.get("focus_nodes")]
                    if isinstance(arguments.get("focus_nodes"), list)
                    else None
                ),
            )

        if tool_name == "node_centrality":
            centrality_type = str(arguments.get("centrality_type", "degree"))
            return self._compute.node_centrality(nx_graph, centrality_type)

        if tool_name == "graph_connectivity":
            return self._compute.graph_connectivity(nx_graph)

        if tool_name == "find_cycles":
            return self._compute.find_cycles(nx_graph)

        if tool_name == "maximum_cycle":
            return self._compute.maximum_cycle(
                nx_graph,
                max_cycles=int(arguments.get("max_cycles", 10000)),
                optimize_by=str(arguments.get("optimize_by", "length")),
            )

        if tool_name == "topological_sort":
            return self._compute.topological_sort(nx_graph)

        if tool_name == "degree_analysis":
            return self._compute.degree_analysis(nx_graph)

        if tool_name == "subgraph_extraction":
            create_version = _optional_bool(arguments, "create_version", True)
            result = self._compute.subgraph_extraction(
                nx_graph,
                str(arguments.get("method", "")),
                node=str(arguments.get("node") or "") or None,
                k=int(arguments.get("k", 2)),
                nodes=arguments.get("nodes") if isinstance(arguments.get("nodes"), list) else None,
                include_full_nodes=create_version,
            )
            full_nodes = result.pop("_full_nodes", None)
            if not result.get("success") or not create_version:
                result["current_graph_replaced"] = False
                return result
            session = self.get_required(session_id, user_id=user_id)
            gd = session.get_graph()
            if gd is None:
                result["current_graph_replaced"] = False
                return result
            previous_version = session.graph_version
            sub_nodes = [str(n) for n in (full_nodes or []) if gd.has_node_name(str(n))]
            sub_gd = gd.subgraph_by_names(sub_nodes)
            description = str(arguments.get("description") or "").strip()
            if not description:
                description = (
                    f"子图版本: {result.get('method')} "
                    f"({result.get('num_nodes', 0)} nodes, {result.get('num_edges', 0)} edges)"
                )
            session.set_graph(sub_gd, description=description)
            cache_key = self._cache_key(session_id, user_id)
            self._compute.invalidate(cache_key)
            self._search_index_cache.pop(cache_key, None)
            result.update(
                {
                    "current_graph_replaced": True,
                    "previous_graph_version": previous_version,
                    "graph_version": session.graph_version,
                    "version_description": description,
                    "message": (
                        "已创建子图版本并切换为当前图；后续图计算将基于该子图。"
                        "可用 list_graph_versions 查看版本，load_graph_version 回退到完整图或其他版本。"
                    ),
                }
            )
            return result

        if tool_name == "link_prediction_score":
            return self._compute.link_prediction_score(
                nx_graph,
                str(arguments.get("method", "")),
                source=str(arguments.get("source") or "") or None,
                target=str(arguments.get("target") or "") or None,
                top_k=int(arguments.get("top_k", 10)),
            )

        if tool_name == "path_analysis":
            return self._compute.path_analysis(
                nx_graph,
                str(arguments.get("method", "")),
                source=str(arguments.get("source") or "") or None,
                target=str(arguments.get("target") or "") or None,
                cutoff=int(arguments.get("cutoff", 5)),
            )

        if tool_name == "clique_analysis":
            return self._compute.clique_analysis(
                nx_graph,
                str(arguments.get("method", "")),
                node=str(arguments.get("node") or "") or None,
            )

        if tool_name == "reachability":
            return self._compute.reachability(
                nx_graph,
                str(arguments.get("method", "")),
                node=str(arguments.get("node") or "") or None,
            )

        if tool_name == "global_metrics":
            return self._compute.global_metrics(nx_graph, str(arguments.get("metric", "")))

        # ── 第 4 批新增（图可视化）────────────────────────────────
        if tool_name == "visualize_subgraph":
            node_names = arguments.get("node_names")
            if node_names is not None:
                node_names = list(node_names)
            include_community = bool(arguments.get("include_community", True))
            highlights = arguments.get("highlights")
            max_nodes = arguments.get("max_nodes")
            return self.do_visualize_subgraph(session_id, node_names, include_community, highlights, max_nodes=max_nodes, user_id=user_id)

        return {"success": False, "error": f"未知工具: {tool_name}"}

    def do_visualize_subgraph(self, session_id: str, node_names: Optional[list], include_community: bool, highlights: Optional[dict] = None, max_nodes: Optional[int] = None, user_id: str = "") -> dict:
        """获取图的结构用于可视化；远程图只物化有界子图。"""
        session = self.get_required(session_id, user_id=user_id)
        if session.is_remote:
            try:
                cap = max(1, min(int(max_nodes or 800), 800))
            except (TypeError, ValueError):
                cap = 800
            requested_nodes = [str(name) for name in (node_names or []) if str(name)]
            requested_nodes = sorted(set(requested_nodes))
            seeds: list[str] = []
            if not requested_nodes and isinstance(highlights, dict):
                for item in highlights.get("nodes", []):
                    if isinstance(item, dict):
                        seeds.extend(str(name) for name in item.get("ids", []) if str(name))
                for item in highlights.get("paths", []):
                    if isinstance(item, dict):
                        seeds.extend(str(name) for name in item.get("node_sequence", []) if str(name))
                for item in highlights.get("subgraphs", []):
                    if isinstance(item, dict):
                        seeds.extend(str(name) for name in item.get("nodes", []) if str(name))
            seeds = sorted(set(seeds))
            summary = session.graph_summary()
            show_full_graph = not requested_nodes and summary.num_nodes <= cap
            expand_to_limit = not requested_nodes and not show_full_graph
            # Nebula must expose more than the final canvas budget; otherwise
            # GraphCompute receives exactly ``cap`` nodes and its type/community
            # smart selector never runs.  Four times the budget keeps I/O
            # bounded while giving the selector meaningful topology to rank.
            source_node_count = len(requested_nodes) if requested_nodes else int(summary.num_nodes)
            candidate_cap = min(max(cap, source_node_count), cap * 4)
            if expand_to_limit and not seeds:
                try:
                    seed = self._nebula.visualization_seed_name_primary(
                        user_id=user_id,
                        session_id=session_id,
                        graph_version=session.graph_version,
                    )
                except NebulaRepositoryError as exc:
                    return exc.to_dict()
                if seed:
                    seeds = [seed]
            try:
                gd = self._nebula.materialize_subgraph(
                    user_id=user_id,
                    session_id=session_id,
                    graph_version=session.graph_version,
                    seed_names=requested_nodes or (seeds if expand_to_limit else None),
                    hops=0,
                    direction="both",
                    max_nodes=candidate_cap,
                    max_edges=min(200_000, max(candidate_cap * 10, 200)),
                    allow_truncate=expand_to_limit or len(requested_nodes) > candidate_cap,
                    expand_to_limit=expand_to_limit,
                )
            except NebulaRepositoryError as exc:
                return exc.to_dict()
            result = self._compute.visualize_subgraph(
                gd.to_networkx(),
                gd,
                list(gd.node2id) if requested_nodes else None,
                include_community,
                highlights,
                max_nodes=cap,
            )
            total_nodes = source_node_count
            if total_nodes > gd.num_nodes or result.get("nodes_truncated"):
                result["truncated"] = True
                result["total_nodes"] = total_nodes
                if not requested_nodes:
                    result["total_edges"] = summary.num_edges
                result["nodes_truncated"] = True
                # This is the final payload size, not the larger candidate pool.
                result["truncated_to"] = int(result.get("num_nodes", 0))
                overview = result.setdefault("overview", {
                    "mode": "smart",
                    "algorithm": "topology-types-communities-v1",
                    "deterministic": True,
                    "node_limit": cap,
                })
                overview["candidate_node_count"] = gd.num_nodes
                overview["source_partial"] = total_nodes > gd.num_nodes
            result["degree_scope"] = "subgraph" if gd.num_nodes < summary.num_nodes or gd.num_edges < summary.num_edges else "full_graph"
            result["backend"] = "nebula"
            result["graph_version"] = session.graph_version
            return result
        try:
            _, gd, nx_graph = self._require_graph(session_id, user_id=user_id)
        except ValueError:
            return {"success": False, "error": "尚未加载任何图数据。"}
        return self._compute.visualize_subgraph(nx_graph, gd, node_names, include_community, highlights, max_nodes=max_nodes)

    def _dispatch_graph_ml_tool(
        self,
        session_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        progress_callback=None,
        user_id: str = "",
    ) -> dict:
        if tool_name == "load_graph_classification_graph":
            return self.do_load_graph_classification_graph(
                session_id,
                arguments,
                user_id=user_id,
            )
        runtime_session_id = self._cache_key(session_id, user_id)
        if tool_name == "load_graph_classification_dataset":
            return self._graph_ml.load_graph_classification_dataset(
                runtime_session_id,
                arguments,
                progress_callback=progress_callback,
            )
        if tool_name == "train":
            return self._graph_ml.train(runtime_session_id, arguments, progress_callback=progress_callback)
        if tool_name == "infer":
            return self._graph_ml.infer(runtime_session_id, arguments)
        if tool_name == "visualize_model_architecture":
            return self._graph_ml.visualize_model_architecture(runtime_session_id, arguments)
        return {"success": False, "error": f"未知 graph_ml 工具: {tool_name}"}

    async def _submit_graph_ml_tool_job(
        self,
        session_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        user_id: str = "",
    ) -> dict:
        clean_arguments = self._strip_internal_graph_ml_args(arguments or {})
        job_id = await self._job_manager.submit(
            session_id,
            tool_name,
            self._dispatch_graph_ml_tool_limited,
            session_id,
            user_id,
            tool_name,
            clean_arguments,
            _identity={"user_id": user_id},
            _progress_kwarg=(
                "progress_callback"
                if tool_name in {"load_graph_classification_dataset", "train"}
                else None
            ),
        )
        return {
            "success": True,
            "async": True,
            "job_id": job_id,
            "status": "queued",
            "message": f"{tool_name} 已作为异步作业启动。",
        }

    @staticmethod
    def _strip_internal_graph_ml_args(arguments: Dict[str, Any]) -> Dict[str, Any]:
        clean_arguments = {
            key: value
            for key, value in (arguments or {}).items()
            if key not in {"async_job", "__async"}
        }
        algo_config = clean_arguments.get("algo_config")
        if isinstance(algo_config, dict):
            clean_algo = {
                key: value
                for key, value in algo_config.items()
                if key not in {"__igraph_async_job", "__graphagent_async_job"}
            }
            clean_arguments["algo_config"] = clean_algo
        return clean_arguments

    @staticmethod
    def _wants_graph_ml_async_job(arguments: Dict[str, Any]) -> bool:
        if bool(arguments.get("async_job")) or bool(arguments.get("__async")):
            return True
        algo_config = arguments.get("algo_config")
        return isinstance(algo_config, dict) and bool(
            algo_config.get("__igraph_async_job") or algo_config.get("__graphagent_async_job")
        )

    @staticmethod
    def _canonical_tool_arguments(arguments: Dict[str, Any]) -> str:
        return json.dumps(
            arguments or {},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=repr,
        )

    async def _dispatch_heavy_graph_ml_tool_once(
        self, session_id: str, tool_name: str, arguments: Dict[str, Any], user_id: str = ""
    ) -> dict:
        if job_context.get() and self._execution_host:
            from graph_data_service.isolated_compute import run_isolated
            return await self._run_with_tool_policy(session_id, tool_name, arguments, user_id,
                lambda: self._run_with_heavy_limits(user_id, lambda: run_isolated(self, session_id, user_id, tool_name, arguments)))
        runtime_key = self._cache_key(session_id, user_id)
        key = (runtime_key, tool_name, self._canonical_tool_arguments(arguments))
        async with self._graph_ml_inflight_lock:
            task = self._graph_ml_inflight.get(key)
            if task is None:
                if user_id:
                    invoke = lambda: run_sync(
                        self._dispatch_graph_ml_tool,
                        session_id,
                        tool_name,
                        arguments,
                        user_id=user_id,
                    )
                else:
                    # Preserve compatibility with injected/test dispatchers
                    # that implement the original three-argument contract.
                    invoke = lambda: run_sync(
                        self._dispatch_graph_ml_tool,
                        session_id,
                        tool_name,
                        arguments,
                    )
                task = asyncio.create_task(
                    self._run_with_tool_policy(session_id, tool_name, arguments, user_id,
                        lambda: self._run_with_heavy_limits(user_id, invoke))
                )
                self._graph_ml_inflight[key] = task

                def forget_done(done_task: asyncio.Task, inflight_key=key) -> None:
                    async def remove() -> None:
                        async with self._graph_ml_inflight_lock:
                            if self._graph_ml_inflight.get(inflight_key) is done_task:
                                self._graph_ml_inflight.pop(inflight_key, None)

                    asyncio.create_task(remove())

                task.add_done_callback(forget_done)
            else:
                _service_logger(session_id, "graph_ml", user_id=user_id).event(
                    "GRAPH_ML_INFLIGHT_JOIN", tool=tool_name
                )

        try:
            result = await asyncio.shield(task)
            return dict(result) if isinstance(result, dict) else result
        finally:
            if task.done():
                async with self._graph_ml_inflight_lock:
                    if self._graph_ml_inflight.get(key) is task:
                        self._graph_ml_inflight.pop(key, None)

    async def _dispatch_graph_ml_tool_limited(
        self,
        session_id: str,
        user_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        progress_callback=None,
    ) -> dict:
        if job_context.get() and self._execution_host:
            from graph_data_service.isolated_compute import run_isolated
            return await self._run_with_heavy_limits(user_id, lambda: run_isolated(self, session_id, user_id, tool_name, arguments, progress_callback))
        return await self._run_with_heavy_limits(
            user_id,
            lambda: run_sync(
                self._dispatch_graph_ml_tool,
                session_id,
                tool_name,
                arguments,
                progress_callback=progress_callback,
                user_id=user_id,
            ),
        )

    def _route_large_graph_tool(self, session_id: str, tool_name: str, arguments: Dict[str, Any], user_id: str):
        """Choose a representation before any legacy executor can materialise it."""
        if tool_name in NX_TOOL_NAMES:
            session = self.get_required(session_id, user_id=user_id)
            graph_summary = session.graph_summary()
            if (
                _is_global_nx_operation(
                    tool_name, arguments, remote_primary=session.is_remote
                )
                and self._large_graph.router.is_large(
                    graph_summary.num_nodes, graph_summary.num_edges
                )
            ):
                small_source = session.version_source(session.graph_version)
                if not self._large_graph.version_matches_small_binding(
                    user_id=user_id,
                    session_id=session_id,
                    graph_id=session_id,
                    graph_version=session.graph_version,
                    small_source=small_source,
                    directed=graph_summary.directed,
                ):
                    return {
                        "success": False,
                        "code": "GRAPH_VERSION_REPRESENTATION_MISMATCH",
                        "error": (
                            "当前小图版本没有同源、同指纹、同方向且语义兼容的已发布"
                            "大图快照；为防止在错误数据上计算，已拒绝仅按版本号路由。"
                        ),
                        "graph_version": session.graph_version,
                        "next_action": (
                            "请对产生当前图版本的同一上传文件调用 prepare_large_graph，"
                            "或显式调用 run_large_graph_algorithm 选择正确快照。"
                        ),
                    }
                with self._graph_write_lock(session_id, user_id):
                    creates_derived_version = bool(
                        tool_name == "subgraph_extraction"
                        and _optional_bool(arguments, "create_version", True)
                    )
                    result = self._large_graph.submit_compute(
                        user_id=user_id,
                        session_id=session_id,
                        graph_id=session_id,
                        graph_version=session.graph_version,
                        algorithm=_large_algorithm_for_legacy_tool(
                            tool_name, arguments
                        ),
                        parameters={
                            **arguments,
                            "top_k": arguments.get(
                                "top_k", arguments.get("limit", 20)
                            ),
                            "legacy_tool": tool_name,
                        },
                        minimum_derived_version=(
                            session.next_graph_version()
                            if creates_derived_version
                            else None
                        ),
                    )
                    new_version = result.get("new_graph_version")
                    if result.get("success") and new_version is not None:
                        session.reserve_version_floor(int(new_version))
                    return result
        return None

    async def dispatch_tool(self, session_id: str, tool_name: str, arguments: Dict[str, Any], user_id: str = "",
                            *, construction_scope: str | None = None) -> dict:
        """Admit independent tools concurrently and serialize resource conflicts."""
        if tool_name in {"get_job_status", "get_job_result"}:
            # Cursor reads touch the durable journal without reserving the graph.
            return await run_sync(self._dispatch_graph_tool, session_id, tool_name, arguments, user_id)
        if tool_name == "cancel_job":
            # Task.cancel()/Event.set() belong to this event loop.
            return self._dispatch_graph_tool(session_id, tool_name, arguments, user_id)
        if tool_name in _GRAPH_ML_HEAVY_TOOLS and not self._wants_graph_ml_async_job(arguments):
            # Identical training/inference requests share the existing inflight
            # task. Reserve resources inside that task, once per computation.
            return await self._dispatch_heavy_graph_ml_tool_once(session_id, tool_name, arguments, user_id)
        try:
            return await self._run_with_tool_policy(session_id, tool_name, arguments, user_id,
                lambda: self._dispatch_tool_unlocked(session_id, tool_name, arguments, user_id, construction_scope=construction_scope))
        except UnsupportedPropertyGraphAppend as exc:
            return exc.to_dict()

    async def _dispatch_tool_unlocked(self, session_id: str, tool_name: str, arguments: Dict[str, Any], user_id: str = "",
                                      *, construction_scope: str | None = None) -> dict:
        """Execute an admitted tool; child jobs inherit its reservation."""
        if tool_name in {"load_graph", "load_graph_from_upload", "load_graph_from_text", "load_graph_from_triples"} and _optional_bool(arguments, "append", False):
            # Check after queue admission and before file parsing/LLM extraction.
            # Merge helpers also check both inputs immediately before conversion.
            session = self.get_or_create(session_id, user_id=user_id)
            await run_sync(require_lossless_append, session.get_graph())
        # Resolve after queue admission so an earlier catalog/grant cannot keep a
        # deleted or migrated dataset accessible to a waiting tool.
        if tool_name == "list_datasets" and user_id:
            grant = await self._construction_access.authorize(user_id, session_id=session_id, scope=construction_scope)
            return await run_sync(self.do_list_datasets, user_id, tuple(grant.dataset_ids))
        if tool_name == "load_dataset" and user_id:
            dataset_id = _required_str(arguments, "dataset_id")
            state = await run_sync(self._construction.status, user_id, dataset_id)
            if state["status"] == "ready":
                await self._construction_access.authorize(user_id, session_id=session_id,
                    scope=construction_scope, dataset_id=dataset_id)
                return {"success": False, "code": "USE_CONSTRUCTED_GRAPH_LOADER", "dataset_id": dataset_id,
                        "error": "已构图数据集请使用 load_constructed_graph 加载，以同步当前会话的数据集选择。"}
        if tool_name == "get_graph_construction_capabilities":
            return self._construction.capabilities()
        if tool_name in DATASET_TOOLS:
            dataset_id = _required_str(arguments, "dataset_id")
            applicability = await self._construction_access.authorize_tool(user_id, tool_name,
                session_id=session_id, scope=construction_scope, dataset_id=dataset_id)
            if applicability is not None:
                return applicability
            if tool_name == "get_graph_construction_status":
                return await run_sync(self.do_get_graph_construction_status, dataset_id, user_id)
            if tool_name == "get_constructed_graph":
                return await run_sync(self.do_get_constructed_graph, dataset_id, user_id)
            if tool_name == "load_constructed_graph":
                return await run_sync(self.do_load_constructed_graph, session_id, dataset_id, user_id)
            method = self.do_preview_graph_optimization if tool_name == "preview_graph_optimization" else self.do_commit_graph_optimization
            return await run_sync(method, dataset_id, arguments.get("base_revision"), arguments.get("operations"), user_id)
        if tool_name in NX_TOOL_NAMES:
            routed = await run_sync(self._route_large_graph_tool, session_id, tool_name, arguments, user_id)
            if routed is not None:
                return routed
        if (job_context.get() and self._execution_host
                and tool_name in NX_TOOL_NAMES | ISOLATED_DATA_TOOLS
                and tool_name != 'load_graph_from_upload'):
            session = self.get_required(session_id, user_id=user_id)
            if not session.is_remote:
                cached = getattr(session, '_controlled_graph_info', None)
                if tool_name == 'get_graph_info' and cached and cached[0] == session.graph_version:
                    import copy
                    return copy.deepcopy(cached[1])
                from graph_data_service.isolated_compute import run_isolated
                return await self._run_with_compute_limits(user_id, lambda: run_isolated(self, session_id, user_id, tool_name, arguments))
        if tool_name == "nebula_status":
            return await run_sync(self.do_nebula_status, session_id, user_id)
        if tool_name == "nebula_sync_graph":
            return await self.submit_nebula_sync(session_id, user_id=user_id)
        if tool_name in _NEBULA_QUERY_TOOLS:
            return await run_sync(
                self._dispatch_nebula_query,
                session_id,
                tool_name,
                arguments,
                user_id,
            )
        if tool_name == "load_graph_from_upload":
            filename = _required_str(arguments, "filename")
            append = _optional_bool(arguments, "append", False)
            directed = _optional_bool(arguments, "directed", True)
            return await self.do_load_graph_from_upload(
                session_id,
                filename,
                append,
                user_id=user_id,
                directed=directed,
            )

        if tool_name == "load_graph_from_text":
            text = str(arguments.get("text") or "")
            filename = str(arguments.get("filename") or "")
            directed = _optional_bool(arguments, "directed", True)
            append = _optional_bool(arguments, "append", False)
            extraction_goal = str(arguments.get("extraction_goal") or "")
            job_id = await self._job_manager.submit(
                session_id,
                "load_graph_from_text",
                self._do_load_graph_from_text_limited,
                session_id,
                user_id,
                text,
                directed,
                _progress_kwarg="progress_callback",
                _identity={"user_id": user_id},
                filename=filename,
                append=append,
                extraction_goal=extraction_goal,
            )
            return {
                "success": True,
                "async": True,
                "job_id": job_id,
                "status": "queued",
                "message": "text2graph 已作为异步作业启动。",
            }
        if tool_name == "search_node":
            return await run_sync(self.do_search_node, session_id, arguments, user_id)
        if tool_name == "search_edge":
            return await run_sync(self.do_search_edge, session_id, arguments, user_id)
        if tool_name in _GRAPH_ML_TOOL_NAMES:
            if tool_name in _GRAPH_ML_HEAVY_TOOLS and self._wants_graph_ml_async_job(arguments):
                return await self._submit_graph_ml_tool_job(session_id, tool_name, arguments, user_id=user_id)
            if tool_name in _GRAPH_ML_HEAVY_TOOLS:
                return await self._dispatch_heavy_graph_ml_tool_once(session_id, tool_name, arguments, user_id=user_id)
            return await run_sync(self._dispatch_graph_ml_tool, session_id, tool_name, arguments, user_id=user_id)
        if tool_name == "shortest_path" and not _optional_bool(arguments, "use_weight", True):
            session = self.get_required(session_id, user_id=user_id)
            if session.is_remote:
                return await run_sync(
                    self._dispatch_graph_tool,
                    session_id,
                    tool_name,
                    arguments,
                    user_id,
                )
        if tool_name == "subgraph_extraction":
            session = self.get_required(session_id, user_id=user_id)
            if session.is_remote or _optional_bool(arguments, "create_version", True):
                return await self._run_with_compute_limits(
                    user_id,
                    lambda: run_sync(
                        self._dispatch_graph_tool,
                        session_id,
                        tool_name,
                        arguments,
                        user_id,
                    ),
                )
        if tool_name in NX_TOOL_NAMES:
            session = self.get_required(session_id, user_id=user_id)
            scope_meta: dict[str, Any] = {}
            if session.is_remote and tool_name == "degree_analysis" and arguments.get("node"):
                return await run_sync(
                    self._dispatch_graph_tool, session_id, tool_name, arguments, user_id
                )
            if session.is_remote and tool_name in {"all_shortest_paths", "path_analysis", "reachability"}:
                try:
                    nx_graph, scope_meta = await run_sync(
                        self._primary_bounded_nx_graph,
                        session,
                        tool_name,
                        arguments,
                        user_id,
                    )
                except NebulaRepositoryError as exc:
                    return exc.to_dict()
            else:
                _, _, nx_graph = await run_sync(self._require_graph, session_id, user_id=user_id)
            if scope_meta.get("target_in_scope") is False:
                if tool_name == "all_shortest_paths":
                    return {
                        "success": True,
                        "count_returned": 0,
                        "paths": [],
                        "message": f"在 {scope_meta.get('max_steps')} 跳范围内不存在路径",
                        **scope_meta,
                    }
                if tool_name == "path_analysis":
                    method = str(arguments.get("method") or "")
                    base = {
                        "success": True,
                        "method": method,
                        "message": f"在 {scope_meta.get('max_steps')} 跳范围内不存在路径",
                        **scope_meta,
                    }
                    if method == "has_path":
                        base["has_path"] = False
                    else:
                        base.update({"count": 0, "paths": [], "truncated": False})
                    return base
            result = await self._run_with_compute_limits(
                user_id,
                lambda: self._run_nx_in_pool(tool_name, nx_graph, arguments),
            )
            if scope_meta and isinstance(result, dict):
                result.update(scope_meta)
            return result
        if tool_name in FINANCE_TOOL_NAMES:
            return await self._run_with_compute_limits(
                user_id, lambda: run_sync(self._dispatch_graph_tool, session_id, tool_name, arguments, user_id),
            )
        return await run_sync(self._dispatch_graph_tool, session_id, tool_name, arguments, user_id)


def create_app(service: Optional[GraphDataService] = None) -> FastAPI:
    svc = service or GraphDataService()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        validate_service_configuration()
        from graph_engine.finance_preset import install_finance_preset
        await run_sync(install_finance_preset, _dp.data_root())
        from graph_engine.novel_preset import install_novel_preset
        await run_sync(install_novel_preset, _dp.data_root())
        await svc._construction_coordinator.restore()
        if svc._execution_host:
            await svc._execution_host.restore()
        yield
        await svc._construction_coordinator.close()
        if svc._execution_host:
            await svc._execution_host.close()
        # SIGTERM 时取消所有后台 Job 的 asyncio Task（底层线程会随进程退出）
        cancelled = svc._job_manager.cancel_all()
        if cancelled:
            await asyncio.gather(*(svc._job_manager.wait(job_id)
                for job_id in list(svc._job_manager._tasks)), return_exceptions=True)
        if svc._tool_reservations:
            await asyncio.gather(*svc._tool_reservations, return_exceptions=True)
        # 不等待 ProcessPool worker 进程自然结束，直接让 OS 回收
        svc._process_pool.shutdown(wait=False, cancel_futures=True)
        # Stop a running child process and leave its durable job queued so a
        # replacement service can safely resume it.
        svc._large_graph.close()
        svc._nebula.close()

    app = FastAPI(title="GraphDataServer", version="2.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _get_user_id(request: Request) -> str:
        return request.headers.get("X-User-ID", "").strip()

    @app.exception_handler(ConstructionError)
    async def construction_error(_request: Request, exc: ConstructionError):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    def construction_user(request: Request):
        require_service(request)
        uid = _get_user_id(request)
        if not uid:
            raise HTTPException(401, {"code": "user_required", "message": "A dataset owner is required."})
        return uid

    async def controlled_tool(session_id, tool_name, arguments, request):
        uid = _get_user_id(request)
        scoped = tool_name in DATASET_TOOLS or (uid and tool_name in {"list_datasets", "load_dataset"})
        if scoped or tool_name == "get_graph_construction_capabilities":
            uid = construction_user(request)
        if scoped and request.headers.get("X-Session-ID", session_id) != session_id:
            raise HTTPException(403, {"code": "DATASET_SCOPE_REQUIRED"})

        async def dispatch():
            if scoped:
                return await svc.dispatch_tool(session_id, tool_name, arguments, user_id=uid,
                    construction_scope=request.headers.get(SCOPE_HEADER))
            return await svc.dispatch_tool(session_id, tool_name, arguments, user_id=uid)
        rid = request.headers.get('X-Run-ID', '')
        if svc._execution_host and tool_name == 'cancel_job':
            require_service(request)
            import uuid
            cancel_id = str(uuid.UUID(request.headers['X-Operation-ID'])) if request.headers.get('X-Operation-ID') else str(uuid.uuid4())
            return await svc._execution_host.request_job_stop(uid, session_id, _required_str(arguments, 'job_id'), cancel_id)
        if svc._execution_host and rid and tool_name not in {'get_job_status', 'get_job_result', 'cancel_job'}:
            require_service(request)
            try:
                import uuid
                ctx = JobContext(uid, session_id, str(uuid.UUID(rid)), int(request.headers['X-Execution-Generation']),
                                 str(uuid.UUID(request.headers['X-Operation-ID'])))
            except (ValueError, KeyError):
                raise HTTPException(422, {'code': 'INVALID_EXECUTION_IDENTITY'})
            if tool_name in DATASET_TOOLS:
                # An idempotent replay can return a completed job without
                # invoking dispatch. Revalidate before exposing that result.
                applicability = await svc._construction_access.authorize_tool(uid, tool_name,
                    session_id=session_id, scope=request.headers.get(SCOPE_HEADER),
                    dataset_id=_required_str(arguments, "dataset_id"))
                if applicability is not None:
                    return applicability
            result = await svc._execution_host.execute(ctx, tool_name, arguments, dispatch)
            if tool_name == "list_datasets" and uid and isinstance(result, dict) and isinstance(result.get("datasets"), list):
                grant = await svc._construction_access.authorize(uid, session_id=session_id,
                    scope=request.headers.get(SCOPE_HEADER))
                allowed = set(grant.dataset_ids)
                result = {**result, "datasets": [row for row in result["datasets"]
                    if row.get("storage_backend") != "construction" or row.get("dataset_id") in allowed]}
            return result
        return await dispatch()

    async def construction_grant(request, dataset_id=None, *, cleanup=False):
        uid = construction_user(request)
        grant = await svc._construction_access.authorize(uid,
            session_id=request.headers.get("X-Session-ID"), scope=request.headers.get(SCOPE_HEADER),
            dataset_id=dataset_id, cleanup=cleanup)
        return uid, grant

    def check_source(req, grant):
        source = grant.source
        try:
            invalid = (source is None
                or (req.source_path is not None and Path(req.source_path).resolve() != Path(source.source_path).resolve())
                or (req.source_filename is not None and req.source_filename != source.source_filename))
        except (OSError, ValueError, RuntimeError):
            invalid = True
        if invalid:
            raise HTTPException(403, {"code": "invalid_source", "message": "数据源与已授权数据集不匹配。"})

    @app.get("/construction/models")
    async def construction_models_route(request: Request):
        construction_user(request)
        from graph_engine.construction.llm import construction_models, selected_model
        return {"models": construction_models(), "default_model": selected_model()}

    @app.get("/construction/model-settings")
    async def construction_model_settings(request: Request):
        require_service(request)
        construction_user(request)
        from graph_engine.construction.settings import public_settings
        return await run_sync(public_settings)

    @app.post("/construction/model-settings")
    async def save_construction_model_settings(request: Request):
        require_service(request)
        construction_user(request)
        from graph_engine.construction.settings import ConstructionSettingsWrite, save_settings
        from pydantic import ValidationError
        try:
            body = ConstructionSettingsWrite.model_validate(await request.json())
            return await run_sync(save_settings, body)
        except (ValidationError, ValueError):
            raise HTTPException(422, {"code": "invalid_construction_model_settings",
                "message": "请检查模型 ID、HTTP(S) API URL 和 API Key；首次配置请同时填写 URL 和 Key。"}) from None

    @app.get("/construction/capabilities")
    def construction_capabilities(request: Request):
        construction_user(request)
        return svc._construction.capabilities()

    @app.get("/construction/{dataset_id}")
    async def construction_status(dataset_id: str, request: Request):
        uid = construction_user(request)
        applicability = await svc._construction_access.authorize_tool(uid, "get_graph_construction_status",
            session_id=request.headers.get("X-Session-ID"), scope=request.headers.get(SCOPE_HEADER),
            dataset_id=dataset_id)
        if applicability is not None:
            return applicability
        return await svc._construction_coordinator.status(uid, dataset_id)

    @app.post("/construction/{dataset_id}/source")
    async def construction_source(dataset_id: str, req: ConstructionSourceReq, request: Request):
        uid, grant = await construction_grant(request, dataset_id)
        check_source(req, grant)
        svc._construction_coordinator.ensure_writable(uid, dataset_id)
        return await run_sync(svc._construction.register_source, uid, dataset_id, grant.source.source_path, grant.source.source_filename)

    @app.post("/construction/{dataset_id}/build")
    async def construction_build(dataset_id: str, req: ConstructionBuildReq, request: Request):
        uid, grant = await construction_grant(request, dataset_id)
        check_source(req, grant)
        # An omitted path keeps the registered source/ready-result reuse contract.
        source_path = grant.source.source_path if req.source_path is not None else None
        return await svc._construction_coordinator.build(uid, dataset_id, source_path, grant.source.source_filename, req.options)

    @app.post("/construction/{dataset_id}/preset")
    async def construction_preset(dataset_id: str, req: ConstructionPresetReq, request: Request):
        require_service(request)
        uid, grant = await construction_grant(request, dataset_id)
        if grant.scope != "personal" or grant.source is None:
            raise HTTPException(403, "示例数据源必须为已授权个人数据集")
        svc._construction_coordinator.ensure_writable(uid, dataset_id)
        from graph_engine.user_case_sources import seed_construction
        return await run_sync(seed_construction, svc._construction, uid, dataset_id,
            grant.source.source_path, grant.source.source_filename, req.case_id)

    @app.post("/construction/{dataset_id}/import")
    async def construction_import(dataset_id: str, req: ConstructionImportReq, request: Request):
        require_service(request)
        uid, target_grant = await construction_grant(request, dataset_id)
        if not target_grant.scope.startswith("project:") or target_grant.source is None:
            raise HTTPException(403, "导入目标必须为已授权项目数据集")
        await svc._construction_access.authorize(uid, scope="personal", dataset_id=req.source_dataset_id)
        svc._construction_coordinator.ensure_writable(uid, dataset_id)
        svc._construction_coordinator.ensure_writable(uid, req.source_dataset_id)
        from graph_data_service.construction_import import copy_constructed_graph
        return await run_sync(copy_constructed_graph, svc._construction, uid, req.source_dataset_id,
            dataset_id, target_grant.source.source_path, target_grant.source.source_filename)

    @app.get("/construction/{dataset_id}/graph")
    async def constructed_graph(dataset_id: str, request: Request):
        uid, _ = await construction_grant(request, dataset_id)
        return await run_sync(svc.do_get_constructed_graph, dataset_id, uid)

    @app.get("/construction/{dataset_id}/schema")
    async def constructed_schema(dataset_id: str, request: Request):
        uid, _ = await construction_grant(request, dataset_id)
        return await run_sync(svc.do_get_constructed_schema, dataset_id, uid)

    @app.get("/construction/{dataset_id}/source-preview")
    async def construction_source_preview(dataset_id: str, request: Request, delimiter: str = ","):
        from graph_data_service.source_preview import source_preview
        _uid, grant = await construction_grant(request, dataset_id)
        if grant.source is None:
            raise HTTPException(404, {"code": "source_preview_unavailable", "message": "数据源不可用。"})
        return await run_sync(source_preview, grant.source.source_path, grant.source.source_filename,
                              dataset_id, delimiter)

    @app.post("/construction/{dataset_id}/preview")
    async def construction_preview(dataset_id: str, req: ConstructionOptimizeReq, request: Request):
        uid, _ = await construction_grant(request, dataset_id)
        svc._construction_coordinator.ensure_writable(uid, dataset_id)
        return await run_sync(svc.do_preview_graph_optimization, dataset_id, req.base_revision, req.operations, uid)

    @app.post("/construction/{dataset_id}/commit")
    async def construction_commit(dataset_id: str, req: ConstructionSaveReq, request: Request):
        uid = construction_user(request)
        if request.headers.get("X-Run-ID") and svc._execution_host:
            sid = request.headers.get("X-Session-ID", "")
            if not sid:
                raise HTTPException(422, {"code": "INVALID_EXECUTION_IDENTITY"})
            return await controlled_tool(sid, "commit_graph_optimization", {"dataset_id": dataset_id, **req.model_dump()}, request)
        uid, _ = await construction_grant(request, dataset_id)
        svc._construction_coordinator.ensure_writable(uid, dataset_id)
        return await run_sync(svc.do_commit_graph_optimization, dataset_id, req.base_revision, req.operations, uid)

    @app.delete("/construction/{dataset_id}")
    async def construction_delete(dataset_id: str, request: Request):
        uid, _ = await construction_grant(request, dataset_id, cleanup=True)
        svc._construction_coordinator.ensure_writable(uid, dataset_id)
        return await run_sync(svc._construction.delete, uid, dataset_id)

    @app.post("/sessions/{session_id}/graph/load_construction")
    async def load_constructed_graph(session_id: str, req: ConstructionLoadReq, request: Request):
        uid = construction_user(request)
        if not request.headers.get("X-Run-ID"):
            active = [job for job in svc._job_manager.list_jobs(session_id)
                      if job.user_id == uid and job.status not in {"done", "failed", "cancelled"}]
            if active:
                raise HTTPException(409, {"code": "SESSION_EXECUTION_ACTIVE", "message": "The session is processing a graph."})
        return await controlled_tool(session_id, "load_constructed_graph", {"dataset_id": req.dataset_id}, request)

    @app.get('/internal/capabilities')
    async def control_capabilities(request: Request):
        require_service(request)
        from importlib.metadata import version
        # Remote graph writes have a different commit protocol. Keep that
        # deployment on its existing path until its executor is certified.
        ready = bool(svc._execution_host and not svc.nebula_primary and os.name == 'posix'
                     and os.path.isdir('/dev/fd') and version('anyio') == '4.14.1')
        return {'run_cancel_v1': ready, 'job_cancel_v1': ready}

    @app.post('/internal/sessions/{sid}/runs/{rid}/cancel')
    async def stop_owned_run(sid: str, rid: str, request: Request):
        require_service(request)
        uid = _get_user_id(request)
        ids = svc._job_manager.cancel_run(uid, sid, rid)
        durable = await run_sync(svc._large_graph.catalog.execution_jobs,
                                 user_id=uid, session_id=sid, run_id=rid, active_only=True)
        for job in durable:
            await run_sync(svc._large_graph.cancel, job['job_id'], user_id=uid, session_id=sid)
        pending = [jid for jid in ids if svc._job_manager.status(jid).status not in {'done', 'failed', 'cancelled'}]
        for job in durable:
            state = await run_sync(svc._large_graph.job_control, job['job_id'], user_id=uid, session_id=sid)
            if not state['execution_stopped']:
                pending.append(job['job_id'])
        return {'run_id': rid, 'pending_jobs': pending}

    @app.post('/internal/sessions/{sid}/large-jobs/{jid}/cancel')
    async def stop_large_job(sid: str, jid: str, request: Request):
        require_service(request)
        import uuid
        body = await request.json()
        try:
            cancel_id = str(uuid.UUID(body['cancel_request_id']))
        except (KeyError, ValueError, TypeError):
            raise HTTPException(422, {'code': 'INVALID_CANCEL_IDENTITY'})
        uid = _get_user_id(request)
        if svc._execution_host:
            return await svc._execution_host.request_job_stop(uid, sid, jid, cancel_id)
        state = await run_sync(svc._large_graph.job_control, jid, user_id=uid, session_id=sid)
        if state is None:
            raise HTTPException(404, {'code': 'JOB_NOT_FOUND'})
        await run_sync(svc._large_graph.cancel, jid, user_id=uid, session_id=sid)
        return await run_sync(svc._large_graph.job_control, jid, user_id=uid, session_id=sid)

    @app.get('/internal/sessions/{sid}/large-jobs/{jid}/control')
    def large_job_control(sid: str, jid: str, request: Request):
        require_service(request)
        state = svc._large_graph.job_control(jid, user_id=_get_user_id(request), session_id=sid)
        if state is None:
            raise HTTPException(404, {'code': 'JOB_NOT_FOUND'})
        return state

    @app.post('/internal/sessions/{sid}/jobs/{jid}/cancel')
    async def stop_owned_job(sid: str, jid: str, request: Request):
        require_service(request)
        if not svc._execution_host:
            raise HTTPException(409, {'code': 'JOB_CANCEL_UNAVAILABLE'})
        return svc._execution_host.cancel_job(_get_user_id(request), sid, jid)

    @app.get('/internal/sessions/{sid}/resource-status')
    def session_resource_status(sid: str, request: Request):
        require_service(request)
        uid = _get_user_id(request)
        pending = [job.job_id for job in svc._job_manager.list_jobs(sid)
                   if job.user_id == uid and job.status not in {'done', 'failed', 'cancelled'}]
        pending.extend(row['job_id'] for row in svc._large_graph.catalog.execution_jobs(
            user_id=uid, session_id=sid, active_only=True))
        pending.extend(row['job_id'] for row in svc._large_graph.catalog.active_scope_jobs(user_id=uid, session_id=sid))
        pending = sorted(set(pending))
        return {'execution_stopped': not pending, 'pending_jobs': pending}

    @app.post('/internal/artifacts/ownership')
    async def record_artifact_owners(request: Request):
        require_service(request)
        uid = request.headers.get('X-User-ID', '')
        body = await request.json()
        for item in body.get('sessions', []):
            svc._graph_ml._store.record_owner(svc._session_key(item['session_id'], uid), uid,
                                              item['session_id'], item.get('project_id'))
        return {'success': True}

    @app.delete('/internal/projects/{project_id}/artifacts')
    async def delete_project_artifacts(project_id: str, request: Request):
        require_service(request)
        uid = request.headers.get('X-User-ID', '')
        body = await request.json()
        targets = svc._graph_ml._store.project_targets(uid, project_id, body.get('session_ids', []))
        for owner in targets.values():
            sid = owner['session_id']
            if any(job.user_id == uid and job.status not in {'done', 'failed', 'cancelled'}
                   for job in svc._job_manager.list_jobs(sid)):
                raise HTTPException(409, {'code': 'SESSION_EXECUTION_ACTIVE'})
        count = await run_sync(svc._graph_ml._store.delete_project, uid, project_id, body.get('session_ids', []))
        return {'success': True, 'deleted_sessions': count}

    @app.get('/internal/artifacts/ownership-report')
    def artifact_ownership_report(request: Request):
        require_service(request)
        return svc._graph_ml._store.ownership_report()

    @app.get("/datasets")
    async def list_datasets_endpoint(request: Request):
        if request.headers.get("X-Session-ID") or request.headers.get(SCOPE_HEADER):
            uid, grant = await construction_grant(request)
            return await run_sync(svc.do_list_datasets, uid, tuple(grant.dataset_ids))
        # Legacy registry requests without scope retain their public/account
        # catalog. Construction entries always require an explicit grant.
        return await run_sync(svc.do_list_datasets, _get_user_id(request))

    @app.post("/sessions/{session_id}")
    async def create_session(session_id: str, request: Request):
        user_id = _get_user_id(request)
        if svc._execution_host:
            require_service(request)
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.get(svc._execution_host.runtime_url + f'/internal/sessions/{session_id}/admission',
                                            headers=service_headers(user_id))
                if response.status_code >= 400:
                    raise HTTPException(response.status_code, response.json().get('detail'))
        body = await request.json() if await request.body() else {}
        with svc._session_lifecycle_lock:
            session = svc.get_or_create(session_id, user_id=user_id)
            if 'project_id' in body:
                svc._graph_ml._store.record_owner(svc._session_key(session_id, user_id), user_id, session_id, body['project_id'])
        _service_logger(session_id, "graph_data", user_id=user_id).event(
            "SESSION_CREATE", graph_version=session.graph_version
        )
        return {
            "success": True,
            "session_id": session_id,
            "graph_version": session.graph_version,
        }

    @app.patch("/sessions/{session_id}/config")
    def update_session_config(session_id: str, body: dict, request: Request):
        user_id = _get_user_id(request)
        session = svc.get_required(session_id, user_id=user_id)
        if "llm_model" in body:
            session.llm_model = str(body["llm_model"] or "")
        return {"success": True, "session_id": session_id}

    @app.delete("/datasets/{dataset_id}")
    def delete_dataset(dataset_id: str, request: Request):
        user_id = _get_user_id(request)
        return svc.do_delete_dataset(dataset_id, user_id=user_id)

    @app.delete("/sessions/{session_id}")
    def delete_session(session_id: str, request: Request, permanent: bool = False):
        user_id = _get_user_id(request)
        if permanent:
            require_service(request)
        _service_logger(session_id, "graph_data", user_id=user_id).event("SESSION_DELETE")
        svc.delete_session(session_id, user_id=user_id, permanent=permanent)
        close_service_loggers(session_id)
        return {"success": True, "session_id": session_id}

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "sessions": svc.session_count,
            "graph_storage_backend": svc._storage_backend,
            "nebula_enabled": bool(svc._nebula.enabled),
        }

    @app.get("/tools")
    def list_tools(include_graph_ml: bool = True, include_reporting: bool | None = None):
        """返回工具规格列表（tool_catalog 唯一真相源）。"""
        return get_all_public_tools(
            include_graph_ml=include_graph_ml,
            include_reporting=include_reporting,
        )

    @app.post("/server-datasets/prepare")
    def prepare_server_dataset(req: ServerDatasetPrepareReq):
        source_path = _registered_server_dataset_path(
            req.source_path,
            relative_path=req.relative_path,
            server_dataset_id=req.server_dataset_id,
            revision=req.revision,
            directed=req.directed,
        )
        if source_path is None:
            raise HTTPException(
                status_code=403,
                detail="服务器数据源引用不在预置目录中或版本已经变化",
            )
        return svc.ensure_server_dataset_prepared(
            server_dataset_id=req.server_dataset_id,
            revision=req.revision,
            source_path=str(source_path),
            source_name=req.original_filename,
            relative_path=req.relative_path,
            directed=req.directed,
        )

    @app.post("/sessions/{session_id}/server-datasets/attach")
    def attach_server_dataset(
        session_id: str, req: ServerDatasetAttachReq, request: Request
    ):
        return svc.attach_server_dataset(
            session_id,
            user_id=_get_user_id(request),
            server_dataset_id=req.server_dataset_id,
            revision=req.revision,
        )

    @app.post("/sessions/{session_id}/uploads/register")
    def register_upload(session_id: str, req: RegisterUploadReq, request: Request):
        user_id = _get_user_id(request)
        registered_path = _registered_upload_path(req.saved_path, user_id)
        if registered_path is None:
            raise HTTPException(
                status_code=403,
                detail="上传引用必须是当前用户 Graph Engine 上传目录中的普通文件",
            )
        session = svc.get_or_create(session_id, user_id=user_id)
        session.register_upload(
            file_token=req.file_token,
            original_filename=req.original_filename,
            saved_path=str(registered_path),
        )
        return {
            "success": True,
            "session_id": session_id,
            "filename": req.original_filename,
            "file_token": req.file_token,
        }

    @app.post("/sessions/{session_id}/uploads/ingest")
    async def ingest_upload(
        session_id: str,
        request: Request,
        file_token: str = Form(...),
        original_filename: str = Form(...),
        file: UploadFile = File(...),
    ):
        user_id = _get_user_id(request)
        session = svc.get_or_create(session_id, user_id=user_id)
        if user_id:
            upload_root = _dp.user_uploads_dir(user_id)
        else:
            upload_root = Path(_graph_data_upload_root())
        session_dir = upload_root / _dp.safe_path_component(session_id, "_session")
        session_dir.mkdir(parents=True, exist_ok=True)

        safe_name = _sanitize_filename(original_filename or file.filename or "")
        safe_token = _sanitize_filename(file_token or "upload").strip(".") or "upload"
        saved_path = (session_dir / f"{safe_token}_{safe_name}").resolve()
        if not saved_path.is_relative_to(session_dir.resolve()):
            raise HTTPException(status_code=400, detail="非法上传路径")
        size_bytes = 0
        temporary_path = saved_path.with_name(f".{saved_path.name}.uploading")
        max_upload_bytes = max(
            1, _env_int("LARGE_GRAPH_UPLOAD_MAX_BYTES", 100 * 1024**3)
        )
        user_quota_bytes = max(
            max_upload_bytes,
            _env_int("LARGE_GRAPH_USER_UPLOAD_QUOTA_BYTES", 200 * 1024**3),
        )
        existing_size = saved_path.stat().st_size if saved_path.is_file() else 0
        existing_usage = max(0, _regular_file_bytes(upload_root) - existing_size)
        disk_reserve = max(
            0, _env_int("LARGE_GRAPH_DISK_RESERVE_BYTES", 2 * 1024**3)
        )
        try:
            with temporary_path.open("wb") as output:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"单文件上传超过 {max_upload_bytes} 字节上限",
                        )
                    if existing_usage + size_bytes > user_quota_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"用户上传总量超过 {user_quota_bytes} 字节配额",
                        )
                    if shutil.disk_usage(upload_root).free < disk_reserve + len(chunk):
                        raise HTTPException(
                            status_code=507,
                            detail="磁盘剩余空间已触及大图服务预留线",
                        )
                    output.write(chunk)
            temporary_path.replace(saved_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        session.register_upload(
            file_token=file_token,
            original_filename=original_filename,
            saved_path=str(saved_path),
        )
        _service_logger(session_id, "graph_data", user_id=user_id).event(
            "UPLOAD_INGEST",
            filename=original_filename,
            file_token=file_token,
            saved_path=str(saved_path),
            size_bytes=size_bytes,
        )
        return {
            "success": True,
            "session_id": session_id,
            "filename": original_filename,
            "file_token": file_token,
            "saved_path": str(saved_path),
            "size": size_bytes,
        }

    @app.post("/sessions/{session_id}/graph/load_text")
    async def load_graph_text(session_id: str, req: LoadGraphTextReq, request: Request):
        return await controlled_tool(session_id, "load_graph", req.model_dump(), request)

    @app.post("/sessions/{session_id}/graph/load_upload")
    async def load_graph_upload(session_id: str, req: LoadGraphUploadReq, request: Request):
        return await controlled_tool(session_id, 'load_graph_from_upload', req.model_dump(), request)

    @app.post("/sessions/{session_id}/graph/clear")
    async def clear_graph(session_id: str, request: Request):
        return await controlled_tool(session_id, "clear_graph", {}, request)

    @app.post("/sessions/{session_id}/graph/to_undirected")
    async def to_undirected(session_id: str, request: Request):
        return await controlled_tool(session_id, "to_undirected", {}, request)

    @app.get("/sessions/{session_id}/graph/info")
    async def graph_info(session_id: str, request: Request):
        return await controlled_tool(session_id, "get_graph_info", {}, request)

    @app.post("/sessions/{session_id}/graph/visualize")
    async def visualize_graph(session_id: str, request: Request, req: ToolInvokeReq = None):
        """获取图的完整结构用于可视化渲染（直接端点）。"""
        arguments = req.arguments if req else {}
        return await controlled_tool(session_id, "visualize_subgraph", arguments, request)

    @app.get("/sessions/{session_id}/graph/document")
    async def graph_document(session_id: str, request: Request):
        user_id = _get_user_id(request)
        return await svc._run_with_tool_policy(session_id, "get_graph_info", {}, user_id,
            lambda: run_sync(graph_document_snapshot, session_id, request))

    def graph_document_snapshot(session_id: str, request: Request):
        user_id = _get_user_id(request)
        session = svc.get_required(session_id, user_id=user_id)
        gd = session.get_graph()
        if gd is None and session.is_remote:
            max_nodes, max_edges = svc._primary_materialize_limits()
            try:
                gd = svc._nebula.materialize_subgraph(
                    user_id=user_id,
                    session_id=session_id,
                    graph_version=session.graph_version,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                )
            except NebulaRepositoryError as exc:
                return {
                    "loaded": True,
                    "backend": "nebula",
                    "session_id": session_id,
                    "graph_version": session.graph_version,
                    "graph": None,
                    **exc.to_dict(),
                }
        if gd is None:
            return {
                "loaded": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "graph": None,
                "message": "尚未加载任何图数据。",
            }
        return {
            "loaded": True,
            "backend": "nebula" if session.is_remote else "local",
            "session_id": session_id,
            "graph_version": session.graph_version,
            "graph": graph_data_to_document(gd),
        }

    @app.get(
        "/sessions/{session_id}/large-graph/artifacts/"
        "{artifact_id}/partitions/{partition}"
    )
    def download_large_graph_artifact_partition(
        session_id: str, artifact_id: str, partition: int, request: Request
    ):
        """Download one full-result partition outside the Agent/tool context."""
        user_id = _get_user_id(request)
        result_path = svc._large_graph.artifact_partition_path(
            artifact_id,
            partition,
            user_id=user_id,
            session_id=session_id,
        )
        if result_path is None:
            raise HTTPException(status_code=404, detail="Artifact 分区不存在")
        # NDJSON allows clients to stream the partition without loading it all.
        return FileResponse(
            result_path,
            media_type="application/x-ndjson",
            filename=f"{artifact_id}-part-{int(partition):05d}.jsonl",
        )

    @app.post("/sessions/{session_id}/tools/{tool_name}")
    async def execute_tool(session_id: str, tool_name: str, req: ToolInvokeReq, request: Request):
        user_id = _get_user_id(request)
        arguments = req.arguments or {}
        if not isinstance(arguments, dict):
            return {"success": False, "error": "arguments 必须为对象。"}

        if tool_name in _DATA_TOOLS:
            svc_name = "graph_data"
        elif tool_name in _GRAPH_ML_TOOL_NAMES:
            svc_name = "graph_ml"
        elif tool_name in _REPORTING_TOOL_NAMES:
            svc_name = "graph_compute"
        else:
            svc_name = "graph_compute"
        svc_log = _service_logger(session_id, svc_name, user_id=user_id)
        svc_log.request(tool_name, arguments)
        t0 = time.monotonic()

        try:
            result = await controlled_tool(session_id, tool_name, arguments, request)
            duration_ms = int((time.monotonic() - t0) * 1000)
            svc_log.response(tool_name, result, duration_ms)
            return result
        except ConstructionError:
            raise
        except ValueError as e:
            svc_log.error(tool_name, str(e))
            return {"success": False, "error": str(e)}
        except NebulaRepositoryError as e:
            svc_log.error(tool_name, str(e))
            return e.to_dict()
        except HTTPException:
            raise
        except Exception as e:
            svc_log.error(tool_name, str(e))
            raise HTTPException(status_code=500, detail=f"工具执行失败: {e}")

    return app

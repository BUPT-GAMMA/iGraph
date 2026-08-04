"""
GraphData FastAPI app factory.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict, Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import numpy as np
from pydantic import BaseModel, Field

import backend.data_paths as _dp
from backend.logging.logger import close_service_loggers, get_service_logger
from backend.agent.model_registry import DEFAULT_MODEL, normalize_model_name, provider_for_model
from backend.agent.provider_gateway import ProviderGateway
from backend.agent.sub_agents.file_format_classifier import FileFormatPreCheckSubAgent
from backend.agent.sub_agents.text_graph_extractor import TextGraphExtractionSubAgent
from graph_data_service.data_pipeline import GraphBuilder
from graph_data_service.dataset_registry import DatasetRegistry
from graph_data_service.tool_catalog import GRAPH_ML_TOOLS, get_all_public_tools
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
from graph_data_service.embedding import default_provider
from graph_data_service.graph_data import GraphData
from graph_data_service.graph_ml import GraphMLModule
from graph_data_service.job_manager import JobManager
from graph_data_service.rag import LightweightRAG
from graph_data_service.search import EdgeSearchRequest, EntitySearchIndex, NodeSearchRequest
from graph_data_service.session import GraphSession
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
}

_GRAPH_ML_TOOL_NAMES = {spec.get("name", "").strip() for spec in GRAPH_ML_TOOLS}
_GRAPH_ML_HEAVY_TOOLS = {"train", "infer"}
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


class ToolInvokeReq(BaseModel):
    arguments: Dict[str, Any] = Field(default_factory=dict)


class _NullTraceLogger:
    def log_trace_event(self, *_args, **_kwargs) -> None:
        return None


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

    if nl_lines > len(sample) * 0.2:
        return "freetext", detail
    if long_lines > len(sample) * 0.2:
        return "freetext", detail

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
    ) -> None:
        self._sessions: Dict[str, GraphSession] = {}
        self._compute = GraphCompute()
        self._graph_ml = GraphMLModule(get_session=self.get_required)
        # Migrate legacy datasets/ → data/global/datasets/ on first startup.
        try:
            from backend.migration import migrate_global_datasets
            migrate_global_datasets()
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
        self._job_manager = JobManager()
        self._nebula = nebula_repository or NebulaGraphRepository()
        self._embedding_provider: Any = None  # lazy-initialized on first search/rag use
        _workers = max(1, int(os.getenv("GRAPH_COMPUTE_CONCURRENCY", str(min(4, (os.cpu_count() or 2))))))
        self._process_pool = concurrent.futures.ProcessPoolExecutor(max_workers=_workers)
        self._rag_cache: Dict[str, LightweightRAG] = {}
        self._search_index_cache: Dict[str, EntitySearchIndex] = {}
        self._graph_ml_inflight: Dict[tuple[str, str, str], asyncio.Task] = {}
        self._graph_ml_inflight_lock = asyncio.Lock()
        self._compute_global_sem = asyncio.Semaphore(_env_int("GRAPH_COMPUTE_GLOBAL_CONCURRENCY", _workers))
        self._compute_per_user_limit = _env_int("GRAPH_COMPUTE_PER_USER_CONCURRENCY", 1)
        self._compute_user_sems: dict[str, asyncio.Semaphore] = {}
        self._heavy_global_sem = asyncio.Semaphore(_env_int("GRAPH_HEAVY_GLOBAL_CONCURRENCY", max(1, min(2, _workers))))
        self._heavy_per_user_limit = _env_int("GRAPH_HEAVY_PER_USER_CONCURRENCY", 1)
        self._heavy_user_sems: dict[str, asyncio.Semaphore] = {}
        self._restore_sessions_from_disk()

    @staticmethod
    def _create_text_graph_provider_gateway(model_override: str = "") -> ProviderGateway | None:
        model_raw = model_override or os.getenv("TEXT_GRAPH_EXTRACTOR_MODEL") or os.getenv("LLM_MODEL") or os.getenv("LLM_DEFAULT_MODEL") or DEFAULT_MODEL
        try:
            model = normalize_model_name(model_raw)
            provider = provider_for_model(model)
        except ValueError:
            return None

        if provider in {"openai", "anthropic"}:
            api_key = os.getenv("LLM_API_KEY")
            base_url = os.getenv("LLM_BASE_URL")
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

    def _get_user_dataset_registry(self, user_id: str) -> DatasetRegistry:
        if user_id not in self._user_dataset_registries:
            user_dir = str(_dp.user_datasets_dir(user_id))
            self._user_dataset_registries[user_id] = DatasetRegistry(user_dir)
        return self._user_dataset_registries[user_id]

    def get_or_create(self, session_id: str, user_id: str = "") -> GraphSession:
        key = self._session_key(session_id, user_id)
        if key not in self._sessions:
            self._sessions[key] = GraphSession(session_id=session_id, user_id=user_id)
            self._sessions[key].load_snapshot()
        return self._sessions[key]

    def get_required(self, session_id: str, user_id: str = "") -> GraphSession:
        key = self._session_key(session_id, user_id)
        session = self._sessions.get(key)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return session

    def delete_session(self, session_id: str, user_id: str = "") -> None:
        key = self._session_key(session_id, user_id)
        self._sessions.pop(key, None)
        self._rag_cache.pop(key, None)
        self._search_index_cache.pop(key, None)

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
            for root_name in ("sessions", "uploads"):
                root = user_dir / root_name
                if not root.exists():
                    continue
                for session_dir in root.iterdir():
                    if not session_dir.is_dir() or not (session_dir / "graph_current.npz").exists():
                        continue
                    restore_one(session_dir.name, user_id=user_dir.name)

    def _require_graph(self, session_id: str, user_id: str = ""):
        session = self.get_required(session_id, user_id=user_id)
        gd = session.get_graph()
        if gd is None:
            raise ValueError("尚未加载任何图数据。")
        nx_graph = self._compute.resolve_graph(self._cache_key(session_id, user_id), session.graph_version, gd)
        return session, gd, nx_graph

    # ── NebulaGraph 混合存储 ────────────────────────────────────────

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
        if graph is None:
            return {
                "success": False,
                "backend": "nebula",
                "code": "NOT_SYNCED",
                "error": "尚未加载任何图数据。",
            }
        try:
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
        session._parse_edgelist(edge_text, append=append)
        self._compute.invalidate(self._cache_key(session_id, user_id))
        return {
            "success": True,
            "session_id": session_id,
            "graph_version": session.graph_version,
            "message": (
                f"图加载成功（已{('追加' if append else '覆盖')}当前图）。"
                " 如需查看图规模与结构摘要，请调用 get_graph_info。"
            ),
        }

    async def do_load_graph_from_upload(self, session_id: str, filename: str, append: bool, user_id: str = "") -> dict:
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

        # 二进制或结构化 JSON：由 session 层按扩展名处理
        if lower_path.endswith((".npz", ".pt", ".json")):
            svc_log.event("UPLOAD_FORMAT_DETECTED", filename=record.original_filename, format="binary_json")
            try:
                session.load_graph_from_upload_path(record.saved_path, append=append)
                self._compute.invalidate(self._cache_key(session_id, user_id))
            except FileNotFoundError as e:
                svc_log.event("UPLOAD_LOAD_ERROR", filename=record.original_filename, format="binary_json", error=str(e))
                return {
                    "success": False,
                    "session_id": session_id,
                    "graph_version": session.graph_version,
                    "filename": record.original_filename,
                    "error": str(e),
                }
            except Exception as e:
                svc_log.event("UPLOAD_LOAD_ERROR", filename=record.original_filename, format="binary_json", error=str(e))
                raise HTTPException(status_code=500, detail=f"加载上传文件失败: {e}")
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
        session_model = session.llm_model
        if session_model:
            session_gateway = self._create_text_graph_provider_gateway(session_model)
            if session_gateway is not None:
                classifier = FileFormatPreCheckSubAgent(provider_gateway=session_gateway)
        use_heuristic = not classifier.available()
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
                # LLM 调用失败，降级到启发式
                use_heuristic = True

        if use_heuristic:
            fmt_raw, fmt_detail = _detect_text_format_verbose(text)
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
                n, e = session._parse_edgelist(text, append=append)
                self._compute.invalidate(self._cache_key(session_id, user_id))
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
            result = self.do_load_graph_from_triples(session_id, triples, directed=True, append=append, user_id=user_id)
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
            True,
            _progress_kwarg="progress_callback",
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
        session.clear_graph()
        self._compute.invalidate(self._cache_key(session_id, user_id))
        return {
            "success": True,
            "session_id": session_id,
            "graph_version": session.graph_version,
            "message": "图已清空。",
        }

    def do_undo_graph(self, session_id: str, user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
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
            "num_nodes": gd.num_nodes if gd is not None else 0,
            "num_edges": gd.num_edges if gd is not None else 0,
            "message": "图已回滚到上一版本。",
        }

    def do_to_undirected(self, session_id: str, user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
        gd = session.get_graph()
        if gd is None:
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "error": "尚未加载任何图数据。",
            }
        if not gd.directed:
            return {
                "success": False,
                "session_id": session_id,
                "graph_version": session.graph_version,
                "message": "当前图已经是无向图，无需转换。",
            }
        new_gd = gd.to_undirected()
        session.set_graph(new_gd, description="转换为无向图")
        self._compute.invalidate(self._cache_key(session_id, user_id))
        return {
            "success": True,
            "session_id": session_id,
            "graph_version": session.graph_version,
            "message": "图已转换为无向图。如需查看结构摘要，请调用 get_graph_info。",
            "num_nodes": new_gd.num_nodes,
            "num_edges": new_gd.num_edges,
        }

    def do_list_datasets(self, user_id: str = "") -> dict:
        global_metas = self._dataset_registry.list_datasets()
        global_dicts = [dict(m.to_dict(), scope="global") for m in global_metas]
        if user_id:
            user_metas = self._get_user_dataset_registry(user_id).list_datasets()
            user_dicts = [dict(m.to_dict(), scope="user") for m in user_metas]
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
        graph = registry.load_graph(dataset_id, version=version)
        actual_version = version if version != "latest" else registry.get(dataset_id).versions[-1]
        session.set_graph(graph, description=f"加载数据集 {dataset_id}@{actual_version}")
        session.bind_dataset(dataset_id, actual_version)
        self._compute.invalidate(self._cache_key(session_id, user_id))
        return {
            "success": True,
            "session_id": session_id,
            "dataset_id": dataset_id,
            "dataset_version": actual_version,
            "graph_version": session.graph_version,
            "num_nodes": graph.num_nodes,
            "num_edges": graph.num_edges,
        }

    def do_save_as_dataset(self, session_id: str, name: str, description: str, user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
        graph = session.get_graph()
        if graph is None:
            return {"success": False, "error": "尚未加载任何图数据。"}
        registry = self._get_user_dataset_registry(user_id) if user_id else self._dataset_registry
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
            "num_nodes": loaded.get("num_nodes", loaded.get("node_count", 0)),
            "num_edges": loaded.get("num_edges", loaded.get("edge_count", 0)),
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
        if append and session.get_graph() is not None:
            graph = self._merge_graphs(session.get_graph(), graph, directed=directed)
        session.set_graph(
            graph,
            description="从结构化三元组构建图" + ("（追加）" if append else ""),
        )
        cache_key = self._cache_key(session_id, user_id)
        self._compute.invalidate(cache_key)
        self._search_index_cache.pop(cache_key, None)
        return {
            "success": True,
            "session_id": session_id,
            "graph_version": session.graph_version,
            "nodes": graph.num_nodes,
            "edges": graph.num_edges,
            "num_nodes": graph.num_nodes,
            "num_edges": graph.num_edges,
            "directed": graph.directed,
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
        if session_model:
            call_gateway = self._create_text_graph_provider_gateway(session_model)
            if call_gateway is not None:
                from backend.agent.sub_agents.text_graph_extractor import TextGraphExtractionSubAgent
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
        if append and session.get_graph() is not None:
            graph = self._merge_graphs(session.get_graph(), graph, directed=directed)
        session.set_graph(
            graph,
            description="从文本构建图" + (f": {filename}" if filename else ""),
        )
        cache_key = self._cache_key(session_id, user_id)
        self._compute.invalidate(cache_key)
        self._search_index_cache.pop(cache_key, None)
        return {
            "success": True,
            "session_id": session_id,
            "graph_version": session.graph_version,
            "nodes": graph.num_nodes,
            "edges": graph.num_edges,
            "num_nodes": graph.num_nodes,
            "num_edges": graph.num_edges,
            "directed": graph.directed,
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

    def do_search_node(self, session_id: str, arguments: Dict[str, Any], user_id: str = "") -> dict:
        index = self._get_search_index(session_id, user_id=user_id)
        req = NodeSearchRequest(
            query=str(arguments.get("query") or ""),
            top_k=int(arguments.get("top_k", 8)),
            filters=arguments.get("filters") if isinstance(arguments.get("filters"), dict) else {},
            sort=arguments.get("sort") if isinstance(arguments.get("sort"), dict) else None,
            rerank=_optional_bool(arguments, "rerank", True),
        )
        return index.search_nodes(req)

    def do_search_edge(self, session_id: str, arguments: Dict[str, Any], user_id: str = "") -> dict:
        index = self._get_search_index(session_id, user_id=user_id)
        req = EdgeSearchRequest(
            query=str(arguments.get("query") or ""),
            src_query=arguments.get("src_query"),
            dst_query=arguments.get("dst_query"),
            top_k=int(arguments.get("top_k", 8)),
            filters=arguments.get("filters") if isinstance(arguments.get("filters"), dict) else {},
            sort=arguments.get("sort") if isinstance(arguments.get("sort"), dict) else None,
            rerank=_optional_bool(arguments, "rerank", True),
        )
        return index.search_edges(req)

    def do_get_graph_info(self, session_id: str, user_id: str = "") -> dict:
        session = self.get_required(session_id, user_id=user_id)
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
        graph_info_payload = {
            "num_nodes": summary.num_nodes,
            "num_edges": summary.num_edges,
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
            "num_nodes": summary.num_nodes,
            "num_edges": summary.num_edges,
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

    def _dispatch_graph_tool(self, session_id: str, tool_name: str, arguments: Dict[str, Any], user_id: str = "") -> dict:
        """图数据/图计算工具分发。"""
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
            job = self._job_manager.status(job_id)
            pending = self._job_manager.drain_pending_progress(job_id)
            return {
                "success": bool(job),
                "job": job.to_dict() if job else None,
                "progress_updates": pending,
            }

        if tool_name == "cancel_job":
            job_id = _required_str(arguments, "job_id")
            return {"success": self._job_manager.cancel(job_id), "job_id": job_id}

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
            return self._compute.community_detection(nx_graph, algorithm)

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
            include_community = bool(arguments.get("include_community", False))
            highlights = arguments.get("highlights")
            max_nodes = arguments.get("max_nodes")
            return self.do_visualize_subgraph(session_id, node_names, include_community, highlights, max_nodes=max_nodes, user_id=user_id)

        return {"success": False, "error": f"未知工具: {tool_name}"}

    def do_visualize_subgraph(self, session_id: str, node_names: Optional[list], include_community: bool, highlights: Optional[dict] = None, max_nodes: Optional[int] = None, user_id: str = "") -> dict:
        """获取图的完整结构用于可视化渲染。"""
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
        runtime_session_id = self._cache_key(session_id, user_id)
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
            _progress_kwarg="progress_callback" if tool_name == "train" else None,
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
        runtime_key = self._cache_key(session_id, user_id)
        key = (runtime_key, tool_name, self._canonical_tool_arguments(arguments))
        async with self._graph_ml_inflight_lock:
            task = self._graph_ml_inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._run_with_heavy_limits(
                        user_id,
                        lambda: asyncio.to_thread(
                            self._dispatch_graph_ml_tool,
                            session_id,
                            tool_name,
                            arguments,
                            user_id=user_id,
                        ),
                    )
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
        return await self._run_with_heavy_limits(
            user_id,
            lambda: asyncio.to_thread(
                self._dispatch_graph_ml_tool,
                session_id,
                tool_name,
                arguments,
                progress_callback=progress_callback,
                user_id=user_id,
            ),
        )

    async def dispatch_tool(self, session_id: str, tool_name: str, arguments: Dict[str, Any], user_id: str = "") -> dict:
        """统一工具分发入口。"""
        if tool_name == "nebula_status":
            return await asyncio.to_thread(self.do_nebula_status, session_id, user_id)
        if tool_name == "nebula_sync_graph":
            return await self.submit_nebula_sync(session_id, user_id=user_id)
        if tool_name in _NEBULA_QUERY_TOOLS:
            return await asyncio.to_thread(
                self._dispatch_nebula_query,
                session_id,
                tool_name,
                arguments,
                user_id,
            )
        if tool_name == "load_graph_from_upload":
            filename = _required_str(arguments, "filename")
            append = _optional_bool(arguments, "append", False)
            return await self.do_load_graph_from_upload(session_id, filename, append, user_id=user_id)

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
            return await asyncio.to_thread(self.do_search_node, session_id, arguments, user_id)
        if tool_name == "search_edge":
            return await asyncio.to_thread(self.do_search_edge, session_id, arguments, user_id)
        if tool_name in _GRAPH_ML_TOOL_NAMES:
            if tool_name in _GRAPH_ML_HEAVY_TOOLS and self._wants_graph_ml_async_job(arguments):
                return await self._submit_graph_ml_tool_job(session_id, tool_name, arguments, user_id=user_id)
            if tool_name in _GRAPH_ML_HEAVY_TOOLS:
                return await self._dispatch_heavy_graph_ml_tool_once(session_id, tool_name, arguments, user_id=user_id)
            return self._dispatch_graph_ml_tool(session_id, tool_name, arguments, user_id=user_id)
        if tool_name == "subgraph_extraction" and _optional_bool(arguments, "create_version", True):
            return await self._run_with_compute_limits(
                user_id,
                lambda: asyncio.to_thread(
                    self._dispatch_graph_tool,
                    session_id,
                    tool_name,
                    arguments,
                    user_id,
                ),
            )
        if tool_name in NX_TOOL_NAMES:
            _, _, nx_graph = self._require_graph(session_id, user_id=user_id)
            loop = asyncio.get_running_loop()
            return await self._run_with_compute_limits(
                user_id,
                lambda: loop.run_in_executor(
                    self._process_pool, run_nx_tool, tool_name, nx_graph, arguments
                ),
            )
        return await asyncio.to_thread(self._dispatch_graph_tool, session_id, tool_name, arguments, user_id)


def create_app(service: Optional[GraphDataService] = None) -> FastAPI:
    svc = service or GraphDataService()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # SIGTERM 时取消所有后台 Job 的 asyncio Task（底层线程会随进程退出）
        cancelled = svc._job_manager.cancel_all()
        if cancelled:
            # 让 CancelledError 传播一个调度周期
            await asyncio.sleep(0)
        # 不等待 ProcessPool worker 进程自然结束，直接让 OS 回收
        svc._process_pool.shutdown(wait=False, cancel_futures=True)
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

    @app.get("/datasets")
    def list_datasets_endpoint(request: Request):
        user_id = _get_user_id(request)
        return svc.do_list_datasets(user_id=user_id)

    @app.post("/sessions/{session_id}")
    def create_session(session_id: str, request: Request):
        user_id = _get_user_id(request)
        session = svc.get_or_create(session_id, user_id=user_id)
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
        session = svc.get_or_create(session_id, user_id=user_id)
        if "llm_model" in body:
            session.llm_model = str(body["llm_model"] or "")
        return {"success": True, "session_id": session_id}

    @app.delete("/datasets/{dataset_id}")
    def delete_dataset(dataset_id: str, request: Request):
        user_id = _get_user_id(request)
        return svc.do_delete_dataset(dataset_id, user_id=user_id)

    @app.delete("/sessions/{session_id}")
    def delete_session(session_id: str, request: Request):
        user_id = _get_user_id(request)
        _service_logger(session_id, "graph_data", user_id=user_id).event("SESSION_DELETE")
        svc.delete_session(session_id, user_id=user_id)
        close_service_loggers(session_id)
        return {"success": True, "session_id": session_id}

    @app.get("/health")
    def health():
        return {"status": "ok", "sessions": svc.session_count}

    @app.get("/tools")
    def list_tools(include_graph_ml: bool = True):
        """返回工具规格列表（tool_catalog 唯一真相源）。"""
        return get_all_public_tools(include_graph_ml=include_graph_ml)

    @app.post("/sessions/{session_id}/uploads/register")
    def register_upload(session_id: str, req: RegisterUploadReq, request: Request):
        user_id = _get_user_id(request)
        session = svc.get_or_create(session_id, user_id=user_id)
        session.register_upload(
            file_token=req.file_token,
            original_filename=req.original_filename,
            saved_path=req.saved_path,
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
            upload_root = str(_dp.user_uploads_dir(user_id))
        else:
            upload_root = _graph_data_upload_root()
        session_dir = os.path.join(upload_root, session_id)
        os.makedirs(session_dir, exist_ok=True)

        safe_name = _sanitize_filename(original_filename or file.filename or "")
        saved_path = os.path.abspath(os.path.join(session_dir, f"{file_token}_{safe_name}"))
        content = await file.read()
        with open(saved_path, "wb") as f:
            f.write(content)

        session.register_upload(
            file_token=file_token,
            original_filename=original_filename,
            saved_path=saved_path,
        )
        _service_logger(session_id, "graph_data", user_id=user_id).event(
            "UPLOAD_INGEST",
            filename=original_filename,
            file_token=file_token,
            saved_path=saved_path,
            size_bytes=len(content),
        )
        return {
            "success": True,
            "session_id": session_id,
            "filename": original_filename,
            "file_token": file_token,
            "saved_path": saved_path,
            "size": len(content),
        }

    @app.post("/sessions/{session_id}/graph/load_text")
    def load_graph_text(session_id: str, req: LoadGraphTextReq, request: Request):
        user_id = _get_user_id(request)
        return svc.do_load_graph_text(session_id, req.edge_text, req.append, user_id=user_id)

    @app.post("/sessions/{session_id}/graph/load_upload")
    async def load_graph_upload(session_id: str, req: LoadGraphUploadReq, request: Request):
        user_id = _get_user_id(request)
        return await svc.do_load_graph_from_upload(session_id, req.filename, req.append, user_id=user_id)

    @app.post("/sessions/{session_id}/graph/clear")
    def clear_graph(session_id: str, request: Request):
        user_id = _get_user_id(request)
        return svc.do_clear_graph(session_id, user_id=user_id)

    @app.post("/sessions/{session_id}/graph/to_undirected")
    def to_undirected(session_id: str, request: Request):
        user_id = _get_user_id(request)
        return svc.do_to_undirected(session_id, user_id=user_id)

    @app.get("/sessions/{session_id}/graph/info")
    def graph_info(session_id: str, request: Request):
        user_id = _get_user_id(request)
        return svc.do_get_graph_info(session_id, user_id=user_id)

    @app.post("/sessions/{session_id}/graph/visualize")
    def visualize_graph(session_id: str, request: Request, req: ToolInvokeReq = None):
        """获取图的完整结构用于可视化渲染（直接端点）。"""
        arguments = req.arguments if req else {}
        node_names = arguments.get("node_names")
        if node_names is not None:
            node_names = list(node_names)
        include_community = bool(arguments.get("include_community", False))
        highlights = arguments.get("highlights")
        max_nodes = arguments.get("max_nodes")
        user_id = _get_user_id(request)
        return svc.do_visualize_subgraph(session_id, node_names, include_community, highlights, max_nodes=max_nodes, user_id=user_id)

    @app.get("/sessions/{session_id}/graph/document")
    def graph_document(session_id: str, request: Request):
        user_id = _get_user_id(request)
        session = svc.get_required(session_id, user_id=user_id)
        gd = session.get_graph()
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
            "session_id": session_id,
            "graph_version": session.graph_version,
            "graph": graph_data_to_document(gd),
        }

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
        else:
            svc_name = "graph_compute"
        svc_log = _service_logger(session_id, svc_name, user_id=user_id)
        svc_log.request(tool_name, arguments)
        t0 = time.monotonic()

        try:
            result = await svc.dispatch_tool(session_id, tool_name, arguments, user_id=user_id)
            duration_ms = int((time.monotonic() - t0) * 1000)
            svc_log.response(tool_name, result, duration_ms)
            return result
        except ValueError as e:
            svc_log.error(tool_name, str(e))
            return {"success": False, "error": str(e)}
        except HTTPException:
            raise
        except Exception as e:
            svc_log.error(tool_name, str(e))
            raise HTTPException(status_code=500, detail=f"工具执行失败: {e}")

    return app

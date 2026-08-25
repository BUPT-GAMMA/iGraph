"""
API Gateway Server.

聚合前端页面与会话聊天接口，并按需托管子服务进程。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional

import httpx
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_runtime import (
    RuntimeFactory,
    RuntimeSession,
    adapt_legacy_agent,
    create_runtime,
    extract_generated_skill_name,
    stream_skill_generation_from_env,
)
from agent_runtime.application_services import (
    DEFAULT_MODEL,
    SUPPORTED_MODELS,
    BuiltinMemoryProvider,
    FileMemoryProvider,
    MemoryEngine,
    MemoryManager,
    MemoryService,
    context_usage as build_context_usage,
    normalize_model_name,
    skill_events_path,
)
from backend.logging.sft_exporter import export_sft_records
from backend.logging.logger import Logger
from backend.runtime.dotenv_compat import load_dotenv_compat
from backend.runtime.process_supervisor import ProcessSupervisor

load_dotenv_compat()

_SUPPORTED_IDS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "supported_ids.txt",
)
_ADMIN_USER_ID = os.getenv("IGRAPH_ADMIN_USER_ID", "admin").strip() or "admin"
_RESTRICTED_MODEL_PREFIXES = ("gpt-", "claude-")
_DEFAULT_ADMIN_MODEL = os.getenv("IGRAPH_DEFAULT_ADMIN_MODEL", DEFAULT_MODEL).strip()
_DEFAULT_NON_ADMIN_MODEL = os.getenv("IGRAPH_DEFAULT_USER_MODEL", "qwen3.6-plus").strip()


def _load_supported_ids() -> set:
    ids: set = set()
    try:
        with open(_SUPPORTED_IDS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    ids.add(line)
    except FileNotFoundError:
        pass
    return ids


def _validate_user_id(user_id: str) -> bool:
    return bool(user_id) and user_id in _load_supported_ids()


def _is_admin_user(user_id: str) -> bool:
    return str(user_id or "").strip() == _ADMIN_USER_ID


def _is_restricted_model(model: str) -> bool:
    normalized = str(model or "").strip()
    return normalized.startswith(_RESTRICTED_MODEL_PREFIXES)


def _available_models_for_user(user_id: str) -> list[str]:
    models = sorted(SUPPORTED_MODELS)
    if _is_admin_user(user_id):
        return models
    return [m for m in models if not _is_restricted_model(m)]


def _default_model_for_user(user_id: str) -> str:
    allowed = _available_models_for_user(user_id)
    if _is_admin_user(user_id):
        return _DEFAULT_ADMIN_MODEL if _DEFAULT_ADMIN_MODEL in allowed else DEFAULT_MODEL
    if _DEFAULT_NON_ADMIN_MODEL in allowed:
        return _DEFAULT_NON_ADMIN_MODEL
    return allowed[0] if allowed else DEFAULT_MODEL


def _normalize_model_for_user(model: Optional[str], user_id: str, *, default: Optional[str] = None) -> str:
    fallback = default or _default_model_for_user(user_id)
    normalized = normalize_model_name(model, default=fallback)
    if normalized not in _available_models_for_user(user_id):
        raise ValueError(f"用户 {user_id} 无权使用模型 {normalized}")
    return normalized


def _get_request_user(request: Request) -> str:
    """从请求头读取并验证 X-User-ID，无效时抛出 401。"""
    user_id = request.headers.get("X-User-ID", "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="缺少 X-User-ID 请求头")
    if not _validate_user_id(user_id):
        raise HTTPException(status_code=401, detail="无效的用户 ID")
    return user_id


class CreateSessionReq(BaseModel):
    model: Optional[str] = None
    reasoning_mode: Literal["staged", "direct"] = "direct"
    planning_mode: Literal["never", "auto", "always"] = "auto"


class ChatReq(BaseModel):
    message: str
    attachments: Optional[List["AttachmentRef"]] = None
    reasoning_mode: Optional[Literal["staged", "direct"]] = None
    planning_mode: Optional[Literal["never", "auto", "always"]] = None
    output_format: Optional[Literal["chat", "report", "json"]] = "chat"
    regenerate: bool = False


class FeedbackRespondReq(BaseModel):
    request_id: str
    decision: Literal["approve", "modify", "reject"]
    patched_args: Optional[dict] = None
    comment: str = ""


class SetReasoningModeReq(BaseModel):
    reasoning_mode: Literal["staged", "direct"]


class SetPlanningModeReq(BaseModel):
    planning_mode: Literal["never", "auto", "always"]


class SetModelReq(BaseModel):
    model: str


class LoadDatasetReq(BaseModel):
    dataset_id: str
    version: str = "latest"
    name: Optional[str] = None


class SaveDatasetReq(BaseModel):
    name: str
    description: str = ""


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


class MemoryDeleteReq(BaseModel):
    scope: str = "long_term"
    reason: str = ""


class SkillCreateReq(BaseModel):
    name: str
    body: str
    metadata: dict = {}
    destination: str = "user"


class SkillUpdateReq(BaseModel):
    body: str
    source: str = "user"


class SkillApproveReq(BaseModel):
    target_name: str = ""
    playbook_id: str = ""


class SkillRejectReq(BaseModel):
    reason: str = ""
    remove_files: bool = True


class SkillPromoteReq(BaseModel):
    playbook_id: str
    skill_name: str
    description: str = ""
    domain: str = "generated"
    risk_level: str = "medium"


class SkillGenerateReq(BaseModel):
    playbook_id: str
    session_id: str = ""
    model: str = ""


class AttachmentRef(BaseModel):
    file_token: str
    filename: Optional[str] = None


@dataclass
class ApiGatewayConfig:
    upload_dir: str
    mcp_port: int
    graph_data_port: int
    host: str
    port: int
    mcp_url: str
    graph_data_base_url: str

    def resolved_mcp_url(self) -> str:
        return self.mcp_url

    def resolved_graph_data_base_url(self) -> str:
        return self.graph_data_base_url


AgentFactory = Callable[..., Any]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


GRAPH_DATA_MAX_RETRIES = 5
SESSION_CONTEXT_TOKEN_LIMIT = 192_000
GRAPH_DATA_TOOL_TIMEOUT_SECONDS = _env_float("GRAPH_DATA_TOOL_TIMEOUT_SECONDS", 600.0)
GRAPH_DATA_HEAVY_TOOL_TIMEOUT_SECONDS = _env_float(
    "GRAPH_DATA_HEAVY_TOOL_TIMEOUT_SECONDS",
    900.0,
)
_GRAPH_DATA_HEAVY_TOOLS = {
    "load_graph_classification_dataset",
    "train",
    "infer",
    "load_graph_from_text",
}


def _graph_data_retry_delay(attempt: int) -> float:
    return min(0.2 * (2 ** (attempt - 1)), 2.0)


def _graph_data_attempts_text(attempts: int = GRAPH_DATA_MAX_RETRIES) -> str:
    return f"已尝试 {attempts} 次"


def _graph_data_response_error(resp: httpx.Response) -> str:
    text = resp.text.strip()
    if text:
        return f"HTTP {resp.status_code}: {text}"
    return f"HTTP {resp.status_code}"


def _graph_data_tool_timeout(tool_name: str, default_timeout: float) -> float:
    if tool_name in _GRAPH_DATA_HEAVY_TOOLS:
        return GRAPH_DATA_HEAVY_TOOL_TIMEOUT_SECONDS
    return default_timeout


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


class ApiGatewayServer:
    """
    API 网关服务。

    - manage_subprocesses=True: 由当前进程托管 GraphData / MCP。
    - manage_subprocesses=False: 假定下游服务由外部部署（适合独立测试/部署）。
    """

    def __init__(
        self,
        config: Optional[ApiGatewayConfig] = None,
        *,
        manage_subprocesses: bool = True,
        process_supervisor: Optional[ProcessSupervisor] = None,
        logger: Optional[Logger] = None,
        agent_factory: Optional[AgentFactory] = None,
        runtime_factory: Optional[RuntimeFactory] = None,
    ) -> None:
        if config is None:
            raise ValueError("ApiGatewayConfig 不能为空，请从环境变量构建配置后再启动。")
        self.config = config
        if not os.path.isabs(self.config.upload_dir):
            root_dir = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            self.config.upload_dir = os.path.join(root_dir, self.config.upload_dir)
        self.config.upload_dir = os.path.abspath(self.config.upload_dir)
        os.makedirs(self.config.upload_dir, exist_ok=True)

        self._proc_supervisor = process_supervisor
        if manage_subprocesses:
            if self._proc_supervisor is None:
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                self._proc_supervisor = ProcessSupervisor(
                    base_dir=base_dir,
                    graph_data_port=self.config.graph_data_port,
                    mcp_port=self.config.mcp_port,
                    graph_data_url=self.config.graph_data_base_url,
                )
            self._proc_supervisor.start_all()
            self.config.graph_data_base_url = self._proc_supervisor.effective_graph_data_url

        self.logger = logger or Logger()
        if runtime_factory is not None:
            self._runtime_factory = runtime_factory
        elif agent_factory is not None:
            # 保留旧测试和外部注入点；进入 Backend 前统一适配为 RuntimeSession。
            self._runtime_factory = lambda **kwargs: adapt_legacy_agent(
                agent_factory(**kwargs)
            )
        else:
            self._runtime_factory = create_runtime
        self._agents: Dict[str, RuntimeSession] = {}
        self._uploaded_files: Dict[str, Dict[str, dict]] = {}
        self._memory_manager: Optional[MemoryManager] = None
        self._memory_service: Optional[MemoryService] = None
        # 每用户独立的 memory engine/manager/service 缓存
        self._user_memory_engines: Dict[str, MemoryEngine] = {}
        self._user_memory_managers: Dict[str, MemoryManager] = {}
        self._user_memory_services: Dict[str, MemoryService] = {}
        self._agent_global_sem = asyncio.Semaphore(_env_int("AGENT_GLOBAL_CONCURRENCY", 16))
        self._agent_per_user_limit = _env_int("AGENT_PER_USER_CONCURRENCY", 2)
        self._agent_user_sems: Dict[str, asyncio.Semaphore] = {}
        self._context_token_limit = _env_int(
            "SESSION_CONTEXT_TOKEN_LIMIT",
            SESSION_CONTEXT_TOKEN_LIMIT,
        )
        # Anonymous memory manager removed — all memory is per-user now.
        self._memory_manager = None
        self._memory_service = None

        self.app = FastAPI(title="iGraph API", version="2.1.0")
        self._setup_middleware()
        self._setup_routes()

    @staticmethod
    def _agent_key(user_id: str, session_id: str) -> str:
        uid = str(user_id or "").strip()
        sid = str(session_id or "").strip()
        return f"{uid}:{sid}" if uid else sid

    @staticmethod
    def _session_id_from_agent_key(agent_key: str) -> str:
        return str(agent_key or "").split(":", 1)[-1]

    @staticmethod
    def _user_id_from_agent_key(agent_key: str) -> str:
        text = str(agent_key or "")
        return text.split(":", 1)[0] if ":" in text else ""

    def _get_live_agent(self, session_id: str, user_id: str = "") -> Optional[RuntimeSession]:
        return self._agents.get(self._agent_key(user_id, session_id))

    def _set_live_agent(self, agent: RuntimeSession, user_id: str = "") -> None:
        self._agents[self._agent_key(user_id, agent.session_id)] = agent

    def _pop_live_agent(self, session_id: str, user_id: str = "") -> Optional[RuntimeSession]:
        return self._agents.pop(self._agent_key(user_id, session_id), None)

    def _agent_user_sem(self, user_id: str) -> asyncio.Semaphore:
        key = str(user_id or "_anonymous")
        if key not in self._agent_user_sems:
            self._agent_user_sems[key] = asyncio.Semaphore(max(1, self._agent_per_user_limit))
        return self._agent_user_sems[key]

    async def _stream_agent_events(self, user_id: str, agent: RuntimeSession, message: str):
        user_sem = self._agent_user_sem(user_id)
        async with user_sem:
            async with self._agent_global_sem:
                async for event in agent.chat_stream(message):
                    yield event

    async def _stream_feedback_events(
        self,
        user_id: str,
        agent: RuntimeSession,
        req: FeedbackRespondReq,
    ):
        user_sem = self._agent_user_sem(user_id)
        async with user_sem:
            async with self._agent_global_sem:
                async for event in agent.resume_feedback_stream(
                    request_id=req.request_id,
                    decision=req.decision,
                    patched_args=req.patched_args,
                    comment=req.comment,
                ):
                    yield event

    def _setup_middleware(self) -> None:
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def _create_agent(
        self,
        model: Optional[str],
        reasoning_mode: str = "direct",
        *,
        planning_mode: str = "auto",
        session_id: Optional[str] = None,
        user_id: str = "",
        is_new: bool = True,
        provider: str = "transparent_openai_sdk",
        restored_messages: Optional[List[dict]] = None,
        restored_has_graph: bool = False,
    ) -> RuntimeSession:
        sid = session_id or str(uuid.uuid4())[:8]
        effective_user = user_id.strip()
        normalized_model = _normalize_model_for_user(model, effective_user)
        try:
            session_logger = self.logger.create_session_logger(
                sid,
                provider=provider,
                model=normalized_model or "",
                reasoning_mode=reasoning_mode,
                is_new=is_new,
                user_id=effective_user,
            )
        except (TypeError, ValueError):
            # 兼容测试中的 FakeLogger（旧签名）。
            session_logger = self.logger.create_session_logger(
                sid, provider=provider, model=normalized_model or ""
            )
        # 使用用户独立的 memory_manager
        memory_manager = (
            self._get_user_memory_manager(effective_user)
            if effective_user
            else self._memory_manager
        )
        return self._runtime_factory(
            session_logger=session_logger,
            model=normalized_model,
            reasoning_mode=reasoning_mode,
            planning_mode=planning_mode,
            mcp_url=self.config.resolved_mcp_url(),
            restored_messages=restored_messages,
            restored_has_graph=restored_has_graph,
            memory_manager=memory_manager,
            user_id=effective_user,
        )

    def _list_session_metas(self, user_id: str = "") -> List[dict]:
        if hasattr(self.logger, "list_sessions"):
            try:
                rows = self.logger.list_sessions(limit=300, user_id=user_id)
                if isinstance(rows, list):
                    return [r for r in rows if isinstance(r, dict)]
            except Exception:
                pass
        return []

    def _get_user_memory_engine(self, user_id: str) -> Optional[MemoryEngine]:
        if user_id not in self._user_memory_engines:
            try:
                store = (
                    self.logger.get_user_store(user_id)
                    if hasattr(self.logger, "get_user_store")
                    else getattr(self.logger, "store", None)
                )
                engine = MemoryEngine(self.logger.LOGS_DIR, store, user_id=user_id)
                self._user_memory_engines[user_id] = engine
            except Exception:
                return None
        return self._user_memory_engines.get(user_id)

    def _get_user_memory_manager(self, user_id: str) -> Optional[MemoryManager]:
        if user_id not in self._user_memory_managers:
            engine = self._get_user_memory_engine(user_id)
            if engine is None:
                return None
            self._user_memory_managers[user_id] = MemoryManager(
                BuiltinMemoryProvider(engine),
                extra=FileMemoryProvider(user_id=user_id),
            )
        return self._user_memory_managers.get(user_id)

    def _get_user_memory_service(self, user_id: str) -> Optional[MemoryService]:
        if user_id not in self._user_memory_services:
            engine = self._get_user_memory_engine(user_id)
            if engine is None:
                return None
            self._user_memory_services[user_id] = MemoryService(engine)
        return self._user_memory_services.get(user_id)

    def _check_session_ownership(self, session_id: str, user_id: str) -> None:
        """若会话属于其他用户则抛出 403。"""
        meta = self._get_session_meta(session_id, user_id=user_id)
        if not meta:
            raise HTTPException(status_code=404, detail="会话不存在")
        index_user = (
            self.logger.get_session_owner(session_id)
            if hasattr(self.logger, "get_session_owner")
            else (
                self.logger._read_session_index(session_id)
                if hasattr(self.logger, "_read_session_index")
                else ""
            )
        )
        session_user = index_user or str(meta.get("user_id") or "").strip()
        if session_user and session_user != user_id:
            raise HTTPException(status_code=403, detail="无权访问此会话")

    def _ensure_agent_model_allowed(self, agent: RuntimeSession, user_id: str) -> None:
        if agent.model in _available_models_for_user(user_id):
            return
        fallback = _default_model_for_user(user_id)
        try:
            agent.set_model(fallback)
        except Exception as e:
            raise HTTPException(
                status_code=403,
                detail=f"当前用户无权使用模型 {agent.model}，且无法自动切换到 {fallback}: {e}",
            )

    def _get_session_meta(self, session_id: str, user_id: str = "") -> dict:
        try:
            if user_id and hasattr(self.logger, "get_session_meta"):
                meta = self.logger.get_session_meta(session_id, user_id=user_id)
                if isinstance(meta, dict) and meta:
                    return meta
            if hasattr(self.logger, "get_session_meta"):
                meta = self.logger.get_session_meta(session_id)
                if isinstance(meta, dict):
                    return meta
        except Exception:
            pass
        return {}

    def _load_context_messages(self, session_id: str, user_id: str = "") -> List[dict]:
        try:
            if user_id and hasattr(self.logger, "load_context_messages"):
                rows = self.logger.load_context_messages(session_id, user_id=user_id)
                if isinstance(rows, list):
                    return [m for m in rows if isinstance(m, dict)]
            if hasattr(self.logger, "load_context_messages"):
                rows = self.logger.load_context_messages(session_id)
                if isinstance(rows, list):
                    return [m for m in rows if isinstance(m, dict)]
        except Exception:
            pass
        return []

    def _load_chat_messages(self, session_id: str, user_id: str = "") -> List[dict]:
        try:
            if user_id and hasattr(self.logger, "load_chat_messages"):
                rows = self.logger.load_chat_messages(session_id, user_id=user_id)
                if isinstance(rows, list):
                    return [m for m in rows if isinstance(m, dict)]
            if hasattr(self.logger, "load_chat_messages"):
                rows = self.logger.load_chat_messages(session_id)
                if isinstance(rows, list):
                    return [m for m in rows if isinstance(m, dict)]
        except Exception:
            pass
        return []

    def _context_usage_for_session(
        self,
        session_id: str,
        agent: Optional[RuntimeSession] = None,
        user_id: str = "",
    ) -> dict:
        if agent is not None and hasattr(agent, "context_usage"):
            try:
                usage = agent.context_usage()
                if isinstance(usage, dict):
                    return usage
            except Exception:
                pass
        messages = self._load_context_messages(session_id, user_id=user_id)
        model = ""
        if agent is not None:
            model = str(getattr(agent, "model", "") or "")
        if not model:
            meta = self._get_session_meta(session_id, user_id=user_id)
            model = str(meta.get("model") or DEFAULT_MODEL)
        return build_context_usage(
            messages,
            limit=self._context_token_limit,
            model=model,
        )

    @staticmethod
    def _normalize_restore_args(meta: dict, user_id: str = "") -> tuple[str, str, str, dict]:
        raw_model = str(meta.get("model") or "").strip()
        raw_reasoning = str(meta.get("reasoning_mode") or "").strip()
        raw_provider = str(meta.get("provider") or "transparent_openai_sdk").strip()

        try:
            model = _normalize_model_for_user(raw_model, user_id)
        except ValueError:
            model = _default_model_for_user(user_id)
        reasoning_mode = raw_reasoning if raw_reasoning in {"staged", "direct"} else "direct"
        provider = raw_provider or "transparent_openai_sdk"

        patch: dict = {}
        if raw_model != model:
            patch["model"] = model
        if raw_reasoning != reasoning_mode:
            patch["reasoning_mode"] = reasoning_mode
        if raw_provider != provider:
            patch["provider"] = provider
        return model, reasoning_mode, provider, patch

    def _restore_agent_if_needed(self, session_id: str, user_id: str = "") -> tuple[RuntimeSession, bool, bool]:
        """
        返回 (agent, restored, requires_graph_reload)。
        restored=True 表示本次从磁盘历史恢复了 agent。
        """
        live = self._get_live_agent(session_id, user_id=user_id)
        if live is not None:
            self._ensure_agent_model_allowed(live, user_id)
            return live, False, False

        meta = self._get_session_meta(session_id, user_id=user_id)
        if not meta:
            raise HTTPException(status_code=404, detail="会话不存在")

        session_user_id = str(meta.get("user_id") or "").strip() or user_id
        model, reasoning_mode, provider, patch = self._normalize_restore_args(meta, session_user_id)
        if patch:
            try:
                store = (
                    self.logger.get_user_store(session_user_id)
                    if session_user_id and hasattr(self.logger, "get_user_store")
                    else getattr(self.logger, "store", None)
                )
                if store:
                    store.update_meta(session_id, patch)
            except Exception:
                pass
        context_messages = self._load_context_messages(session_id, user_id=session_user_id)

        # 历史会话仅恢复对话上下文；图数据运行态无法跨进程恢复。
        agent = self._create_agent(
            model=model,
            reasoning_mode=reasoning_mode,
            session_id=session_id,
            user_id=session_user_id,
            is_new=False,
            provider=provider,
            restored_messages=context_messages if context_messages else None,
            restored_has_graph=False,
        )
        self._set_live_agent(agent, session_user_id)
        requires_graph_reload = bool(meta.get("last_has_graph", False))
        return agent, True, requires_graph_reload

    _MAX_LIVE_AGENTS: int = 20
    _IDLE_TIMEOUT_SECONDS: float = 600.0

    @staticmethod
    def _inject_synthetic_turn(agent: RuntimeSession, user_text: str, assistant_text: str) -> None:
        """向 agent 上下文注入一对伪造的 user/assistant 消息，同时持久化以便 session 恢复。"""
        agent.inject_synthetic_turn(user_text, assistant_text)

    def _evict_idle_agents(self, keep_agent_key: Optional[str] = None, user_id: str = "") -> None:
        """淘汰超时空闲 Agent；若仍超出上限，淘汰最久未活跃的。"""
        now = time.monotonic()
        for agent_key in list(self._agents):
            if agent_key == keep_agent_key:
                continue
            agent = self._agents[agent_key]
            idle = now - getattr(agent, "last_active", now)
            if idle > self._IDLE_TIMEOUT_SECONDS:
                try:
                    agent.cleanup()
                except Exception:
                    pass
                self._agents.pop(agent_key, None)
        if user_id:
            user_keys = [
                key for key in self._agents
                if self._user_id_from_agent_key(key) == user_id and key != keep_agent_key
            ]
            per_user_limit = max(1, _env_int("MAX_LIVE_AGENTS_PER_USER", 3))
            while len(user_keys) >= per_user_limit:
                oldest = min(user_keys, key=lambda k: getattr(self._agents[k], "last_active", 0.0))
                try:
                    self._agents[oldest].cleanup()
                except Exception:
                    pass
                self._agents.pop(oldest, None)
                user_keys.remove(oldest)
        while len(self._agents) >= self._MAX_LIVE_AGENTS:
            candidates = [s for s in self._agents if s != keep_agent_key]
            if not candidates:
                break
            oldest = min(candidates, key=lambda s: getattr(self._agents[s], "last_active", 0.0))
            try:
                self._agents[oldest].cleanup()
            except Exception:
                pass
            self._agents.pop(oldest, None)

    def _resolve_attachments(
        self,
        session_id: str,
        message: str,
        attachments: Optional[List[AttachmentRef]],
        user_id: str = "",
    ) -> str:
        if not attachments:
            return message

        file_map = self._uploaded_files.get(self._agent_key(user_id, session_id), {})
        names: List[str] = []
        for item in attachments:
            rec = file_map.get(item.file_token)
            if not rec:
                continue
            filename = (item.filename or rec["original"] or "").replace("\n", " ").strip()
            if filename:
                names.append(filename)
        if not names:
            return message

        _STRUCTURED_EXTS = {".npz", ".pt", ".json", ".csv", ".tsv", ".txt", ".edgelist"}
        _TEXT_EXTS = {".txt", ".md", ".pdf", ".docx", ".doc"}
        hints: List[str] = []
        for n in names:
            ext = os.path.splitext(n)[1].lower()
            if ext == ".pt":
                hints.append(
                    f"- {n}（若为 HIV/PCBA 或多图图分类数据集，调用 "
                    "load_graph_classification_dataset；若为普通单图，调用 "
                    "load_graph_from_upload）"
                )
            elif ext == ".npz":
                hints.append(f"- {n}（二进制单图文件，调用 load_graph_from_upload）")
            elif ext in (".csv", ".tsv", ".edgelist"):
                hints.append(f"- {n}（表格/边列表，调用 load_graph_from_upload）")
            elif ext == ".json":
                hints.append(f"- {n}（JSON，调用 load_graph_from_upload）")
            elif ext in _TEXT_EXTS:
                hints.append(
                    f"- {n}（文本文件：若为边列表/三元组调用 load_graph_from_upload；"
                    f"若为自然语言文档调用 load_graph_from_text(filename={n})）"
                )
            else:
                hints.append(f"- {n}（先判断内容再选工具）")
        attachment_block = (
            "\n\n[附件文件]\n"
            "以下是用户已上传文件（仅文件名，不含服务器路径）：\n"
            + "\n".join(hints)
            + "\n回答中不得输出服务器内部路径。"
            + "\n除非用户明确要求，否则不要在最终回答中复述文件名。"
        )
        return message + attachment_block

    async def _post_graph_data_with_retry(
        self,
        url: str,
        *,
        timeout: float,
        json_body: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
        retry_on_timeout: bool = True,
        retry_on_server_error: bool = True,
        user_id: str = "",
    ) -> httpx.Response:
        headers = {"X-User-ID": user_id} if user_id else {}
        last_error: Optional[BaseException] = None
        for attempt in range(1, GRAPH_DATA_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                    resp = await client.post(
                        url,
                        json=json_body,
                        data=data,
                        files=files,
                        headers=headers,
                    )
                if resp.status_code >= 500 and attempt < GRAPH_DATA_MAX_RETRIES:
                    if retry_on_server_error:
                        await asyncio.sleep(_graph_data_retry_delay(attempt))
                        continue
                if resp.status_code >= 500:
                    raise RuntimeError(
                        "GraphData Server 请求失败"
                        f"({_graph_data_attempts_text(attempt)}): {_graph_data_response_error(resp)}"
                    )
                resp.raise_for_status()
                return resp
            except (httpx.TimeoutException, httpx.RequestError) as e:
                last_error = e
                if (
                    retry_on_timeout
                    and isinstance(e, httpx.TimeoutException)
                    and attempt < GRAPH_DATA_MAX_RETRIES
                ):
                    await asyncio.sleep(_graph_data_retry_delay(attempt))
                    continue
                if (
                    not isinstance(e, httpx.TimeoutException)
                    and attempt < GRAPH_DATA_MAX_RETRIES
                ):
                    await asyncio.sleep(_graph_data_retry_delay(attempt))
                    continue
                raise RuntimeError(
                    "GraphData Server 连接失败"
                    f"({_graph_data_attempts_text(attempt)}): {e}"
                ) from e

        if last_error is not None:
            raise RuntimeError(
                "GraphData Server 连接失败"
                f"({_graph_data_attempts_text()}): {last_error}"
            ) from last_error
        raise RuntimeError(f"GraphData Server 请求失败({_graph_data_attempts_text()})")

    async def _register_upload_to_graph_data(
        self,
        session_id: str,
        file_token: str,
        original_filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        user_id: str = "",
    ) -> None:
        url = (
            f"{self.config.resolved_graph_data_base_url()}"
            f"/sessions/{session_id}/uploads/ingest"
        )
        data = {
            "file_token": file_token,
            "original_filename": original_filename,
        }
        await self._post_graph_data_with_retry(
            url,
            timeout=60.0,
            data=data,
            files={
                "file": (
                    original_filename,
                    content,
                    content_type or "application/octet-stream",
                ),
            },
            user_id=user_id,
        )

    def _setup_routes(self) -> None:
        app = self.app
        root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        figures_dir = os.path.join(root_dir, "figures")
        if os.path.isdir(figures_dir):
            app.mount("/figures", StaticFiles(directory=figures_dir), name="figures")

        @app.get("/favicon.ico")
        def serve_favicon():
            icon_path = os.path.join(figures_dir, "igraph_light.png")
            if not os.path.isfile(icon_path):
                raise HTTPException(status_code=404, detail="favicon not found")
            return FileResponse(icon_path, media_type="image/png")

        @app.get("/", response_class=HTMLResponse)
        def serve_frontend():
            html_path = os.path.join(root_dir, "index.html")
            with open(html_path, "r", encoding="utf-8") as f:
                return HTMLResponse(
                    content=f.read(),
                    headers={"Cache-Control": "no-store"},
                )

        @app.post("/api/auth/validate")
        def auth_validate(request: Request):
            user_id = request.headers.get("X-User-ID", "").strip()
            if not user_id:
                return {"valid": False, "reason": "missing_user_id"}
            if _validate_user_id(user_id):
                return {
                    "valid": True,
                    "user_id": user_id,
                    "is_admin": _is_admin_user(user_id),
                    "available_models": _available_models_for_user(user_id),
                    "default_model": _default_model_for_user(user_id),
                }
            return {"valid": False, "reason": "user_not_supported"}

        @app.get("/api/sessions")
        def list_sessions(request: Request):
            user_id = _get_request_user(request)
            metas = self._list_session_metas(user_id=user_id)
            active_ids = {
                self._session_id_from_agent_key(key)
                for key in self._agents
                if self._user_id_from_agent_key(key) == user_id
            }
            rows: List[dict] = []
            seen_ids: set[str] = set()
            for meta in metas:
                sid = str(meta.get("session_id") or "").strip()
                if not sid:
                    continue
                raw_model = str(meta.get("model") or "").strip()
                try:
                    model = _normalize_model_for_user(raw_model, user_id)
                except ValueError:
                    model = _default_model_for_user(user_id)
                live = self._get_live_agent(sid, user_id=user_id)
                if live is not None:
                    model = (
                        live.model
                        if live.model in _available_models_for_user(user_id)
                        else _default_model_for_user(user_id)
                    )
                seen_ids.add(sid)
                rows.append(
                    {
                        "session_id": sid,
                        "title": meta.get("title") or "新会话",
                        "last_message_preview": meta.get("last_message_preview") or "",
                        "updated_at": meta.get("updated_at"),
                        "created_at": meta.get("created_at"),
                        "model": model,
                        "provider": live.provider if live is not None else meta.get("provider") or "",
                        "reasoning_mode": (
                            live.reasoning_mode
                            if live is not None
                            else meta.get("reasoning_mode") or "direct"
                        ),
                        "archived": bool(meta.get("archived", False)),
                        "active": sid in active_ids,
                    }
                )
            for sid in sorted(active_ids):
                if sid in seen_ids:
                    continue
                agent = self._get_live_agent(sid, user_id=user_id)
                if not agent:
                    continue
                # 只显示属于当前用户的活跃会话
                agent_meta = self._get_session_meta(sid, user_id=user_id)
                if not agent_meta:
                    continue
                agent_user = str((agent_meta or {}).get("user_id") or "").strip()
                # user_id 有值时严格匹配；空字符串（旧会话）仅对创建它的用户可见
                # 通过索引文件判断归属
                index_uid = self.logger._read_session_index(sid) if hasattr(self.logger, "_read_session_index") else ""
                owner = index_uid or agent_user
                if owner and owner != user_id:
                    continue
                rows.append(
                    {
                        "session_id": sid,
                        "title": "运行中会话",
                        "last_message_preview": "",
                        "updated_at": None,
                        "created_at": None,
                        "model": (
                            agent.model
                            if agent.model in _available_models_for_user(user_id)
                            else _default_model_for_user(user_id)
                        ),
                        "provider": agent.provider,
                        "reasoning_mode": agent.reasoning_mode,
                        "archived": False,
                        "active": True,
                    }
                )
            rows.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
            return {"success": True, "sessions": rows}

        @app.post("/api/sessions")
        def create_session(req: CreateSessionReq, request: Request):
            user_id = _get_request_user(request)
            try:
                self._evict_idle_agents(user_id=user_id)
                agent = self._create_agent(
                    req.model, req.reasoning_mode,
                    planning_mode=req.planning_mode,
                    user_id=user_id,
                )
                self._set_live_agent(agent, user_id)
                self.logger.info(f"HTTP POST /api/sessions -> session_id={agent.session_id}")
                return {
                    "success": True,
                    "session_id": agent.session_id,
                    "provider": agent.provider,
                    "model": agent.model,
                    "reasoning_mode": agent.reasoning_mode,
                    "planning_mode": agent.planning_mode,
                    "context_usage": self._context_usage_for_session(agent.session_id, agent, user_id=user_id),
                    "available_models": _available_models_for_user(user_id),
                    "default_model": _default_model_for_user(user_id),
                }
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                self.logger.error(f"HTTP POST /api/sessions 失败: {e}")
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/sessions/{session_id}/activate")
        def activate_session(session_id: str, request: Request):
            user_id = _get_request_user(request)
            self._check_session_ownership(session_id, user_id)
            # 若 agent 已在内存中，直接返回当前状态，不创建新 agent 也不驱逐任何会话
            live = self._get_live_agent(session_id, user_id=user_id)
            if live is not None:
                self._ensure_agent_model_allowed(live, user_id)
                messages = self._load_chat_messages(session_id, user_id=user_id)
                self.logger.info(
                    f"HTTP POST /api/sessions/{session_id}/activate restored=False "
                    f"msg_count={len(messages)}"
                )
                return {
                    "success": True,
                    "session_id": session_id,
                    "restored": False,
                    "requires_graph_reload": False,
                    "messages": messages,
                    "has_graph": live.has_graph(),
                    "pending_feedback": live.pending_feedback(),
                    "provider": live.provider,
                    "model": live.model,
                    "reasoning_mode": live.reasoning_mode,
                    "planning_mode": live.planning_mode,
                    "context_usage": self._context_usage_for_session(session_id, live, user_id=user_id),
                    "available_models": _available_models_for_user(user_id),
                    "default_model": _default_model_for_user(user_id),
                }

            # agent 不在内存中 —— 只读 meta，不创建 agent（惰性初始化：第一次发消息时创建）
            meta = self._get_session_meta(session_id, user_id=user_id)
            if not meta:
                raise HTTPException(status_code=404, detail="会话不存在")

            model, reasoning_mode, provider, patch = self._normalize_restore_args(meta, user_id)
            if patch and hasattr(self.logger, "get_user_store"):
                try:
                    self.logger.get_user_store(user_id).update_meta(session_id, patch)
                except Exception:
                    pass

            messages = self._load_chat_messages(session_id, user_id=user_id)
            has_graph = bool(meta.get("last_has_graph", False))
            self.logger.info(
                f"HTTP POST /api/sessions/{session_id}/activate restored=True "
                f"msg_count={len(messages)}"
            )
            return {
                "success": True,
                "session_id": session_id,
                "restored": True,
                "requires_graph_reload": has_graph,
                "messages": messages,
                "has_graph": has_graph,
                "pending_feedback": None,
                "provider": provider,
                "model": model,
                "reasoning_mode": reasoning_mode,
                "planning_mode": "auto",
                "context_usage": self._context_usage_for_session(session_id, user_id=user_id),
                "available_models": _available_models_for_user(user_id),
                "default_model": _default_model_for_user(user_id),
            }

        @app.get("/api/sessions/{session_id}/messages")
        def get_session_messages(session_id: str, request: Request):
            user_id = _get_request_user(request)
            self._check_session_ownership(session_id, user_id)
            messages = self._load_chat_messages(session_id, user_id=user_id)
            return {"success": True, "session_id": session_id, "messages": messages}

        @app.get("/api/sessions/{session_id}/context-usage")
        def get_context_usage(session_id: str, request: Request):
            user_id = _get_request_user(request)
            self._check_session_ownership(session_id, user_id)
            live = self._get_live_agent(session_id, user_id=user_id)
            return {
                "success": True,
                "session_id": session_id,
                "context_usage": self._context_usage_for_session(session_id, live, user_id=user_id),
            }

        @app.delete("/api/sessions/{session_id}")
        def delete_session(session_id: str, request: Request):
            user_id = _get_request_user(request)
            self._check_session_ownership(session_id, user_id)
            agent = self._pop_live_agent(session_id, user_id=user_id)
            if agent:
                agent.cleanup()
            self._uploaded_files.pop(self._agent_key(user_id, session_id), None)
            # 在 meta 中标记删除，使 list_sessions 过滤掉该会话
            if hasattr(self.logger, "get_user_store"):
                try:
                    self.logger.get_user_store(user_id).update_meta(session_id, {"deleted": True})
                except Exception:
                    pass
            self.logger.info(f"HTTP DELETE /api/sessions/{session_id}")
            return {"success": True}

        @app.get("/api/sessions/{session_id}/info")
        def session_info(session_id: str, request: Request):
            user_id = _get_request_user(request)
            self._check_session_ownership(session_id, user_id)
            live = self._get_live_agent(session_id, user_id=user_id)
            if live is not None:
                return {
                    "session_id": session_id,
                    "has_graph": live.has_graph(),
                    "pending_feedback": live.pending_feedback(),
                    "provider": live.provider,
                    "model": live.model,
                    "reasoning_mode": live.reasoning_mode,
                    "planning_mode": live.planning_mode,
                    "context_usage": self._context_usage_for_session(session_id, live, user_id=user_id),
                    "available_models": _available_models_for_user(user_id),
                    "default_model": _default_model_for_user(user_id),
                }
            meta = self._get_session_meta(session_id, user_id=user_id)
            if not meta:
                raise HTTPException(status_code=404, detail="会话不存在")
            model, reasoning_mode, provider, _ = self._normalize_restore_args(meta, user_id)
            return {
                "session_id": session_id,
                "has_graph": bool(meta.get("last_has_graph", False)),
                "pending_feedback": None,
                "provider": provider,
                "model": model,
                "reasoning_mode": reasoning_mode,
                "planning_mode": "auto",
                "context_usage": self._context_usage_for_session(session_id, user_id=user_id),
                "available_models": _available_models_for_user(user_id),
                "default_model": _default_model_for_user(user_id),
            }

        @app.patch("/api/sessions/{session_id}/model")
        def set_model(session_id: str, req: SetModelReq, request: Request):
            user_id = _get_request_user(request)
            agent, _restored, _needs_graph_reload = self._restore_agent_if_needed(session_id, user_id=user_id)
            try:
                agent.set_model(_normalize_model_for_user(req.model, user_id))
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))
            self.logger.info(
                f"HTTP PATCH /api/sessions/{session_id}/model -> "
                f"{agent.provider}/{agent.model}"
            )
            return {
                "success": True,
                "session_id": session_id,
                "provider": agent.provider,
                "model": agent.model,
                "available_models": _available_models_for_user(user_id),
                "default_model": _default_model_for_user(user_id),
            }

        @app.patch("/api/sessions/{session_id}/reasoning-mode")
        def set_reasoning_mode(session_id: str, req: SetReasoningModeReq, request: Request):
            user_id = _get_request_user(request)
            agent, _restored, _needs_graph_reload = self._restore_agent_if_needed(session_id, user_id=user_id)
            agent.set_reasoning_mode(req.reasoning_mode)
            self.logger.info(
                f"HTTP PATCH /api/sessions/{session_id}/reasoning-mode -> {req.reasoning_mode}"
            )
            return {
                "success": True,
                "session_id": session_id,
                "reasoning_mode": agent.reasoning_mode,
            }

        @app.patch("/api/sessions/{session_id}/planning-mode")
        def set_planning_mode(session_id: str, req: SetPlanningModeReq, request: Request):
            user_id = _get_request_user(request)
            agent, _restored, _needs_graph_reload = self._restore_agent_if_needed(session_id, user_id=user_id)
            agent.set_planning_mode(req.planning_mode)
            self.logger.info(
                f"HTTP PATCH /api/sessions/{session_id}/planning-mode -> {req.planning_mode}"
            )
            return {
                "success": True,
                "session_id": session_id,
                "planning_mode": agent.planning_mode,
            }

        @app.post("/api/sessions/{session_id}/chat")
        async def chat(session_id: str, req: ChatReq, request: Request):
            user_id = _get_request_user(request)
            agent, _restored, _needs_graph_reload = self._restore_agent_if_needed(session_id, user_id=user_id)
            try:
                if req.reasoning_mode and req.reasoning_mode != agent.reasoning_mode:
                    agent.set_reasoning_mode(req.reasoning_mode)
                if req.planning_mode and req.planning_mode != agent.planning_mode:
                    agent.set_planning_mode(req.planning_mode)
                if req.regenerate:
                    await agent.rollback_last_user_turn()
                message = self._resolve_attachments(session_id, req.message, req.attachments, user_id=user_id)
                self.logger.info(f"HTTP POST /api/sessions/{session_id}/chat msg_len={len(message)}")
                tokens: list[str] = []
                async for event in self._stream_agent_events(user_id, agent, message):
                    if event.get("type") == "token":
                        tokens.append(event["content"])
                response = "".join(tokens)
                self.logger.info(
                    f"HTTP POST /api/sessions/{session_id}/chat resp_len={len(response)}"
                )
                return {
                    "success": True,
                    "response": response,
                    "has_graph": agent.has_graph(),
                    "reasoning_mode": agent.reasoning_mode,
                    "context_usage": self._context_usage_for_session(session_id, agent, user_id=user_id),
                }
            except Exception as e:
                agent.log_error(traceback.format_exc())
                traceback.print_exc()
                return {"success": False, "error": str(e)}

        @app.post("/api/sessions/{session_id}/chat/stream")
        async def chat_stream(session_id: str, req: ChatReq, request: Request):
            user_id = _get_request_user(request)
            agent, _restored, _needs_graph_reload = self._restore_agent_if_needed(session_id, user_id=user_id)

            if req.reasoning_mode and req.reasoning_mode != agent.reasoning_mode:
                agent.set_reasoning_mode(req.reasoning_mode)
            if req.planning_mode and req.planning_mode != agent.planning_mode:
                agent.set_planning_mode(req.planning_mode)
            if req.regenerate:
                await agent.rollback_last_user_turn()
            message = self._resolve_attachments(session_id, req.message, req.attachments, user_id=user_id)
            self.logger.info(
                f"HTTP POST /api/sessions/{session_id}/chat/stream msg_len={len(message)}"
            )

            async def event_generator():
                try:
                    async for evt in self._stream_agent_events(user_id, agent, message):
                        yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except Exception as e:
                    err = {"type": "error", "error": str(e)}
                    yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @app.post("/api/sessions/{session_id}/feedback/stream")
        async def feedback_stream(session_id: str, req: FeedbackRespondReq, request: Request):
            user_id = _get_request_user(request)
            self._check_session_ownership(session_id, user_id)
            live = self._get_live_agent(session_id, user_id=user_id)
            if live is None:
                raise HTTPException(status_code=409, detail="当前会话没有可恢复的待确认执行。")

            self.logger.info(
                f"HTTP POST /api/sessions/{session_id}/feedback/stream decision={req.decision}"
            )

            async def event_generator():
                try:
                    async for evt in self._stream_feedback_events(user_id, live, req):
                        yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                except Exception as e:
                    err = {"type": "error", "error": str(e)}
                    yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # ── Dataset 端点 ────────────────────────────────────────────

        async def _call_graph_data_tool(
            self, session_id: str, tool_name: str, arguments: dict, user_id: str = ""
        ) -> dict:
            """通过 httpx 调用 graph_data_service 的工具端点。"""
            url = (
                f"{self.config.resolved_graph_data_base_url()}"
                f"/sessions/{session_id}/tools/{tool_name}"
            )
            resp = await self._post_graph_data_with_retry(
                url,
                timeout=_graph_data_tool_timeout(tool_name, GRAPH_DATA_TOOL_TIMEOUT_SECONDS),
                json_body={"arguments": arguments},
                retry_on_timeout=tool_name not in _GRAPH_DATA_HEAVY_TOOLS,
                retry_on_server_error=tool_name not in _GRAPH_DATA_HEAVY_TOOLS,
                user_id=user_id,
            )
            return resp.json()

        @app.get("/api/datasets")
        async def list_datasets(request: Request):
            user_id = _get_request_user(request)
            try:
                async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                    resp = await client.get(
                        f"{self.config.resolved_graph_data_base_url()}/datasets",
                        headers={"X-User-ID": user_id} if user_id else {},
                    )
                    resp.raise_for_status()
                    return resp.json()
            except Exception as e:
                self.logger.error(f"GET /api/datasets 失败: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/sessions/{session_id}/load-dataset")
        async def load_dataset(session_id: str, req: LoadDatasetReq, request: Request):
            user_id = _get_request_user(request)
            agent, _restored, _needs_graph_reload = self._restore_agent_if_needed(session_id, user_id=user_id)
            try:
                result = await _call_graph_data_tool(
                    self, session_id, "load_dataset",
                    {"dataset_id": req.dataset_id, "version": req.version},
                    user_id=user_id,
                )
                if result.get("success"):
                    agent.apply_graph_load_result(result)
                    label = req.name or req.dataset_id
                    self._inject_synthetic_turn(
                        agent,
                        user_text=f"请加载数据集 {label}",
                        assistant_text=f"已完成数据集「{label}」的加载，我可以运用图的专业知识帮你对其进行分析。",
                    )
                return result
            except Exception as e:
                self.logger.error(f"POST /api/sessions/{session_id}/load-dataset 失败: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/sessions/{session_id}/graph/clear")
        async def clear_graph(session_id: str, request: Request):
            user_id = _get_request_user(request)
            agent, _restored, _needs_graph_reload = self._restore_agent_if_needed(session_id, user_id=user_id)
            try:
                result = await _call_graph_data_tool(self, session_id, "clear_graph", {}, user_id=user_id)
                if result.get("success"):
                    agent.clear_graph_state()
                return result
            except Exception as e:
                self.logger.error(f"POST /api/sessions/{session_id}/graph/clear 失败: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/sessions/{session_id}/save-dataset")
        async def save_dataset(session_id: str, req: SaveDatasetReq, request: Request):
            user_id = _get_request_user(request)
            agent, _restored, _needs_graph_reload = self._restore_agent_if_needed(session_id, user_id=user_id)
            try:
                result = await _call_graph_data_tool(
                    self, session_id, "save_as_dataset",
                    {"name": req.name, "description": req.description},
                    user_id=user_id,
                )
                return result
            except Exception as e:
                self.logger.error(f"POST /api/sessions/{session_id}/save-dataset 失败: {e}")
                raise HTTPException(status_code=500, detail=str(e))

        @app.delete("/api/datasets/{dataset_id}")
        async def delete_dataset_api(dataset_id: str, request: Request):
            user_id = _get_request_user(request)
            try:
                async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                    resp = await client.delete(
                        f"{self.config.resolved_graph_data_base_url()}/datasets/{dataset_id}",
                        headers={"X-User-ID": user_id} if user_id else {},
                    )
                    return resp.json()
            except Exception as e:
                self.logger.error(f"DELETE /api/datasets/{dataset_id} 失败: {e}")
                raise HTTPException(status_code=500, detail=str(e))
        @app.post("/api/sessions/{session_id}/graph/visualize")
        def visualize_graph(session_id: str, request: Request, req: dict = None):
            user_id = _get_request_user(request)
            self._check_session_ownership(session_id, user_id)
            """
            获取图的完整结构用于可视化渲染。
            请求体: { "include_community": bool (可选) }
            """
            # 直接调用 graph_data_service API，不依赖 MCP/agent
            import httpx
            
            include_community = (req or {}).get("include_community", False)
            graph_data_url = self.config.resolved_graph_data_base_url()
            
            try:
                resp = httpx.post(
                    f"{graph_data_url}/sessions/{session_id}/graph/visualize",
                    json={"arguments": {"include_community": include_community}},
                    headers={"X-User-ID": user_id},
                    timeout=30.0,
                )
                data = resp.json()
                if data.get("success"):
                    return {
                        "success": True,
                        **data,
                    }
                else:
                    return {"success": False, "error": data.get("error", "获取图数据失败")}
            except Exception as e:
                self.logger.error(f"调用 graph_data_service 可视化API失败: {e}")
                return {"success": False, "error": f"获取图数据失败: {str(e)}"}

        @app.post("/api/sessions/{session_id}/upload")
        async def upload_file(session_id: str, request: Request, file: UploadFile = File(...)):
            user_id = _get_request_user(request)
            agent, _restored, _needs_graph_reload = self._restore_agent_if_needed(session_id, user_id=user_id)

            file_token = uuid.uuid4().hex

            content = await file.read()
            original_filename = file.filename or f"upload_{file_token}.bin"
            self.logger.info(
                f"HTTP POST /api/sessions/{session_id}/upload "
                f"file={original_filename} size={len(content)}B token={file_token}"
            )
            try:
                await self._register_upload_to_graph_data(
                    session_id=session_id,
                    file_token=file_token,
                    original_filename=original_filename,
                    content=content,
                    content_type=file.content_type or "application/octet-stream",
                    user_id=user_id,
                )
            except Exception as e:
                self.logger.error(f"上传文件传输到 GraphData Server 失败: {e}")
                raise HTTPException(status_code=500, detail="上传失败：GraphData 服务未能接收文件。")

            self._uploaded_files.setdefault(self._agent_key(user_id, session_id), {})[file_token] = {
                "original": original_filename,
                "saved": "managed_by_graph_data_service",
            }
            self._inject_synthetic_turn(
                agent,
                user_text=f"请上传文件 {original_filename}",
                assistant_text=f"已完成文件「{original_filename}」的上传，我可以运用图的专业知识帮你对其进行分析。",
            )
            return {"success": True, "filename": original_filename, "file_token": file_token}

        @app.get("/api/memory")
        def list_memory(request: Request, scope: str = "long_term", query: str = "", limit: int = 100):
            user_id = _get_request_user(request)
            svc = self._get_user_memory_service(user_id)
            if svc is None:
                raise HTTPException(status_code=503, detail="Memory service unavailable")
            return svc.list_items(scope=scope, query=query, limit=limit)

        @app.post("/api/memory")
        def write_memory(req: MemoryWriteReq, request: Request):
            user_id = _get_request_user(request)
            svc = self._get_user_memory_service(user_id)
            if svc is None:
                raise HTTPException(status_code=503, detail="Memory service unavailable")
            result = svc.write_item(
                scope=req.scope,
                key=req.key,
                value=req.value,
                category=req.category,
                confidence=req.confidence,
                reason=req.reason,
            )
            if not result.get("written"):
                raise HTTPException(status_code=400, detail="Memory write rejected or had no effect")
            return result

        @app.patch("/api/memory/{memory_id}")
        def patch_memory(memory_id: str, req: MemoryPatchReq, request: Request):
            user_id = _get_request_user(request)
            svc = self._get_user_memory_service(user_id)
            if svc is None:
                raise HTTPException(status_code=503, detail="Memory service unavailable")
            result = svc.update_item(
                scope=req.scope,
                memory_id=memory_id,
                key=req.key,
                value=req.value,
                reason=req.reason,
            )
            if not result.get("updated"):
                raise HTTPException(status_code=404, detail="Memory item not found or update rejected")
            return result

        @app.delete("/api/memory/{memory_id}")
        def delete_memory(memory_id: str, request: Request, scope: str = "long_term", reason: str = ""):
            user_id = _get_request_user(request)
            svc = self._get_user_memory_service(user_id)
            if svc is None:
                raise HTTPException(status_code=503, detail="Memory service unavailable")
            result = svc.delete_item(
                scope=scope,
                memory_id=memory_id,
                reason=reason,
            )
            if not result.get("deleted"):
                raise HTTPException(status_code=404, detail="Memory item not found")
            return result

        @app.get("/api/memory/events")
        def memory_events(request: Request, limit: int = 200):
            user_id = _get_request_user(request)
            svc = self._get_user_memory_service(user_id)
            if svc is None:
                raise HTTPException(status_code=503, detail="Memory service unavailable")
            return svc.events(limit=limit)

        @app.get("/api/memory/usage")
        def memory_usage(request: Request):
            user_id = _get_request_user(request)
            svc = self._get_user_memory_service(user_id)
            if svc is None:
                raise HTTPException(status_code=503, detail="Memory service unavailable")
            return svc.usage()

        @app.get("/api/metrics/knowledge")
        def knowledge_metrics():
            counters: dict[str, int] = {}

            def _inc(name: str) -> None:
                counters[name] = counters.get(name, 0) + 1

            if self._memory_service is not None:
                for event in self._memory_service.events(limit=10000).get("events", []):
                    event_type = str(event.get("type") or "")
                    if event_type:
                        _inc(event_type)
            path = skill_events_path()
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines()[-10000:]:
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    event_type = str(item.get("type") or "")
                    if event_type:
                        _inc(event_type)
            return {"counters": counters}

        @app.get("/api/export/sft")
        def export_sft(
            request: Request,
            format: str = "alpaca",
            min_turns: int = 1,
            require_tool_calls: bool = False,
        ):
            user_id = _get_request_user(request)
            rows: list[dict] = []
            sessions = self.logger.list_sessions(limit=1000, user_id=user_id) if hasattr(self.logger, "list_sessions") else []
            store = self.logger.get_user_store(user_id) if hasattr(self.logger, "get_user_store") else None
            if store is None:
                return {"success": True, "format": format, "items": rows}
            for meta in sessions:
                session_id = str(meta.get("session_id") or "").strip()
                if not session_id:
                    continue
                records = store.load_message_records(session_id)
                visible = [record for record in records if record.get("visible")]
                if len(visible) < max(int(min_turns), 1) * 2:
                    continue
                if require_tool_calls:
                    trace = store.load_trace_records(session_id)
                    if not any(str(item.get("event") or "") == "TOOL_CALL" for item in trace):
                        continue
                rows.extend(export_sft_records(records, fmt=format))
            return {"success": True, "format": format, "items": rows}

        # ── Skills 端点 ─────────────────────────────────────────────

        @app.get("/api/skills")
        def list_skills(request: Request, source: str = "all"):
            user_id = _get_request_user(request)
            from pathlib import Path as _Path
            from agent_runtime.application_services import SkillRegistry
            from agent_runtime.application_services import (
                user_skills_dir,
                per_user_skills_dir, per_user_review_queue_dir,
            )
            dirs: list = []
            if source in ("all", "global", "bundled"):
                dirs.append(user_skills_dir())
            if source in ("all", "user"):
                dirs.append(per_user_skills_dir(user_id))
            rq = source in ("all", "review_queue")
            if rq:
                dirs.append(per_user_review_queue_dir(user_id))
            registry = SkillRegistry(dirs, include_review_queue=rq)
            specs = registry.discover()
            result = []
            for s in specs:
                skill_dir = _Path(s.path).parent
                has_rejected = (skill_dir / "REJECTED.json").exists()
                meta_path = skill_dir / "metadata.json"
                meta_extra: dict = {}
                if meta_path.exists():
                    try:
                        import json as _json
                        meta_extra = _json.loads(meta_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                result.append({
                    "name": s.name,
                    "description": s.description,
                    "domain": s.domain,
                    "version": s.version,
                    "risk_level": s.risk_level,
                    "source": s.source,
                    "path": s.path,
                    "rejected": has_rejected,
                    "playbook_id": meta_extra.get("playbook_id", ""),
                    "condition": {
                        "requires_tools": list(s.condition.requires_tools),
                        "any_tools": list(s.condition.any_tools),
                        "requires_graph_state": s.condition.requires_graph_state,
                        "activates_on_intents": list(s.condition.activates_on_intents),
                        "tags": list(s.condition.tags),
                    },
                })
            return {"success": True, "skills": result}

        @app.get("/api/skills/{name}")
        def get_skill(name: str, request: Request):
            user_id = _get_request_user(request)
            from pathlib import Path as _Path
            from agent_runtime.application_services import SkillRegistry
            from agent_runtime.application_services import (
                user_skills_dir,
                per_user_skills_dir, per_user_review_queue_dir,
            )
            registry = SkillRegistry(
                [user_skills_dir(), per_user_skills_dir(user_id),
                 per_user_review_queue_dir(user_id)],
                include_review_queue=True,
            )
            spec = registry.get(name)
            if not spec:
                raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
            skill_path = _Path(spec.path)
            content = skill_path.read_text(encoding="utf-8") if skill_path.exists() else ""
            return {
                "success": True,
                "skill": {
                    "name": spec.name,
                    "description": spec.description,
                    "domain": spec.domain,
                    "version": spec.version,
                    "risk_level": spec.risk_level,
                    "source": spec.source,
                    "path": spec.path,
                    "content": content,
                    "condition": {
                        "requires_tools": list(spec.condition.requires_tools),
                        "any_tools": list(spec.condition.any_tools),
                        "requires_graph_state": spec.condition.requires_graph_state,
                        "activates_on_intents": list(spec.condition.activates_on_intents),
                        "tags": list(spec.condition.tags),
                    },
                },
            }

        @app.post("/api/skills")
        def create_skill(req: SkillCreateReq, request: Request):
            user_id = _get_request_user(request)
            from agent_runtime.application_services import SkillManager
            try:
                result = SkillManager(user_id=user_id).create(
                    name=req.name,
                    body=req.body,
                    metadata=req.metadata,
                    destination=req.destination,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            if not result.get("ok"):
                raise HTTPException(status_code=400, detail=result.get("message", "创建失败"))
            return result

        @app.put("/api/skills/{name}")
        def update_skill(name: str, req: SkillUpdateReq, request: Request):
            user_id = _get_request_user(request)
            from pathlib import Path as _Path
            from agent_runtime.application_services import (
                clear_skill_caches,
                per_user_review_queue_dir,
                per_user_skills_dir,
                scan_skill_content,
            )
            root = per_user_skills_dir(user_id) if req.source == "user" else per_user_review_queue_dir(user_id)
            skill_dir = root / name
            if not skill_dir.exists():
                raise HTTPException(status_code=404, detail="Skill not found")
            reason = scan_skill_content(req.body)
            if reason:
                raise HTTPException(status_code=400, detail=f"Content rejected: {reason}")
            (skill_dir / "SKILL.md").write_text(req.body, encoding="utf-8")
            clear_skill_caches()
            return {"success": True, "name": name, "source": req.source}

        @app.delete("/api/skills/{name}")
        def delete_skill(name: str, request: Request, source: str = "user", reason: str = ""):
            user_id = _get_request_user(request)
            import shutil as _shutil
            from agent_runtime.application_services import (
                append_jsonl,
                clear_skill_caches,
                per_user_review_queue_dir,
                per_user_skill_events_path,
                per_user_skills_dir,
            )
            import time as _time
            if source == "user":
                skill_dir = per_user_skills_dir(user_id) / name
            elif source == "review_queue":
                skill_dir = per_user_review_queue_dir(user_id) / name
            else:
                raise HTTPException(status_code=400, detail="invalid source")
            if not skill_dir.exists():
                raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
            _shutil.rmtree(skill_dir)
            clear_skill_caches()
            append_jsonl(str(per_user_skill_events_path(user_id)), {
                "ts": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "type": "skill_deleted",
                "skill_name": name,
                "source": source,
                "reason": reason or "deleted via management panel",
            })
            return {"success": True, "name": name, "source": source}

        @app.post("/api/skills/{name}/approve")
        def approve_skill(name: str, request: Request, req: SkillApproveReq = SkillApproveReq()):
            user_id = _get_request_user(request)
            from agent_runtime.application_services import SkillManager
            try:
                result = SkillManager(user_id=user_id).approve(
                    candidate_name=name,
                    target_name=req.target_name,
                    playbook_id=req.playbook_id,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            if not result.get("ok"):
                raise HTTPException(status_code=400, detail=result.get("message", "审批失败"))
            return result

        @app.post("/api/skills/{name}/reject")
        def reject_skill(name: str, request: Request, req: SkillRejectReq = SkillRejectReq()):
            user_id = _get_request_user(request)
            from agent_runtime.application_services import SkillManager
            try:
                result = SkillManager(user_id=user_id).reject(
                    candidate_name=name,
                    reason=req.reason,
                    remove_files=req.remove_files,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            if not result.get("ok"):
                raise HTTPException(status_code=400, detail=result.get("message", "拒绝失败"))
            return result

        @app.post("/api/skills/generate-from-playbook")
        async def generate_skill_from_playbook(req: SkillGenerateReq, request: Request):
            user_id = _get_request_user(request)
            import json as _json

            playbook_item: dict = {}
            svc = self._get_user_memory_service(user_id)
            if svc is not None:
                items = svc.list_items(scope="playbook", limit=500).get("items", [])
                for item in items:
                    if str(item.get("playbook_id") or "") == req.playbook_id:
                        playbook_item = item
                        break
            if not playbook_item:
                raise HTTPException(status_code=404, detail=f"Playbook '{req.playbook_id}' not found")

            # Prefer the live session agent's model/credentials so the skill
            # generator uses the same LLM the user is already talking to.
            live_agent = self._get_live_agent(req.session_id, user_id=user_id) if req.session_id else None
            try:
                if live_agent is not None:
                    token_stream = live_agent.stream_skill_generation(
                        playbook_item,
                        req.model or live_agent.model,
                    )
                else:
                    token_stream = stream_skill_generation_from_env(
                        playbook_item,
                        req.model or None,
                    )
            except Exception as e:
                raise HTTPException(status_code=503, detail=f"无法初始化生成器: {e}")

            async def _event_gen():
                full: list[str] = []
                try:
                    async for token in token_stream:
                        full.append(token)
                        yield f"data: {_json.dumps({'type': 'token', 'content': token}, ensure_ascii=False)}\n\n"
                    full_content = "".join(full)
                    final_name = extract_generated_skill_name(full_content) or "generated_skill"
                    yield f"data: {_json.dumps({'type': 'done', 'suggested_name': final_name, 'full_content': full_content}, ensure_ascii=False)}\n\n"
                except Exception as e:
                    yield f"data: {_json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"

            return StreamingResponse(
                _event_gen(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @app.post("/api/skills/promote-playbook")
        def promote_playbook_to_skill(req: SkillPromoteReq, request: Request):
            user_id = _get_request_user(request)
            from agent_runtime.application_services import SkillManager, render_candidate_skill
            import json as _json
            from agent_runtime.application_services import per_user_review_queue_dir
            # Load playbook data if memory service available
            playbook_item: dict = {}
            svc = self._get_user_memory_service(user_id)
            if svc is not None:
                items = svc.list_items(scope="playbook", limit=200).get("items", [])
                for item in items:
                    if str(item.get("playbook_id") or "") == req.playbook_id:
                        playbook_item = item
                        break
            if playbook_item:
                body, metadata = render_candidate_skill(playbook_item, name=req.skill_name)
                if req.description:
                    metadata["description"] = req.description
                metadata["domain"] = req.domain
                metadata["risk_level"] = req.risk_level
            else:
                metadata = {
                    "description": req.description or f"从 Playbook {req.playbook_id} 升级",
                    "domain": req.domain,
                    "risk_level": req.risk_level,
                    "version": "0.1.0",
                }
                body = f"# {req.skill_name}\n\n从 Playbook `{req.playbook_id}` 手动升级的技能。\n\n## 使用场景\n描述使用场景。\n\n## 操作步骤\n描述操作步骤。\n"
            try:
                result = SkillManager(user_id=user_id).create(
                    name=req.skill_name,
                    body=body,
                    metadata=metadata,
                    destination="review_queue",
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            if not result.get("ok"):
                raise HTTPException(status_code=400, detail=result.get("message", "升级失败"))
            # Write metadata.json
            meta_path = per_user_review_queue_dir(user_id) / req.skill_name / "metadata.json"
            meta_path.write_text(
                _json.dumps({
                    "source": "manual_promote",
                    "playbook_id": req.playbook_id,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return {**result, "playbook_id": req.playbook_id}

    def run(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        try:
            uvicorn.run(self.app, host=host or self.config.host, port=port or self.config.port)
        finally:
            if self._proc_supervisor is not None:
                self._proc_supervisor.stop_all()


def create_server(
    config: Optional[ApiGatewayConfig] = None,
    *,
    manage_subprocesses: bool = True,
    process_supervisor: Optional[ProcessSupervisor] = None,
    logger: Optional[Logger] = None,
    agent_factory: Optional[AgentFactory] = None,
    runtime_factory: Optional[RuntimeFactory] = None,
) -> ApiGatewayServer:
    return ApiGatewayServer(
        config=config,
        manage_subprocesses=manage_subprocesses,
        process_supervisor=process_supervisor,
        logger=logger,
        agent_factory=agent_factory,
        runtime_factory=runtime_factory,
    )


def create_app(
    config: Optional[ApiGatewayConfig] = None,
    *,
    manage_subprocesses: bool = True,
    process_supervisor: Optional[ProcessSupervisor] = None,
    logger: Optional[Logger] = None,
    agent_factory: Optional[AgentFactory] = None,
    runtime_factory: Optional[RuntimeFactory] = None,
) -> FastAPI:
    return create_server(
        config=config,
        manage_subprocesses=manage_subprocesses,
        process_supervisor=process_supervisor,
        logger=logger,
        agent_factory=agent_factory,
        runtime_factory=runtime_factory,
    ).app

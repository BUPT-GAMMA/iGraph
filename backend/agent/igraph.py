"""
iGraph — 图分析 Agent 核心

协议模式（通过环境变量 LLM_TOOL_PROTOCOL 切换）：
  "text"   (默认) — 文本协议，工具描述写入 system prompt，输出 <think>/<tool_call>/<answer>
  "native"        — 原生 Function Calling，工具走 API tools 参数，输出 tool_calls 字段

两种模式对外产生完全相同的事件流：
  {"type": "thinking_start", "phase": "thinking"}
  {"type": "thinking",       "phase": str, "content": str}
  {"type": "thinking_end",   "phase": str}
  {"type": "token",          "content": str}
  {"type": "tool_call",      "tool": str, "args": dict, "call_id": str}
  {"type": "tool_result",    "tool": str, "call_id": str}
  {"type": "analysis_report","call_id": str, "data": {"report": dict}}
  {"type": "done",           "has_graph": bool}
  {"type": "error",          "error": str}
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
import traceback
import uuid
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from backend.agent.context import (
    ContextManager,
    ReasoningMode,
    build_user_turn_content,
)
from backend.agent.context.token_counter import context_usage
from backend.agent.model_registry import (
    DEFAULT_MODEL as MODEL_DEFAULT,
    SUPPORTED_MODELS as MODEL_SUPPORTED,
    default_model_for_provider,
    normalize_model_name,
    provider_for_model,
)
from backend.agent.memory.manager import MemoryManager
from backend.agent.memory.scope import DEFAULT_SCOPE
from backend.agent.memory.tools import VIRTUAL_MEMORY_TOOL_SPECS, handle_memory_tool_call
from backend.agent.sub_agents.planner import AnalysisPlan, Planner
from backend.agent.sub_agents.plan_solver import PlanSolver
from backend.agent.sub_agents.context_compressor import ContextCompressor
from backend.agent.sub_agents.query_analyzer import QueryAnalyzer
from backend.agent.provider_gateway import ProviderGateway
from backend.agent.skills.registry import SkillRegistry
from backend.agent.skills.tools import VIRTUAL_SKILL_TOOL_SPECS, handle_skill_tool_call
from backend.agent.tools.core_set import CORE_TOOL_NAMES
from backend.agent.sub_agents.tool_selector import LLMToolSelector, ToolSelectionRequest
from backend.logging.logger import SessionLogger
from backend.runtime.dotenv_compat import load_dotenv_compat
from mcp_service.raw_client import RawMCPClient

PlanningMode = Literal["never", "auto", "always"]

_MAX_STEPS = 16
_MAX_PROTOCOL_RETRIES = 2
_DEFAULT_CONTEXT_TOKEN_LIMIT = 192_000
_DEFAULT_PRESERVE_RECENT_USER_TURNS = 3
_HIDDEN_TOOLS = {
    "register_session",
    "delete_graph_session",
    "set_session_config",
    "discard_graph_version",
    # Backend-specific aliases remain available over MCP/HTTP for diagnostics,
    # but the Agent uses the unified tools to avoid duplicate native + wrapper
    # calls for the same graph query.
    "nebula_get_node_info",
    "nebula_get_neighbors",
    "nebula_shortest_path",
}
# Hard-coded baseline of tools that always require user confirmation. The
# effective set is the union of this baseline with any tool declared by a
# SKILL.md whose frontmatter says ``risk_level: high`` — see
# `IGraph._high_risk_tool_names()`. Skills can therefore opt extra
# tools into the confirmation flow without editing Agent code.
_HIGH_RISK_TOOLS_BASE = frozenset({"train", "infer", "clear_graph"})
# Tools that can raise pending_feedback mid-batch and therefore must run
# serially within a tool_call batch so their continuation semantics are
# preserved. Agent computes the dynamic counterpart at call time.
_FEEDBACK_ONLY_TOOLS = frozenset({"request_user_feedback"})
_GRAPH_MUTATION_TOOLS = frozenset({
    "load_graph",
    "load_graph_from_upload",
    "load_graph_from_text",
    "load_graph_from_triples",
    "load_dataset",
    "load_graph_version",
    "discard_graph_version",
    "clear_graph",
    "undo_graph",
    "to_undirected",
    "subgraph_extraction",
})
_CORE_TOOL_NAMES_TEXT = ", ".join(sorted(CORE_TOOL_NAMES))
_COMPACT_JSON = {"ensure_ascii": False, "separators": (",", ":")}
_VIRTUAL_TOOL_SPECS: List[dict] = [
    *VIRTUAL_SKILL_TOOL_SPECS,
    *VIRTUAL_MEMORY_TOOL_SPECS,
    {
        "server": "igraph",
        "name": "tool_select",
        "description": (
            "按当前任务目标检索并加载相关图分析工具 schema。"
            f"核心工具（{_CORE_TOOL_NAMES_TEXT}）可直接调用；需要调用当前上下文未加载的非核心图工具时，"
            "先用自然语言 query 调用此工具获取完整 schema。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用于选择工具的当前任务或子目标，尽量具体。",
                },
                "top_k": {
                    "type": "integer",
                    "description": "最多加载的工具数量，默认 8。",
                    "default": 8,
                },
            },
            "required": ["query"],
        },
    },
    {
        "server": "igraph",
        "name": "tool_expand",
        "description": "按名称加载 selector 未注入的工具完整 schema，下一轮即可调用该工具。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "server": "igraph",
        "name": "request_user_feedback",
        "description": "向用户发起确认/修改请求，并暂停当前执行流程，等待反馈后再继续。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_type": {"type": "string", "default": "confirmation"},
                "message": {"type": "string"},
                "proposed_tool": {"type": "string"},
                "proposed_args": {"type": "object", "default": {}},
                "editable_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "step_id": {"type": "integer"},
            },
        },
    },
]
load_dotenv_compat()


def _flag(env_name: str, default: bool = True) -> bool:
    val = os.getenv(env_name, "").strip().lower()
    return default if not val else val not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


class IGraph:
    """
    单个会话的图分析 Agent。

    依赖：
      - ContextManager  管理对话历史
      - MCP Server      工具代理
      - SessionLogger   会话日志
    """

    PROVIDER_NAME = "transparent_openai_sdk"
    SUPPORTED_MODELS = MODEL_SUPPORTED
    DEFAULT_MODEL = MODEL_DEFAULT

    def __init__(
        self,
        session_logger: SessionLogger,
        provider: str = PROVIDER_NAME,
        model: Optional[str] = None,
        reasoning_mode: ReasoningMode = "direct",
        planning_mode: PlanningMode = "auto",
        mcp_url: Optional[str] = None,
        debug: bool = False,
        restored_messages: Optional[List[dict]] = None,
        restored_has_graph: bool = False,
        memory_manager: Optional[MemoryManager] = None,
        user_id: str = "",
    ) -> None:
        self.session_id = session_logger.session_id
        self.user_id = str(user_id or "").strip()
        self.model = normalize_model_name(model or self._default_model(provider))
        resolved_provider, api_key, base_url = self._resolve_model_endpoint(self.model)
        self.provider = resolved_provider
        self.temperature = 0.7
        self.reasoning_mode: ReasoningMode = reasoning_mode
        self.planning_mode: PlanningMode = planning_mode
        self.logger = session_logger
        self.debug = debug
        self._memory_manager = memory_manager
        self._memory_prompt_block = ""
        if self._memory_manager is not None:
            self._memory_prompt_block = self._memory_manager.load_and_freeze(DEFAULT_SCOPE)
        self._memory_update_chain: Optional[asyncio.Task] = None
        self._turn_lock = asyncio.Lock()
        self.last_active: float = time.monotonic()

        self._protocol = os.getenv("LLM_TOOL_PROTOCOL", "text").strip().lower()
        self._provider_gateway = ProviderGateway(
            model=self.model,
            api_key=api_key,
            base_url=base_url,
            temperature=self.temperature,
            logger=self.logger,
        )
        self.context_manager = ContextManager(
            session_id=self.session_id,
            reasoning_mode=reasoning_mode,
            protocol=self._protocol,
            provider_gateway=self._provider_gateway,
            logger=self.logger,
        )
        self._plan_solver = PlanSolver(
            context_manager=self.context_manager,
            logger=self.logger,
            protocol_getter=lambda: self._protocol,
            active_tool_specs=self._active_tool_specs,
            visible_tool_specs=lambda: self._visible_tool_specs,
            memory_prompt=lambda: self._memory_prompt_block if self._memory_manager is not None else "",
            graph_context_summary=self._graph_context_summary,
            graph_info_payload=self._graph_info_payload,
            run_tool=lambda tool_call, step_id, progress_callback=None: self._run_tool(
                tool_call,
                step_id=step_id,
                progress_callback=progress_callback,
            ),
            clone_data=self._clone_data,
            pending_feedback=self.pending_feedback,
            set_paused_plan_state=self._set_paused_plan_state,
            register_tools=self._register_skill_tools,
            should_run_batch_serially=self._should_run_batch_serially,
            prepare_tool_result=self._prepare_tool_result_for_observation,
            run_tool_calls_parallel=lambda tcs, step_id: self._run_tool_calls_parallel(
                tcs, step_id=step_id
            ),
        )
        if restored_messages:
            recovered = list(restored_messages)
            if not recovered or recovered[0].get("role") != "system":
                recovered.insert(0, self.context_manager.messages[0])
            self.context_manager.update_messages(recovered)
        else:
            self.logger.log_context_message(
                self.context_manager.messages[0],
                visible=False,
                kind="system_prompt",
                include_in_context=True,
            )

        resolved_mcp_url = (mcp_url or "").strip()
        if not resolved_mcp_url:
            raise ValueError("缺少 MCP URL，请通过环境变量配置后传入 mcp_url。")
        self._mcp_client = RawMCPClient(resolved_mcp_url)
        self._mcp_connected = False
        self._mcp_loop = None
        self._tool_specs: List[dict] = []
        self._visible_tool_specs: List[dict] = []
        self._mcp_visible_tool_specs: List[dict] = []
        self._tool_map: Dict[str, dict] = {}
        self._tool_selector: Optional[LLMToolSelector] = None
        self._expanded_tool_names: set[str] = set()
        self._visible_graphs_for_turn: List[dict] = []
        self._visible_reports_for_turn: List[dict] = []
        self._turn_selected_tool_names: set[str] = set()
        self._skill_registry = self._build_skill_registry()
        self._query_analyzer = QueryAnalyzer(provider_gateway=self._provider_gateway)
        self._context_token_limit = _env_int(
            "SESSION_CONTEXT_TOKEN_LIMIT",
            _DEFAULT_CONTEXT_TOKEN_LIMIT,
        )
        self._context_compressor = ContextCompressor(
            provider_gateway=self._provider_gateway,
        )
        self._context_compression_count = 0
        self._planner: Optional[Planner] = None
        self._has_graph: bool = bool(restored_has_graph)
        self._graph_node_count: int = 0
        self._graph_edge_count: int = 0
        self._graph_is_directed: bool = False
        self._graph_has_weights: bool = False
        self._pending_feedback: Optional[dict[str, Any]] = None
        self._paused_plan_state: Optional[dict[str, Any]] = None
        self._paused_react_state: Optional[dict[str, Any]] = None
        self.logger.set_model(self.provider, self.model)
        self.logger.set_reasoning_mode(self.reasoning_mode)
        self.logger.set_graph_flag(self._has_graph)

    # ── 模型端点解析 ──────────────────────────────────────────────

    def _build_skill_registry(self) -> SkillRegistry:
        try:
            from backend.agent.skills.paths import per_user_skills_dir, user_skills_dir

            dirs = [user_skills_dir()]
            if self.user_id:
                dirs.append(per_user_skills_dir(self.user_id))
            return SkillRegistry(dirs)
        except Exception:
            return SkillRegistry()

    @staticmethod
    def _require_any_env(candidates: Tuple[str, ...], purpose: str) -> str:
        for name in candidates:
            print(f"尝试获取环境变量 {name} 用于 {purpose}...")
            value = os.getenv(name)
            if value:
                return value
        raise ValueError(
            f"缺少环境变量({purpose})，请在 .env 中配置其一: {', '.join(candidates)}"
        )

    @staticmethod
    def _provider_for_model(model: str) -> str:
        return provider_for_model(model)

    @staticmethod
    def _resolve_model_endpoint(model: str) -> Tuple[str, str, str]:
        model = normalize_model_name(model)
        provider = IGraph._provider_for_model(model)
        if provider == "openai":
            api_key = IGraph._require_any_env(
                ("LLM_API_KEY", ""), "api_key"
            )
            base_url = IGraph._require_any_env(
                ("LLM_BASE_URL", ""), "base_url"
            )
            print(f"使用模型 {model}，提供者 {provider}，API Key 为 {api_key}，Base URL 来自环境变量 LLM_BASE_URL: {base_url}")
            return provider, api_key, base_url
        if provider == "anthropic":
            api_key = IGraph._require_any_env(
                ("LLM_API_KEY", ""), "api_key"
            )
            base_url = IGraph._require_any_env(
                ("LLM_BASE_URL", ""), "base_url"
            )
            print(f"使用模型 {model}，提供者 {provider}，API Key 为 {api_key}，Base URL 来自环境变量 LLM_BASE_URL: {base_url}")
            return provider, api_key, base_url
        if provider == "deepseek":
            api_key = IGraph._require_any_env(
                ("DEEPSEEK_API_KEY", "LLM_API_KEY"), "api_key"
            )
            base_url = IGraph._require_any_env(
                ("DEEPSEEK_BASE_URL", "LLM_BASE_URL"), "base_url"
            )
            print(f"使用模型 {model}，提供者 {provider}，Base URL: {base_url}")
            return provider, api_key, base_url
        if provider == "doubao":
            api_key = IGraph._require_any_env(
                ("ARK_API_KEY", "DOUBAO_API_KEY", "LLM_API_KEY"), "api_key"
            )
            base_url = IGraph._require_any_env(
                ("ARK_BASE_URL", "DOUBAO_BASE_URL", "LLM_BASE_URL"), "base_url"
            )
            print(f"使用模型 {model}，提供者 {provider}，Base URL: {base_url}")
            return provider, api_key, base_url
        api_key = IGraph._require_any_env(
            ("QWEN_API_KEY", "DASHSCOPE_API_KEY", "LLM_API_KEY"), "api_key"
        )
        base_url = IGraph._require_any_env(
            ("QWEN_BASE_URL", "DASHSCOPE_BASE_URL", "LLM_BASE_URL"), "base_url"
        )
        print(f"使用模型 {model}，提供者 {provider}，API Key 来自环境变量，Base URL: {base_url}")
        return provider, api_key, base_url

    @staticmethod
    def _default_model(provider_hint: Optional[str] = None) -> str:
        return default_model_for_provider(provider_hint)

    def set_model(self, model_or_provider: Optional[str], model: Optional[str] = None) -> None:
        if model is not None:
            resolved_model = model
        else:
            value = model_or_provider
            if value in {"openai", "anthropic", "qwen"}:
                resolved_model = self._default_model(value)
            else:
                resolved_model = value
        resolved_model = normalize_model_name(resolved_model or self._default_model())
        resolved_provider, api_key, base_url = self._resolve_model_endpoint(resolved_model)
        self.provider = resolved_provider
        self.model = resolved_model
        self._provider_gateway.set_model_endpoint(
            model=resolved_model,
            api_key=api_key,
            base_url=base_url,
        )
        self.logger.set_model(self.provider, self.model)
        if self._mcp_connected and "set_session_config" in self._tool_map:
            import asyncio as _asyncio
            try:
                _asyncio.get_event_loop().create_task(
                    self._mcp_client.call_tool("set_session_config", {"llm_model": self.model})
                )
            except Exception:
                pass

    def set_reasoning_mode(self, reasoning_mode: ReasoningMode) -> None:
        self.reasoning_mode = reasoning_mode
        self.context_manager.set_reasoning_mode(reasoning_mode)
        self.logger.set_reasoning_mode(reasoning_mode)
        if self.context_manager.messages and self.context_manager.messages[0].get("role") == "system":
            self.logger.log_context_message(
                self.context_manager.messages[0],
                visible=False,
                kind="system_prompt_update",
                include_in_context=True,
            )

    def set_planning_mode(self, planning_mode: PlanningMode) -> None:
        self.planning_mode = planning_mode

    async def rollback_last_user_turn(self) -> None:
        await self._rollback_graph_effects_for_regenerate()
        if not hasattr(self.logger, "rollback_last_user_turn"):
            return
        restored = self.logger.rollback_last_user_turn()
        if restored:
            self.context_manager.update_messages(restored)
            return
        system_msg = (
            self.context_manager.messages[0]
            if self.context_manager.messages and self.context_manager.messages[0].get("role") == "system"
            else None
        )
        self.context_manager.update_messages([system_msg] if system_msg else [])

    async def _rollback_graph_effects_for_regenerate(self) -> None:
        if not hasattr(self.logger, "graph_version_effects_after_last_visible_user_turn"):
            return
        effects = self.logger.graph_version_effects_after_last_visible_user_turn()
        if not effects:
            return
        try:
            await self._ensure_connected()
        except Exception:
            self.logger.log_trace_event(
                "GRAPH_VERSION_REGENERATE_ROLLBACK_SKIPPED",
                {"reason": "mcp_connect_failed", "effects": effects},
            )
            return
        for effect in reversed(effects):
            if str(effect.get("type") or "") != "subgraph_version":
                continue
            graph_version = effect.get("graph_version")
            previous = effect.get("previous_graph_version")
            if graph_version is None:
                continue
            try:
                result_str = await self._mcp_client.call_tool(
                    "discard_graph_version",
                    {
                        "graph_version": int(graph_version),
                        "fallback_graph_version": int(previous) if previous is not None else None,
                    },
                )
                payload = self._decode_mcp_payload(result_str)
                self.logger.log_trace_event(
                    "GRAPH_VERSION_REGENERATE_ROLLBACK",
                    {"effect": effect, "result": payload},
                )
                if isinstance(payload, dict) and payload.get("success"):
                    self._has_graph = True
                    self._graph_node_count = int(payload.get("num_nodes") or 0)
                    self._graph_edge_count = int(payload.get("num_edges") or 0)
                    self.logger.set_graph_meta(
                        has_graph=True,
                        node_count=payload.get("num_nodes"),
                        edge_count=payload.get("num_edges"),
                        graph_version=payload.get("graph_version"),
                    )
            except Exception:
                self.logger.log_trace_event(
                    "GRAPH_VERSION_REGENERATE_ROLLBACK_ERROR",
                    {"effect": effect, "error": traceback.format_exc()},
                )

    # ── MCP 连接管理 ──────────────────────────────────────────────

    async def _ensure_connected(self) -> None:
        current_loop = asyncio.get_running_loop()
        if self._mcp_connected and self._mcp_loop is current_loop:
            return
        if self._mcp_connected and self._mcp_loop is not current_loop:
            try:
                await self._mcp_client.close()
            except Exception:
                pass
            self._mcp_connected = False
            self._mcp_loop = None
            self._tool_map = {}
            self._tool_specs = []
            self._visible_tool_specs = []
            self._mcp_visible_tool_specs = []
            self._turn_selected_tool_names.clear()

        await self._mcp_client.open()
        self._mcp_connected = True
        self._mcp_loop = current_loop
        tools = await self._mcp_client.list_tools()
        self._tool_specs = self._build_tool_specs(tools)
        self._tool_map = {t["name"]: t for t in self._tool_specs}
        self._mcp_visible_tool_specs = [
            spec for spec in self._tool_specs if spec["name"] not in _HIDDEN_TOOLS
        ]
        _exclude: set[str] = set()
        if not _flag("USE_TOOL_SELECT", True):
            _exclude |= {"tool_select", "tool_expand"}
        if not _flag("ENABLE_MEMORY_UPDATE", True):
            _exclude |= {"memory_read", "memory_write", "memory_update", "memory_delete"}
        virtual_specs = [s for s in _VIRTUAL_TOOL_SPECS if s.get("name") not in _exclude]
        self._visible_tool_specs = [*self._mcp_visible_tool_specs, *virtual_specs]
        self._tool_selector = LLMToolSelector(
            self._mcp_visible_tool_specs,
            provider_gateway=self._provider_gateway,
            audit_path=os.path.join(getattr(self.logger, "_store", None).logs_dir, "tool_selection_events.jsonl")
            if getattr(getattr(self.logger, "_store", None), "logs_dir", "")
            else os.path.join("logs", "tool_selection_events.jsonl"),
            model_version=self.model,
        )
        self._planner = Planner(
            self._tool_selector,
            self._mcp_visible_tool_specs,
            provider_gateway=self._provider_gateway,
        )
        self.logger.log_tools_snapshot("mcp_tools_list", self._tool_specs)
        self.logger.log_tools_snapshot("agent_visible_tools", self._visible_tool_specs)
        self.logger.log_trace_event(
            "MCP_CONNECTED",
            {"tools": list(self._tool_map.keys())},
        )
        if "register_session" in self._tool_map:
            try:
                await self._mcp_client.call_tool(
                    "register_session",
                    {"agent_session_id": self.session_id, "user_id": self.user_id},
                )
            except Exception:
                pass
        if "set_session_config" in self._tool_map:
            try:
                await self._mcp_client.call_tool(
                    "set_session_config",
                    {"llm_model": self.model},
                )
            except Exception:
                pass

    async def _cleanup_mcp(self) -> None:
        if self._mcp_connected and "delete_graph_session" in self._tool_map:
            try:
                await self._mcp_client.call_tool("delete_graph_session", {})
            except Exception:
                pass
        if self._mcp_connected:
            try:
                await self._mcp_client.close()
            except Exception:
                pass
        self._mcp_connected = False
        self._mcp_loop = None
        self._tool_map = {}
        self._tool_specs = []
        self._visible_tool_specs = []
        self._mcp_visible_tool_specs = []
        self._expanded_tool_names.clear()
        self._turn_selected_tool_names.clear()

    async def _reconnect_mcp(self) -> None:
        if self._mcp_connected:
            try:
                await self._mcp_client.close()
            except Exception:
                pass
        self._mcp_connected = False
        self._mcp_loop = None
        self._tool_map = {}
        self._tool_specs = []
        self._visible_tool_specs = []
        self._mcp_visible_tool_specs = []
        self._expanded_tool_names.clear()
        self._turn_selected_tool_names.clear()
        await self._ensure_connected()

    # ── 工具执行 ──────────────────────────────────────────────────

    async def _run_tool(
        self,
        tool_call: dict,
        *,
        step_id: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        name = tool_call["name"]
        call_id = tool_call.get("id") or tool_call.get("call_id") or uuid.uuid4().hex
        args = tool_call.get("args", tool_call.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}

        if name == "tool_select":
            return await self._handle_tool_select(args if isinstance(args, dict) else {})

        virtual_result = self._handle_virtual_tool(name, args if isinstance(args, dict) else {})
        if virtual_result is not None:
            self.logger.log_trace_event(
                "VIRTUAL_TOOL_EXEC",
                {"tool": name, "call_id": call_id, "arguments": args, "result": virtual_result},
            )
            return virtual_result

        for attempt in range(2):
            if name not in self._tool_map:
                return f"错误：未知工具 {name}"
            if not self._is_tool_active(name):
                hint = "请先调用 tool_select 按当前任务检索必要工具。" if _flag("USE_TOOL_SELECT", True) else "该工具在当前可用工具列表中不存在。"
                return f"错误：工具 {name} 尚未加载到当前上下文；{hint}"
            if name in self._high_risk_tool_names():
                request = self._request_high_risk_feedback(
                    name=name,
                    args=args if isinstance(args, dict) else {},
                    call_id=call_id,
                    step_id=step_id,
                )
                return json.dumps(
                    {
                        "ok": False,
                        "feedback_requested": True,
                        "request": request,
                        "message": request.get("message") or "",
                    },
                    **_COMPACT_JSON,
                )
            try:
                result_str = await self._execute_tool_call(
                    name=name,
                    args=args if isinstance(args, dict) else {},
                    call_id=call_id,
                    progress_callback=progress_callback,
                )
                return result_str
            except Exception as e:
                if attempt == 0 and type(e).__name__ == "ClosedResourceError":
                    self.logger.log_error(
                        f"MCP 连接已断开，正在重连...\n{traceback.format_exc()}"
                    )
                    await self._reconnect_mcp()
                    continue
                self.logger.log_error(traceback.format_exc())
                err_msg = str(e) or type(e).__name__
                return f"工具 {name} 执行错误：{err_msg}"

        return f"工具 {name} 执行错误：重连后仍然失败"

    # ── 内部辅助 ──────────────────────────────────────────────────

    @staticmethod
    def _clone_data(value: Any) -> Any:
        try:
            return json.loads(json.dumps(value, ensure_ascii=False))
        except Exception:
            return value

    @classmethod
    def _merge_feedback_args(cls, original: Any, patched: Any) -> dict[str, Any]:
        """Merge user-edited feedback args without losing nested config fields."""
        base = cls._clone_data(original or {})
        patch = cls._clone_data(patched or {})
        if not isinstance(base, dict):
            base = {}
        if not isinstance(patch, dict):
            return base

        def merge_dict(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
            for key, value in src.items():
                if isinstance(value, dict) and isinstance(dst.get(key), dict):
                    dst[key] = merge_dict(cls._clone_data(dst[key]), value)
                else:
                    dst[key] = cls._clone_data(value)
            return dst

        return merge_dict(base, patch)

    @staticmethod
    def _sanitize_feedback_fields(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        fields: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in fields:
                fields.append(text)
        return fields

    async def _execute_tool_call(
        self,
        *,
        name: str,
        args: dict,
        call_id: str,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        started_at = asyncio.get_running_loop().time()
        if name == "train":
            result = await self._execute_train_tool_with_progress(
                args=args,
                progress_callback=progress_callback,
            )
        elif name == "load_graph_classification_dataset":
            result = await self._execute_graph_classification_dataset_with_progress(
                args=args,
                progress_callback=progress_callback,
            )
        elif name in {"load_graph_from_text", "load_graph_from_upload"}:
            result = await self._execute_text2graph_tool_with_progress(
                tool_name=name,
                args=args,
                progress_callback=progress_callback,
            )
        else:
            result = await self._mcp_client.call_tool(name, args)
        duration_ms = int((asyncio.get_running_loop().time() - started_at) * 1000)
        result_str = self._format_mcp_tool_result(result)

        if name in {
            "load_graph",
            "load_graph_from_upload",
            "load_dataset",
            "load_graph_classification_graph",
            "load_graph_from_text",
            "load_graph_from_triples",
            "undo_graph",
            "to_undirected",
            "subgraph_extraction",
            "discard_graph_version",
        }:
            try:
                payload = json.loads(result_str)
                if payload.get("success"):
                    self._has_graph = True
                    self._graph_node_count = payload.get("num_nodes") or 0
                    self._graph_edge_count = payload.get("num_edges") or 0
                    self._graph_is_directed = bool(payload.get("directed"))
                    self._graph_has_weights = bool(payload.get("has_edge_weight"))
                    self.logger.set_graph_meta(
                        has_graph=True,
                        node_count=payload.get("num_nodes"),
                        edge_count=payload.get("num_edges"),
                        is_directed=payload.get("directed"),
                        has_weights=payload.get("has_edge_weight"),
                        graph_version=payload.get("graph_version"),
                    )
            except Exception:
                pass
        elif name == "clear_graph":
            self._has_graph = False
            self._graph_node_count = 0
            self._graph_edge_count = 0
            self._graph_is_directed = False
            self._graph_has_weights = False
            self.logger.set_graph_meta(has_graph=False, node_count=0, edge_count=0)
        elif name == "get_graph_info":
            try:
                payload = json.loads(result_str)
                if payload.get("loaded"):
                    self._has_graph = True
                    self._graph_node_count = payload.get("num_nodes") or 0
                    self._graph_edge_count = payload.get("num_edges") or 0
                    self._graph_is_directed = bool(payload.get("directed"))
                    self._graph_has_weights = bool(payload.get("graph_info", {}).get("has_edge_weight"))
                    self.logger.set_graph_meta(
                        has_graph=True,
                        node_count=payload.get("num_nodes"),
                        edge_count=payload.get("num_edges"),
                        is_directed=payload.get("directed"),
                        has_weights=payload.get("graph_info", {}).get("has_edge_weight"),
                        graph_version=payload.get("graph_version"),
                    )
            except Exception:
                pass

        self.logger.log_trace_event(
            "TOOL_EXEC_RAW",
            {
                "tool": name,
                "call_id": call_id,
                "arguments": args,
                "result": result_str,
                "duration_ms": duration_ms,
            },
        )
        return result_str

    @staticmethod
    def _graph_version_effect_from_result(name: str, result: Any) -> Optional[dict]:
        if name != "subgraph_extraction":
            return None
        payload = result
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return None
        if not isinstance(payload, dict):
            return None
        if not payload.get("success") or not payload.get("current_graph_replaced"):
            return None
        graph_version = payload.get("graph_version")
        previous = payload.get("previous_graph_version")
        if graph_version is None:
            return None
        return {
            "type": "subgraph_version",
            "graph_version": graph_version,
            "previous_graph_version": previous,
        }

    async def _execute_train_tool_with_progress(
        self,
        *,
        args: dict,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> dict:
        initial = self._decode_mcp_payload(await self._mcp_client.call_tool("train", dict(args or {})))
        job_id = str(initial.get("job_id") or "").strip() if isinstance(initial, dict) else ""
        if not job_id:
            return initial if isinstance(initial, dict) else {"success": False, "error": str(initial)}

        progress_metadata = self._train_progress_metadata(args)
        last_delivered_kind = ""
        # Two-phase polling:
        # - Pre-training (data loading / preprocessing): poll every 2 s — no epoch
        #   updates will arrive yet, so fast polling only wastes requests.
        # - Training in progress (first epoch update received): switch to 0.2 s so
        #   per-epoch events are delivered promptly.
        first_progress_seen = False
        poll_interval = 2.0
        while True:
            status_payload = self._decode_mcp_payload(
                await self._mcp_client.call_tool("get_job_status", {"job_id": job_id})
            )
            job = status_payload.get("job") if isinstance(status_payload, dict) else None
            if not isinstance(job, dict):
                return {"success": False, "error": f"训练作业不存在: {job_id}", "job_id": job_id}

            # Deliver all per-epoch progress updates accumulated since the last poll.
            pending_updates = status_payload.get("progress_updates") or []
            if pending_updates and not first_progress_seen:
                first_progress_seen = True
                poll_interval = 0.2
            for update in pending_updates:
                if progress_callback is not None and isinstance(update, dict):
                    progress_callback(self._with_train_progress_metadata(update, progress_metadata))
                    last_delivered_kind = str(update.get("kind") or "")

            status = str(job.get("status") or "").lower()
            if status == "done":
                result = job.get("result")
                if isinstance(result, dict):
                    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
                    terminal_kinds = {"train_done", "train_early_stop"}
                    if progress_callback is not None and last_delivered_kind not in terminal_kinds:
                        detail = job.get("detail") if isinstance(job.get("detail"), dict) else {}
                        base = {
                            **detail,
                            "job_id": job_id,
                            "status": status,
                        }
                        if metrics.get("stopped_early"):
                            progress_callback(
                                self._with_train_progress_metadata(
                                    {
                                        **base,
                                        "kind": "train_early_stop",
                                        "progress": 1.0,
                                        "stopped_early": True,
                                        "epoch": metrics.get("trained_epochs") or detail.get("epoch"),
                                        "best_epoch": metrics.get("best_epoch") or detail.get("best_epoch"),
                                        "message": "training completed with early stop",
                                    },
                                    progress_metadata,
                                )
                            )
                        else:
                            progress_callback(
                                self._with_train_progress_metadata(
                                    {
                                        **base,
                                        "kind": "train_done",
                                        "progress": 1.0,
                                        "message": "training completed",
                                    },
                                    progress_metadata,
                                )
                            )
                    return result
                return {"success": True, "result": result, "job_id": job_id}
            if status in {"failed", "cancelled"}:
                return {
                    "success": False,
                    "error": str(job.get("message") or status),
                    "job_id": job_id,
                    "status": status,
                }
            await asyncio.sleep(poll_interval)

    async def _execute_graph_classification_dataset_with_progress(
        self,
        *,
        args: dict,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> dict:
        initial = self._decode_mcp_payload(
            await self._mcp_client.call_tool(
                "load_graph_classification_dataset",
                dict(args or {}),
            )
        )
        job_id = str(initial.get("job_id") or "").strip() if isinstance(initial, dict) else ""
        if not job_id:
            return initial if isinstance(initial, dict) else {
                "success": False,
                "error": str(initial),
            }

        last_delivered_kind = ""
        poll_interval = 1.0
        while True:
            try:
                status_payload = self._decode_mcp_payload(
                    await self._mcp_client.call_tool(
                        "get_job_status",
                        {"job_id": job_id},
                    )
                )
            except Exception as exc:
                return {
                    "success": False,
                    "error": f"轮询图分类数据集加载作业失败: {exc}",
                    "job_id": job_id,
                }
            job = status_payload.get("job") if isinstance(status_payload, dict) else None
            if not isinstance(job, dict):
                return {
                    "success": False,
                    "error": f"图分类数据集加载作业不存在: {job_id}",
                    "job_id": job_id,
                }

            pending_updates = status_payload.get("progress_updates") or []
            if pending_updates:
                poll_interval = 0.2
            for update in pending_updates:
                if progress_callback is not None and isinstance(update, dict):
                    progress_callback(update)
                    last_delivered_kind = str(update.get("kind") or "")

            status = str(job.get("status") or "").lower()
            if status == "done":
                result = job.get("result")
                if (
                    progress_callback is not None
                    and last_delivered_kind != "graph_classification_dataset_load_done"
                ):
                    detail = job.get("detail") if isinstance(job.get("detail"), dict) else {}
                    progress_callback(
                        {
                            **detail,
                            "kind": "graph_classification_dataset_load_done",
                            "progress": 1.0,
                            "message": "graph classification dataset loaded",
                            "job_id": job_id,
                        }
                    )
                return result if isinstance(result, dict) else {
                    "success": True,
                    "result": result,
                    "job_id": job_id,
                }
            if status in {"failed", "cancelled"}:
                return {
                    "success": False,
                    "error": str(job.get("message") or status),
                    "job_id": job_id,
                    "status": status,
                }
            await asyncio.sleep(poll_interval)

    async def _poll_text2graph_job(
        self,
        job_id: str,
        *,
        tool_name: str,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> dict:
        is_upload = tool_name == "load_graph_from_upload"
        poll_interval = 5.0 if is_upload else 1.0
        last_delivered_kind = ""
        while True:
            try:
                status_payload = self._decode_mcp_payload(
                    await self._mcp_client.call_tool("get_job_status", {"job_id": job_id})
                )
            except Exception as exc:
                return {"success": False, "error": f"轮询后台图任务失败: {exc}", "job_id": job_id}

            job = status_payload.get("job") if isinstance(status_payload, dict) else None
            if not isinstance(job, dict):
                return {"success": False, "error": f"后台图任务不存在: {job_id}", "job_id": job_id}

            pending_updates = status_payload.get("progress_updates") or []
            if pending_updates:
                poll_interval = 5.0 if is_upload else 0.5
            elif is_upload:
                # Binary uploads can spend minutes parsing or importing a large
                # graph. Poll less often while progress is unchanged so status
                # checks do not compete with the CPU-bound worker.
                poll_interval = min(30.0, poll_interval * 2.0)
            for update in pending_updates:
                if progress_callback is not None and isinstance(update, dict):
                    progress_callback(update)
                    last_delivered_kind = str(update.get("kind") or "")

            status = str(job.get("status") or "").lower()
            if status == "done":
                result = job.get("result")
                if progress_callback is not None and last_delivered_kind != "text2graph_done":
                    detail = job.get("detail") if isinstance(job.get("detail"), dict) else {}
                    progress_callback({
                        **detail,
                        "kind": "text2graph_done",
                        "progress": 1.0,
                        "message": "done",
                        "job_id": job_id,
                    })
                return result if isinstance(result, dict) else {"success": True, "result": result, "job_id": job_id}
            if status in {"failed", "cancelled"}:
                return {
                    "success": False,
                    "error": str(job.get("message") or status),
                    "job_id": job_id,
                    "status": status,
                }
            await asyncio.sleep(poll_interval)

    async def _execute_text2graph_tool_with_progress(
        self,
        *,
        tool_name: str,
        args: dict,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> dict:
        initial = self._decode_mcp_payload(
            await self._mcp_client.call_tool(tool_name, dict(args or {}))
        )
        job_id = str(initial.get("job_id") or "").strip() if isinstance(initial, dict) else ""
        if not job_id:
            return initial if isinstance(initial, dict) else {"success": False, "error": str(initial)}
        return await self._poll_text2graph_job(
            job_id,
            tool_name=tool_name,
            progress_callback=progress_callback,
        )

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "on"}:
                return True
            if normalized in {"0", "false", "no", "n", "off"}:
                return False
        return bool(value)

    @classmethod
    def _train_progress_metadata(cls, args: dict) -> dict[str, Any]:
        payload = args if isinstance(args, dict) else {}
        algo = payload.get("algo_config") if isinstance(payload.get("algo_config"), dict) else {}
        train = payload.get("train_config") if isinstance(payload.get("train_config"), dict) else {}
        task = str(payload.get("task") or "")
        method = str(payload.get("method") or "").strip()
        model = str(payload.get("model") or "").strip().upper()

        if task == "pretrain" and method.lower() == "node2vec":
            display_model = "Node2Vec"
            model_config = {
                "embedding_dim": int(algo.get("embedding_dim", 128)),
                "walk_length": int(algo.get("walk_length", 20)),
                "context_size": int(algo.get("context_size", 10)),
            }
        else:
            if not model:
                model = "GCN"
            hidden_dim = int(algo.get("hidden_dim", 64))
            model_config = {
                "num_layers": int(algo.get("num_layers", 2)),
                "hidden_dim": hidden_dim,
                "embedding_dim": int(algo.get("embedding_dim", hidden_dim)),
                "dropout": float(algo.get("dropout", 0.2)),
                "use_residual_connection": cls._as_bool(algo.get("use_residual_connection"), False),
                "use_normalization": cls._as_bool(algo.get("use_normalization"), False),
                "norm_pos": str(algo.get("norm_pos", "post") or "post"),
                "norm_type": str(algo.get("norm_type", "layer_norm") or "layer_norm"),
            }
            if model == "GAT":
                model_config["gat_heads"] = int(algo.get("gat_heads", 2))
            if task == "graph_classification":
                model_config["node_embedding_dim"] = int(
                    algo.get("node_embedding_dim", 64)
                )
            display_model = model

        return {
            "task": task,
            "model": model or None,
            "method": method or None,
            "display_model": display_model,
            "model_config": model_config,
            "train_config": {
                "epochs": int(train.get("epochs", 200)),
                "lr": float(train.get("lr", 0.05)),
                "weight_decay": float(train.get("weight_decay", 0.0)),
                "early_stop_patience": int(train.get("early_stop_patience", 0)),
                "seed": int(train.get("seed", 42)),
                "batch_size": int(train.get("batch_size", 64)),
                "num_workers": int(train.get("num_workers", 0)),
            },
        }

    @staticmethod
    def _with_train_progress_metadata(payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        merged = {**metadata, **payload}
        for key in ("model_config", "train_config"):
            base = metadata.get(key) if isinstance(metadata.get(key), dict) else {}
            override = payload.get(key) if isinstance(payload.get(key), dict) else {}
            if base or override:
                merged[key] = {**base, **override}
        return merged

    def _decode_mcp_payload(self, result: Any) -> Any:
        if isinstance(result, dict) and "content" not in result:
            return result
        text = self._format_mcp_tool_result(result)
        try:
            return json.loads(text)
        except Exception:
            return result

    @staticmethod
    def _make_tool_progress_event(
        *,
        tool: str,
        call_id: str,
        payload: dict[str, Any],
        step_id: int | None = None,
    ) -> dict[str, Any]:
        event = {
            "type": "tool_progress",
            "tool": tool,
            "call_id": call_id,
            "progress": payload,
        }
        if step_id is not None:
            event["step_id"] = step_id
        return event

    def _should_run_batch_serially(self, tool_calls: List[dict]) -> bool:
        """Force serial execution when a batch contains tools that may pause
        execution mid-batch (high-risk tools requiring confirmation, or the
        explicit request_user_feedback virtual tool). Single-call batches
        also stay on the serial path since there is nothing to parallelize.

        tool_select and all virtual tools must also run serially: they modify
        agent state (loaded schemas, memory, skill registry) that subsequent
        tools in the same batch may depend on.
        """
        if not _flag("ENABLE_PARALLEL_TOOLS", True):
            return True
        if len(tool_calls) <= 1:
            return True
        virtual_names = {str(spec.get("name") or "") for spec in _VIRTUAL_TOOL_SPECS}
        virtual_names.add("tool_select")
        feedback_triggering = self._feedback_triggering_tool_names()
        for tc in tool_calls:
            name = str(tc.get("name") or "")
            if (
                name in feedback_triggering
                or name in virtual_names
                or name in _GRAPH_MUTATION_TOOLS
            ):
                return True
        return False

    async def _run_tool_calls_parallel(
        self,
        tool_calls: List[dict],
        *,
        step_id: int | None = None,
    ):
        """Concurrently execute a batch of tool_calls.

        Yields the same event types as the serial path (tool_call,
        tool_progress, tool_result), and finally yields a single sentinel
        {"type": "_batch_done", "observations": [...]} that the caller
        consumes to flush observations into context. Observation order
        matches `tool_calls` order for deterministic context layout, even
        though tasks may complete out of order.
        """
        for tc in tool_calls:
            args = tc.get("arguments", {})
            self.logger.log_tool_call(tc["name"], args)
            yield {
                "type": "tool_call",
                "tool": tc["name"],
                "args": args,
                "call_id": tc.get("id"),
                "server": tc.get("server", "graph"),
            }

        merged_queue: asyncio.Queue = asyncio.Queue()
        DONE = object()

        async def runner(tc: dict) -> None:
            progress_q: asyncio.Queue = asyncio.Queue()
            inner_task = asyncio.create_task(
                self._run_tool(
                    tc,
                    step_id=step_id,
                    progress_callback=lambda payload, q=progress_q: q.put_nowait(payload),
                )
            )
            try:
                while not inner_task.done():
                    try:
                        payload = await asyncio.wait_for(progress_q.get(), timeout=0.25)
                        merged_queue.put_nowait(("progress", tc, payload))
                    except asyncio.TimeoutError:
                        continue
                while not progress_q.empty():
                    payload = progress_q.get_nowait()
                    merged_queue.put_nowait(("progress", tc, payload))
                try:
                    result_str = inner_task.result()
                except Exception as exc:
                    result_str = f"工具 {tc.get('name', '')} 执行错误：{exc}"
                merged_queue.put_nowait(("result", tc, result_str))
            finally:
                merged_queue.put_nowait((DONE, tc, None))

        tasks = [asyncio.create_task(runner(tc)) for tc in tool_calls]

        results_by_call_id: Dict[str, dict] = {}
        remaining_runners = len(tasks)
        try:
            while remaining_runners > 0:
                kind, tc, payload = await merged_queue.get()
                call_id = tc.get("id")
                if kind == "progress":
                    yield self._make_tool_progress_event(
                        tool=tc["name"],
                        call_id=str(call_id or ""),
                        payload=payload if isinstance(payload, dict) else {},
                        step_id=step_id,
                    )
                elif kind == "result":
                    self.logger.log_tool_result(tc["name"], payload)
                    extra_events, context_result = self._prepare_tool_result_for_observation(
                        tc["name"],
                        payload,
                        str(call_id or ""),
                    )
                    for extra_event in extra_events:
                        yield extra_event
                    results_by_call_id[str(call_id or "")] = {
                        "call_id": call_id,
                        "server": tc.get("server", "graph"),
                        "name": tc["name"],
                        "arguments": tc.get("arguments", {}),
                        "result": context_result,
                    }
                    yield {"type": "tool_result", "tool": tc["name"], "call_id": call_id}
                elif kind is DONE:
                    remaining_runners -= 1
        finally:
            pending = [t for t in tasks if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        ordered_observations = [
            results_by_call_id[str(tc.get("id") or "")]
            for tc in tool_calls
            if str(tc.get("id") or "") in results_by_call_id
        ]
        yield {"type": "_batch_done", "observations": ordered_observations}

    async def _progress_events_until_done(
        self,
        task: asyncio.Task,
        queue: asyncio.Queue,
        *,
        tool: str,
        call_id: str,
        step_id: int | None = None,
    ):
        while not task.done():
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            if isinstance(payload, dict):
                yield self._make_tool_progress_event(
                    tool=tool,
                    call_id=call_id,
                    payload=payload,
                    step_id=step_id,
                )
        while not queue.empty():
            payload = queue.get_nowait()
            if isinstance(payload, dict):
                yield self._make_tool_progress_event(
                    tool=tool,
                    call_id=call_id,
                    payload=payload,
                    step_id=step_id,
                )

    def _make_feedback_request(
        self,
        *,
        request_type: str,
        message: str,
        proposed_tool: str,
        proposed_args: dict[str, Any] | None = None,
        editable_fields: list[str] | None = None,
        input_schema: dict[str, Any] | None = None,
        step_id: int | None = None,
        call_id: str = "",
        source: str = "agent",
    ) -> dict[str, Any]:
        request = {
            "request_id": f"fb_{uuid.uuid4().hex[:12]}",
            "request_type": str(request_type or "confirmation").strip() or "confirmation",
            "message": str(message or "").strip(),
            "proposed_tool": str(proposed_tool or "").strip(),
            "proposed_args": self._clone_data(proposed_args or {}),
            "editable_fields": list(editable_fields or []),
            "input_schema": self._clone_data(input_schema or {}),
            "step_id": step_id,
            "call_id": call_id,
            "source": source,
            "status": "pending",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self._pending_feedback = request
        self.logger.log_trace_event("USER_FEEDBACK_REQUESTED", request)
        return request

    def _request_high_risk_feedback(
        self,
        *,
        name: str,
        args: dict[str, Any],
        call_id: str,
        step_id: int | None,
    ) -> dict[str, Any]:
        if self._pending_feedback is not None:
            return self._pending_feedback
        return self._make_feedback_request(
            request_type="confirmation",
            message=(
                f"高风险工具 {name} 需要用户确认后才能执行。"
                "请确认或修改以下参数。"
            ),
            proposed_tool=name,
            proposed_args=args,
            editable_fields=[],
            input_schema=(self._tool_map.get(name) or {}).get("inputSchema", {}),
            step_id=step_id,
            call_id=call_id,
            source="high_risk_guard",
        )

    def pending_feedback(self) -> Optional[dict[str, Any]]:
        if self._pending_feedback is None:
            return None
        return self._clone_data(self._pending_feedback)

    def _set_paused_plan_state(self, state: dict[str, Any]) -> None:
        self._paused_plan_state = state

    def _active_tool_names(self) -> set[str]:
        if not _flag("USE_TOOL_SELECT", True):
            return {str(s.get("name") or "") for s in self._visible_tool_specs}
        return set(CORE_TOOL_NAMES) | set(self._expanded_tool_names) | set(self._turn_selected_tool_names)

    def _active_tool_specs(self) -> List[dict]:
        active_names = self._active_tool_names()
        return [
            spec
            for spec in self._visible_tool_specs
            if str(spec.get("name") or "") in active_names
        ]

    def _is_tool_active(self, name: str) -> bool:
        return name in self._active_tool_names()

    def _high_risk_tool_names(self) -> set[str]:
        """Effective high-risk tool set = baseline ∪ tools declared by
        skills with ``risk_level: high``. Recomputed lazily because the
        SKILL registry is cheap to scan and may pick up user-installed
        skills mid-session."""
        derived: set[str] = set()
        if self._skill_registry is not None:
            try:
                derived = self._skill_registry.high_risk_tool_names()
            except Exception:
                derived = set()
        return set(_HIGH_RISK_TOOLS_BASE) | derived

    def _feedback_triggering_tool_names(self) -> set[str]:
        return self._high_risk_tool_names() | set(_FEEDBACK_ONLY_TOOLS)

    def _register_skill_tools(self, tool_names: Any) -> list[str]:
        """Inject schemas for tools declared by a SKILL.md / plan step.

        Mirrors what `tool_select` does, but without an extra LLM round-trip:
        once the model views a skill or starts a plan step, the tools it
        references should be immediately callable.
        Uses _expanded_tool_names (session-scoped) so that tools loaded via
        skill_view remain active across subsequent turns, not just the current one.
        """
        if not tool_names:
            return []
        # When USE_TOOL_SELECT is off all tools are already active; nothing to register.
        if not _flag("USE_TOOL_SELECT", True):
            return []
        visible = {str(spec.get("name") or "") for spec in self._mcp_visible_tool_specs}
        accepted: list[str] = []
        seen: set[str] = set()
        for raw in tool_names:
            name = str(raw or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if name in CORE_TOOL_NAMES or name in self._expanded_tool_names:
                accepted.append(name)
                continue
            if name not in visible or name in _HIDDEN_TOOLS:
                continue
            self._expanded_tool_names.add(name)
            accepted.append(name)
        if accepted:
            self.logger.log_trace_event(
                "SKILL_TOOLS_PRELOAD",
                {"requested": list(tool_names), "loaded": accepted},
            )
        return accepted

    async def _handle_tool_select(self, args: dict) -> str:
        query = str((args or {}).get("query") or "").strip()
        if not query:
            return "错误：tool_select 需要 query 参数，用于描述当前需要哪些工具。"
        try:
            top_k = int((args or {}).get("top_k") or 8)
        except (TypeError, ValueError):
            top_k = 8
        top_k = max(1, min(top_k, 16))

        if self._tool_selector is None:
            result = {
                "ok": False,
                "error": "selector_unavailable",
                "message": "缺少必要的工具选择器，无法可靠加载完成该任务所需的工具。",
                "query": query,
            }
            return json.dumps(result, **_COMPACT_JSON)

        req = ToolSelectionRequest(
            user_input=query,
            graph_state_summary=self._graph_context_summary(),
            detected_intent=None,
            conversation_recent_tail="",
            tool_catalog=self._tool_selector.catalog,
        )
        result = await self._tool_selector.select(req, k=top_k)
        candidate_names = {str(spec.get("name") or "") for spec in self._mcp_visible_tool_specs}
        selected = [
            name
            for name in result.selected
            if name in candidate_names and name not in _HIDDEN_TOOLS
        ]

        if not selected:
            payload = {
                "ok": False,
                "error": "selector_failed",
                "message": "未能可靠选择完成该任务所需的工具。请告知用户当前缺少必要工具，或请用户补充任务/数据条件后重试。",
                "query": query,
                "reasoning": result.reasoning,
                "fallback": result.fallback_used,
            }
            self.logger.log_trace_event(
                "TOOL_SELECT",
                {"query": query, "selected": [], "fallback": result.fallback_used, "ok": False},
            )
            return json.dumps(payload, **_COMPACT_JSON)

        self._turn_selected_tool_names.update(selected)
        loaded_schemas = [
            spec
            for spec in self._mcp_visible_tool_specs
            if str(spec.get("name") or "") in selected
        ]
        payload = {
            "ok": True,
            "query": query,
            "selected_tools": selected,
            "message": (
                "工具 schema 已加入上下文（关键词匹配兜底，结果仅供参考，请确认工具符合需求）。"
                if result.fallback_used else
                "这些工具完整 schema 已加入后续模型上下文，请基于返回 schema 继续完成任务。"
            ),
            "reasoning": result.reasoning,
            "fallback_used": result.fallback_used,
            "loaded_tools": loaded_schemas,
            "loaded_tool_schemas": loaded_schemas,
        }
        self.logger.log_trace_event(
            "TOOL_SELECT",
            {"query": query, "selected": selected, "fallback": result.fallback_used, "ok": True},
        )
        return json.dumps(payload, **_COMPACT_JSON)

    def _handle_virtual_tool(self, name: str, args: dict) -> Optional[str]:
        skill_result = handle_skill_tool_call(
            name,
            args,
            self._skill_registry,
            user_id=getattr(self, "user_id", ""),
            register_tools=self._register_skill_tools,
        )
        if skill_result is not None:
            return skill_result

        if self._memory_manager is not None:
            engine = self._memory_manager.engine
            mem_result = handle_memory_tool_call(name, args if isinstance(args, dict) else {}, engine)
            if mem_result is not None:
                return mem_result

        if name == "tool_expand":
            tool_name = str((args or {}).get("name") or "").strip()
            if not tool_name:
                return "错误：tool_expand 需要 name 参数"
            spec = next((spec for spec in self._visible_tool_specs if spec.get("name") == tool_name), None)
            if spec is None or tool_name in _HIDDEN_TOOLS:
                return f"错误：未知或不可展开工具 {tool_name}"
            self._expanded_tool_names.add(tool_name)
            return json.dumps(
                {
                    "expanded": tool_name,
                    "message": "该工具 schema 已加入后续模型上下文。",
                    "schema": spec,
                },
                **_COMPACT_JSON,
            )
        if name == "request_user_feedback":
            request_type = str((args or {}).get("request_type") or "confirmation").strip() or "confirmation"
            message = str((args or {}).get("message") or "").strip()
            proposed_tool = str((args or {}).get("proposed_tool") or "").strip()
            proposed_args = (args or {}).get("proposed_args")
            if not isinstance(proposed_args, dict):
                proposed_args = {}
            editable_fields = self._sanitize_feedback_fields((args or {}).get("editable_fields"))
            raw_step_id = (args or {}).get("step_id")
            try:
                step_id = int(raw_step_id) if raw_step_id is not None else None
            except (TypeError, ValueError):
                step_id = None
            request = self._make_feedback_request(
                request_type=request_type,
                message=message or "请用户确认或修改后继续。",
                proposed_tool=proposed_tool,
                proposed_args=proposed_args,
                editable_fields=[],
                input_schema=(self._tool_map.get(proposed_tool) or {}).get("inputSchema", {}),
                step_id=step_id,
                source="virtual_tool",
            )
            return json.dumps(
                {
                    "ok": True,
                    "feedback_requested": True,
                    "request": request,
                },
                **_COMPACT_JSON,
            )
        return None

    def _make_memory_provider_gateway(self) -> ProviderGateway:
        return ProviderGateway(
            model=self.model,
            api_key=self._provider_gateway.api_key,
            base_url=self._provider_gateway.base_url,
            temperature=self.temperature,
            logger=self.logger,
        )

    def _schedule_memory_update(
        self,
        *,
        user_input: str,
        assistant_output: str,
        tool_observations: List[dict],
        turn_messages: Optional[List[dict]] = None,
    ) -> None:
        if not _flag("ENABLE_MEMORY_UPDATE", True):
            return
        if self._memory_manager is None or not assistant_output:
            return

        previous_task = self._memory_update_chain
        observations = [dict(ob) for ob in tool_observations if isinstance(ob, dict)]
        safe_turn_messages = [dict(msg) for msg in (turn_messages or []) if isinstance(msg, dict)]
        try:
            provider_for_memory = self._make_memory_provider_gateway()
        except Exception:
            provider_for_memory = self._provider_gateway

        async def _run_memory_update() -> None:
            if previous_task is not None:
                try:
                    await previous_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            try:
                await self._memory_manager.sync_turn(
                    session_id=self.session_id,
                    user_input=user_input,
                    assistant_output=assistant_output,
                    tool_observations=observations,
                    turn_messages=safe_turn_messages,
                    provider_gateway=provider_for_memory,
                    session_logger=self.logger,
                )
            except Exception:
                self.logger.log_trace_event(
                    "MEMORY_AGENT_ERROR",
                    {"session_id": self.session_id, "error": traceback.format_exc()},
                )
            # Refresh the cached snapshot so writes from this turn become
            # visible to the *next* turn's prompt instead of waiting for
            # the next session. Only updates _memory_prompt_block when the
            # rebuilt block actually differs, to keep prefix-cache hits
            # when memory wrote nothing.
            try:
                changed, new_block = self._memory_manager.refresh_snapshot()
                if changed:
                    self._memory_prompt_block = new_block
                    self.logger.log_trace_event(
                        "MEMORY_SNAPSHOT_REFRESHED",
                        {"session_id": self.session_id, "block_chars": len(new_block)},
                    )
            except Exception:
                self.logger.log_trace_event(
                    "MEMORY_SNAPSHOT_REFRESH_ERROR",
                    {"session_id": self.session_id, "error": traceback.format_exc()},
                )

        self._memory_update_chain = asyncio.create_task(_run_memory_update())
        self.logger.log_trace_event(
            "MEMORY_AGENT_SCHEDULED",
            {
                "session_id": self.session_id,
                "queued": bool(previous_task is not None and not previous_task.done()),
            },
        )

    @staticmethod
    def _build_tool_specs(tools: List[dict]) -> List[dict]:
        catalog_by_name: dict[str, dict] = {}
        try:
            from graph_data_service.tool_catalog import get_all_public_tools

            catalog_by_name = {
                str(spec.get("name") or ""): spec
                for spec in get_all_public_tools(include_graph_ml=True)
                if str(spec.get("name") or "")
            }
        except Exception:
            catalog_by_name = {}

        specs: List[dict] = []
        for t in tools:
            name = str(t.get("name", "")).strip()
            if not name:
                continue
            catalog_spec = catalog_by_name.get(name) or {}
            desc = str(catalog_spec.get("description") or t.get("description") or "").strip()
            input_schema = (
                catalog_spec.get("inputSchema")
                if isinstance(catalog_spec.get("inputSchema"), dict)
                else t.get("inputSchema")
            )
            spec = {
                "server": "graph",
                "name": name,
                "description": desc,
                "inputSchema": copy.deepcopy(input_schema) if isinstance(input_schema, dict) else {},
            }
            specs.append(spec)
        return specs

    @staticmethod
    def _format_mcp_tool_result(result: dict) -> str:
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                texts: List[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(str(block.get("text", "")))
                if texts:
                    return "\n".join(texts)
            try:
                return json.dumps(result, **_COMPACT_JSON)
            except Exception:
                return str(result)
        return str(result)

    @staticmethod
    def _build_visualize_event(result_str: str) -> Optional[dict]:
        """从 visualize_subgraph 工具结果中提取可视化数据，构建 SSE 事件。

        将后端的嵌套 highlights 格式展平为前端所需的一维数组格式:
        后端: {"nodes": [{"ids": [...], "color": "#hex", "label": "..."}], "paths": [...], "subgraphs": [...]}
        前端: [{"id": "hl-0", "type": "node", "nodes": [...], "edges": [...], "color": "#hex", "label": "..."}]
        """
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or not payload.get("success"):
            return None

        viz_data = {}
        # 映射后端 edges → 前端 links（前端 D3 使用 source/target）
        if "nodes" in payload:
            viz_data["nodes"] = payload["nodes"]
        if "edges" in payload:
            viz_data["links"] = [
                {"source": e.get("src", e.get("source")),
                 "target": e.get("dst", e.get("target")),
                 "weight": e.get("weight", 1.0)}
                for e in payload["edges"]
            ]
        if "links" in payload and "links" not in viz_data:
            viz_data["links"] = payload["links"]
        # 社区信息
        if "community" in payload:
            viz_data["communities"] = payload["community"]
        if "communities" in payload:
            viz_data["communities"] = payload["communities"]
        for key in ("directed", "num_nodes", "num_edges", "truncated", "total_nodes", "truncated_to"):
            if key in payload:
                viz_data[key] = payload[key]

        if not viz_data.get("nodes") or "links" not in viz_data:
            return None

        # ── 展平 highlights ──
        hl = payload.get("highlights")
        if hl and isinstance(hl, dict):
            flat: list = []
            idx = 0
            # node highlights
            for item in hl.get("nodes", []):
                flat.append({
                    "id": f"hl-{idx}",
                    "type": "node",
                    "nodes": item.get("ids", []),
                    "edges": [],
                    "color": item.get("color", "#10b981"),
                    "label": item.get("label", "节点"),
                })
                idx += 1
            # path highlights
            for item in hl.get("paths", []):
                flat.append({
                    "id": f"hl-{idx}",
                    "type": "path",
                    "nodes": item.get("node_sequence", []),
                    "edges": item.get("edges", []),
                    "color": item.get("color", "#f59e0b"),
                    "label": item.get("label", "路径"),
                })
                idx += 1
            # subgraph highlights
            for item in hl.get("subgraphs", []):
                flat.append({
                    "id": f"hl-{idx}",
                    "type": "subgraph",
                    "nodes": item.get("nodes", []),
                    "edges": item.get("edges", []),
                    "color": item.get("color", "#8b5cf6"),
                    "label": item.get("label", "子图"),
                })
                idx += 1
            if flat:
                viz_data["highlights"] = flat

        return {"type": "graph_visualize", "data": viz_data}

    @staticmethod
    def _safe_graph_id(call_id: str = "") -> str:
        raw = str(call_id or "").strip()
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in raw)
        return f"ig-{safe}" if safe else f"ig-{uuid.uuid4().hex[:10]}"

    @classmethod
    def _visible_graph_payload_from_event(
        cls,
        viz_event: Optional[dict],
        *,
        call_id: str = "",
    ) -> Optional[dict]:
        if not isinstance(viz_event, dict) or viz_event.get("type") != "graph_visualize":
            return None
        data = viz_event.get("data")
        if not isinstance(data, dict):
            return None
        nodes = data.get("nodes")
        links = data.get("links", data.get("edges"))
        if not isinstance(nodes, list) or not isinstance(links, list):
            return None

        edges: List[dict] = []
        for link in links:
            if not isinstance(link, dict):
                continue
            src = link.get("source", link.get("src"))
            dst = link.get("target", link.get("dst"))
            if src is None or dst is None:
                continue
            edge = {"src": src, "dst": dst, "source": src, "target": dst}
            if "weight" in link:
                edge["weight"] = link.get("weight")
            edges.append(edge)

        graph: dict[str, Any] = {
            "id": cls._safe_graph_id(call_id),
            "nodes": cls._clone_data(nodes),
            "edges": edges,
            "directed": bool(data.get("directed", True)),
            "num_nodes": int(data.get("num_nodes", len(nodes)) or 0),
            "num_edges": int(data.get("num_edges", len(edges)) or 0),
            "activeHighlights": {},
        }
        if isinstance(data.get("highlights"), list):
            graph["highlights"] = cls._clone_data(data["highlights"])
        communities = data.get("communities")
        if communities is not None:
            graph["communities"] = cls._clone_data(communities)
        for key in ("truncated", "total_nodes", "truncated_to"):
            if key in data:
                graph[key] = cls._clone_data(data[key])
        return graph

    def _remember_visible_graph(self, graph: Optional[dict]) -> None:
        if not isinstance(graph, dict):
            return
        if not isinstance(self._visible_graphs_for_turn, list):
            self._visible_graphs_for_turn = []
        graph_id = str(graph.get("id") or "")
        if graph_id and any(str(g.get("id") or "") == graph_id for g in self._visible_graphs_for_turn):
            return
        self._visible_graphs_for_turn.append(self._clone_data(graph))

    @staticmethod
    def _build_report_event(result_str: str, call_id: str = "") -> Optional[dict]:
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return None
        report = payload.get("report")
        if not isinstance(report, dict) or report.get("version") != "analysis_report.v1":
            return None
        return {
            "type": "analysis_report",
            "call_id": str(call_id or ""),
            "data": {"report": copy.deepcopy(report)},
        }

    def _remember_visible_report(self, report: Optional[dict]) -> None:
        if not isinstance(report, dict):
            return
        if not isinstance(self._visible_reports_for_turn, list):
            self._visible_reports_for_turn = []
        report_id = str(report.get("id") or "").strip()
        if report_id and any(
            str(item.get("id") or "").strip() == report_id
            for item in self._visible_reports_for_turn
            if isinstance(item, dict)
        ):
            return
        self._visible_reports_for_turn.append(self._clone_data(report))

    def _prepare_tool_result_for_observation(
        self,
        tool_name: str,
        result_str: str,
        call_id: str = "",
    ) -> tuple[List[dict], str]:
        if tool_name == "create_analysis_report":
            try:
                report_event = self._build_report_event(result_str, call_id=call_id)
                if report_event is None:
                    return [], result_str
                self._remember_visible_report(report_event["data"]["report"])
                return [report_event], self._summarize_report_result(result_str)
            except Exception:
                return [], result_str

        if tool_name != "visualize_subgraph":
            return [], result_str

        events: List[dict] = []
        try:
            viz_event = self._build_visualize_event(result_str)
            if viz_event is not None:
                graph_payload = self._visible_graph_payload_from_event(
                    viz_event,
                    call_id=call_id,
                )
                self._remember_visible_graph(graph_payload)
                events.append(viz_event)
        except Exception:
            events = []
        return events, self._summarize_visualize_result(result_str)

    @staticmethod
    def _summarize_report_result(result_str: str) -> str:
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return result_str
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return result_str
        report = payload.get("report")
        if not isinstance(report, dict) or report.get("version") != "analysis_report.v1":
            return result_str

        sections = report.get("sections")
        sections = sections if isinstance(sections, list) else []
        metric_count = 0
        chart_count = 0
        table_count = 0
        for section in sections:
            if not isinstance(section, dict):
                continue
            metrics = section.get("metrics")
            charts = section.get("charts")
            tables = section.get("tables")
            metric_count += len(metrics) if isinstance(metrics, list) else 0
            chart_count += len(charts) if isinstance(charts, list) else 0
            table_count += len(tables) if isinstance(tables, list) else 0

        title = str(report.get("title") or "图分析报告").strip()
        intent = str(report.get("intent") or "analysis").strip()
        summary = str(report.get("summary") or "").strip()
        result = (
            f"图分析报告已生成：标题「{title}」，意图 {intent}，"
            f"包含 {len(sections)} 个章节、{metric_count} 个指标、"
            f"{chart_count} 个图表和 {table_count} 个表格。"
        )
        if summary:
            result += f" 摘要：{summary}"
        return result + " 完整报告已作为 artifact 发送到前端，无需在回答中重复完整数据。"

    @staticmethod
    def _summarize_visualize_result(result_str: str) -> str:
        """将 visualize_subgraph 的完整 JSON 结果压缩为摘要字符串写入对话历史。

        原始结果包含全量节点/边列表，大图时可能有数万 token。
        摘要只保留统计信息和高亮结构，让 LLM 知道可视化已完成，但不占用大量上下文。
        """
        try:
            payload = json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return result_str  # 解析失败则原样返回

        if not isinstance(payload, dict) or not payload.get("success"):
            return result_str

        num_nodes = payload.get("num_nodes", len(payload.get("nodes", [])))
        num_edges = payload.get("num_edges", len(payload.get("edges", [])))
        directed  = payload.get("directed", True)

        truncated = payload.get("truncated", False)
        total_nodes = payload.get("total_nodes")
        truncated_to = payload.get("truncated_to")

        if truncated and total_nodes and truncated_to:
            truncate_note = (
                f"（图共 {total_nodes} 个节点，因超出上限已采样展示 {truncated_to} 个，"
                f"高亮节点优先保留，其余随机采样；如需更多节点可指定 max_nodes 参数）"
            )
        else:
            truncate_note = ""

        parts = [
            f"可视化已生成：{'有向图' if directed else '无向图'}，"
            f"{num_nodes} 个节点，{num_edges} 条边。"
            f"{truncate_note}"
            f"数据已推送至前端展示，无需在回答中重复列举节点或边。"
        ]

        # 社区信息摘要
        community = payload.get("community")
        if community and isinstance(community, dict):
            parts.append(f"社区数：{community.get('num_communities', '?')}。")

        # highlights 摘要（只保留结构类型和标签，不保留节点列表）
        hl = payload.get("highlights")
        if hl and isinstance(hl, dict):
            hl_parts = []
            for item in hl.get("nodes", []):
                hl_parts.append(f"节点高亮「{item.get('label','')}」({len(item.get('ids',[]))} 个节点)")
            for item in hl.get("paths", []):
                hl_parts.append(f"路径高亮「{item.get('label','')}」({len(item.get('node_sequence',[]))} 跳)")
            for item in hl.get("subgraphs", []):
                hl_parts.append(f"子图高亮「{item.get('label','')}」({len(item.get('nodes',[]))} 个节点)")
            if hl_parts:
                parts.append("高亮：" + "；".join(hl_parts) + "。")

        return " ".join(parts)

    @staticmethod
    def _build_visible_tools(tool_observations: List[dict]) -> List[dict]:
        tools: List[dict] = []
        for ob in tool_observations:
            if not isinstance(ob, dict):
                continue
            name = str(ob.get("name") or ob.get("tool") or "").strip()
            if not name:
                continue
            item: dict[str, Any] = {"tool": name, "done": True}
            args = ob.get("arguments", ob.get("args"))
            if isinstance(args, dict):
                item["args"] = args
            effect = IGraph._graph_version_effect_from_result(name, ob.get("result"))
            if effect is not None:
                item["graph_version_effect"] = effect
            call_id = ob.get("call_id") or ob.get("id")
            if call_id:
                item["call_id"] = str(call_id)
            server = str(ob.get("server") or "").strip()
            if server:
                item["server"] = server
            tools.append(item)
        return tools

    @staticmethod
    def _build_visible_thinking(thinking_blocks: List[dict]) -> List[dict]:
        rows: List[dict] = []
        for block in thinking_blocks:
            if not isinstance(block, dict):
                continue
            phase = str(block.get("phase") or "thinking").strip() or "thinking"
            text = str(block.get("text") or "").strip()
            if not text:
                continue
            rows.append({"phase": phase, "text": text, "streaming": False})
        return rows

    async def _invoke_aux_llm(self, prompt: str) -> str:
        model_msgs = [
            {"role": "system", "content": "你是一个严格输出 JSON 的图分析辅助组件。"},
            {"role": "user", "content": prompt},
        ]
        return await self._provider_gateway.invoke_text(
            model_msgs,
            step=900000 + int(time.time()) % 1000,
            max_tokens=512,
            temperature=0.0,
        )

    async def _stream_aux_llm(self, prompt: str):
        model_msgs = [
            {"role": "system", "content": "你是一个严格按要求输出内容的图分析辅助组件。"},
            {"role": "user", "content": prompt},
        ]
        async for delta in self._provider_gateway.stream_text(
            model_msgs,
            step=910000 + int(time.time()) % 1000,
            max_tokens=512,
            temperature=0.0,
        ):
            if delta:
                yield delta

    def _graph_context_summary(self) -> str:
        if not self._has_graph:
            return "graph_loaded=false"
        return (
            f"graph_loaded=true, nodes={self._graph_node_count}, "
            f"edges={self._graph_edge_count}, "
            f"directed={self._graph_is_directed}, "
            f"weighted={self._graph_has_weights}"
        )

    def _graph_info_payload(self) -> dict:
        return {
            "has_graph": self._has_graph,
            "node_count": self._graph_node_count,
            "edge_count": self._graph_edge_count,
            "is_directed": self._graph_is_directed,
            "has_weights": self._graph_has_weights,
        }

    def context_usage(self) -> dict:
        usage = context_usage(
            self.context_manager.messages,
            limit=self._context_token_limit,
            model=self.model,
        )
        usage["compression_count"] = self._context_compression_count
        return usage

    @staticmethod
    def _fallback_context_summary(messages: List[dict]) -> str:
        snippets: list[str] = []
        for msg in messages[-12:]:
            role = str(msg.get("role") or "unknown")
            content = str(msg.get("content") or "").strip().replace("\n", " ")
            if len(content) > 800:
                content = content[:800] + "..."
            if content:
                snippets.append(f"- {role}: {content}")
        if not snippets:
            return "暂无可压缩的历史上下文。"
        return "## 短期记忆摘要\n\n压缩器未能生成完整摘要，以下保留最近关键上下文：\n" + "\n".join(snippets)

    @staticmethod
    def _is_real_user_turn_message(msg: dict) -> bool:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            return False
        content = str(msg.get("content") or "").lstrip()
        return not content.startswith("[CONTEXT_SUMMARY")

    @classmethod
    def _split_context_for_compression(
        cls,
        messages: List[dict],
        *,
        preserve_recent_user_turns: int,
    ) -> tuple[dict, List[dict], List[dict], int]:
        system_msg = (
            messages[0]
            if messages and messages[0].get("role") == "system"
            else {"role": "system", "content": ""}
        )
        non_system_start = 1 if messages and messages[0].get("role") == "system" else 0
        real_user_indexes = [
            idx
            for idx, msg in enumerate(messages)
            if idx >= non_system_start and cls._is_real_user_turn_message(msg)
        ]
        if not real_user_indexes:
            return system_msg, [], [], 0

        latest_user_idx = real_user_indexes[-1]
        previous_user_indexes = [idx for idx in real_user_indexes[:-1] if idx < latest_user_idx]
        keep_turns = max(int(preserve_recent_user_turns or 0), 0)
        if keep_turns and previous_user_indexes:
            preserve_start_idx = previous_user_indexes[-keep_turns]
        else:
            preserve_start_idx = latest_user_idx

        compressible = [
            dict(msg)
            for msg in messages[non_system_start:preserve_start_idx]
            if isinstance(msg, dict) and msg.get("role") != "system"
        ]
        preserved = [
            dict(msg)
            for msg in messages[preserve_start_idx:]
            if isinstance(msg, dict) and msg.get("role") != "system"
        ]
        return system_msg, compressible, preserved, len(previous_user_indexes[-keep_turns:]) if keep_turns else 0

    async def _maybe_compress_context(
        self,
        *,
        trigger: str,
        preserve_recent_user_turns: int = _DEFAULT_PRESERVE_RECENT_USER_TURNS,
    ) -> Optional[dict]:
        before = self.context_usage()
        trigger_pct = _env_int("CONTEXT_COMPRESSION_TRIGGER_PCT", 90)
        threshold = int(self._context_token_limit * trigger_pct / 100)
        if int(before.get("current_tokens") or 0) < threshold:
            return None

        messages = self.context_manager.messages
        if len(messages) <= 2:
            return None

        system_msg, compressible, preserved_context, preserved_user_turns = (
            self._split_context_for_compression(
                messages,
                preserve_recent_user_turns=preserve_recent_user_turns,
            )
        )
        if not compressible:
            return None

        self.logger.log_trace_event(
            "CONTEXT_COMPRESSION_START",
            {
                "trigger": trigger,
                "before": before,
                "compressible_messages": len(compressible),
                "preserved_context_messages": len(preserved_context),
                "preserved_recent_user_turns": preserved_user_turns,
            },
        )
        try:
            summary = await self._context_compressor.compress(
                messages=compressible,
                current_usage=before,
                graph_summary=self._graph_context_summary(),
            )
        except Exception:
            self.logger.log_trace_event(
                "CONTEXT_COMPRESSION_ERROR",
                {"trigger": trigger, "error": traceback.format_exc()},
            )
            summary = self._fallback_context_summary(compressible)
        summary = summary.strip() or self._fallback_context_summary(compressible)
        # Inject as role=user so the first non-system message is always a user
        # turn, satisfying the API contract for both OpenAI and Anthropic.
        summary_msg = {
            "role": "user",
            "content": (
                "[CONTEXT_SUMMARY - 历史上下文摘要，非用户实际输入]\n"
                "以下是本会话较早上下文的压缩摘要，请据此继续作答：\n\n"
                f"{summary}"
            ),
        }
        self.context_manager.update_messages([system_msg, summary_msg, *preserved_context])
        self._context_compression_count += 1
        after = self.context_usage()
        stats = {
            "trigger": trigger,
            "before_tokens": before.get("current_tokens"),
            "after_tokens": after.get("current_tokens"),
            "token_limit": self._context_token_limit,
            "compression_count": self._context_compression_count,
            "preserved_context_messages": len(preserved_context),
            "preserved_recent_user_turns": preserved_user_turns,
        }
        if hasattr(self.logger, "log_context_compaction_async"):
            await self.logger.log_context_compaction_async(
                summary_msg,
                stats=stats,
                preserve_recent_user_turns=preserve_recent_user_turns,
            )
        elif hasattr(self.logger, "log_context_compaction"):
            self.logger.log_context_compaction(
                summary_msg,
                stats=stats,
                preserve_recent_user_turns=preserve_recent_user_turns,
            )
        else:
            self.logger.log_context_message(
                summary_msg,
                visible=False,
                kind="context_compression",
                include_in_context=True,
            )
        self.logger.log_trace_event("CONTEXT_COMPRESSION_DONE", stats)
        return {"before": before, "after": after, "stats": stats}

    async def _run_react_loop(
        self,
        msgs: List[dict],
        *,
        user_input: str,
        max_steps: int,
        state: dict,
    ):
        answered = False
        final_answer = ""
        turn_tool_observations: List[dict] = []
        turn_thinking_blocks: List[dict] = []
        turn_progress_items: List[dict] = []

        for step in range(1, max_steps + 1):
            memory_prompt = (
                self._memory_prompt_block
                if self._memory_manager is not None
                else ""
            )
            active_tool_specs = self._active_tool_specs()
            model_msgs = self.context_manager.build_model_messages(
                active_tool_specs,
                memory_prompt=memory_prompt,
                query=user_input,
                visible_tool_specs=self._visible_tool_specs,
                graph_state=self._graph_info_payload(),
            )
            self.logger.log_model_request(step, model_msgs, active_tool_specs)
            self.logger.log_context_snapshot(f"MODEL_INPUT_STEP_{step}", model_msgs)

            turn_result = None
            parse_error = ""
            retry_notice = ""
            for attempt in range(1, _MAX_PROTOCOL_RETRIES + 2):
                yield {"type": "model_turn_start", "step": step, "attempt": attempt}
                turn_result = None
                attempt_msgs = model_msgs
                if retry_notice:
                    attempt_msgs = list(model_msgs)
                    insert_at = 0
                    while (
                        insert_at < len(attempt_msgs)
                        and attempt_msgs[insert_at].get("role") == "system"
                    ):
                        insert_at += 1
                    attempt_msgs.insert(insert_at, {"role": "system", "content": retry_notice})
                async for event in self.context_manager.stream_turn(
                    attempt_msgs, active_tool_specs, step=step
                ):
                    if event["type"] == "turn_end":
                        turn_result = event
                        break
                    yield event

                if turn_result is None:
                    break

                raw_text = turn_result.get("raw_text", "")
                self.logger.log_model_response_raw(step, raw_text)
                parse_error = str(turn_result.get("parse_error") or "").strip()
                if not parse_error and not raw_text.strip() and not turn_result.get("tool_calls"):
                    parse_error = "empty_response"
                if (
                    not parse_error
                    and turn_result.get("action") == "answer"
                    and not turn_result.get("answer_text", "").strip()
                    and not turn_result.get("tool_calls")
                ):
                    parse_error = "no_answer_content"
                if not parse_error:
                    break
                retry_notice = (
                    "上一轮模型返回了空响应，请重新生成本轮输出。"
                    if parse_error == "empty_response"
                    else (
                        "上一轮输出只有 <think> 推理内容但缺少 <answer> 正文，用户无法看到回复。"
                        "请将你的结论和回复放入 <answer>...</answer> 标签中输出。"
                    )
                    if parse_error == "no_answer_content"
                    else (
                        "上一轮输出无法解析为规定协议格式。请重新生成本轮输出，且只能使用以下二选一格式："
                        "`<think>...</think><tool_call>{...}</tool_call>` 或 "
                        "`<think>...</think><answer>...</answer>`。"
                        "不要在 `<answer>` 正文后继续输出 `<tool_call>`，不要输出标签外正文。"
                    )
                )
                if attempt <= _MAX_PROTOCOL_RETRIES:
                    yield {
                        "type": "model_turn_retry",
                        "step": step,
                        "attempt": attempt,
                        "reason": parse_error,
                    }
                    continue
                yield {
                    "type": "model_turn_retry",
                    "step": step,
                    "attempt": attempt,
                    "reason": parse_error,
                }
                final_answer = "模型连续输出了无法解析的协议格式，请稍后重试。"
                yield {"type": "token", "content": final_answer}
                answered = True
                break

            if turn_result is None or (parse_error and answered):
                break

            action = turn_result.get("action")
            tool_calls = turn_result.get("tool_calls", [])
            final_answer = turn_result.get("answer_text", "").strip()
            think_text = turn_result.get("think_text", "")
            if isinstance(think_text, str) and think_text.strip():
                turn_thinking_blocks.append({"phase": "thinking", "text": think_text})

            self.logger.log_protocol_parse(
                step=step,
                think=think_text,
                tool_calls=tool_calls,
                final_answer=final_answer,
            )

            if self._protocol == "native" and action == "tool_calls":
                openai_tool_calls = turn_result.get("openai_tool_calls") or []
                assistant_msg = {
                    "role": "assistant",
                    "content": think_text or None,
                    "tool_calls": openai_tool_calls,
                }
            else:
                assistant_content = raw_text or (
                    f"<think>{think_text}</think><answer>{final_answer}</answer>"
                    if action == "answer"
                    else f"<think>{think_text}</think>"
                )
                assistant_msg = {"role": "assistant", "content": assistant_content}

            msgs.append(assistant_msg)
            self.logger.log_context_message(
                assistant_msg,
                visible=False,
                kind="assistant_protocol",
                include_in_context=True,
            )

            if action == "tool_calls" and tool_calls:
                batch_observations: List[dict] = []
                if not self._should_run_batch_serially(tool_calls):
                    async for batch_event in self._run_tool_calls_parallel(tool_calls):
                        if batch_event.get("type") == "_batch_done":
                            batch_observations.extend(batch_event.get("observations") or [])
                        else:
                            if batch_event.get("type") == "tool_progress":
                                turn_progress_items.append(batch_event)
                            yield batch_event
                    if batch_observations:
                        turn_tool_observations.extend(batch_observations)
                        obs_msgs = self.context_manager.build_tool_observation(batch_observations)
                        for obs_msg in obs_msgs:
                            msgs.append(obs_msg)
                            self.logger.log_context_message(
                                obs_msg,
                                visible=False,
                                kind="tool_observation",
                                include_in_context=True,
                            )
                    continue

                for tc in tool_calls:
                    args = tc.get("arguments", {})
                    call_id = tc.get("id")
                    self.logger.log_tool_call(tc["name"], args)
                    yield {
                        "type": "tool_call",
                        "tool": tc["name"],
                        "args": args,
                        "call_id": call_id,
                        "server": tc.get("server", "graph"),
                    }
                    progress_queue: asyncio.Queue = asyncio.Queue()
                    result_task = asyncio.create_task(
                        self._run_tool(
                            tc,
                            progress_callback=lambda payload, q=progress_queue: q.put_nowait(payload),
                        )
                    )
                    async for progress_event in self._progress_events_until_done(
                        result_task,
                        progress_queue,
                        tool=tc["name"],
                        call_id=str(call_id or ""),
                    ):
                        turn_progress_items.append(progress_event)
                        yield progress_event
                    result_str = await result_task
                    if (
                        self._pending_feedback is not None
                        and str(self._pending_feedback.get("call_id") or "") == str(call_id or "")
                    ):
                        if batch_observations:
                            obs_msgs = self.context_manager.build_tool_observation(batch_observations)
                            for obs_msg in obs_msgs:
                                msgs.append(obs_msg)
                                self.logger.log_context_message(
                                    obs_msg,
                                    visible=False,
                                    kind="tool_observation",
                                    include_in_context=True,
                                )
                            turn_tool_observations.extend(batch_observations)
                        self._paused_react_state = {
                            "origin": "react",
                            "user_input": user_input,
                            "max_steps": max_steps,
                        }
                        yield {
                            "type": "await_user_feedback",
                            "request": self.pending_feedback(),
                        }
                        state.update(
                            {
                                "answered": True,
                                "final_answer": "",
                                "tool_observations": turn_tool_observations,
                                "thinking_blocks": turn_thinking_blocks,
                                "paused_for_feedback": True,
                            }
                        )
                        return
                    self.logger.log_tool_result(tc["name"], result_str)
                    extra_events, context_result = self._prepare_tool_result_for_observation(
                        tc["name"],
                        result_str,
                        str(call_id or ""),
                    )
                    for extra_event in extra_events:
                        yield extra_event
                    batch_observations.append(
                        {
                            "call_id": call_id,
                            "server": tc.get("server", "graph"),
                            "name": tc["name"],
                            "arguments": args,
                            "result": context_result,
                        }
                    )
                    yield {"type": "tool_result", "tool": tc["name"], "call_id": call_id}

                if batch_observations:
                    turn_tool_observations.extend(batch_observations)
                    obs_msgs = self.context_manager.build_tool_observation(batch_observations)
                    for obs_msg in obs_msgs:
                        msgs.append(obs_msg)
                        self.logger.log_context_message(
                            obs_msg,
                            visible=False,
                            kind="tool_observation",
                            include_in_context=True,
                        )
                continue

            if not final_answer:
                final_answer = "已完成分析，但当前回合未生成可展示内容。"
                yield {"type": "token", "content": final_answer}
            answered = True
            break

        if not answered:
            final_answer = "已达到最大分析步数上限，请尝试提问更具体的问题或分步骤进行分析。"
            yield {"type": "token", "content": final_answer}

        state.update(
            {
                "answered": answered,
                "final_answer": final_answer,
                "tool_observations": turn_tool_observations,
                "thinking_blocks": turn_thinking_blocks,
                "progress_items": turn_progress_items,
            }
        )

    # ── Plan Solver（隔离的规划求解循环） ────────────────────────

    async def _run_plan_solver(
        self,
        plan: AnalysisPlan,
        msgs: List[dict],
        *,
        start_step_id: int | None = None,
    ):
        """
        逐步执行规划中的每个 step。

        与主 react loop 的区别：
        - 事件类型加 solve_ 前缀（前端渲染到"求解框"而非主聊天）
        - 共享 msgs 对话链（前序步骤的工具结果对后续可见）
        - 不影响主 Agent 的 state

        Yields:
            solve_step_start / solve_token / solve_thinking / solve_thinking_end
            solve_tool_call / solve_tool_result / solve_step_done
        """
        _SOLVER_MAX_TURNS = 4

        for plan_step in plan.steps:
            if start_step_id is not None and plan_step.step_id < start_step_id:
                continue
            # 依赖检查
            if any(
                dep.status != "done"
                for dep in plan.steps
                if dep.step_id in plan_step.depends_on
            ):
                plan_step.status = "skipped"
                yield {
                    "type": "solve_step_done",
                    "step_id": plan_step.step_id,
                    "status": "skipped",
                    "summary": "依赖步骤未完成",
                }
                continue

            plan_step.status = "running"
            self._persist_plan(plan)
            yield {
                "type": "solve_step_start",
                "step_id": plan_step.step_id,
                "goal": plan_step.goal,
            }

            step_prompt = (
                f"[计划步骤 {plan_step.step_id}/{len(plan.steps)}]\n"
                f"目标: {plan_step.goal}\n"
                f"候选工具: {', '.join(plan_step.candidate_tools) or '全部'}\n"
                "请仅完成当前步骤，并在必要时调用工具。"
            )
            user_msg = {"role": "user", "content": step_prompt}
            msgs.append(user_msg)
            self.logger.log_context_message(
                user_msg, visible=False, kind="plan_step", include_in_context=True,
            )

            step_answer = ""
            step_answered = False

            for turn in range(1, _SOLVER_MAX_TURNS + 1):
                streamed_thinking_chunks: list[str] = []
                memory_prompt = (
                    self._memory_prompt_block
                    if self._memory_manager is not None
                    else ""
                )
                active_tool_specs = self._active_tool_specs()
                model_msgs = self.context_manager.build_model_messages(
                    active_tool_specs,
                    memory_prompt=memory_prompt,
                    query=step_prompt,
                    visible_tool_specs=self._visible_tool_specs,
                    graph_state=self._graph_info_payload(),
                )

                turn_result = None
                async for event in self.context_manager.stream_turn(
                    model_msgs, active_tool_specs, step=900 + plan_step.step_id * 10 + turn,
                ):
                    etype = event.get("type", "")
                    if etype == "turn_end":
                        turn_result = event
                        break
                    # 将内部事件映射为 solve_ 前缀事件
                    if etype == "thinking_start":
                        yield {"type": "solve_thinking", "step_id": plan_step.step_id, "phase": "start"}
                    elif etype == "thinking":
                        chunk = str(event.get("content") or "")
                        if chunk:
                            streamed_thinking_chunks.append(chunk)
                            plan_step.thinking = f"{plan_step.thinking}{chunk}"
                        yield {"type": "solve_thinking", "step_id": plan_step.step_id, "phase": "content", "content": chunk}
                    elif etype == "thinking_end":
                        yield {"type": "solve_thinking", "step_id": plan_step.step_id, "phase": "end"}
                    elif etype == "token":
                        yield {"type": "solve_token", "step_id": plan_step.step_id, "content": event.get("content", "")}

                if turn_result is None:
                    break

                raw_text = turn_result.get("raw_text", "")
                action = turn_result.get("action")
                tool_calls = turn_result.get("tool_calls", [])
                step_answer = turn_result.get("answer_text", "").strip()
                think_text = turn_result.get("think_text", "")
                if think_text and not streamed_thinking_chunks:
                    plan_step.thinking = f"{plan_step.thinking}{think_text}"

                # 构造 assistant 消息写回共享对话链
                if self._protocol == "native" and action == "tool_calls":
                    openai_tool_calls = turn_result.get("openai_tool_calls") or []
                    assistant_msg = {
                        "role": "assistant",
                        "content": think_text or None,
                        "tool_calls": openai_tool_calls,
                    }
                else:
                    assistant_content = raw_text or (
                        f"<think>{think_text}</think><answer>{step_answer}</answer>"
                        if action == "answer"
                        else f"<think>{think_text}</think>"
                    )
                    assistant_msg = {"role": "assistant", "content": assistant_content}
                msgs.append(assistant_msg)

                if action == "tool_calls" and tool_calls:
                    batch_obs: List[dict] = []
                    for tc in tool_calls:
                        args = tc.get("arguments", {})
                        call_id = tc.get("id")
                        self.logger.log_tool_call(tc["name"], args)
                        yield {
                            "type": "solve_tool_call",
                            "step_id": plan_step.step_id,
                            "tool": tc["name"],
                            "args": args,
                            "call_id": call_id,
                        }
                        result_str = await self._run_tool(tc, step_id=plan_step.step_id)
                        if (
                            self._pending_feedback is not None
                            and str(self._pending_feedback.get("call_id") or "") == str(call_id or "")
                        ):
                            if batch_obs:
                                for obs_msg in self.context_manager.build_tool_observation(batch_obs):
                                    msgs.append(obs_msg)
                                    self.logger.log_context_message(
                                        obs_msg,
                                        visible=False,
                                        kind="tool_observation",
                                        include_in_context=True,
                                    )
                            plan_step.status = "waiting_user"
                            plan_step.result_summary = str(self._pending_feedback.get("message") or "")
                            self._paused_plan_state = {
                                "origin": "plan",
                                "plan": plan,
                                "step_id": plan_step.step_id,
                                "pending_tool_call": self._clone_data(tc),
                            }
                            self._persist_plan(plan)
                            yield {
                                "type": "await_user_feedback",
                                "request": self.pending_feedback(),
                            }
                            yield {
                                "type": "solve_step_done",
                                "step_id": plan_step.step_id,
                                "status": plan_step.status,
                                "summary": plan_step.result_summary,
                            }
                            return
                        self.logger.log_tool_result(tc["name"], result_str)
                        extra_events, context_result = self._prepare_tool_result_for_observation(
                            tc["name"],
                            result_str,
                            str(call_id or ""),
                        )
                        for extra_event in extra_events:
                            yield extra_event
                        batch_obs.append({
                            "call_id": call_id,
                            "server": tc.get("server", "graph"),
                            "name": tc["name"],
                            "arguments": args,
                            "result": context_result,
                        })
                        yield {
                            "type": "solve_tool_result",
                            "step_id": plan_step.step_id,
                            "tool": tc["name"],
                            "call_id": call_id,
                        }
                    # 工具结果写回共享对话链
                    for obs_msg in self.context_manager.build_tool_observation(batch_obs):
                        msgs.append(obs_msg)
                        self.logger.log_context_message(
                            obs_msg,
                            visible=False,
                            kind="tool_observation",
                            include_in_context=True,
                        )
                    continue

                if not step_answer:
                    step_answer = "步骤已执行但未生成明确结论。"
                step_answered = True
                break

            plan_step.status = "done" if step_answered else "failed"
            plan_step.result_summary = step_answer[:500]
            self._persist_plan(plan)
            yield {
                "type": "solve_step_done",
                "step_id": plan_step.step_id,
                "status": plan_step.status,
                "summary": plan_step.result_summary,
            }

    # ── Plan flow ────────────────────────────────────────────────
    @staticmethod
    def _build_plan_summary_prompt(plan: AnalysisPlan) -> str:
        """构造规划结果摘要，注入主 Agent 上下文供反思/总结。"""
        lines = ["以下是针对你的问题所执行的分析计划及其结果：\n"]
        lines.append(f"原始问题: {plan.original_query}\n")
        for step in plan.steps:
            lines.append(f"步骤 {step.step_id}（{step.status}）: {step.goal}")
            if step.result_summary:
                lines.append(f"  结果: {step.result_summary}")
        lines.append("\n请基于以上分析结果，对原始问题给出完整、准确的最终答复。")
        return "\n".join(lines)

    @staticmethod
    def _planner_context_messages(messages: List[dict]) -> List[dict]:
        return [
            dict(msg)
            for msg in messages
            if isinstance(msg, dict) and str(msg.get("role") or "") != "system"
        ]

    def _persist_plan(self, plan: AnalysisPlan) -> None:
        if hasattr(self.logger, "log_trace_event"):
            self.logger.log_trace_event("PLAN_STATE", plan.to_dict())
        store = getattr(self.logger, "_store", None)
        if store is not None and hasattr(store, "save_plan"):
            try:
                store.save_plan(self.session_id, plan.plan_id, plan.to_dict())
            except Exception:
                pass

    @staticmethod
    def _build_visible_solver_steps(plan: Optional[AnalysisPlan]) -> List[dict]:
        if plan is None:
            return []
        rows: List[dict] = []
        for step in plan.steps:
            row = {
                "step_id": step.step_id,
                "goal": step.goal,
                "status": step.status,
                "tokens": step.result_summary,
                "tools": list(step.tools) if isinstance(getattr(step, "tools", None), list) else [],
                "thinking": str(step.thinking or ""),
            }
            progress_items = getattr(step, "progress_items", None)
            if isinstance(progress_items, list) and progress_items:
                row["progressItems"] = list(progress_items)
            rows.append(row)
        return rows

    @staticmethod
    def _build_feedback_response_text(
        request: dict[str, Any],
        *,
        decision: str,
        patched_args: dict[str, Any] | None = None,
        comment: str = "",
    ) -> str:
        tool_name = str(request.get("proposed_tool") or "当前操作")
        if decision == "approve":
            return f"确认执行 {tool_name}。"
        if decision == "modify":
            return (
                f"我已修改 {tool_name} 的参数，请按新参数继续。"
                + (f"\n备注：{comment}" if comment else "")
                + (f"\n新参数：{json.dumps(patched_args or {}, ensure_ascii=False)}" if patched_args else "")
            )
        return f"我拒绝执行 {tool_name}。" + (f"\n原因：{comment}" if comment else "")

    @staticmethod
    def _find_plan_step(plan: AnalysisPlan, step_id: int | None) -> Any:
        if step_id is None:
            return None
        for step in plan.steps:
            if step.step_id == step_id:
                return step
        return None

    async def _execute_plan_flow(
        self,
        *,
        plan: AnalysisPlan,
        msgs: List[dict],
        run_state: dict,
        start_step_id: int | None = None,
        resume_step_messages: Optional[List[dict]] = None,
    ):
        async for event in self._plan_solver.run(
            plan,
            msgs,
            persist_plan=self._persist_plan,
            start_step_id=start_step_id,
            resume_step_messages=resume_step_messages,
        ):
            yield event

        if self._paused_plan_state is not None:
            run_state["paused_for_feedback"] = True
            return

        yield {"type": "plan_complete", "plan_id": plan.plan_id}
        summary_prompt = self._build_plan_summary_prompt(plan)
        summary_msg = {"role": "user", "content": summary_prompt}
        msgs.append(summary_msg)
        self.logger.log_context_message(
            summary_msg, visible=False, kind="plan_summary", include_in_context=True,
        )
        async for event in self._run_react_loop(
            msgs, user_input=summary_prompt, max_steps=4, state=run_state,
        ):
            yield event

    async def resume_feedback_stream(
        self,
        *,
        request_id: str,
        decision: str,
        patched_args: Optional[dict[str, Any]] = None,
        comment: str = "",
    ):
        self.last_active = time.monotonic()
        async with self._turn_lock:
            async for event in self._resume_feedback_stream_impl(
                request_id=request_id,
                decision=decision,
                patched_args=patched_args,
                comment=comment,
            ):
                yield event

    async def _resume_feedback_stream_impl(
        self,
        *,
        request_id: str,
        decision: str,
        patched_args: Optional[dict[str, Any]] = None,
        comment: str = "",
    ):
        await self._ensure_connected()
        self._visible_graphs_for_turn = []
        self._visible_reports_for_turn = []

        request = self._pending_feedback
        if request is None:
            yield {"type": "error", "error": "当前没有待处理的用户反馈请求。"}
            return
        if str(request.get("request_id") or "") != str(request_id or "").strip():
            yield {"type": "error", "error": "反馈请求已失效或与当前待处理请求不匹配。"}
            return
        paused_plan = self._paused_plan_state
        paused_react = self._paused_react_state
        if paused_plan is None and paused_react is None:
            yield {"type": "error", "error": "当前待处理反馈不支持恢复执行。"}
            return

        decision_norm = str(decision or "").strip().lower()
        if decision_norm not in {"approve", "modify", "reject"}:
            yield {"type": "error", "error": "不支持的反馈决策。"}
            return
        if patched_args is not None and not isinstance(patched_args, dict):
            yield {"type": "error", "error": "patched_args 必须是对象。"}
            return

        request["status"] = decision_norm
        if comment:
            request["comment"] = comment
        if patched_args:
            request["patched_args"] = self._clone_data(patched_args)
        resolved_feedback_request = self._clone_data(request)

        response_text = self._build_feedback_response_text(
            request,
            decision=decision_norm,
            patched_args=patched_args,
            comment=comment,
        )
        feedback_user_content = build_user_turn_content(response_text, self.reasoning_mode)
        feedback_user_msg = {"role": "user", "content": feedback_user_content}
        self.context_manager.messages.append(feedback_user_msg)
        self.logger.log_context_message(
            feedback_user_msg,
            visible=False,
            display_content=response_text,
            kind="user_feedback",
            include_in_context=True,
        )
        self.logger.log_trace_event(
            "USER_FEEDBACK_RECEIVED",
            {
                "request_id": request_id,
                "decision": decision_norm,
                "patched_args": patched_args or {},
                "comment": comment,
            },
        )
        self._pending_feedback = None
        self._paused_plan_state = None
        self._paused_react_state = None

        run_state: dict = {}
        completed_plan: Optional[AnalysisPlan] = None
        yield {
            "type": "feedback_resolved",
            "request": resolved_feedback_request,
        }
        if paused_plan is not None:
            plan = paused_plan["plan"]
            completed_plan = plan
            step_id = paused_plan.get("step_id")
            plan_step = self._find_plan_step(plan, step_id)
            if plan_step is None:
                yield {"type": "error", "error": "待恢复的计划步骤不存在。"}
                return

            if decision_norm == "reject":
                plan_step.status = "failed"
                plan_step.result_summary = comment or "用户拒绝执行高风险工具。"
                self._persist_plan(plan)
                yield {
                    "type": "solve_step_done",
                    "step_id": plan_step.step_id,
                    "status": plan_step.status,
                    "summary": plan_step.result_summary,
                }
                async for event in self._execute_plan_flow(
                    plan=plan,
                    msgs=self.context_manager.messages,
                    run_state=run_state,
                    start_step_id=plan_step.step_id + 1,
                ):
                    yield event
            else:
                step_messages = [
                    dict(msg)
                    for msg in paused_plan.get("step_messages", [])
                    if isinstance(msg, dict)
                ]
                if not step_messages:
                    step_messages = self._plan_solver.build_step_messages(
                        plan,
                        plan_step,
                        [
                            dict(msg)
                            for msg in self.context_manager.messages
                            if isinstance(msg, dict)
                            and str(msg.get("role") or "") == "user"
                            and str(msg.get("content") or "").lstrip().startswith("[PLAN_STEP_RESULT")
                        ],
                    )
                resumed_args = self._merge_feedback_args(
                    request.get("proposed_args") or {},
                    patched_args if decision_norm == "modify" else {},
                )
                pending_tool_call = self._clone_data(paused_plan.get("pending_tool_call") or {})
                call_id = str(request.get("call_id") or pending_tool_call.get("id") or uuid.uuid4().hex)
                tool_name = str(request.get("proposed_tool") or pending_tool_call.get("name") or "").strip()
                plan_step.status = "running"
                plan_step.result_summary = ""
                self._persist_plan(plan)
                yield {
                    "type": "solve_step_start",
                    "step_id": plan_step.step_id,
                    "goal": plan_step.goal,
                }
                try:
                    progress_queue: asyncio.Queue = asyncio.Queue()
                    result_task = asyncio.create_task(
                        self._execute_tool_call(
                            name=tool_name,
                            args=resumed_args,
                            call_id=call_id,
                            progress_callback=lambda payload, q=progress_queue: q.put_nowait(payload),
                        )
                    )
                    async for progress_event in self._progress_events_until_done(
                        result_task,
                        progress_queue,
                        tool=tool_name,
                        call_id=call_id,
                        step_id=plan_step.step_id,
                    ):
                        yield progress_event
                    result_str = await result_task
                except Exception as e:
                    self.logger.log_error(traceback.format_exc())
                    plan_step.status = "failed"
                    plan_step.result_summary = f"恢复执行 {tool_name} 失败：{e}"
                    self._persist_plan(plan)
                    yield {
                        "type": "solve_step_done",
                        "step_id": plan_step.step_id,
                        "status": plan_step.status,
                        "summary": plan_step.result_summary,
                    }
                    yield {"type": "error", "error": str(e)}
                    return

                self.logger.log_tool_result(tool_name, result_str)
                extra_events, context_result = self._prepare_tool_result_for_observation(
                    tool_name,
                    result_str,
                    call_id,
                )
                for extra_event in extra_events:
                    yield extra_event
                yield {
                    "type": "solve_tool_result",
                    "step_id": plan_step.step_id,
                    "tool": tool_name,
                    "call_id": call_id,
                }
                obs = {
                    "call_id": call_id,
                    "server": pending_tool_call.get("server", "graph"),
                    "name": tool_name,
                    "arguments": resumed_args,
                    "result": context_result,
                }
                for obs_msg in self.context_manager.build_tool_observation([obs]):
                    step_messages.append(obs_msg)
                    self.logger.log_context_message(
                        obs_msg,
                        visible=False,
                        kind="plan_step_intermediate",
                        include_in_context=False,
                    )

                plan_step.status = "pending"
                plan_step.result_summary = ""
                self._persist_plan(plan)
                async for event in self._execute_plan_flow(
                    plan=plan,
                    msgs=self.context_manager.messages,
                    run_state=run_state,
                    start_step_id=plan_step.step_id,
                    resume_step_messages=step_messages,
                ):
                    yield event
        else:
            react_state = paused_react or {}
            if decision_norm == "reject":
                run_state["answered"] = True
                run_state["final_answer"] = comment or "用户拒绝执行该高风险操作，因此本轮不继续调用相关工具。"
            else:
                resumed_args = self._merge_feedback_args(
                    request.get("proposed_args") or {},
                    patched_args if decision_norm == "modify" else {},
                )
                tool_name = str(request.get("proposed_tool") or "").strip()
                call_id = str(request.get("call_id") or uuid.uuid4().hex)
                try:
                    progress_queue: asyncio.Queue = asyncio.Queue()
                    result_task = asyncio.create_task(
                        self._execute_tool_call(
                            name=tool_name,
                            args=resumed_args,
                            call_id=call_id,
                            progress_callback=lambda payload, q=progress_queue: q.put_nowait(payload),
                        )
                    )
                    async for progress_event in self._progress_events_until_done(
                        result_task,
                        progress_queue,
                        tool=tool_name,
                        call_id=call_id,
                    ):
                        yield progress_event
                    result_str = await result_task
                except Exception as e:
                    self.logger.log_error(traceback.format_exc())
                    yield {"type": "error", "error": str(e)}
                    return
                self.logger.log_tool_result(tool_name, result_str)
                extra_events, context_result = self._prepare_tool_result_for_observation(
                    tool_name,
                    result_str,
                    call_id,
                )
                for extra_event in extra_events:
                    yield extra_event
                yield {"type": "tool_result", "tool": tool_name, "call_id": call_id}
                obs = {
                    "call_id": call_id,
                    "server": "graph",
                    "name": tool_name,
                    "arguments": resumed_args,
                    "result": context_result,
                }
                for obs_msg in self.context_manager.build_tool_observation([obs]):
                    self.context_manager.messages.append(obs_msg)
                    self.logger.log_context_message(
                        obs_msg,
                        visible=False,
                        kind="tool_observation",
                        include_in_context=True,
                    )
                async for event in self._run_react_loop(
                    self.context_manager.messages,
                    user_input=str(react_state.get("user_input") or ""),
                    max_steps=int(react_state.get("max_steps") or _MAX_STEPS),
                    state=run_state,
                ):
                    yield event

        final_answer = str(run_state.get("final_answer") or "")
        self.logger.log_assistant_message(
            final_answer,
            tools=self._build_visible_tools(list(run_state.get("tool_observations", []))),
            thinking=self._build_visible_thinking(list(run_state.get("thinking_blocks", []))),
            plan=completed_plan.to_dict() if completed_plan is not None else None,
            solver_steps=self._build_visible_solver_steps(completed_plan),
            progress_items=list(run_state.get("progress_items", [])),
            feedback_request=resolved_feedback_request if isinstance(resolved_feedback_request, dict) else None,
            graphs=list(self._visible_graphs_for_turn),
            reports=list(self._visible_reports_for_turn),
        )
        self.context_manager.update_messages(self.context_manager.messages)
        yield {
            "type": "done",
            "has_graph": self.has_graph(),
            "context_usage": self.context_usage(),
        }

    # ── 对话接口 ──────────────────────────────────────────────────

    def chat(self, user_input: str) -> str:
        """同步对话，仅用于 CLI（main.py）场景。"""
        return asyncio.run(self._collect_chat(user_input))

    async def _collect_chat(self, user_input: str) -> str:
        tokens: list[str] = []
        async for event in self.chat_stream(user_input):
            if event["type"] == "token":
                tokens.append(event["content"])
        return "".join(tokens)

    async def chat_stream(self, user_input: str):
        """
        流式对话：异步生成器，yield 事件字典。
        直接在调用方的事件循环中运行（FastAPI/uvicorn 单一事件循环）。
        """
        self.last_active = time.monotonic()
        async with self._turn_lock:
            async for event in self._chat_stream_impl(user_input):
                yield event

    async def _chat_stream_impl(self, user_input: str):
        """实际执行聊天循环。"""
        await self._ensure_connected()
        self._visible_graphs_for_turn = []
        self._visible_reports_for_turn = []

        # Mark new user turn so selected tools and selector cache reset together.
        self._turn_selected_tool_names.clear()
        if self._tool_selector is not None:
            self._tool_selector.set_turn_id(uuid.uuid4().hex[:12])

        msgs = self.context_manager.messages
        turn_start_idx = len(msgs)
        user_content = build_user_turn_content(user_input, self.reasoning_mode)
        user_msg = {"role": "user", "content": user_content}
        msgs.append(user_msg)
        self.logger.log_context_message(
            user_msg,
            visible=True,
            display_content=user_input,
            kind="user_turn",
            include_in_context=True,
        )
        yield {"type": "context_usage", "usage": self.context_usage()}
        current_usage = self.context_usage()
        trigger_pct = _env_int("CONTEXT_COMPRESSION_TRIGGER_PCT", 90)
        compression_threshold = int(self._context_token_limit * trigger_pct / 100)
        if int(current_usage.get("current_tokens") or 0) >= compression_threshold:
            yield {
                "type": "context_compressing",
                "usage": current_usage,
                "threshold": compression_threshold,
            }
        compression = await self._maybe_compress_context(
            trigger="chat_turn_start",
            preserve_recent_user_turns=_env_int(
                "CONTEXT_COMPRESSION_PRESERVE_RECENT_USER_TURNS",
                _DEFAULT_PRESERVE_RECENT_USER_TURNS,
            ),
        )
        if compression is not None:
            yield {
                "type": "context_compressed",
                "before": compression["before"],
                "after": compression["after"],
                "stats": compression["stats"],
            }
            yield {"type": "context_usage", "usage": compression["after"]}
            after_tokens = int(compression["after"].get("current_tokens") or 0)
            if after_tokens >= self._context_token_limit:
                yield {
                    "type": "error",
                    "error": (
                        "当前消息内容过长，压缩历史上下文后仍超出模型限制，"
                        "请缩短输入后重试。"
                    ),
                }
                return
            msgs = self.context_manager.messages
            turn_start_idx = max(0, len(msgs) - 1)

        try:
            run_state: dict = {}
            should_plan = False
            plan_reason = ""
            completed_plan: Optional[AnalysisPlan] = None
            if self.planning_mode == "always":
                should_plan = True
                plan_reason = "planning_mode=always"
            elif self.planning_mode == "never":
                should_plan = False
                plan_reason = "planning_mode=never"
            elif self._planner is not None:
                should_plan, plan_reason = await self._query_analyzer.should_plan(
                    user_input,
                    self._graph_context_summary(),
                    [spec["name"] for spec in self._visible_tool_specs],
                )
            self.logger.log_trace_event(
                "PLAN_DECISION",
                {"should_plan": should_plan, "reason": plan_reason,
                 "planning_mode": self.planning_mode, "query": user_input},
            )

            if should_plan and self._planner is not None:
                # ① 通知前端正在分析
                yield {"type": "plan_analyzing"}
                planner_memory_prompt = (
                    self._memory_prompt_block
                    if self._memory_manager is not None
                    else ""
                )
                plan: Optional[AnalysisPlan] = None
                async for plan_event in self._planner.stream_plan(
                    user_input,
                    self._graph_info_payload(),
                    memory_prompt=planner_memory_prompt,
                    conversation_messages=self._planner_context_messages(msgs),
                ):
                    if plan_event.get("type") == "plan_step":
                        yield {
                            "type": "plan_step",
                            "plan_id": plan_event.get("plan_id"),
                            "step": plan_event.get("step"),
                        }
                        continue
                    if plan_event.get("type") == "plan_ready":
                        plan = plan_event.get("plan")
                        break
                if plan is None:
                    plan = self._planner._fallback_plan(user_input)

                # ② 发送计划完成信号到前端规划框
                self._persist_plan(plan)
                yield {"type": "plan_ready", "plan_id": plan.plan_id, "steps": [s.__dict__ for s in plan.steps]}

                completed_plan = plan
                # ③ PlanSolver 逐步执行（事件输出到前端求解框）并在完成后生成总结
                async for event in self._execute_plan_flow(
                    plan=plan,
                    msgs=msgs,
                    run_state=run_state,
                ):
                    yield event
                final_answer_for_memory = (
                    ""
                    if run_state.get("paused_for_feedback")
                    else str(run_state.get("final_answer") or "")
                )
            else:
                async for event in self._run_react_loop(
                    msgs,
                    user_input=user_input,
                    max_steps=_MAX_STEPS,
                    state=run_state,
                ):
                    yield event
                final_answer_for_memory = str(run_state.get("final_answer") or "")

            turn_tool_observations = list(run_state.get("tool_observations", []))
            turn_thinking_blocks = list(run_state.get("thinking_blocks", []))

            self.logger.log_assistant_message(
                final_answer_for_memory,
                tools=self._build_visible_tools(turn_tool_observations),
                thinking=self._build_visible_thinking(turn_thinking_blocks),
                plan=completed_plan.to_dict() if completed_plan is not None else None,
                solver_steps=self._build_visible_solver_steps(completed_plan),
                feedback_request=self.pending_feedback(),
                progress_items=list(run_state.get("progress_items", [])),
                graphs=list(self._visible_graphs_for_turn),
                reports=list(self._visible_reports_for_turn),
            )

            turn_messages_for_memory = [
                dict(msg) for msg in msgs[turn_start_idx:] if isinstance(msg, dict)
            ]
            if final_answer_for_memory:
                has_final = any(
                    str(msg.get("role") or "") == "assistant"
                    and final_answer_for_memory in str(msg.get("content") or "")
                    for msg in turn_messages_for_memory
                )
                if not has_final:
                    turn_messages_for_memory.append(
                        {"role": "assistant", "content": final_answer_for_memory}
                    )

            # ── 记忆更新（后台任务，不阻塞主对话） ──
            if not run_state.get("paused_for_feedback"):
                self._schedule_memory_update(
                    user_input=user_input,
                    assistant_output=final_answer_for_memory,
                    tool_observations=turn_tool_observations,
                    turn_messages=turn_messages_for_memory,
                )

            self.context_manager.update_messages(msgs)

        except Exception as e:
            self.logger.log_error(traceback.format_exc())
            yield {"type": "error", "error": str(e)}
            return

        yield {
            "type": "done",
            "has_graph": self.has_graph(),
            "context_usage": self.context_usage(),
        }

    # ── 状态查询 / 清理 ───────────────────────────────────────────

    def has_graph(self) -> bool:
        return self._has_graph

    def cleanup(self) -> None:
        self.logger.log_session_end()
        self.context_manager.messages.clear()
        if self._memory_update_chain is not None and not self._memory_update_chain.done():
            self._memory_update_chain.cancel()
        self._memory_update_chain = None
        self._pending_feedback = None
        self._paused_plan_state = None
        self._paused_react_state = None
        self._has_graph = False
        self.logger.set_graph_flag(False)
        if self._mcp_connected:
            try:
                asyncio.run(self._cleanup_mcp())
            except RuntimeError:
                # 如果当前已有运行中的事件循环，跳过异步清理
                pass
            except Exception:
                pass

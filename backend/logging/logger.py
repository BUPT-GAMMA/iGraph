"""
日志模块

Logger        — 服务端全局日志 + 会话日志工厂 + 历史会话查询
SessionLogger — 单会话 message_list.jsonl（消息）+ trace.jsonl（调试）
ServiceLogger — 单服务单会话日志（{service}.log），供子服务进程直接使用
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, List, Optional

import backend.data_paths as _dp
from backend.logging.session_store import SessionStore

# Legacy env-var override kept for backward compat; new code uses data_paths.
_DEFAULT_LOGS_DIR = "logs"

# ServiceLogger 实例缓存，key = "{session_id}.{service}"
_svc_logger_cache: dict[str, "ServiceLogger"] = {}


def _ensure_session_dir(session_id: str, logs_dir: str) -> str:
    """确保 logs/{session_id}/ 目录存在，返回其绝对路径。"""
    path = os.path.join(logs_dir, _dp.safe_path_component(session_id))
    os.makedirs(path, exist_ok=True)
    return path


def get_service_logger(
    session_id: str,
    service: str,
    logs_dir: Optional[str] = None,
) -> "ServiceLogger":
    """
    获取（或创建）指定会话、指定服务的 ServiceLogger。

    参数:
    - session_id: 业务会话 ID
    - service: 服务名称，如 "mcp" / "graph_data" / "graph_compute" / "graph_ml"
    - logs_dir: 日志根目录，默认使用 IGRAPH_LOGS_DIR 环境变量或 "logs"

    线程安全：同进程内对同一 (session_id, service) 复用同一实例。
    """
    key = f"{session_id}.{service}"
    if key not in _svc_logger_cache:
        resolved_dir = (
            logs_dir
            or os.getenv("IGRAPH_LOGS_DIR")
            or os.getenv("GRAPH_AGENT_LOGS_DIR")
            or _DEFAULT_LOGS_DIR
        )
        _svc_logger_cache[key] = ServiceLogger(session_id, service, resolved_dir)
    return _svc_logger_cache[key]


def close_service_loggers(session_id: str) -> None:
    """关闭并移除指定会话的所有 ServiceLogger（释放文件句柄）。"""
    keys = [k for k in _svc_logger_cache if k.startswith(f"{session_id}.")]
    for k in keys:
        _svc_logger_cache.pop(k).close()


class ServiceLogger:
    """
    单服务单会话日志记录器。

    供 MCP Server / GraphData Service / GraphML Service 等子进程直接调用，
    写入 logs/{session_id}/{service}.log。
    """

    def __init__(self, session_id: str, service: str, logs_dir: str) -> None:
        self.session_id = session_id
        self.service = service
        sdir = _ensure_session_dir(session_id, logs_dir)
        self._log_path = os.path.join(sdir, f"{service}.log")

        lg_name = f"svc.{session_id}.{service}"
        self._lg = logging.getLogger(lg_name)
        self._lg.setLevel(logging.DEBUG)
        self._lg.propagate = False
        if not self._lg.handlers:
            fh = logging.FileHandler(self._log_path, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(message)s"))
            self._lg.addHandler(fh)

    def request(self, tool: str, args: dict) -> None:
        """记录工具调用请求。"""
        args_str = json.dumps(args, ensure_ascii=False)
        self._lg.info(f"[{self._ts()}] → {tool}  args={args_str}")

    def response(self, tool: str, result: Any, duration_ms: Optional[int] = None) -> None:
        """记录工具调用响应。"""
        if isinstance(result, dict):
            result_str = json.dumps(result, ensure_ascii=False)
        else:
            result_str = str(result)
        if len(result_str) > 500:
            result_str = result_str[:500] + "…"
        dur = f"  ({duration_ms}ms)" if duration_ms is not None else ""
        self._lg.info(f"[{self._ts()}] ← {tool}  result={result_str}{dur}")

    def event(self, label: str, **fields) -> None:
        """记录任意命名事件。"""
        parts = [f"[{self._ts()}] {label}"]
        for k, v in fields.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            parts.append(f"  {k}={v}")
        self._lg.info("  ".join(parts))

    def error(self, tool: str, err: str) -> None:
        """记录错误。"""
        self._lg.info(f"[{self._ts()}] ✗ {tool}  error={err}")

    def close(self) -> None:
        """关闭文件句柄。"""
        for h in self._lg.handlers[:]:
            h.close()
            self._lg.removeHandler(h)

    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class Logger:
    """服务端全局日志管理器，同时作为 SessionLogger 的工厂。"""

    # Legacy attribute kept so existing code that reads Logger.LOGS_DIR still works.
    LOGS_DIR: str = ""

    def __init__(self) -> None:
        data_root = _dp.data_root()
        data_root.mkdir(parents=True, exist_ok=True)
        self.LOGS_DIR = str(data_root)
        self.server = self._build_server_logger(data_root)
        # Pre-create the global directory structure.
        _dp.global_skills_dir().mkdir(parents=True, exist_ok=True)
        _dp.global_review_queue_dir().mkdir(parents=True, exist_ok=True)
        _dp.global_datasets_dir().mkdir(parents=True, exist_ok=True)
        # One-time migration from legacy paths.
        try:
            from backend.migration import migrate_global_skills, migrate_bundled_skills
            migrate_global_skills()
            migrate_bundled_skills()
        except Exception:
            pass
        # Per-user session stores, keyed by user_id.
        self._user_stores: dict[str, SessionStore] = {}

    def _build_server_logger(self, data_root) -> logging.Logger:
        lg = logging.getLogger("igraph.server")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not lg.handlers:
            fh = logging.FileHandler(
                str(data_root / "server.log"), encoding="utf-8"
            )
            fh.setFormatter(logging.Formatter(
                "[%(asctime)s] %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            lg.addHandler(fh)
            ch = logging.StreamHandler()
            ch.setFormatter(logging.Formatter(
                "[%(asctime)s] %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            ))
            lg.addHandler(ch)
        return lg

    def get_user_store(self, user_id: str) -> SessionStore:
        """返回指定用户的 SessionStore（懒加载，路径：data/users/{user_id}/sessions/）。"""
        if user_id not in self._user_stores:
            sessions_dir = _dp.user_sessions_dir(user_id)
            sessions_dir.mkdir(parents=True, exist_ok=True)
            self._user_stores[user_id] = SessionStore(str(sessions_dir))
        return self._user_stores[user_id]

    def _write_session_index(self, session_id: str, user_id: str) -> None:
        """写入 session_id → user_id 的全局索引（O(1) 反查）。"""
        if not session_id or not user_id:
            return
        try:
            idx_path = _dp.session_index_path(session_id)
            idx_path.parent.mkdir(parents=True, exist_ok=True)
            idx_path.write_text(user_id, encoding="utf-8")
        except Exception:
            pass

    def _read_session_index(self, session_id: str) -> str:
        """读取索引文件，返回 user_id；不存在则返回空字符串。"""
        try:
            return _dp.session_index_path(session_id).read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def get_session_owner(self, session_id: str) -> str:
        """Best-effort owner lookup for sessions whose meta.user_id is still empty."""
        uid = self._read_session_index(session_id)
        if uid:
            return uid
        users_root = _dp.data_root() / "users"
        try:
            if users_root.exists():
                for user_dir in users_root.iterdir():
                    if not user_dir.is_dir():
                        continue
                    candidate = user_dir / "sessions" / session_id / "session_meta.json"
                    if candidate.exists():
                        found_uid = user_dir.name
                        self._write_session_index(session_id, found_uid)
                        return found_uid
        except Exception:
            return ""
        return ""

    def _legacy_store(self) -> Optional[SessionStore]:
        """旧 logs/ 目录的只读 fallback store（目录存在时才返回）。"""
        legacy = (
            os.getenv("IGRAPH_LOGS_DIR")
            or os.getenv("GRAPH_AGENT_LOGS_DIR")
            or _DEFAULT_LOGS_DIR
        )
        if os.path.isdir(legacy):
            return SessionStore(legacy)
        return None

    def _find_store_for_session(self, session_id: str) -> Optional[SessionStore]:
        """查找持有 session_id 的 store，优先用索引文件，fallback 扫描 + 旧 logs/。"""
        # 1. 索引文件 O(1) 查找
        uid = self._read_session_index(session_id)
        if uid:
            store = self.get_user_store(uid)
            if store.session_exists(session_id):
                return store

        # 2. 全目录扫描（索引缺失时的兜底，同时补写索引）
        users_root = _dp.data_root() / "users"
        if users_root.exists():
            for user_dir in users_root.iterdir():
                if not user_dir.is_dir():
                    continue
                candidate = user_dir / "sessions" / session_id / "session_meta.json"
                if candidate.exists():
                    found_uid = user_dir.name
                    store_dir = str(user_dir / "sessions")
                    if found_uid not in self._user_stores:
                        self._user_stores[found_uid] = SessionStore(store_dir)
                    self._write_session_index(session_id, found_uid)  # 补写索引
                    return self._user_stores[found_uid]

        # 3. 旧 logs/ 目录（历史数据向后兼容）
        legacy = self._legacy_store()
        if legacy and legacy.session_exists(session_id):
            return legacy

        return None

    def info(self, msg: str) -> None:
        self.server.info(msg)

    def error(self, msg: str) -> None:
        self.server.error(msg)

    def list_sessions(self, limit: int = 200, user_id: str = "") -> list[dict]:
        if user_id:
            return self.get_user_store(user_id).list_sessions(limit=limit, user_id=user_id)
        # 无 user_id：合并所有用户的 sessions（admin 场景）
        all_sessions: list[dict] = []
        users_root = _dp.data_root() / "users"
        if users_root.exists():
            for user_dir in users_root.iterdir():
                if user_dir.is_dir():
                    store = self.get_user_store(user_dir.name)
                    all_sessions.extend(store.list_sessions(limit=limit))
        all_sessions.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
        return all_sessions[:limit]

    def get_session_meta(self, session_id: str, user_id: str = "") -> dict:
        if user_id:
            meta = self.get_user_store(user_id).load_meta(session_id)
            if meta:
                return meta
        store = self._find_store_for_session(session_id)
        return store.load_meta(session_id) if store else {}

    def load_context_messages(self, session_id: str, user_id: str = "") -> list[dict]:
        if user_id:
            msgs = self.get_user_store(user_id).load_context_messages(session_id)
            if msgs:
                return msgs
        store = self._find_store_for_session(session_id)
        return store.load_context_messages(session_id) if store else []

    def load_chat_messages(self, session_id: str, user_id: str = "") -> list[dict]:
        if user_id:
            msgs = self.get_user_store(user_id).load_chat_messages(session_id)
            if msgs:
                return msgs
        store = self._find_store_for_session(session_id)
        return store.load_chat_messages(session_id) if store else []

    def create_session_logger(
        self,
        session_id: str,
        provider: str = "",
        model: str = "",
        reasoning_mode: str = "direct",
        is_new: bool = True,
        user_id: str = "",
    ) -> "SessionLogger":
        """工厂方法：为指定会话创建 SessionLogger 实例。"""
        if not user_id:
            raise ValueError(f"create_session_logger 必须提供 user_id（session_id={session_id}）")
        store = self.get_user_store(user_id)
        self._write_session_index(session_id, user_id)
        # 确保 user_id 写入 session_meta.json，用于后续权限过滤
        try:
            store.update_meta(session_id, {"user_id": user_id})
        except Exception:
            pass
        return SessionLogger(
            session_id=session_id,
            server_logger=self.server,
            store=store,
            provider=provider,
            model=model,
            reasoning_mode=reasoning_mode,
            is_new=is_new,
        )


class SessionLogger:
    """
    单会话日志记录器。

    - message_list.jsonl: 每行一条 message record（append-only）
    - trace.jsonl: 调试事件（可用于排错）
    """

    def __init__(
        self,
        session_id: str,
        server_logger: logging.Logger,
        store: SessionStore,
        provider: str = "",
        model: str = "",
        reasoning_mode: str = "direct",
        is_new: bool = True,
    ) -> None:
        self.session_id = session_id
        self._server = server_logger
        self._store = store

        # 总是确保目录存在，便于 service 子日志落盘。
        self._store.ensure_session_dir(session_id)

        if is_new:
            self._store.update_meta(
                session_id,
                {
                    "provider": provider,
                    "model": model,
                    "reasoning_mode": reasoning_mode,
                    "archived": False,
                },
            )
            self._emit_trace("SESSION_START", {"provider": provider, "model": model})
            self._server.info(
                f"[{session_id}] 会话创建  provider={provider}  model={model}"
            )

    # ── 消息记录（message_list.jsonl） ──────────────────────────────

    def log_context_message(
        self,
        message: dict[str, Any],
        *,
        visible: bool = False,
        display_content: str = "",
        kind: str = "context",
        include_in_context: bool = True,
    ) -> None:
        self._append_message_record(
            message=message,
            visible=visible,
            display_content=display_content,
            kind=kind,
            include_in_context=include_in_context,
        )

    def log_user_message(self, content: str) -> None:
        # 兼容旧调用：仅作为可见聊天消息，不参与上下文恢复。
        self._append_message_record(
            message={"role": "user", "content": content},
            visible=True,
            display_content=content,
            kind="user_visible",
            include_in_context=False,
        )

    def log_assistant_message(
        self,
        content: str,
        *,
        tools: Optional[list[dict[str, Any]]] = None,
        thinking: Optional[list[dict[str, Any]]] = None,
        plan: Optional[dict[str, Any]] = None,
        solver_steps: Optional[list[dict[str, Any]]] = None,
        feedback_request: Optional[dict[str, Any]] = None,
        progress_items: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        # 仅记录用户可见最终答复，不参与上下文恢复。
        self._append_message_record(
            message={"role": "assistant", "content": content},
            visible=True,
            display_content=content,
            kind="assistant_visible",
            include_in_context=False,
            extras={
                "tools": tools if isinstance(tools, list) else [],
                "thinking": thinking if isinstance(thinking, list) else [],
                "plan": plan if isinstance(plan, dict) else None,
                "solver_steps": solver_steps if isinstance(solver_steps, list) else [],
                "feedback_request": feedback_request if isinstance(feedback_request, dict) else None,
                "progress_items": progress_items if isinstance(progress_items, list) else [],
            },
        )

    def log_context_compaction(
        self,
        summary_message: dict[str, Any],
        *,
        stats: Optional[dict[str, Any]] = None,
        preserve_recent_user_turns: int = 3,
    ) -> None:
        self._store.compact_context_records(
            self.session_id,
            summary_message,
            stats=stats or {},
            preserve_recent_user_turns=preserve_recent_user_turns,
        )
        self._emit_trace(
            "CONTEXT_COMPACTED",
            {"summary_message": summary_message, "stats": stats or {}},
        )

    async def log_context_compaction_async(
        self,
        summary_message: dict[str, Any],
        *,
        stats: Optional[dict[str, Any]] = None,
        preserve_recent_user_turns: int = 3,
    ) -> None:
        import asyncio
        await asyncio.to_thread(
            self._store.compact_context_records,
            self.session_id,
            summary_message,
            stats=stats or {},
            preserve_recent_user_turns=preserve_recent_user_turns,
        )
        self._emit_trace(
            "CONTEXT_COMPACTED",
            {"summary_message": summary_message, "stats": stats or {}},
        )

    def rollback_last_user_turn(self) -> list[dict]:
        self._emit_trace("ROLLBACK_LAST_USER_TURN", {})
        self._store.truncate_before_last_visible_user_turn(self.session_id)
        return self._store.load_context_messages(self.session_id)

    def graph_version_effects_after_last_visible_user_turn(self) -> list[dict]:
        try:
            return self._store.graph_version_effects_after_last_visible_user_turn(self.session_id)
        except Exception:
            return []

    def set_reasoning_mode(self, reasoning_mode: str) -> None:
        self._store.update_meta(self.session_id, {"reasoning_mode": reasoning_mode})

    def set_model(self, provider: str, model: str) -> None:
        self._store.update_meta(self.session_id, {"provider": provider, "model": model})

    def set_graph_flag(self, has_graph: bool) -> None:
        self._store.update_meta(self.session_id, {"last_has_graph": bool(has_graph)})

    def set_graph_meta(
        self,
        *,
        has_graph: bool,
        node_count: int | None = None,
        edge_count: int | None = None,
        is_directed: bool | None = None,
        has_weights: bool | None = None,
        graph_version: int | None = None,
    ) -> None:
        patch: dict[str, Any] = {"last_has_graph": bool(has_graph)}
        if node_count is not None:
            patch["graph_node_count"] = int(node_count)
        if edge_count is not None:
            patch["graph_edge_count"] = int(edge_count)
        if is_directed is not None:
            patch["graph_is_directed"] = bool(is_directed)
        if has_weights is not None:
            patch["graph_has_weights"] = bool(has_weights)
        if graph_version is not None:
            patch["graph_version"] = int(graph_version)
        self._store.update_meta(self.session_id, patch)

    # ── 调试 trace（trace.jsonl） ─────────────────────────────────

    def log_tool_call(self, tool_name: str, args: dict) -> None:
        self._emit_trace("TOOL_CALL", {"tool": tool_name, "args": args})
        self._server.info(
            f"[{self.session_id}] 工具调用 → {tool_name}  "
            f"args={json.dumps(args, ensure_ascii=False)}"
        )

    def log_thinking(self, phase: str, content: str) -> None:
        self._emit_trace("THINKING", {"phase": phase, "content": content})

    def log_context_snapshot(self, label: str, msgs: list) -> None:
        """调试上下文快照（仅 trace，不再写 messages.log）。"""
        serialized = [self._serialize_message(m, i) for i, m in enumerate(msgs)]
        self._emit_trace("CONTEXT_SNAPSHOT", {"label": label, "messages": serialized})

    def log_tools_snapshot(self, source: str, tool_specs: list[dict]) -> None:
        self._emit_trace("TOOLS_SNAPSHOT", {"source": source, "tools": tool_specs})

    def log_model_request(self, step: int, msgs: list, tool_specs: list[dict]) -> None:
        serialized = [self._serialize_message(m, i) for i, m in enumerate(msgs)]
        self._emit_trace(
            "MODEL_REQUEST",
            {"step": step, "messages": serialized, "available_tools": tool_specs},
        )

    def log_model_response_raw(self, step: int, raw_text: str) -> None:
        self._emit_trace("MODEL_RESPONSE_RAW", {"step": step, "raw_text": raw_text})

    def log_protocol_parse(
        self,
        step: int,
        think: str,
        tool_calls: list[dict],
        final_answer: str,
        parse_error: Optional[str] = None,
    ) -> None:
        payload = {
            "step": step,
            "think": think,
            "tool_calls": tool_calls,
            "final_answer": final_answer,
        }
        if parse_error:
            payload["parse_error"] = parse_error
        self._emit_trace("MODEL_RESPONSE_PARSED", payload)

    def log_trace_event(self, event: str, payload: dict[str, Any]) -> None:
        self._emit_trace(event, payload)

    def log_tool_result(self, tool_name: str, result: str) -> None:
        self._emit_trace("TOOL_RESULT", {"tool": tool_name, "result": result})

    # ── 错误 / 生命周期 ───────────────────────────────────────────

    def log_error(self, error: str) -> None:
        self._emit_trace("ERROR", {"error": error})
        self._server.error(f"[{self.session_id}] 错误: {error}")

    def log_session_end(self) -> None:
        self._emit_trace("SESSION_END", {})
        self._store.update_meta(self.session_id, {"archived": True})
        self._server.info(f"[{self.session_id}] 会话销毁")
        # 同步关闭本会话所有 ServiceLogger
        close_service_loggers(self.session_id)

    # ── 内部工具 ──────────────────────────────────────────────────

    def _append_message_record(
        self,
        *,
        message: dict[str, Any],
        visible: bool,
        display_content: str,
        kind: str,
        include_in_context: bool,
        extras: Optional[dict[str, Any]] = None,
    ) -> None:
        record = {
            "message": message,
            "visible": bool(visible),
            "display_content": display_content,
            "kind": kind,
            "include_in_context": bool(include_in_context),
        }
        if isinstance(extras, dict):
            record.update(extras)
        self._store.append_message_record(self.session_id, record)

    def _emit_trace(self, event: str, data: dict[str, Any]) -> None:
        record = {
            "timestamp": self._ts(),
            "session_id": self.session_id,
            "event": event,
            **data,
        }
        self._store.append_trace_record(self.session_id, record)

    @staticmethod
    def _serialize_message(msg: Any, index: int) -> dict:
        if isinstance(msg, dict):
            raw_content = msg.get("content", "")
            content_str = raw_content if isinstance(raw_content, str) else str(raw_content)
            entry: dict[str, Any] = {
                "index": index,
                "type": "dict_message",
                "role": msg.get("role"),
                "content": content_str,
            }
            for k in ("name", "tool_call_id", "tool_calls", "call_id", "observations"):
                if k in msg:
                    entry[k] = msg[k]
            return entry

        type_name = type(msg).__name__
        raw_content = getattr(msg, "content", "")
        content_str = raw_content if isinstance(raw_content, str) else str(raw_content)
        tool_calls = getattr(msg, "tool_calls", None) or []
        tool_call_id = getattr(msg, "tool_call_id", None)
        msg_name = getattr(msg, "name", None)

        entry: dict[str, Any] = {"index": index, "type": type_name, "content": content_str}
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id
        if msg_name:
            entry["name"] = msg_name
        return entry

    @staticmethod
    def _ts() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

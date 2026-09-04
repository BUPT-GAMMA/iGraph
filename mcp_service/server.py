"""
MCP Server — 图分析工具服务（动态注册版）

工具规格从 graph_data_service.tool_catalog（唯一真相源）动态读取，
通过统一 handler 转发到 GraphData Server 执行。

新增工具只需修改 graph_data_service/tool_catalog.py，无需改动本文件。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

from fastmcp import Context, FastMCP

from graph_engine.observability import get_service_logger, close_service_loggers
import graph_engine.paths as _dp
from graph_engine.env import load_environment
from mcp_service.config import MCPConfig
from mcp_service.graph_data_client import GraphDataClient
from mcp_service.session_registry import SessionRegistry
from mcp_service.tool_forwarding import (
    forwarded_graph_tool_arguments,
    schema_parameter_type,
)

load_environment()


def _setup_logging() -> logging.Logger:
    lg = logging.getLogger("mcp_server")
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    if lg.handlers:
        return lg
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    lg.addHandler(ch)
    try:
        os.makedirs("logs", exist_ok=True)
        fh = logging.FileHandler("logs/mcp_server.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        lg.addHandler(fh)
    except OSError:
        pass
    return lg


logger = _setup_logging()
config = MCPConfig.from_env()
logger.info("GraphData target configured: %s", config.graph_data_url)

mcp = FastMCP(
    "iGraph",
    instructions="图论分析与图机器学习工具集。工具规格由 tool_catalog 统一定义，动态注册到 MCP。",
)

_graph_data = GraphDataClient(config.graph_data_url)
_session_registry = SessionRegistry()


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _mcp_key(ctx: Context) -> str:
    return str(id(ctx.request_context.session))


def _http_headers() -> dict[str, str]:
    """Read HTTP headers when the streamable-http transport is active."""
    try:
        from fastmcp.server.dependencies import get_http_headers

        return {str(key).lower(): str(value) for key, value in get_http_headers().items()}
    except Exception:
        return {}


def _sid(ctx: Context) -> str:
    header_value = _http_headers().get("x-session-id", "").strip()
    if header_value:
        return header_value
    key = _mcp_key(ctx)
    sid = _session_registry.get(key)
    if sid is None:
        raise RuntimeError(f"未注册的 MCP session (key={key})，请先调用 register_session。")
    return sid


def _uid(ctx: Context) -> str:
    header_value = _http_headers().get("x-user-id", "").strip()
    return header_value or _session_registry.get_user_id(_mcp_key(ctx))


def _run_id() -> str:
    return _http_headers().get("x-run-id", "").strip()


def _service_logger(session_id: str, service: str, user_id: str = ""):
    logs_dir = str(_dp.user_sessions_dir(user_id)) if user_id else None
    return get_service_logger(session_id, service, logs_dir=logs_dir)


async def _invoke_graph_tool(tool_name: str, ctx: Context, arguments: dict) -> str:
    sid = _sid(ctx)
    user_id = _uid(ctx)
    forwarded_arguments = forwarded_graph_tool_arguments(tool_name, arguments)

    mcp_log = _service_logger(sid, "mcp", user_id=user_id)
    mcp_log.request(tool_name, forwarded_arguments)
    if tool_name in {"load_graph_classification_dataset", "train", "infer"}:
        mcp_log.event(
            "GRAPH_DATA_FORWARD",
            tool=tool_name,
            target_base_url=_graph_data.base_url,
        )
    start = asyncio.get_event_loop().time()
    result = await _graph_data.call_tool(
        sid,
        tool_name,
        forwarded_arguments,
        user_id=user_id,
        run_id=_run_id(),
    )
    duration_ms = int((asyncio.get_event_loop().time() - start) * 1000)
    result_dict = result if isinstance(result, dict) else {"success": False, "error": str(result)}
    mcp_log.response(tool_name, result_dict, duration_ms)
    if isinstance(result, dict):
        return _json(result)
    return _json(result_dict)


# ──────────────────────────────────────────────────────────────────────────────
# 动态工具注册
# ──────────────────────────────────────────────────────────────────────────────

def _build_handler_with_signature(
    tool_name: str,
    input_schema: dict,
    invoke_fn,
) -> Any:
    """
    构建带有真实参数签名的 async handler。

    FastMCP 通过 inspect.signature() 从函数签名生成 MCP inputSchema。
    使用 **kwargs 会被 FastMCP 解析为名为 'kwargs' 的必填字段，导致 LLM 调用失败。
    因此必须为每个工具生成具有真实参数名的函数签名。
    """
    properties: Dict[str, dict] = input_schema.get("properties") or {}
    required_fields = set(input_schema.get("required") or [])

    # 构造 inspect.Parameter 列表
    params = [
        inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context)
    ]
    annotations: Dict[str, Any] = {"ctx": Context, "return": str}

    for pname, pschema in properties.items():
        raw_type = pschema.get("type", "string")
        type_key = raw_type[0] if isinstance(raw_type, list) else raw_type
        py_type = schema_parameter_type(type_key)
        if pname in required_fields:
            p = inspect.Parameter(
                pname,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=py_type,
            )
            annotations[pname] = py_type
        else:
            default = pschema.get("default")
            opt_type = Optional[py_type]  # type: ignore[valid-type]
            p = inspect.Parameter(
                pname,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=opt_type,
            )
            annotations[pname] = opt_type
        params.append(p)

    sig = inspect.Signature(params, return_annotation=str)

    async def handler(*args, **kwargs):  # type: ignore[misc]
        ctx_val = args[0] if args else kwargs.pop("ctx", None)
        # 过滤掉 None（可选参数未传），避免 _optional_bool 等校验函数收到 None
        clean_args = {k: v for k, v in kwargs.items() if v is not None}
        return await invoke_fn(tool_name, ctx_val, clean_args)

    handler.__signature__ = sig  # type: ignore[attr-defined]
    handler.__annotations__ = annotations
    handler.__name__ = tool_name
    return handler


def _register_graph_tools(tool_specs: List[dict]) -> None:
    """从 tool_catalog 动态注册工具到 FastMCP。"""
    for spec in tool_specs:
        tool_name = spec.get("name", "").strip()
        if not tool_name:
            continue
        description = spec.get("description", "")
        input_schema = spec.get("inputSchema") or {}

        handler = _build_handler_with_signature(
            tool_name, input_schema, _invoke_graph_tool
        )
        handler.__doc__ = description

        try:
            mcp.tool(description=description)(handler)
            logger.debug("Registered tool: %s", tool_name)
        except Exception as e:
            logger.warning("Failed to register tool %s: %s", tool_name, e)


# 从 tool_catalog 导入并注册工具（同步方式，在 module 加载时执行）
try:
    from graph_engine.tool_catalog import get_all_public_tools

    all_tools = get_all_public_tools(include_graph_ml=True)
    _register_graph_tools(all_tools)
    logger.info(
        "Dynamic tool registration complete: %d tools",
        len(all_tools),
    )
except ImportError as _import_err:
    logger.warning(
        "tool_catalog not available, falling back to empty tool set: %s", _import_err
    )


# ──────────────────────────────────────────────────────────────────────────────
# 内部工具（硬编码，不走 catalog）
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def register_session(agent_session_id: str, ctx: Context, user_id: Optional[str] = "") -> str:
    """
    将当前 MCP 会话绑定到业务会话 ID，并在 GraphData 侧创建会话。

    参数:
    - agent_session_id: 业务侧会话 ID（建议由上层 Agent 保证唯一）。

    返回:
    - JSON 字符串，包含会话注册/创建结果。成功后本 MCP 会话可调用图工具。
    """
    key = _mcp_key(ctx)
    effective_user = str(user_id or "").strip()
    _session_registry.register(key, agent_session_id, effective_user)
    result = await _graph_data.create_session(agent_session_id, user_id=effective_user)
    logger.info(
        "register_session  key=%s -> agent_session_id=%s  total=%d",
        key,
        agent_session_id,
        _session_registry.count,
    )
    mcp_log = _service_logger(agent_session_id, "mcp", user_id=effective_user)
    mcp_log.event("SESSION_REGISTER", mcp_key=key, total=_session_registry.count)
    return _json(result if isinstance(result, dict) else {"success": True})


@mcp.tool()
async def delete_graph_session(ctx: Context) -> str:
    """
    删除当前 MCP 会话关联的 GraphData 会话并解除绑定。

    返回:
    - JSON 字符串，包含删除结果。
    """
    key = _mcp_key(ctx)
    user_id = _uid(ctx)
    sid = _session_registry.pop(key)
    if not sid:
        return _json({"success": True})

    mcp_log = _service_logger(sid, "mcp", user_id=user_id)
    mcp_log.event("SESSION_DELETE", mcp_key=key, remaining=_session_registry.count)
    result = await _graph_data.delete_session(sid, user_id=user_id)
    logger.info(
        "delete_graph_session  key=%s  agent_session_id=%s  remaining=%d",
        key,
        sid,
        _session_registry.count,
    )
    close_service_loggers(sid)
    return _json(result if isinstance(result, dict) else {"success": True})


@mcp.tool()
async def set_session_config(llm_model: Optional[str], ctx: Context) -> str:
    """
    更新当前 MCP 会话的配置（目前支持 llm_model）。
    iGraph 在初始化和切换模型时调用，使 load_graph_from_text 等工具
    使用与主 Agent 相同的 LLM。
    """
    key = _mcp_key(ctx)
    sid = _session_registry.get(key)
    if not sid:
        return _json({"success": False, "error": "session_not_registered"})
    if llm_model:
        await _graph_data.set_session_model(sid, llm_model, user_id=_uid(ctx))
    return _json({"success": True})


if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    logger.info("mcp_server starting  transport=%s", transport)
    if transport in ("streamable-http", "http"):
        mcp.run(transport="http", host=config.host, port=config.port)
    elif transport == "sse":
        mcp.run(transport="sse", host=config.host, port=config.port)
    else:
        mcp.run()

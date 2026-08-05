"""
原生 MCP HTTP 客户端（无 LangChain 依赖）。

用途：
1) 通过 streamable-http 端点执行 MCP JSON-RPC 调用
2) 管理 MCP session_id（来自响应头）
3) 提供 tools/list 与 tools/call 的透明封装
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


MCP_HEAVY_TOOL_TIMEOUT_SECONDS = _env_float("MCP_HEAVY_TOOL_TIMEOUT_SECONDS", 900.0)
_MCP_HEAVY_TOOLS = {
    "load_graph_classification_dataset",
    "train",
    "infer",
    "load_graph_from_text",
    "load_graph_from_upload",
}


def _tool_timeout(tool_name: str, default_timeout: float) -> float:
    if tool_name in _MCP_HEAVY_TOOLS:
        return MCP_HEAVY_TOOL_TIMEOUT_SECONDS
    return default_timeout


class RawMCPClient:
    def __init__(self, mcp_url: str, timeout: float = 30.0) -> None:
        self.mcp_url = mcp_url
        self.timeout = timeout
        self._session_id: Optional[str] = None
        self._request_id = 0
        self._client: Optional[httpx.AsyncClient] = None
        self._initialized = False

    async def open(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        await self.ensure_initialized()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None
        self._initialized = False
        self._session_id = None

    async def ensure_initialized(self) -> None:
        if self._initialized:
            return
        await self._initialize()
        self._initialized = True

    async def list_tools(self) -> list[dict]:
        await self.ensure_initialized()
        result = await self._rpc("tools/list", {})
        if isinstance(result, dict):
            tools = result.get("tools", [])
            if isinstance(tools, list):
                return [t for t in tools if isinstance(t, dict)]
        return []

    async def call_tool(self, name: str, arguments: Optional[dict] = None) -> dict:
        await self.ensure_initialized()
        result = await self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=_tool_timeout(name, self.timeout),
        )
        return result if isinstance(result, dict) else {"result": result}

    # ── 内部 ──────────────────────────────────────────────────────

    async def _initialize(self) -> None:
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "graph-agent", "version": "1.0"},
            },
        )
        # initialized 通知失败不阻断主流程（部分 server 可能不强制要求）。
        try:
            await self._notify("notifications/initialized", {})
        except Exception:
            pass

    async def _rpc(
        self,
        method: str,
        params: Optional[dict],
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        data = await self._post(payload, timeout=timeout)
        if not isinstance(data, dict):
            raise RuntimeError(f"MCP 响应不是 JSON 对象: {data}")
        if "error" in data:
            raise RuntimeError(f"MCP 调用失败 method={method}: {data['error']}")
        return data.get("result")

    async def _notify(self, method: str, params: Optional[dict]) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._post(payload)

    async def _post(self, payload: dict, *, timeout: Optional[float] = None) -> Any:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)

        headers = {
            "Content-Type": "application/json",
            # MCP streamable-http 端点通常要求客户端同时声明可接收 JSON 与 SSE。
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        kwargs: dict[str, Any] = {"json": payload, "headers": headers}
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = await self._client.post(self.mcp_url, **kwargs)
        sid = (
            resp.headers.get("mcp-session-id")
            or resp.headers.get("Mcp-Session-Id")
            or resp.headers.get("MCP-Session-Id")
        )
        if sid:
            self._session_id = sid

        text = resp.text
        if resp.status_code >= 400:
            raise RuntimeError(f"MCP HTTP {resp.status_code}: {text}")

        if not text.strip():
            return {}

        content_type = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" in content_type:
            return self._parse_sse_json(text)

        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise RuntimeError(f"MCP 返回非 JSON 文本: {text}") from e

    @staticmethod
    def _parse_sse_json(body: str) -> Any:
        """
        从 SSE 文本中提取 JSON payload。
        支持多条 data: 行，返回最后一个可解析的 JSON 对象。
        """
        json_payload = None
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                json_payload = json.loads(data)
            except json.JSONDecodeError:
                continue
        if json_payload is None:
            raise RuntimeError(f"MCP 返回 SSE 但未包含可解析 JSON: {body}")
        return json_payload

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

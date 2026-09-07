"""
GraphData Server HTTP 客户端
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

import httpx
from execution_protocol import execution_headers, service_headers
from mcp_service.construction_tools import CONSTRUCTION_TOOL_NAMES, construction_request, construction_source_type


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


GRAPH_DATA_MAX_RETRIES = 5
GRAPH_DATA_TIMEOUT_SECONDS = _env_float("GRAPH_DATA_TOOL_TIMEOUT_SECONDS", 120.0)
GRAPH_DATA_HEAVY_TOOL_TIMEOUT_SECONDS = _env_float(
    "GRAPH_DATA_HEAVY_TOOL_TIMEOUT_SECONDS",
    900.0,
)
_GRAPH_DATA_HEAVY_TOOLS = {
    "load_graph_classification_dataset",
    "train",
    "infer",
    "load_graph_from_text",
    "load_graph_from_upload",
}


def _retry_delay(attempt: int) -> float:
    return min(0.2 * (2 ** (attempt - 1)), 2.0)


def _read_response_payload(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _payload_error(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload.get("error") or payload)
    return str(payload)


def _exception_message(exc: BaseException) -> str:
    message = str(exc).strip()
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _is_heavy_tool_path(path: str) -> bool:
    if "/tools/" not in path:
        return False
    return path.rstrip("/").rsplit("/", 1)[-1] in _GRAPH_DATA_HEAVY_TOOLS


def _request_timeout(path: str) -> float:
    if _is_heavy_tool_path(path):
        return GRAPH_DATA_HEAVY_TOOL_TIMEOUT_SECONDS
    return GRAPH_DATA_TIMEOUT_SECONDS


class GraphDataClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def create_session(self, session_id: str, user_id: str = "") -> Dict[str, Any]:
        return await self._post(f"/sessions/{session_id}", user_id=user_id)

    async def delete_session(self, session_id: str, user_id: str = "") -> Dict[str, Any]:
        return await self._delete(f"/sessions/{session_id}", user_id=user_id)

    async def load_graph_text(
        self,
        session_id: str,
        edge_text: str,
        append: bool = False,
    ) -> Dict[str, Any]:
        return await self._post(
            f"/sessions/{session_id}/graph/load_text",
            {"edge_text": edge_text, "append": append},
        )

    async def load_graph_upload(
        self,
        session_id: str,
        filename: str,
        append: bool = False,
    ) -> Dict[str, Any]:
        return await self._post(
            f"/sessions/{session_id}/graph/load_upload",
            {"filename": filename, "append": append},
        )

    async def clear_graph(self, session_id: str) -> Dict[str, Any]:
        return await self._post(f"/sessions/{session_id}/graph/clear")

    async def to_undirected(self, session_id: str) -> Dict[str, Any]:
        return await self._post(f"/sessions/{session_id}/graph/to_undirected")

    async def graph_info(self, session_id: str) -> Dict[str, Any]:
        return await self._get(f"/sessions/{session_id}/graph/info")

    async def graph_document(self, session_id: str) -> Dict[str, Any]:
        return await self._get(f"/sessions/{session_id}/graph/document")

    async def list_tools(self, include_graph_ml: bool = True) -> list:
        """从 GraphData Server 获取工具规格（tool_catalog 唯一真相源）。"""
        params = "?include_graph_ml=true" if include_graph_ml else ""
        result = await self._get(f"/tools{params}")
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "data" in result:
            return result["data"] if isinstance(result["data"], list) else []
        return []

    async def set_session_model(self, session_id: str, model: str, user_id: str = "") -> None:
        """Notify GraphDataService of the session's LLM model so tools like
        load_graph_from_text use the correct model instead of the startup default."""
        try:
            await self._patch(f"/sessions/{session_id}/config", {"llm_model": model}, user_id=user_id)
        except Exception:
            pass

    async def call_tool(
        self,
        session_id: str,
        tool_name: str,
        arguments: Optional[dict] = None,
        user_id: str = "",
        run_id: str = "",
    ) -> Dict[str, Any]:
        args = arguments or {}
        if tool_name in CONSTRUCTION_TOOL_NAMES:
            return await self.call_construction_tool(session_id, tool_name, args, user_id=user_id, run_id=run_id)
        if tool_name == "load_graph_from_upload":
            source_type = construction_source_type(args.get("filename"))
            if source_type:
                return {
                    "success": False,
                    "code": "CONSTRUCTION_REQUIRED",
                    "source_type": source_type,
                    "error": "SQL/CSV/TXT 文件需要在数据集面板中构图；先检查已保存构图状态，ready 时直接加载复用。"
                             "尚未接入的构图算法不能通过旧文件加载或文本抽取代替。",
                }
        return await self._post(
            f"/sessions/{session_id}/tools/{tool_name}",
            {"arguments": arguments or {}},
            user_id=user_id,
            run_id=run_id,
        )

    async def call_construction_tool(
        self, session_id: str, tool_name: str, arguments: dict,
        user_id: str = "", run_id: str = "",
    ) -> Dict[str, Any]:
        if not user_id.strip():
            return {"success": False, "code": "USER_ID_REQUIRED", "error": "访问构图数据集需要已认证用户。"}
        try:
            method, path, body = construction_request(tool_name, session_id, arguments)
        except ValueError as exc:
            return {"success": False, "code": "INVALID_CONSTRUCTION_ARGUMENTS", "error": str(exc)}
        headers = {**execution_headers.get(), **service_headers(user_id), "X-Session-ID": session_id}
        token = execution_headers.set(headers)
        try:
            # A commit/load may already have reached the engine when a response
            # fails. Do not blindly replay persistent or session mutations.
            return await self._request(method, path, body, user_id=user_id, run_id=run_id,
                                       retry_errors=tool_name not in {"commit_graph_optimization", "load_constructed_graph"})
        finally:
            execution_headers.reset(token)

    async def _get(self, path: str, user_id: str = "", run_id: str = "") -> Dict[str, Any]:
        return await self._request("GET", path, user_id=user_id, run_id=run_id)

    async def _post(self, path: str, data: Optional[dict] = None, user_id: str = "", run_id: str = "") -> Dict[str, Any]:
        return await self._request("POST", path, data, user_id=user_id, run_id=run_id)

    async def _patch(self, path: str, data: Optional[dict] = None, user_id: str = "", run_id: str = "") -> Dict[str, Any]:
        return await self._request("PATCH", path, data, user_id=user_id, run_id=run_id)

    async def _delete(self, path: str, user_id: str = "", run_id: str = "") -> Dict[str, Any]:
        return await self._request("DELETE", path, user_id=user_id, run_id=run_id)

    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[dict] = None,
        user_id: str = "",
        run_id: str = "",
        retry_errors: bool = True,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        is_heavy_tool = _is_heavy_tool_path(path)
        last_error: str = ""
        for attempt in range(1, GRAPH_DATA_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(
                    trust_env=False,
                    timeout=_request_timeout(path),
                ) as client:
                    kwargs: dict[str, Any] = {"json": data}
                    headers = dict(execution_headers.get())
                    if user_id:
                        headers["X-User-ID"] = user_id
                    if run_id:
                        headers["X-Run-ID"] = run_id
                    if headers:
                        kwargs["headers"] = headers
                    response = await client.request(method, url, **kwargs)
                payload = _read_response_payload(response)

                if response.status_code >= 500:
                    last_error = _payload_error(payload)
                    if retry_errors and not is_heavy_tool and attempt < GRAPH_DATA_MAX_RETRIES:
                        await asyncio.sleep(_retry_delay(attempt))
                        continue
                    return {
                        **(payload.get("detail", payload) if isinstance(payload, dict)
                           and isinstance(payload.get("detail", payload), dict) else {}),
                        "success": False,
                        "error": (
                            "GraphData Server 请求失败"
                            f"(已尝试 {attempt} 次): {last_error}"
                        ),
                        "status_code": response.status_code,
                    }

                if response.status_code >= 400:
                    error = _payload_error(payload)
                    return {
                        **(payload.get("detail", payload) if isinstance(payload, dict)
                           and isinstance(payload.get("detail", payload), dict) else {}),
                        "success": False,
                        "error": error,
                        "status_code": response.status_code,
                    }
                if isinstance(payload, dict):
                    return payload
                return {"success": True, "data": payload}
            except httpx.TimeoutException as e:
                last_error = _exception_message(e)
                # Never retry on timeout: the server-side task is still running;
                # retrying only piles up more tasks and worsens CPU contention.
                retry_note = "；未重试以避免重复执行重型工具" if is_heavy_tool else ""
                return {
                    "success": False,
                    "error": (
                        "GraphData Server 请求超时"
                        f"(timeout={_request_timeout(path)}s): "
                        f"{last_error}{retry_note}"
                    ),
                }
            except httpx.RequestError as e:
                last_error = _exception_message(e)
                if retry_errors and attempt < GRAPH_DATA_MAX_RETRIES:
                    await asyncio.sleep(_retry_delay(attempt))
                    continue
                return {
                    "success": False,
                    "error": (
                        "GraphData Server 连接失败"
                        f"(已尝试 {attempt} 次): {last_error}" if not retry_errors else
                        "GraphData Server 连接失败"
                        f"(已重试 {GRAPH_DATA_MAX_RETRIES} 次): {last_error}"
                    ),
                }
            except Exception as e:
                return {"success": False, "error": f"GraphData Server 请求失败: {e}"}

        return {
            "success": False,
            "error": (
                "GraphData Server 连接失败"
                f"(已重试 {GRAPH_DATA_MAX_RETRIES} 次): {last_error}"
            ),
        }

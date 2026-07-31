from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import patch

if "httpx" not in sys.modules:
    fake_httpx = types.ModuleType("httpx")

    class _Request:
        def __init__(self, method: str, url: str) -> None:
            self.method = method
            self.url = url

    class _Response:
        def __init__(
            self,
            status_code: int,
            json=None,
            text: str = "",
            request=None,
            headers=None,
        ) -> None:
            self.status_code = status_code
            self._json = json
            self.text = text
            self.request = request
            self.headers = headers or {}

        def json(self):
            if self._json is None:
                raise ValueError("no json")
            return self._json

    class _RequestError(Exception):
        def __init__(self, message: str, request=None) -> None:
            super().__init__(message)
            self.request = request

    class _TimeoutException(Exception):
        pass

    class _ConnectError(_RequestError):
        pass

    fake_httpx.Request = _Request
    fake_httpx.Response = _Response
    fake_httpx.RequestError = _RequestError
    fake_httpx.TimeoutException = _TimeoutException
    fake_httpx.ConnectError = _ConnectError
    fake_httpx.AsyncClient = object
    sys.modules["httpx"] = fake_httpx

import httpx

import mcp_service.graph_data_client as graph_data_client
import mcp_service.raw_client as raw_client
from mcp_service.config import MCPConfig
from mcp_service.graph_data_client import GraphDataClient
from mcp_service.raw_client import RawMCPClient
from mcp_service.session_registry import SessionRegistry


class MCPModulesTest(unittest.TestCase):
    def test_session_registry(self) -> None:
        reg = SessionRegistry()
        self.assertEqual(reg.count, 0)
        reg.register("mcp-k1", "agent-s1")
        self.assertEqual(reg.count, 1)
        self.assertEqual(reg.get("mcp-k1"), "agent-s1")
        self.assertEqual(reg.pop("mcp-k1"), "agent-s1")
        self.assertEqual(reg.count, 0)

    def test_config_from_env(self) -> None:
        old = {
            "MCP_HOST": os.environ.get("MCP_HOST"),
            "MCP_PORT": os.environ.get("MCP_PORT"),
            "GRAPH_DATA_SERVER_URL": os.environ.get("GRAPH_DATA_SERVER_URL"),
        }
        try:
            os.environ["MCP_HOST"] = "127.0.0.1"
            os.environ["MCP_PORT"] = "19110"
            os.environ["GRAPH_DATA_SERVER_URL"] = "http://127.0.0.1:19111"
            cfg = MCPConfig.from_env()
            self.assertEqual(cfg.host, "127.0.0.1")
            self.assertEqual(cfg.port, 19110)
            self.assertEqual(cfg.graph_data_url, "http://127.0.0.1:19111")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class GraphDataClientRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_retries_transient_connection_failure(self) -> None:
        class _FlakyAsyncClient:
            calls = 0

            def __init__(self, **_kwargs) -> None:
                return

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return

            async def request(self, method: str, url: str, json=None):
                _FlakyAsyncClient.calls += 1
                request = httpx.Request(method, url)
                if _FlakyAsyncClient.calls < 3:
                    raise httpx.ConnectError("connection refused", request=request)
                return httpx.Response(200, json={"success": True}, request=request)

        client = GraphDataClient("http://graph-data")
        with patch.object(graph_data_client.httpx, "AsyncClient", _FlakyAsyncClient):
            with patch.object(graph_data_client, "_retry_delay", return_value=0):
                result = await client.call_tool("sid", "graph_info", {})

        self.assertEqual(result, {"success": True})
        self.assertEqual(_FlakyAsyncClient.calls, 3)

    async def test_stops_after_max_retries(self) -> None:
        class _FailingAsyncClient:
            calls = 0

            def __init__(self, **_kwargs) -> None:
                return

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return

            async def request(self, method: str, url: str, json=None):
                _FailingAsyncClient.calls += 1
                request = httpx.Request(method, url)
                raise httpx.ConnectError("connection refused", request=request)

        client = GraphDataClient("http://graph-data")
        with patch.object(graph_data_client.httpx, "AsyncClient", _FailingAsyncClient):
            with patch.object(graph_data_client, "_retry_delay", return_value=0):
                result = await client.call_tool("sid", "graph_info", {})

        self.assertFalse(result["success"])
        self.assertEqual(_FailingAsyncClient.calls, graph_data_client.GRAPH_DATA_MAX_RETRIES)
        self.assertIn("已重试 5 次", result["error"])

    async def test_train_uses_long_timeout_and_does_not_retry_timeout(self) -> None:
        class _TimeoutAsyncClient:
            calls = 0
            timeouts = []

            def __init__(self, **kwargs) -> None:
                _TimeoutAsyncClient.timeouts.append(kwargs.get("timeout"))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return

            async def request(self, method: str, url: str, json=None):
                _TimeoutAsyncClient.calls += 1
                raise httpx.TimeoutException("timed out")

        client = GraphDataClient("http://graph-data")
        with patch.object(graph_data_client.httpx, "AsyncClient", _TimeoutAsyncClient):
            result = await client.call_tool("sid", "train", {"task": "node_classification"})

        self.assertFalse(result["success"])
        self.assertEqual(_TimeoutAsyncClient.calls, 1)
        self.assertEqual(
            _TimeoutAsyncClient.timeouts,
            [graph_data_client.GRAPH_DATA_HEAVY_TOOL_TIMEOUT_SECONDS],
        )
        self.assertIn("未重试以避免重复执行训练/推理", result["error"])


class RawMCPClientTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_train_call_uses_long_mcp_timeout(self) -> None:
        class _FakeAsyncClient:
            posts = []

            def __init__(self, **_kwargs) -> None:
                return

            async def post(self, url: str, **kwargs):
                _FakeAsyncClient.posts.append(kwargs)

                class _JSONResponse:
                    status_code = 200
                    text = '{"jsonrpc":"2.0","result":{"success":true}}'
                    headers = {"content-type": "application/json"}

                    @staticmethod
                    def json():
                        return {"jsonrpc": "2.0", "result": {"success": True}}

                return _JSONResponse()

            async def aclose(self) -> None:
                return

        client = RawMCPClient("http://mcp", timeout=30.0)
        client._initialized = True
        with patch.object(raw_client.httpx, "AsyncClient", _FakeAsyncClient):
            result = await client.call_tool("train", {"task": "node_classification"})

        self.assertEqual(result, {"success": True})
        self.assertEqual(
            _FakeAsyncClient.posts[0].get("timeout"),
            raw_client.MCP_HEAVY_TOOL_TIMEOUT_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()

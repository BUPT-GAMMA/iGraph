from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.api_gateway.app as gateway_app
from backend.api_gateway.app import AttachmentRef, ApiGatewayConfig, create_server

ADMIN_HEADERS = {"X-User-ID": "admin"}
ALICE_HEADERS = {"X-User-ID": "alice"}


def _test_config() -> ApiGatewayConfig:
    return ApiGatewayConfig(
        upload_dir="uploads",
        mcp_port=19110,
        graph_data_port=19111,
        host="127.0.0.1",
        port=19109,
        mcp_url="http://127.0.0.1:19110/mcp",
        graph_data_base_url="http://127.0.0.1:19111",
    )


class _FakeSessionLogger:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def log_error(self, *_args, **_kwargs) -> None:
        return


class _FakeLogger:
    def create_session_logger(self, session_id: str, provider: str, model: str):
        return _FakeSessionLogger(session_id)

    def info(self, *_args, **_kwargs) -> None:
        return

    def error(self, *_args, **_kwargs) -> None:
        return


class _FakeAgent:
    def __init__(
        self,
        session_logger,
        model: str | None,
        reasoning_mode: str,
        provider: str = "transparent_openai_sdk",
        planning_mode: str = "auto",
        **_kwargs,
    ):
        self.session_id = session_logger.session_id
        self.provider = provider
        self.model = model or "fake-model"
        self.reasoning_mode = reasoning_mode
        self.planning_mode = planning_mode
        self.logger = session_logger
        self._has_graph = False

    def set_model(self, model: str, *_args) -> None:
        self.model = model

    def set_reasoning_mode(self, mode: str) -> None:
        self.reasoning_mode = mode

    def set_planning_mode(self, mode: str) -> None:
        self.planning_mode = mode

    def chat(self, message: str) -> str:
        return f"echo:{message}"

    async def chat_stream(self, message: str):
        yield {"type": "token", "content": f"echo:{message}"}

    def has_graph(self) -> bool:
        return self._has_graph

    def cleanup(self) -> None:
        return

    def context_usage(self) -> dict:
        return {"current_tokens": 0, "token_limit": 1024, "percent": 0.0, "status": "ok"}


class ApiGatewayAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server(
            config=_test_config(),
            manage_subprocesses=False,
            logger=_FakeLogger(),
            agent_factory=_FakeAgent,
        )
        self.client = TestClient(self.server.app)

    def test_create_session_and_chat(self) -> None:
        created = self.client.post(
            "/api/sessions",
            json={"model": "gpt-5-2"},
            headers=ADMIN_HEADERS,
        )
        self.assertEqual(created.status_code, 200)
        body = created.json()
        self.assertTrue(body["success"])
        sid = body["session_id"]

        chatted = self.client.post(
            f"/api/sessions/{sid}/chat",
            json={"message": "hello"},
            headers=ADMIN_HEADERS,
        )
        self.assertEqual(chatted.status_code, 200)
        chat_body = chatted.json()
        self.assertTrue(chat_body["success"])
        self.assertEqual(chat_body["response"], "echo:hello")

    def test_pt_attachment_hint_distinguishes_multigraph_and_single_graph(self) -> None:
        key = self.server._agent_key("alice", "session-attachment")
        self.server._uploaded_files[key] = {
            "token-hiv": {"original": "hiv.pt"},
        }

        resolved = self.server._resolve_attachments(
            "session-attachment",
            "请加载数据",
            [AttachmentRef(file_token="token-hiv", filename="hiv.pt")],
            user_id="alice",
        )

        self.assertIn("load_graph_classification_dataset", resolved)
        self.assertIn("load_graph_from_upload", resolved)
        self.assertIn("HIV/PCBA", resolved)

    def test_auth_validate_returns_model_access(self) -> None:
        resp = self.client.post("/api/auth/validate", headers=ADMIN_HEADERS)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["valid"])
        self.assertTrue(body["is_admin"])
        self.assertEqual(body["default_model"], gateway_app.DEFAULT_MODEL)
        self.assertIn("gpt-5-2", body["available_models"])

    def test_admin_session_without_model_uses_admin_default(self) -> None:
        resp = self.client.post("/api/sessions", json={}, headers=ADMIN_HEADERS)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["model"], gateway_app.DEFAULT_MODEL)

    def test_non_admin_session_without_model_uses_non_admin_default(self) -> None:
        resp = self.client.post("/api/sessions", json={}, headers=ALICE_HEADERS)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["model"], "qwen3.6-plus")
        self.assertNotIn("gpt-5-2", body["available_models"])

    def test_non_admin_cannot_select_restricted_model(self) -> None:
        resp = self.client.post(
            "/api/sessions",
            json={"model": "gpt-5-2"},
            headers=ALICE_HEADERS,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("无权使用模型", resp.json()["detail"])


class ApiGatewayGraphDataRetryTest(unittest.IsolatedAsyncioTestCase):
    def _server(self):
        return create_server(
            config=_test_config(),
            manage_subprocesses=False,
            logger=_FakeLogger(),
            agent_factory=_FakeAgent,
        )

    async def test_post_graph_data_retries_transient_connection_failure(self) -> None:
        class _FlakyAsyncClient:
            calls = 0

            def __init__(self, **_kwargs) -> None:
                return

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return

            async def post(self, url: str, **_kwargs):
                _FlakyAsyncClient.calls += 1
                request = gateway_app.httpx.Request("POST", url)
                if _FlakyAsyncClient.calls < 3:
                    raise gateway_app.httpx.ConnectError(
                        "connection refused",
                        request=request,
                    )
                return gateway_app.httpx.Response(
                    200,
                    json={"success": True},
                    request=request,
                )

        server = self._server()
        with patch.object(gateway_app.httpx, "AsyncClient", _FlakyAsyncClient):
            with patch.object(gateway_app, "_graph_data_retry_delay", return_value=0):
                resp = await server._post_graph_data_with_retry(
                    "http://graph-data/sessions/sid/tools/graph_info",
                    timeout=1.0,
                    json_body={"arguments": {}},
                )

        self.assertEqual(resp.json(), {"success": True})
        self.assertEqual(_FlakyAsyncClient.calls, 3)

    async def test_post_graph_data_reports_retries_after_connection_failures(self) -> None:
        class _FailingAsyncClient:
            calls = 0

            def __init__(self, **_kwargs) -> None:
                return

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return

            async def post(self, url: str, **_kwargs):
                _FailingAsyncClient.calls += 1
                request = gateway_app.httpx.Request("POST", url)
                raise gateway_app.httpx.ConnectError("connection refused", request=request)

        server = self._server()
        with patch.object(gateway_app.httpx, "AsyncClient", _FailingAsyncClient):
            with patch.object(gateway_app, "_graph_data_retry_delay", return_value=0):
                with self.assertRaisesRegex(RuntimeError, "已尝试 5 次"):
                    await server._post_graph_data_with_retry(
                        "http://graph-data/sessions/sid/tools/graph_info",
                        timeout=1.0,
                        json_body={"arguments": {}},
                    )

        self.assertEqual(_FailingAsyncClient.calls, gateway_app.GRAPH_DATA_MAX_RETRIES)


if __name__ == "__main__":
    unittest.main()

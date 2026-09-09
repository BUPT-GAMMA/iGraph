"""Long construction RPCs have a bounded override without changing normal RPCs."""
from __future__ import annotations

import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException

from backend.construction import construction_request_timeout, install_construction_routes
from backend.service_client import ServiceClient


class ConstructionTimeoutConfigTest(unittest.TestCase):
    def test_default_environment_override_and_maximum(self):
        with patch.dict(os.environ, {"GRAPH_CONSTRUCTION_MAX_SECONDS": "900"}):
            self.assertEqual(construction_request_timeout({}), 930)
            self.assertEqual(construction_request_timeout({"max_seconds": 60}), 90)
            self.assertEqual(construction_request_timeout({"max_seconds": 3600}), 3630)
        with patch.dict(os.environ, {"GRAPH_CONSTRUCTION_MAX_SECONDS": "45.5"}):
            self.assertEqual(construction_request_timeout({}), 75.5)

    def test_invalid_duration_cannot_extend_rpc_or_start_build(self):
        for value in (True, False, None, "900", -1, 0, 3601, 10 ** 400, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(HTTPException) as raised:
                construction_request_timeout({"max_seconds": value})
            self.assertEqual(raised.exception.status_code, 422)
        for value in ("not-a-number", "inf", "0", "3601"):
            with self.subTest(environment=value), patch.dict(os.environ, {"GRAPH_CONSTRUCTION_MAX_SECONDS": value}):
                with self.assertRaises(HTTPException) as raised:
                    construction_request_timeout({})
                self.assertEqual(raised.exception.status_code, 500)
                self.assertEqual(construction_request_timeout({"max_seconds": 10}), 40)


class ConstructionTimeoutRouteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = FastAPI()
        self.graph = SimpleNamespace(request=AsyncMock())
        self.dataset = SimpleNamespace(dataset_id="dataset-1", user_id="user-1", project_id=None,
            file_type="csv", storage_path="/retained/source.csv", original_filename="source.csv")
        install_construction_routes(self.app, graph=self.graph, directory=object(),
            user_for=lambda _request: SimpleNamespace(user_id="user-1"),
            project_for=lambda *_args: None, dataset_for=lambda *_args: self.dataset)
        self.endpoint = "/api/personal-datasets/dataset-1/construction"

    async def request(self, method, path, **kwargs):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    async def test_build_uses_duration_override_and_preserves_options(self):
        self.graph.request.return_value = {"dataset_id": "dataset-1", "status": "ready", "revision": 1}
        with patch("backend.construction.sync_record") as sync:
            response = await self.request("POST", self.endpoint + "/build",
                                          json={"options": {"max_seconds": 600, "query_count": 15}})
        self.assertEqual(response.status_code, 200)
        arguments = self.graph.request.call_args.kwargs
        self.assertEqual(arguments["timeout"], 630)
        self.assertEqual(arguments["json_body"]["options"], {"max_seconds": 600, "query_count": 15})
        sync.assert_called_once()

    async def test_timeout_does_not_claim_failure_or_overwrite_dataset_metadata(self):
        self.graph.request.side_effect = httpx.ReadTimeout("worker still running")
        with patch("backend.construction.sync_record") as sync:
            response = await self.request("POST", self.endpoint + "/build", json={"options": {"max_seconds": 60}})
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["detail"]["code"], "construction_running_unconfirmed")
        self.assertEqual(response.json()["detail"]["dataset_id"], "dataset-1")
        self.assertNotIn("status", response.json()["detail"])
        sync.assert_not_called()
        # The ordinary status route stays usable for recovery and gets no long
        # request override. The engine, not this BFF, declares the build outcome.
        self.graph.request.reset_mock(side_effect=True)
        self.graph.request.return_value = {"dataset_id": "dataset-1", "status": "building", "revision": 0,
                                           "source_type": "csv"}
        with patch("backend.construction.sync_record"):
            response = await self.request("GET", self.endpoint)
        self.assertEqual(response.json()["status"], "building")
        self.assertNotIn("timeout", self.graph.request.call_args.kwargs)

    async def test_invalid_budget_is_rejected_before_contacting_engine(self):
        response = await self.request("POST", self.endpoint + "/build", json={"options": {"max_seconds": 3601}})
        self.assertEqual(response.status_code, 422)
        self.graph.request.assert_not_awaited()

    async def test_capacity_rejection_is_forwarded_without_changing_metadata(self):
        detail = {"code": "construction_capacity_exhausted", "message": "本次未启动，请稍后重试", "retryable": True}
        self.graph.request.side_effect = HTTPException(429, detail)
        with patch("backend.construction.sync_record") as sync:
            response = await self.request("POST", self.endpoint + "/build", json={"options": {}})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["detail"], detail)
        sync.assert_not_called()
        self.graph.request.assert_awaited_once()

    async def test_duplicate_build_response_preserves_existing_build_identity(self):
        state = {"dataset_id": "dataset-1", "status": "building", "revision": 0,
                 "build_id": "existing-build", "failure_code": None}
        self.graph.request.return_value = state
        with patch("backend.construction.sync_record") as sync:
            response = await self.request("POST", self.endpoint + "/build", json={"options": {}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), state)
        self.assertEqual(sync.call_args.args[-1], state)


class ServiceClientTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_override_is_per_request_and_default_is_unchanged(self):
        client = ServiceClient("http://engine", timeout=120, stream_timeout=1800)
        configured = []

        class FakeHttpClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def request(self, *_args, **_kwargs):
                return httpx.Response(200, json={"status": "ok"})

        def create_client(**kwargs):
            configured.append(kwargs["timeout"])
            return FakeHttpClient()

        with patch("backend.service_client.httpx.AsyncClient", side_effect=create_client):
            await client.request("GET", "/status", headers={})
            await client.request("POST", "/construction/dataset-1/build", headers={}, timeout=930)
            await client.request("GET", "/status", headers={})
        self.assertEqual(configured, [120, 930, 120])
        self.assertEqual(client.timeout, 120)


if __name__ == "__main__":
    unittest.main()

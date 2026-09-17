from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from execution_protocol import execution_headers
from graph_data_service.tool_catalog import get_all_public_tools
from graph_engine.construction_catalog import CONSTRUCTION_TOOL_NAMES
from mcp_service.construction_tools import construction_request
from mcp_service.graph_data_client import GraphDataClient


class ConstructionContractTest(unittest.TestCase):
    def test_catalog_has_no_source_path_or_build_tool(self):
        catalog = {spec["name"]: spec for spec in get_all_public_tools()}
        self.assertTrue(CONSTRUCTION_TOOL_NAMES <= catalog.keys())
        self.assertEqual(len(CONSTRUCTION_TOOL_NAMES), 6)
        for name in CONSTRUCTION_TOOL_NAMES:
            self.assertNotIn("source_path", catalog[name]["inputSchema"]["properties"])
            self.assertNotIn("user_id", catalog[name]["inputSchema"]["properties"])
            self.assertNotIn("build", name)

    def test_preview_and_commit_use_identical_revision_and_operations(self):
        operations = [{"op": "reverse_edges", "edge_ids": ["e1"]}]
        args = {"dataset_id": "dataset-1", "base_revision": 3, "operations": operations}
        preview = construction_request("preview_graph_optimization", "s1", args)
        commit = construction_request("commit_graph_optimization", "s1", args)
        self.assertEqual(preview, ("POST", "/construction/dataset-1/preview", {"base_revision": 3, "operations": operations}))
        self.assertEqual(commit[1], "/construction/dataset-1/commit")
        self.assertEqual(preview[2], commit[2])
        self.assertIs(args["operations"], operations)

    def test_agno_json_operation_arrays_are_decoded(self):
        operations = [{"op": "delete", "target": "edge", "ids": ["e1"]}]
        _, _, body = construction_request("preview_graph_optimization", "s1", {
            "dataset_id": "d1", "base_revision": 1, "operations": json.dumps(operations),
        })
        self.assertEqual(body["operations"], operations)

    def test_rejects_source_path_and_identity_spoofing(self):
        for extra in ({"source_path": "/data/other.csv"}, {"user_id": "someone-else"}, {"session_id": "other"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                construction_request("get_graph_construction_status", "s1", {"dataset_id": "d1", **extra})

    def test_rejects_invalid_revisions_and_operations(self):
        for revision in (None, True, 0, -1, "3"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                construction_request("commit_graph_optimization", "s1", {
                    "dataset_id": "d1", "base_revision": revision, "operations": [{"op": "reverse_edges"}],
                })
        for operations in (None, [], ["delete"], "not-json", '{}'):
            with self.subTest(operations=operations), self.assertRaises(ValueError):
                construction_request("commit_graph_optimization", "s1", {
                    "dataset_id": "d1", "base_revision": 1, "operations": operations,
                })

    def test_loading_uses_current_session_and_never_rebuilds(self):
        self.assertEqual(construction_request("load_constructed_graph", "s1", {"dataset_id": "d1"}),
                         ("POST", "/sessions/s1/graph/load_construction", {"dataset_id": "d1"}))


class ConstructionForwardingTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_construction_routes_preserve_trusted_identity_and_restore_scope(self):
        client = GraphDataClient("http://engine")
        observed = []

        async def request(method, path, body, **kwargs):
            observed.append((method, path, body, kwargs, execution_headers.get().copy()))
            return {"success": True, "status": "ready", "revision": 4}

        existing = {"X-Execution-Generation": "7", "X-Operation-ID": "operation-1"}
        token = execution_headers.set(existing)
        try:
            with patch.dict("os.environ", {"IGRAPH_CONTROL_SERVICE_KEY": "test-only-key", "IGRAPH_RUN_CANCEL_ENABLED": "false"}), \
                 patch.object(client, "_request", side_effect=request):
                result = await client.call_tool("session-1", "load_constructed_graph", {"dataset_id": "d1"},
                                                user_id="user-1", run_id="run-1")
            self.assertEqual(result["revision"], 4)
            self.assertEqual(execution_headers.get(), existing)
        finally:
            execution_headers.reset(token)
        _, path, body, kwargs, headers = observed[0]
        self.assertEqual(path, "/sessions/session-1/graph/load_construction")
        self.assertEqual(body, {"dataset_id": "d1"})
        self.assertEqual(kwargs["user_id"], "user-1")
        self.assertEqual(kwargs["run_id"], "run-1")
        self.assertFalse(kwargs["retry_errors"])
        self.assertEqual(headers, {**existing, "X-iGraph-Service-Key": "test-only-key", "X-User-ID": "user-1", "X-Session-ID": "session-1"})

    async def test_missing_identity_never_reaches_engine(self):
        client = GraphDataClient("http://engine")
        with patch.object(client, "_request", new_callable=AsyncMock) as request:
            result = await client.call_tool("s1", "get_graph_construction_status", {"dataset_id": "d1"})
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "USER_ID_REQUIRED")
        request.assert_not_awaited()

    async def test_raw_sources_cannot_use_legacy_upload_ingestion(self):
        client = GraphDataClient("http://engine")
        with patch.object(client, "_post", new_callable=AsyncMock) as post:
            for filename in ("source.sql", "edges.CSV", "text.txt", "C:\\uploads\\note.TXT"):
                with self.subTest(filename=filename):
                    result = await client.call_tool("s1", "load_graph_from_upload", {"filename": filename}, user_id="u1")
                    self.assertEqual(result["code"], "CONSTRUCTION_REQUIRED")
            post.assert_not_awaited()
            await client.call_tool("s1", "load_graph_from_upload", {"filename": "graph.json"}, user_id="u1")
            post.assert_awaited_once()

    async def test_structured_revision_conflict_is_preserved(self):
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return None
            async def request(self, method, url, **kwargs):
                return httpx.Response(409, json={"detail": {
                    "code": "REVISION_CONFLICT", "revision": 5, "message": "stale revision",
                }})

        with patch("mcp_service.graph_data_client.httpx.AsyncClient", return_value=Client()):
            result = await GraphDataClient("http://engine").call_tool("s1", "commit_graph_optimization", {
                "dataset_id": "d1", "base_revision": 4, "operations": [{"op": "reverse_edges", "edge_ids": ["e1"]}],
            }, user_id="u1")
        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 409)
        self.assertEqual(result["code"], "REVISION_CONFLICT")
        self.assertEqual(result["revision"], 5)

    async def test_commit_failure_is_not_automatically_replayed(self):
        request = AsyncMock(return_value=httpx.Response(503, json={"detail": {"code": "UNAVAILABLE"}}))

        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return None

        client = Client()
        client.request = request
        with patch("mcp_service.graph_data_client.httpx.AsyncClient", return_value=client):
            result = await GraphDataClient("http://engine").call_tool("s1", "commit_graph_optimization", {
                "dataset_id": "d1", "base_revision": 1, "operations": [{"op": "delete", "target": "edge", "ids": ["e1"]}],
            }, user_id="u1")
        self.assertFalse(result["success"])
        request.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

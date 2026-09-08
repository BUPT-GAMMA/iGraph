"""Legacy HTTP keeps its data semantics while clients receive a migration path."""
import asyncio

import pytest

from test_projects import FakeServiceClient, client, headers
from test_construction_api import engine


@pytest.mark.parametrize("method,path,body,target", [
    ("GET", "/api/datasets", None, "/datasets"),
    ("DELETE", "/api/datasets/ds_review", None, "/datasets/ds_review"),
    ("POST", "/api/sessions/legacy-session/load-dataset", {"dataset_id": "ds_review", "version": "v001"},
     "/sessions/legacy-session/tools/load_dataset"),
    ("POST", "/api/sessions/legacy-session/save-dataset", {"name": "Keep my graph"},
     "/sessions/legacy-session/tools/save_as_dataset"),
])
def test_legacy_registry_http_preserves_payloads_and_forwards_same_operation(client, monkeypatch,
                                                                           method, path, body, target):
    expected = {"success": True, "dataset_id": "ds_review", "versions": ["v001", "v002"]}
    if method == "GET":
        expected = {"datasets": [dict(expected, scope="global")]}
    calls = []
    original = FakeServiceClient.request

    async def request(self, request_method, request_path, **kwargs):
        if not self.is_runtime and request_method == method and request_path == target:
            calls.append(kwargs)
            return expected
        return await original(self, request_method, request_path, **kwargs)

    monkeypatch.setattr(FakeServiceClient, "request", request)
    result = client.request(method, path, headers=headers(), json=body)
    assert result.status_code == 200
    assert result.json() == expected
    assert len(calls) == 1
    if body is not None:
        assert calls[0]["json_body"] == {"arguments": body}
    assert result.headers["X-iGraph-API-Deprecated"] == "true"
    guide = client.get(result.headers["X-iGraph-Migration-Guide"], headers=headers())
    assert guide.status_code == 200
    assert guide.json()["removal_scheduled"] is False
    assert guide.json()["registry_migration"]["automatic_redirect"] is False
    assert guide.json()["agent_tools"]["status"] == "supported"


def test_personal_construction_alias_keeps_identity_and_marks_only_old_path(client, engine):
    dataset = client.post("/api/personal-datasets", headers=headers(),
                          files={"file": ("source.csv", b"a,b")}).json()["dataset"]
    did = dataset["dataset_id"]
    old = client.get(f"/api/datasets/{did}/construction", headers=headers())
    current = client.get(f"/api/personal-datasets/{did}/construction", headers=headers())
    assert old.status_code == current.status_code == 200
    assert old.json() == current.json()
    assert old.headers["X-iGraph-API-Deprecated"] == "true"
    assert "X-iGraph-API-Deprecated" not in current.headers
    assert "X-iGraph-API-Deprecated" not in client.get("/api/personal-datasets", headers=headers()).headers
    assert client.get(f"/api/datasets/{did}/construction", headers=headers("alice")).status_code == 404


def test_api_schema_distinguishes_legacy_routes_and_current_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert paths["/api/datasets"]["get"]["deprecated"] is True
    assert paths["/api/sessions/{session_id}/load-dataset"]["post"]["deprecated"] is True
    for suffix, method in [("", "get"), ("/build", "post"), ("/graph", "get"),
                           ("/preview", "post"), ("/commit", "post")]:
        assert paths["/api/datasets/{dataset_id}/construction" + suffix][method]["deprecated"] is True
        assert paths["/api/personal-datasets/{dataset_id}/construction" + suffix][method].get("deprecated", False) is False
    assert paths["/api/personal-datasets"]["get"].get("deprecated", False) is False


def test_agent_dataset_tools_remain_available_with_cancellation_disabled(monkeypatch):
    from fastmcp import Client

    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_PORT", "21110")
    monkeypatch.setenv("GRAPH_DATA_SERVER_URL", "http://127.0.0.1:21111")
    from mcp_service.server import mcp

    async def list_tools():
        async with Client(mcp) as tool_client:
            return {tool.name for tool in await tool_client.list_tools()}

    names = asyncio.run(list_tools())
    assert {"list_datasets", "load_dataset", "save_as_dataset", "load_constructed_graph"} <= names

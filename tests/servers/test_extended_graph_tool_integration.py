"""Service, worker and MCP contracts for the added tools (Linux runtime)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from graph_data_service.app import GraphDataService
from graph_data_service.compute_worker import compute
from graph_engine.extended_tools import EXTENDED_TOOL_NAMES


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("GRAPH_ML_ARTIFACT_ROOT", str(tmp_path / "models"))
    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    instance = GraphDataService(storage_backend="local", migrate_storage=False)
    yield instance
    instance._process_pool.shutdown(wait=True, cancel_futures=True)
    instance._nebula.close()


def test_service_dispatch_reaches_new_compute_tools(service):
    session = service.get_or_create("extensions", user_id="alice")
    service.do_load_graph_text("extensions", "A B\nB C\nC A", False, user_id="alice")
    before = session.graph_version
    result = asyncio.run(service.dispatch_tool("extensions", "triangle_count", {"treat_as_undirected": True}, user_id="alice"))
    assert result["success"] and result["triangle_count"] == 1
    assert session.graph_version == before
    rows = asyncio.run(service.dispatch_tool("extensions", "list_graph_nodes", {"limit": 2}, user_id="alice"))
    assert rows["success"] and rows["total"] == 3 and rows["truncated"]


@pytest.mark.parametrize("remote,nodes,expected", [
    (True, 3, "EXTENDED_TOOL_LOCAL_GRAPH_REQUIRED"),
    (False, 201, "EXTENDED_TOOL_GRAPH_TOO_LARGE"),
])
def test_guard_rejects_before_materializing_a_graph(service, monkeypatch, remote, nodes, expected):
    fake = SimpleNamespace(is_remote=remote, graph_summary=lambda: SimpleNamespace(num_nodes=nodes, num_edges=3))
    monkeypatch.setattr(service, "get_required", lambda *args, **kwargs: fake)
    materialize = Mock(side_effect=AssertionError("must not materialize"))
    monkeypatch.setattr(service, "_require_graph", materialize)
    result = asyncio.run(service._dispatch_tool_unlocked("s", "minimum_cycle_basis", {}))
    assert result["code"] == expected
    materialize.assert_not_called()


def test_worker_allowlist_executes_registered_extension(tmp_path):
    graph = GraphDataService._graph_from_edgelist("A B\nB C\nC A")
    path = tmp_path / "graph.npz"
    graph.save(str(path))
    result = compute({"tool": "triangle_count", "graph_path": str(path),
                      "arguments": {"treat_as_undirected": True}, "session_id": "s",
                      "user_id": "alice", "graph_version": 1})
    assert result["success"] and result["triangle_count"] == 1


def test_expanded_registry_preserves_unique_names():
    from graph_data_service.tool_catalog import get_all_public_tools
    tools = get_all_public_tools(include_reporting=True, include_graph_ml=True)
    names = [tool["name"] for tool in tools]
    assert len(names) == len(set(names))
    assert len(names) >= 111
    assert EXTENDED_TOOL_NAMES <= set(names)

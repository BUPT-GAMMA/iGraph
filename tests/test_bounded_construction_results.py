import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

from mcp_service.bounded_results import bounded_construction_result, CONSTRUCTION_RESULT_MAX_BYTES
from mcp_service.graph_data_client import GraphDataClient


def test_large_properties_are_bounded_and_original_graph_is_unchanged():
    original = {"dataset_id": "d", "revision": 7, "graph": {"directed": True,
        "nodes": [{"id": str(i), "type": "entity", "properties": {"description": "股权" * 50000}}
                  for i in range(25)],
        "edges": [{"id": "e", "source": "0", "target": "1", "type": "holding", "properties": {}}]}}
    before = deepcopy(original)
    result = bounded_construction_result("get_constructed_graph", original)
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= CONSTRUCTION_RESULT_MAX_BYTES
    assert result["inspection"]["node_count"] == 25
    assert result["inspection"]["edge_count"] == 1
    assert result["inspection"]["complete_graph"] is False
    assert result["revision"] == 7
    assert original == before


def test_small_graph_keeps_exact_properties_and_nonconstruction_tools_unchanged():
    payload = {"revision": 1, "graph": {"nodes": [{"id": "a", "properties": {"ratio": "57.1429%"}}], "edges": []}}
    assert bounded_construction_result("get_constructed_graph", payload) == payload
    large = {"graph": {"nodes": [], "edges": []}, "details": "a" * 100000}
    assert bounded_construction_result("finance_economic_interest", large) is large


def test_wide_nested_values_cannot_exceed_byte_budget():
    payload = {"revision": 3, "graph": {"nodes": [{"id": str(i), "properties": {
        str(k): {str(n): "界" * 1000 for n in range(30)} for k in range(30)}} for i in range(20)], "edges": []}}
    result = bounded_construction_result("preview_graph_optimization", payload)
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= CONSTRUCTION_RESULT_MAX_BYTES
    assert result["inspection"]["node_count"] == 20


def test_mcp_construction_adapter_bounds_before_return_and_retains_scope_headers():
    from execution_protocol import execution_headers
    client = GraphDataClient("http://unused")
    observed = {}
    async def request(*args, **kwargs):
        observed.update(execution_headers.get())
        return {"revision": 1, "graph": {"nodes": [{"id": "a", "properties": {"data": "a" * 100000}}], "edges": []}}
    client._request = AsyncMock(side_effect=request)
    result = asyncio.run(client.call_construction_tool("session", "get_constructed_graph", {"dataset_id": "dataset"}, user_id="owner"))
    assert result["inspection"]["truncated"] is True
    assert observed["X-User-ID"] == "owner"
    assert observed["X-Session-ID"] == "session"
    assert execution_headers.get() == {}


def test_bounded_samples_preserve_long_element_ids_for_followup_edits():
    ids = ["same-prefix-" + "x" * 280 + suffix for suffix in ("A", "B")]
    edge_id = "relation-" + "e" * 300
    payload = {"revision": 7, "graph": {"directed": True,
        "nodes": [{"id": identity, "type": "Entity", "properties": {"description": "x" * 100000}} for identity in ids],
        "edges": [{"id": edge_id, "source": ids[0], "target": ids[1], "type": "Related", "properties": {}}]}}
    result = bounded_construction_result("get_constructed_graph", payload)
    assert result["inspection"]["truncated"]
    assert [node["id"] for node in result["graph"]["nodes"]] == ids
    assert result["graph"]["edges"][0] == payload["graph"]["edges"][0]
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= CONSTRUCTION_RESULT_MAX_BYTES

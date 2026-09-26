"""Finance tools through the existing HTTP, worker and native MCP boundaries."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from execution_protocol import service_headers
from graph_data_service.app import GraphDataService, create_app
from graph_data_service.construction import to_graph_data
from graph_engine.finance_catalog import FINANCE_MAX_RESULT_BYTES, FINANCE_TOOL_NAMES
from tests.fixtures.finance_ownership import ownership_document


@pytest.fixture
def finance_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    monkeypatch.setenv("NEBULA_ENABLED", "false")
    service = GraphDataService(storage_backend="local", migrate_storage=False)
    for user, sid, prefix in (("alice", "same", ""), ("bob", "same", "bob-"), ("alice", "other", "other-")):
        session = service.get_or_create(sid, user_id=user)
        session.set_graph(to_graph_data(ownership_document(prefix)), description="finance fixture")
    client = TestClient(create_app(service))
    try:
        yield service, client
    finally:
        client.close()
        service._process_pool.shutdown(wait=True, cancel_futures=True)
        service._large_graph.close()
        service._nebula.close()


def call(client, name, args, *, sid="same", user="alice"):
    return client.post(f"/sessions/{sid}/tools/{name}",
                       headers=service_headers(user), json={"arguments": args})


def test_http_discovery_and_analysis_preserve_selected_graph(finance_engine):
    service, client = finance_engine
    session = service.get_required("same", user_id="alice")
    version = session.graph_version
    original = deepcopy(session.get_graph().graph_metadata)
    available = {s["name"] for s in client.get("/tools?include_graph_ml=false").json()}
    assert FINANCE_TOOL_NAMES <= available
    requests = {
        "finance_inspect_ownership": {"query": "T公司"},
        "finance_first_holder": {"target_id": "T"},
        "finance_control_chain": {"target_id": "T"},
        "finance_group_tree": {"root_id": "R"},
        "finance_economic_interest": {"target_id": "T", "root_id": "R"},
    }
    for name, arguments in requests.items():
        response = call(client, name, arguments)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["success"] and result["complete"], result
        assert result["graph_version"] == version
        assert len(response.content) <= FINANCE_MAX_RESULT_BYTES + 100
    assert session.graph_version == version
    assert session.get_graph().graph_metadata == original


def test_http_cannot_select_another_users_or_sessions_graph(finance_engine):
    _, client = finance_engine
    own = call(client, "finance_first_holder", {"target_id": "bob-T"}, user="bob")
    assert own.json()["result"]["first_holder_id"] == "bob-A"
    for user, sid in (("bob", "same"), ("alice", "other")):
        missing = call(client, "finance_first_holder", {"target_id": "T"}, user=user, sid=sid)
        assert missing.json()["status"] == "NOT_FOUND"
    for user in ("unknown", ""):
        assert call(client, "finance_inspect_ownership", {}, user=user).status_code == 404
    for selector in ("session_id", "dataset_id", "user_id", "construction_graph"):
        response = call(client, "finance_inspect_ownership", {selector: "other"})
        assert response.json()["status"] == "INVALID_ARGUMENT"
        assert "entities" not in response.json()


def test_http_bounds_are_checked_without_mcp(finance_engine):
    _, client = finance_engine
    for args in ({"display_limit": 1000000}, {"evidence_top_k": 1000000}, {"policy": {"invented": "ignored?"}}):
        result = call(client, "finance_inspect_ownership", args).json()
        assert result["status"] == "INVALID_ARGUMENT" and not result["complete"]
    limited = call(client, "finance_first_holder", {"target_id": "T", "max_nodes": 1}).json()
    assert limited["status"] == "TRUNCATED" and not limited["complete"]


def test_finance_runs_in_existing_readonly_worker(tmp_path, monkeypatch):
    from graph_data_service.compute_worker import compute
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path / "original"))
    monkeypatch.setenv("GRAPH_ML_ARTIFACT_ROOT", str(tmp_path / "original-artifacts"))
    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    monkeypatch.setenv("NEBULA_ENABLED", "false")
    graph_path = tmp_path / "input.npz"
    to_graph_data(ownership_document()).save(str(graph_path))
    before = graph_path.read_bytes()
    result = compute({
        "tool": "finance_first_holder", "arguments": {"target_id": "T"},
        "session_id": "worker-finance", "user_id": "alice", "graph_version": 1,
        "graph_path": str(graph_path), "artifact_root": str(tmp_path / "artifacts"),
        "staging_root": str(tmp_path / "stage"), "uploads": {},
    })
    assert result["result"]["first_holder_id"] == "A"
    assert result["graph_version"] == 1
    assert not any(k.startswith("_worker_") for k in result)
    assert graph_path.read_bytes() == before


def test_native_mcp_accepts_object_and_provider_encoded_policy():
    from fastmcp import Client, FastMCP
    from graph_data_service.tool_catalog import get_all_public_tools
    from mcp_service.tool_forwarding import forwarded_graph_tool_arguments
    with patch.dict(os.environ, {"MCP_HOST": "127.0.0.1", "MCP_PORT": "19110", "GRAPH_DATA_SERVER_URL": "http://127.0.0.1:19111"}):
        from mcp_service.server import _build_handler_with_signature, mcp

    async def exercise():
        registered = {t.name: t.parameters for t in await mcp.list_tools(run_middleware=False)}
        specs = {s["name"]: s for s in get_all_public_tools() if s["name"] in FINANCE_TOOL_NAMES}
        for name, spec in specs.items():
            assert registered[name] == spec["inputSchema"]
        forwarded = []

        async def invoke(name, _ctx, args):
            forwarded.append(forwarded_graph_tool_arguments(name, args))
            return json.dumps(forwarded[-1])

        server = FastMCP("finance-policy-boundary")
        spec = specs["finance_first_holder"]
        server.tool()(_build_handler_with_signature(spec["name"], spec["inputSchema"], invoke))
        async with Client(server) as client:
            for policy in ({"stop_statuses": ["注销"]}, '{"stop_statuses":["注销"]}'):
                result = await client.call_tool(spec["name"], {"target_id": "T", "policy": policy})
                assert not result.is_error
                assert forwarded[-1]["policy"] == {"stop_statuses": ["注销"]}
            for invalid in ("[]", "broken", {"unknown_rule": True}):
                result = await client.call_tool(spec["name"], {"target_id": "T", "policy": invalid}, raise_on_error=False)
                assert result.is_error
        assert len(forwarded) == 2

    asyncio.run(exercise())


def _independent_acyclic_path_oracle(document, root_id, target_id):
    """Enumerate this fixture's complete positive-share paths, without finance.py.

    This oracle intentionally rejects relevant cycles: it is not an alternate
    SCC solver and must not silently call simple-path sums total walk interest.
    Generated construction edges and the production projection are unused.
    """
    outgoing, incoming, seen = {}, {}, set()
    with localcontext() as context:
        context.prec = 50
        for node in document["graph"]["nodes"]:
            if node["type"] != "institution_shareholder_relationship":
                continue
            data = node["properties"]["data"]
            holder, target = data["REL_SHRHLR_CUST_ID"], data["UUID"]
            key = (holder, target, data["STATC_DT"])
            assert key not in seen, "Real-data oracle requires unique source facts"
            seen.add(key)
            raw = data["REL_SHRHLR_HOLD_SHR_RTO"]
            assert raw.endswith("%")
            ratio = Decimal(raw[:-1]) / Decimal(100)
            assert ratio.is_finite() and 0 <= ratio <= 1
            if ratio == 0:
                continue
            edge = {"holder_id": holder, "target_id": target,
                    "source_node_id": node["id"], "snapshot_id": data["STATC_DT"],
                    "ratio_raw": raw, "ratio": format(ratio, "f")}
            outgoing.setdefault(holder, []).append(edge)
            incoming.setdefault(target, []).append(holder)

        ancestors, pending = {target_id}, [target_id]
        while pending:
            for holder in incoming.get(pending.pop(), []):
                if holder not in ancestors:
                    ancestors.add(holder)
                    pending.append(holder)
        assert not any(edge["target_id"] in ancestors for edge in outgoing.get(target_id, [])), (
            "Target participates in a cycle; a separately verified SCC oracle is required"
        )

        paths = []

        def visit(node, nodes, edges, product):
            if node == target_id:
                paths.append({"business_id_path": nodes, "source_facts": edges,
                              "product": format(product, "f")})
                return
            for edge in sorted(outgoing.get(node, []), key=lambda row: (row["target_id"], row["source_node_id"])):
                next_node = edge["target_id"]
                if next_node not in ancestors:
                    continue
                assert next_node not in nodes, "Relevant cycle requires a separately verified SCC oracle"
                visit(next_node, nodes + [next_node], edges + [edge], product * Decimal(edge["ratio"]))

        visit(root_id, [root_id], [], Decimal(1))
        total = sum((Decimal(path["product"]) for path in paths), Decimal(0))
        return {"method": "independent Decimal DFS over original ownership rows; relevant region acyclic",
                "policy": "all positive shares; no status or small-ratio filtering",
                "root_id": root_id, "target_id": target_id, "path_count": len(paths),
                "total_interest": format(total, "f"), "paths": paths}


@pytest.mark.skipif(not os.getenv("FINANCE_REAL_GRAPH_PATH"), reason="requires the saved server finance construction response")
def test_actual_finance_graph_through_session_dispatch(finance_engine, tmp_path):
    """Opt-in server acceptance; no fixture data is downloaded by this test."""
    service, client = finance_engine
    document = json.loads(Path(os.environ["FINANCE_REAL_GRAPH_PATH"]).read_text(encoding="utf-8"))
    oracle = _independent_acyclic_path_oracle(document, "SYN-ENT-000001", "SYN-ENT-000044")
    output_path = Path(os.environ["FINANCE_VALIDATION_OUTPUT"]) if os.getenv("FINANCE_VALIDATION_OUTPUT") else None
    oracle_path = output_path.with_suffix(".economic-oracle.json") if output_path else tmp_path / "finance-economic-oracle.json"
    oracle_path.write_text(json.dumps(oracle, ensure_ascii=False, indent=2), encoding="utf-8")
    assert oracle["path_count"] > 1
    control_path = [path for path in oracle["paths"] if path["business_id_path"] == [
        "SYN-ENT-000001", "SYN-ENT-000040", "SYN-ENT-000044",
    ]]
    assert len(control_path) == 1
    assert Decimal(control_path[0]["product"]) == Decimal("0.3428574")
    assert Decimal(oracle["total_interest"]) == Decimal("0.348494958768")
    gd = to_graph_data(document)
    session = service.get_required("same", user_id="alice")
    session.set_graph(gd, description="server finance acceptance")
    version = session.graph_version

    def digest():
        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True)
        result = hashlib.sha256()
        for part in encoder.iterencode(gd.graph_metadata):
            result.update(part.encode())
        return result.hexdigest()

    before = digest()
    requests = {
        "finance_inspect_ownership": {"display_limit": 20},
        "finance_first_holder": {"target_id": "SYN-ENT-000044"},
        "finance_control_chain": {"target_id": "SYN-ENT-000044"},
        "finance_group_tree": {"root_id": "SYN-ENT-000001"},
        "finance_economic_interest": {"target_id": "SYN-ENT-000044", "root_id": "SYN-ENT-000001"},
    }
    results = {}
    for name, arguments in requests.items():
        response = call(client, name, arguments)
        assert response.status_code == 200, response.text[:500]
        result = response.json()
        assert result["success"] and result["complete"], result
        assert result["graph_version"] == version
        assert len(response.content) <= FINANCE_MAX_RESULT_BYTES + 100
        results[name] = result
    projection = results["finance_inspect_ownership"]["result"]
    assert (projection["entity_count"], projection["ownership_fact_count"]) == (6000, 13436)
    assert results["finance_first_holder"]["result"]["first_holder_id"] == "SYN-ENT-000040"
    assert results["finance_control_chain"]["result"]["ultimate_controller_ids"] == ["SYN-ENT-000001"]
    assert results["finance_group_tree"]["result"]["member_count"] == 3
    assert Decimal(results["finance_economic_interest"]["result"]["requested_interest"]["interest"]) == Decimal(oracle["total_interest"])
    assert session.graph_version == version and digest() == before
    if output_path:
        output_path.write_text(json.dumps({
            "graph_version": version, "unchanged_graph_sha256": before, "results": results,
            "independent_economic_oracle": oracle,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

"""Finance tool discovery, argument bounds and pure computation boundary."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from graph_engine.finance_catalog import (
    FINANCE_MAX_RESULT_BYTES, FINANCE_TOOL_NAMES, FINANCE_TOOLS, validate_finance_arguments,
)
from graph_engine.tool_execution import execution_policy, tools_conflict
from graph_data_service.compute import GraphCompute
from graph_data_service.construction import to_graph_data
from graph_data_service.graph_data import GraphData
from graph_data_service.tool_catalog import get_all_public_tools
from graph_data_service.worker_contract import ISOLATED_DATA_TOOLS
from mcp_service.tool_forwarding import forwarded_graph_tool_arguments
from tests.fixtures.finance_ownership import ownership_document


def test_finance_tools_discoverable_readonly_and_isolated():
    public = {s["name"]: s for s in get_all_public_tools(include_graph_ml=False, include_reporting=False)}
    assert len(FINANCE_TOOL_NAMES) == 5
    assert FINANCE_TOOL_NAMES <= public.keys()
    assert FINANCE_TOOL_NAMES <= ISOLATED_DATA_TOOLS
    for name in FINANCE_TOOL_NAMES:
        assert public[name]["category"] == "compute"
        assert public[name]["inputSchema"]["additionalProperties"] is False
        policy = execution_policy(name)
        assert policy.read_only and policy.reads == {"graph"}
        assert not tools_conflict(policy, execution_policy("get_graph_info"))
        assert tools_conflict(policy, execution_policy("load_graph"))


@pytest.mark.parametrize("bad", [
    {"session_id": "other"}, {"dataset_id": "other"}, {"graph": {}},
    {"max_nodes": 0}, {"max_nodes": 200001}, {"max_nodes": True},
    {"max_edges": 2000001}, {"display_limit": 0}, {"display_limit": 501},
    {"evidence_top_k": 31}, {"max_seconds": float("nan")},
    {"max_seconds": 61}, {"max_seconds": 10 ** 400},
    {"policy": "not JSON"}, {"policy": {"invented_rule": True}},
    {"policy": {"stop_entity_ids": ["x"] * 101}},
    {"policy": {"min_edge_ratio": "1.01"}},
    {"policy": {"rule_priority": ["GP", "GP"]}},
    {"query": "x" * 201},
])
def test_direct_and_mcp_calls_reject_invalid_arguments(bad):
    with pytest.raises(ValueError):
        validate_finance_arguments("finance_inspect_ownership", bad)
    with pytest.raises(ValueError):
        forwarded_graph_tool_arguments("finance_inspect_ownership", bad)
    result = GraphCompute.finance_analysis(None, "finance_inspect_ownership", bad)
    assert result["status"] == "INVALID_ARGUMENT" and not result["complete"]


def test_policy_json_normalization_is_strict_and_preserves_caller_arguments():
    policy = {"stop_statuses": ["注销"], "version": "test-v1"}
    for value in (policy, json.dumps(policy)):
        original = {"target_id": "T", "policy": value}
        before = deepcopy(original)
        actual = forwarded_graph_tool_arguments("finance_first_holder", original)
        assert actual["policy"] == policy
        assert actual["max_seconds"] == 10
        assert original == before
    for value in ("[]", "null", "{broken"):
        with pytest.raises(ValueError):
            forwarded_graph_tool_arguments("finance_first_holder", {"target_id": "T", "policy": value})


def test_required_business_ids_and_no_graph_fallback():
    for name in FINANCE_TOOL_NAMES - {"finance_inspect_ownership"}:
        with pytest.raises(ValueError):
            validate_finance_arguments(name, {})
    for graph in (None, GraphData()):
        result = GraphCompute.finance_analysis(graph, "finance_inspect_ownership", {})
        assert result["status"] == "FINANCE_SCHEMA_REQUIRED"
        assert result["success"] is False


@pytest.mark.parametrize("tool_name,field", [
    ("finance_inspect_ownership", "target_id"),
    ("finance_first_holder", "target_id"),
    ("finance_control_chain", "target_id"),
    ("finance_group_tree", "root_id"),
    ("finance_economic_interest", "target_id"),
    ("finance_economic_interest", "root_id"),
])
def test_returned_typed_ids_fit_every_tool_contract(tool_name, field):
    entity_id = "I:" + "x" * 256
    spec = next(spec for spec in FINANCE_TOOLS if spec["name"] == tool_name)
    assert spec["inputSchema"]["properties"][field]["maxLength"] == 258
    arguments = {key: "T" for key in spec["inputSchema"]["required"]}
    arguments[field] = entity_id
    assert validate_finance_arguments(tool_name, arguments)[field] == entity_id
    assert forwarded_graph_tool_arguments(tool_name, arguments)[field] == entity_id
    with pytest.raises(ValueError):
        validate_finance_arguments(tool_name, {**arguments, field: entity_id + "x"})


def test_policy_entity_selectors_accept_the_same_typed_id_boundary():
    entity_id = "P:" + "x" * 256
    relation = {"type": "GP", "target_id": entity_id, "holder_id": entity_id,
                "snapshot_id": "2026-09-23", "authority": "reviewed-source", "evidence_refs": ["record-1"]}
    policy = {"mode": "configured_rules", "stop_entity_ids": [entity_id],
              "exclude_entity_ids": [entity_id], "rule_relations": [relation]}
    arguments = {"target_id": entity_id, "policy": policy}
    assert validate_finance_arguments("finance_first_holder", arguments)["policy"] == policy
    assert forwarded_graph_tool_arguments("finance_first_holder", {
        **arguments, "policy": json.dumps(policy),
    })["policy"] == policy
    for field in ("stop_entity_ids", "exclude_entity_ids"):
        with pytest.raises(ValueError):
            validate_finance_arguments("finance_first_holder", {
                **arguments, "policy": {**policy, field: [entity_id + "x"]},
            })
    for field in ("target_id", "holder_id"):
        with pytest.raises(ValueError):
            validate_finance_arguments("finance_first_holder", {
                **arguments, "policy": {**policy, "rule_relations": [{**relation, field: entity_id + "x"}]},
            })


def test_five_tools_use_original_records_without_mutation_or_networkx(monkeypatch):
    gd = to_graph_data(ownership_document())
    original = deepcopy(gd.graph_metadata)
    monkeypatch.setattr(gd, "to_networkx", lambda: pytest.fail("finance must not materialize generic topology"))
    requests = {
        "finance_inspect_ownership": {"query": "T公司"},
        "finance_first_holder": {"target_id": "T"},
        "finance_control_chain": {"target_id": "T"},
        "finance_group_tree": {"root_id": "R"},
        "finance_economic_interest": {"target_id": "T"},
    }
    results = {}
    for name, arguments in requests.items():
        result = GraphCompute.finance_analysis(gd, name, arguments)
        assert result["success"], result
        assert result["complete"], result
        assert result["rule_status"] == "RULE_DATA_MISSING"
        assert result["graph_source"] == {"dataset_id": "owned-dataset", "construction_revision": 7}
        assert len(json.dumps(result, ensure_ascii=False).encode()) <= FINANCE_MAX_RESULT_BYTES
        results[name] = result["result"]
    assert results["finance_inspect_ownership"]["matched_entity_count"] == 1
    assert results["finance_first_holder"]["first_holder_id"] == "A"
    assert results["finance_control_chain"]["ultimate_controller_ids"] == ["R"]
    assert results["finance_group_tree"]["member_count"] == 3
    assert results["finance_economic_interest"]["root_interests"][0]["business_id"] == "R"
    assert gd.graph_metadata == original


def test_byte_limit_does_not_publish_a_truncated_confirmation(monkeypatch):
    from graph_data_service import finance
    gd = to_graph_data(ownership_document())
    monkeypatch.setattr(finance, "analyze_finance", lambda *_a, **_kw: {
        "success": True, "complete": True, "result": {"confirmed_id": "A", "detail": "股" * FINANCE_MAX_RESULT_BYTES},
    })
    result = GraphCompute.finance_analysis(gd, "finance_first_holder", {"target_id": "T"})
    assert result["status"] == "FINANCE_RESULT_TOO_LARGE"
    assert not result["success"] and not result["complete"]
    assert "confirmed_id" not in json.dumps(result)


def test_budget_and_missing_target_remain_explicit():
    gd = to_graph_data(ownership_document())
    limited = GraphCompute.finance_analysis(gd, "finance_first_holder", {"target_id": "T", "max_nodes": 1})
    assert limited["status"] == "TRUNCATED" and not limited["complete"]
    missing = GraphCompute.finance_analysis(gd, "finance_first_holder", {"target_id": "other"})
    assert missing["status"] == "NOT_FOUND"

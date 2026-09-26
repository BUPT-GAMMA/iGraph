"""Optional acceptance against the supplied three original CSVs (not fixtures)."""
import csv
from decimal import Decimal
import os
from pathlib import Path

import pytest

from graph_data_service.finance import analyze_finance

SOURCE = Path(os.getenv("IGRAPH_FINANCE_TEST_DATA", "F:/mypaper/data/finance/finance_csv"))


@pytest.fixture(scope="module")
def real_graph():
    tables = ("institution_main", "individual_info", "institution_shareholder_relationship")
    if not all((SOURCE / (table + ".csv")).is_file() for table in tables):
        pytest.skip("Set IGRAPH_FINANCE_TEST_DATA to the supplied three-table directory")
    nodes = []
    for table in tables:
        with (SOURCE / (table + ".csv")).open(encoding="utf-8-sig", newline="") as stream:
            for i, row in enumerate(csv.DictReader(stream), 2):
                nodes.append({"id": table + ":line:" + str(i), "type": table, "properties": {"data": row}})
    return {"nodes": nodes, "edges": []}


def test_real_projection_counts(real_graph):
    r = analyze_finance(real_graph, "snapshot")
    assert r["complete"]
    assert r["result"]["entity_count"] == 6000
    assert r["result"]["ownership_fact_count"] == 13436
    assert r["result"]["scc_count"] == 10
    assert r["quality"]["issue_counts"]["OWNERSHIP_TOTAL_EXCEEDED"] == 13


def test_real_first_holder_control_economics_and_group(real_graph):
    target = "SYN-ENT-000044"
    assert analyze_finance(real_graph, "first_holder", target_id=target)["result"]["first_holder_id"] == "SYN-ENT-000040"
    assert analyze_finance(real_graph, "control_chain", target_id=target)["result"]["ultimate_controller_ids"] == ["SYN-ENT-000001"]
    r = analyze_finance(real_graph, "economic_interest", target_id=target, root_id="SYN-ENT-000001")
    # Include every raw valid route, not only 000001->000040->000044.
    target_id, root_id = "SYN-ENT-000044", "SYN-ENT-000001"
    reverse = {}
    for node in real_graph["nodes"]:
        if node["type"] == "institution_shareholder_relationship":
            d = node["properties"]["data"]
            reverse.setdefault(d["UUID"], []).append((d["REL_SHRHLR_CUST_ID"], Decimal(d["REL_SHRHLR_HOLD_SHR_RTO"][:-1]) / 100))
    stack, independent_sum = [(target_id, Decimal(1), frozenset([target_id]))], Decimal(0)
    while stack:
        node, product, visited = stack.pop()
        if node == root_id:
            independent_sum += product
            continue
        for holder, ratio in reverse.get(node, []):
            if holder not in visited:
                stack.append((holder, product * ratio, visited | {holder}))
    assert Decimal(r["result"]["requested_interest"]["interest"]) == independent_sum
    assert independent_sum > Decimal(".3428574")
    assert analyze_finance(real_graph, "group_tree", root_id="SYN-ENT-000001")["result"]["member_count"] == 3


def test_real_ties_and_strict_half(real_graph):
    r = analyze_finance(real_graph, "first_holder", target_id="SYN-ENT-000013")
    assert r["result"]["first_holder_ids"] == ["SYN-IDV-000350", "SYN-IDV-000685"]
    r = analyze_finance(real_graph, "control_chain", target_id="SYN-ENT-003784")
    assert "SYN-ENT-003710" not in r["result"]["controller_ids"]


def test_real_full_root_accumulation_is_independent_of_evidence(real_graph):
    for k in (0, 1, 5):
        r = analyze_finance(real_graph, "first_holder", target_id="SYN-ENT-000041", evidence_top_k=k)
        assert r["result"]["first_holder_id"] == "SYN-IDV-000692"
        assert Decimal(r["result"]["root_interests"][0]["interest"]) == Decimal("0.0119256519764553408")


@pytest.mark.parametrize("target", ["SYN-ENT-000621", "SYN-ENT-000901"])
def test_real_invalid_totals_do_not_confirm(real_graph, target):
    r = analyze_finance(real_graph, "control_chain", target_id=target)
    assert r["status"] == "DATA_CONFLICT"
    assert r["analysis_status"] == "SCC_REVIEW"
    assert r["control_status"] == "SCC_REVIEW"
    assert "SCC_REVIEW" in r["review_reasons"]
    assert r["result"]["confirmed_id"] is None


def test_real_explicit_cancelled_boundary(real_graph):
    r = analyze_finance(real_graph, "control_chain", target_id="SYN-ENT-000072", policy={"stop_statuses": ["注销"]})
    assert "SYN-ENT-000003" not in r["result"]["ultimate_controller_ids"]


def test_real_requested_upstream_path_and_full_interest_have_matching_scope(real_graph):
    root, target = "SYN-ENT-000001", "SYN-ENT-000044"
    r = analyze_finance(real_graph, "economic_interest", root_id=root, target_id=target, evidence_top_k=1)
    assert r["evidence"]["path_scope"] == "requested_upstream"
    assert r["evidence"]["requested_root_entity_id"] == "I:" + root
    assert all(p["nodes"][0] == root and p["nodes"][-1] == target for p in r["evidence"]["paths"])
    path = r["evidence"]["paths"][0]
    assert path["nodes"] == [root, "SYN-ENT-000040", target]
    assert Decimal(path["product"]) == Decimal(".3428574")
    assert Decimal(r["result"]["requested_interest"]["interest"]) == Decimal(".348494958768")
    assert Decimal(r["result"]["requested_interest"]["interest"]) > Decimal(path["product"])
    assert r["result"]["root_interests_scope"] == "boundary_roots"


def test_real_first_holder_separates_total_ownership_from_single_holder_threshold(real_graph):
    r = analyze_finance(real_graph, "first_holder", target_id="SYN-ENT-000013")
    result = r["result"]
    assert Decimal(result["direct_ownership_total"]) == Decimal(".55")
    assert result["direct_strict_majority_holder_count"] == 0
    assert result["threshold_semantics"] == "per_single_holder"
    assert result["direct_ownership_scope"] == "selected_policy_direct_facts"
    assert result["first_holder_ids"] == ["SYN-IDV-000350", "SYN-IDV-000685"]


@pytest.mark.parametrize("display_limit", [1, 100])
def test_real_group_counts_are_complete_and_distinguish_tree_from_control_dag(real_graph, display_limit):
    r = analyze_finance(real_graph, "group_tree", root_id="SYN-ENT-000001", display_limit=display_limit)
    result = r["result"]
    assert result["node_count"] == result["member_count"] == 3
    assert result["node_count_includes_root"] and result["member_count_includes_root"]
    assert result["controlled_member_count"] == 2
    assert result["tree_edge_count"] == 2
    assert result["additional_edge_count"] == 1
    assert result["control_dag_edge_count"] == result["control_edge_count"] == 3
    assert result["count_scope"] == "complete_computed_scope_before_display_limits"
    if display_limit == 1:
        assert r["completeness"]["display_truncated"]
        assert len(result["members"]) == 1
        assert len(result["tree_edges"]) == 0
    else:
        assert len(result["tree_edges"]) == result["tree_edge_count"]
        assert len(result["additional_edges"]) == result["additional_edge_count"]
        assert len(result["control_dag"]) == result["control_dag_edge_count"]

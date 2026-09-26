"""Financial semantics regression tests; all tests exercise the public entry."""
from __future__ import annotations

from decimal import Decimal
from copy import deepcopy

import pytest

from graph_data_service.finance import analyze_finance


def graph(edges, *, persons=(), statuses=None, extra_institutions=()):
    persons = set(persons) | {"unused-person"}
    institutions = ({x for a, b, _ in edges for x in (a, b)} | set(extra_institutions)) - persons
    nodes = []
    for kind, ids, table, column in (("I", institutions, "institution_main", "UUID"),
                                     ("P", persons, "individual_info", "IDV_ID")):
        for bid in sorted(ids):
            data = {column: bid, "STATC_DT": "2026-09-23", "CHN_NM": "Company " + bid,
                    "IDV_NM": "Person " + bid, "ENTP_OPR_ST": (statuses or {}).get(bid, "存续")}
            nodes.append({"id": "row-" + kind + bid, "type": table, "properties": {"data": data}})
    for i, (a, b, ratio) in enumerate(edges):
        nodes.append({"id": "fact-row-" + str(i), "type": "institution_shareholder_relationship", "properties": {"data": {
            "UUID": b, "REL_SHRHLR_CUST_ID": a, "REL_SHRHLR_TP_CODE": "1" if a in persons else "0",
            "REL_SHRHLR_HOLD_SHR_RTO": str(ratio) + "%", "STATC_DT": "2026-09-23"}}})
    return {"nodes": nodes, "edges": []}


def run(g, operation, **kwargs):
    return analyze_finance(g, operation, **kwargs)


def test_projection_uses_raw_records_and_ignores_shortcuts():
    g = graph([("A", "T", 40)])
    g["nodes"].append({"id": "derived-A", "type": "derived", "properties": {"data": {"UUID": "fake"}}})
    g["edges"] = [{"source": "A", "target": "T", "weight": 1}]
    original = deepcopy(g)
    r = run(g, "snapshot")
    assert r["result"]["entity_count"] == 3
    assert r["result"]["ownership_fact_count"] == 1
    assert r["result"]["facts"][0]["ratio"] == "0.4"
    assert r["result"]["facts"][0]["source_node_ids"] == ["fact-row-0"]
    assert g == original


def test_direct_majority_priority_and_economic_interest_are_separate():
    g = graph([("R", "A", "57.1429"), ("A", "T", 60)])
    r = run(g, "first_holder", target_id="T")
    assert r["result"]["first_holder_id"] == "A"
    assert r["result"]["confirmed_id"] is None
    assert r["rule_status"] == "RULE_DATA_MISSING"
    r = run(g, "economic_interest", target_id="T", root_id="R")
    assert Decimal(r["result"]["requested_interest"]["interest"]) == Decimal("0.3428574")


def test_strict_control_preserves_every_proof_step():
    g = graph([("R", "A", "57.1429"), ("A", "T", 60)])
    r = run(g, "control_chain", target_id="T")
    assert r["status"] == "CONFIRMED_CONTROL"
    assert r["result"]["ultimate_controller_ids"] == ["R"]
    proof = r["evidence"]["proof_dag"]
    assert {(x["controller_id"], x["controlled_id"]) for x in proof} == {("R", "A"), ("R", "T")}
    assert {f["source_node_ids"][0] for x in proof for f in x["contributions"]} == {"fact-row-0", "fact-row-1"}


@pytest.mark.parametrize("ratio,expected", [("50", "NO_SINGLE_CONTROLLER"), ("50.00000000000000000000001", "CONFIRMED_CONTROL")])
def test_decimal_strict_half(ratio, expected):
    assert run(graph([("A", "T", ratio)]), "control_chain", target_id="T")["status"] == expected


def test_diamond_votes_count_each_controlled_holder_once():
    g = graph([("R", "A", 60), ("R", "B", 60), ("A", "X", 30), ("B", "X", 30),
               ("X", "T", 30), ("R", "T", 25)])
    r = run(g, "control_chain", target_id="T")
    assert r["result"]["ultimate_controller_ids"] == ["R"]
    row = next(x for x in r["result"]["controllers"] if x["business_id"] == "R")
    assert Decimal(row["controllable_voting_stake"]) == Decimal("0.55")


def test_duplicate_fact_is_not_extra_voting_rights():
    g = graph([("A", "T", 30), ("A", "T", 30)])
    r = run(g, "control_chain", target_id="T")
    assert r["status"] == "NO_SINGLE_CONTROLLER"
    inspect = run(g, "snapshot")["result"]
    assert inspect["ownership_fact_count"] == 1
    assert inspect["facts"][0]["source_count"] == 2


def test_conflicting_sources_reject_by_default_and_max_is_explicit():
    g = graph([("A", "T", 40), ("A", "T", 60)])
    r = run(g, "first_holder", target_id="T")
    assert r["status"] == "DATA_CONFLICT"
    assert r["result"]["confirmed_id"] is None
    r = run(g, "first_holder", target_id="T", policy={"conflict_strategy": "max_ratio_per_business_fact"})
    assert r["status"] == "COMPLETE"
    assert r["result"]["first_holder_id"] == "A"


def test_over_100_blocks_dependent_control_not_unrelated_target():
    g = graph([("A", "X", 60), ("B", "X", 50), ("X", "T", 60), ("C", "U", 60)])
    r = run(g, "control_chain", target_id="T")
    assert r["status"] == "DATA_CONFLICT"
    assert r["result"]["confirmed_id"] is None
    assert run(g, "control_chain", target_id="U")["status"] == "CONFIRMED_CONTROL"


def test_all_path_aggregation_and_ties_do_not_depend_on_evidence_top_k():
    g = graph([("R", "A", 40), ("R", "B", 40), ("A", "T", 25), ("B", "T", 25), ("S", "T", 15)])
    results = [run(g, "first_holder", target_id="T", evidence_top_k=k) for k in (0, 1, 5)]
    assert [r["result"]["first_holder_id"] for r in results] == ["R"] * 3
    assert Decimal(results[0]["result"]["root_interests"][0]["interest"]) == Decimal(".20")
    tied = run(graph([("A", "T", 20), ("B", "T", 20)]), "first_holder", target_id="T")
    assert tied["status"] == "TIED_CANDIDATES"
    assert tied["result"]["first_holder_ids"] == ["A", "B"]


def test_no_owners_never_select_target_as_own_shareholder():
    g = graph([("A", "B", 20)], extra_institutions=["T"])
    r = run(g, "first_holder", target_id="T")
    assert r["status"] == "NO_EVIDENCE"
    assert r["result"]["first_holder_ids"] == []
    assert r["result"]["first_holder_id"] is None


def test_scc_member_equations_include_convergent_repeated_walks():
    g = graph([("R", "A", 40), ("A", "B", 20), ("B", "A", 20), ("B", "T", 30)])
    r = run(g, "economic_interest", target_id="T", root_id="R")
    assert r["status"] == "COMPLETE"
    assert Decimal(r["result"]["requested_interest"]["interest"]) == Decimal(".025")
    component = r["result"]["component_solutions"][0]
    interests = {x["business_id"]: Decimal(x["interest"]) for x in component["member_interests"]}
    assert interests == {"A": Decimal(".0625"), "B": Decimal(".3125")}
    assert Decimal(component["residual"]) == 0


def test_singular_scc_is_non_convergent_not_a_zero_answer():
    r = run(graph([("A", "B", 100), ("B", "A", 100)]), "economic_interest", target_id="B")
    assert r["status"] == "NON_CONVERGENT"
    assert not r["complete"]
    assert r["result"]["ranking_complete"] is False


def test_scc_hard_control_requires_review():
    r = run(graph([("A", "B", 60), ("B", "A", 60)]), "control_chain", target_id="B")
    assert r["status"] == "SCC_REVIEW"
    assert r["result"]["confirmed_id"] is None


def test_group_tree_prefers_actual_control_hierarchy_over_weak_shortcut():
    g = graph([("R", "A", 60), ("A", "T", 60), ("R", "T", 1)])
    r = run(g, "group_tree", root_id="R")
    assert {(e["source"], e["target"]) for e in r["result"]["tree_edges"]} == {("R", "A"), ("A", "T")}
    assert ("R", "T") in {(e["source"], e["target"]) for e in r["result"]["additional_edges"]}
    assert r["result"]["member_count"] == 3
    assert r["result"]["primary_faction_id"] is None


def test_group_coalition_is_proved_without_fake_majority_fact():
    g = graph([("R", "A", 60), ("R", "B", 60), ("A", "T", 30), ("B", "T", 30)])
    r = run(g, "group_tree", root_id="R")
    assert r["result"]["member_count"] == 4
    target_edge = next(x for x in r["result"]["control_dag"] if x["source"] == "R" and x["target"] == "T")
    assert target_edge["contribution_count"] == 2
    assert Decimal(target_edge["controllable_voting_stake"]) == Decimal(".6")


def test_display_limit_never_truncates_computation_or_hides_the_flag():
    g = graph([("R", "A", 60), ("A", "T", 60)])
    r = run(g, "group_tree", root_id="R", display_limit=1)
    assert r["complete"]
    assert r["result"]["member_count"] == 3
    assert r["completeness"]["display_truncated"]
    assert len(r["result"]["members"]) == 1


@pytest.mark.parametrize("budget", [{"max_nodes": 1}, {"max_edges": 1}])
def test_exhausted_budgets_never_claim_complete(budget):
    r = run(graph([("R", "A", 60), ("A", "T", 60)]), "first_holder", target_id="T", **budget)
    assert r["status"] == "TRUNCATED"
    assert not r["complete"]
    assert r["result"]["confirmed_id"] is None


def test_cancellation_boundary_is_explicit_configuration():
    g = graph([("R", "A", 60), ("A", "T", 60)], statuses={"A": "注销"})
    assert run(g, "control_chain", target_id="T")["result"]["ultimate_controller_ids"] == ["R"]
    r = run(g, "control_chain", target_id="T", policy={"stop_statuses": ["注销"]})
    assert r["result"]["ultimate_controller_ids"] == ["A"]


def test_elapsed_budget_does_not_depend_on_machine_timer_resolution(monkeypatch):
    import graph_data_service.finance as finance
    original_check = finance.Budget.check
    def expired(self, *args, **kwargs):
        self.started -= 2
        return original_check(self, *args, **kwargs)
    monkeypatch.setattr(finance.Budget, "check", expired)
    r = run(graph([("R", "A", 60)]), "first_holder", target_id="A", max_seconds=1)
    assert r["status"] == "TRUNCATED"
    assert not r["complete"]


def test_direct_majority_inside_scc_requires_review():
    r = run(graph([("A", "B", 60), ("B", "A", 60)]), "first_holder", target_id="B")
    assert r["status"] == "SCC_REVIEW"
    assert r["result"]["confirmed_id"] is None


@pytest.mark.parametrize("g", [{"graph": []}, {"nodes": [None]}, {"nodes": [{"properties": "bad"}]}])
def test_malformed_graph_returns_structured_error(g):
    assert run(g, "snapshot")["status"] == "INVALID_ARGUMENT"


def test_authority_precedes_majority_but_names_do_not_create_authority():
    g = graph([("A", "T", 60)], persons=["GP"])
    for node in g["nodes"]:
        if node.get("type") == "institution_main":
            node["properties"]["data"]["LGL_RPRT"] = "Person GP"
    assert run(g, "first_holder", target_id="T")["result"]["first_holder_id"] == "A"
    policy = {"mode": "configured_rules", "version": "reviewed-v1", "rule_coverage": "complete", "rule_relations": [
        {"type": "GP", "target_id": "T", "holder_id": "GP", "snapshot_id": "2026-09-23", "authority": "test registry", "evidence_refs": ["record:1"]}]}
    r = run(g, "first_holder", target_id="T", policy=policy)
    assert r["result"]["first_holder_id"] == "GP"
    assert r["result"]["confirmed_id"] == "GP"


def test_mixed_snapshots_and_bad_reference_block_confirmation():
    g = graph([("A", "T", 60)])
    g["nodes"][0]["properties"]["data"]["STATC_DT"] = "2025-01-01"
    r = run(g, "first_holder", target_id="T")
    assert r["status"] == "DATA_CONFLICT"
    assert not r["complete"]
    g = graph([("A", "T", 60)])
    g["nodes"][-1]["properties"]["data"]["REL_SHRHLR_TP_CODE"] = "1"
    assert run(g, "first_holder", target_id="T")["status"] == "DATA_CONFLICT"


def test_identity_type_namespace_and_query_disambiguate_names():
    g = graph([("A", "T", 40)])
    g["nodes"].append({"id": "individual-A", "type": "individual_info", "properties": {"data": {"IDV_ID": "A", "IDV_NM": "Company A", "STATC_DT": "2026-09-23"}}})
    r = run(g, "snapshot", query="Company A")
    assert {x["entity_id"] for x in r["result"]["entities"]} == {"I:A", "P:A"}
    assert run(g, "first_holder", target_id="A")["status"] == "AMBIGUOUS_ID"
    assert run(g, "first_holder", target_id="I:A")["status"] == "NO_EVIDENCE"


def test_semantic_hash_stable_when_records_reordered():
    g = graph([("A", "T", 40), ("B", "T", 20)])
    a = run(g, "snapshot")
    g["nodes"].reverse()
    b = run(g, "snapshot")
    assert a["semantic_hash"] == b["semantic_hash"]
    assert a["input_manifest_hash"] != b["input_manifest_hash"]


@pytest.mark.parametrize("kwargs", [{"policy": {"made_up": True}}, {"invented": 2}, {"policy": {"ratio_format": "guess"}}, {"policy": {"rule_coverage": "complete"}}])
def test_unsupported_or_unreviewed_policy_rejected(kwargs):
    assert run(graph([("A", "T", 60)]), "first_holder", target_id="T", **kwargs)["status"] == "INVALID_ARGUMENT"


def test_nonfinancial_graph_requires_schema():
    assert run({"nodes": [], "edges": []}, "snapshot")["status"] == "FINANCE_SCHEMA_REQUIRED"


@pytest.mark.parametrize("value", ["1e-999999", "1e999999", "50." + "0" * 48 + "1", "NaN", "Infinity"])
def test_ratio_extreme_exponent_or_more_than_50_significant_digits_is_rejected(value):
    r = run(graph([("A", "T", value)]), "control_chain", target_id="T")
    assert r["status"] == "DATA_CONFLICT"
    assert r["quality"]["issue_counts"]["RATIO_INVALID"] == 1
    assert not r["complete"]
    assert r["result"]["confirmed_id"] is None


def test_ratio_50_significant_digits_remains_exact_at_strict_threshold():
    value = "50." + "0" * 47 + "1"
    r = run(graph([("A", "T", value)]), "control_chain", target_id="T")
    assert r["status"] == "CONFIRMED_CONTROL"
    assert Decimal(r["result"]["controllers"][0]["controllable_voting_stake"]) > Decimal(".5")
    # Insignificant trailing zeroes are allowed; they do not use precision.
    r = run(graph([("A", "T", "50." + "0" * 60)]), "control_chain", target_id="T")
    assert r["status"] == "NO_SINGLE_CONTROLLER"


def test_direct_majority_evidence_reports_fact_truncation():
    g = graph([("A", "T", 60), ("B", "T", 20), ("C", "T", 20)])
    r = run(g, "first_holder", target_id="T", display_limit=1)
    assert r["result"]["first_holder_id"] == "A"
    assert r["complete"]
    assert r["evidence"]["fact_count"] == 3
    assert len(r["evidence"]["direct_facts"]) == 1
    assert r["evidence"]["complete"] is False
    assert r["completeness"]["display_truncated"]


def test_group_raw_fact_evidence_reports_truncation():
    g = graph([("R", "A", 60), ("A", "T", 60), ("R", "T", 1)])
    r = run(g, "group_tree", root_id="R", display_limit=1)
    assert r["result"]["member_count"] == 3
    assert r["evidence"]["fact_count"] == 3
    assert r["evidence"]["complete"] is False
    assert r["completeness"]["display_truncated"]


@pytest.mark.parametrize("operation,selector", [("control_chain", {"target_id": "B"}),
                                                 ("group_tree", {"root_id": "A"}),
                                                 ("first_holder", {"target_id": "B"})])
def test_scc_review_survives_data_quality_block(operation, selector):
    g = graph([("A", "B", 60), ("B", "A", 60), ("C", "A", 50)])
    r = run(g, operation, **selector)
    assert r["status"] == "DATA_CONFLICT"
    assert r["analysis_status"] == "SCC_REVIEW"
    assert r["quality"]["status"] == "DATA_CONFLICT"
    assert r["review_required"]
    assert set(r["review_reasons"]) == {"SCC_REVIEW", "DATA_CONFLICT"}
    assert r["result"].get("confirmed_id") is None
    assert r["result"].get("primary_faction_id") is None


def test_long_chain_tiny_interest_uses_bounded_exact_decimal_output():
    g = graph([(str(i), str(i + 1), "1e-20") for i in range(7)])
    r = run(g, "economic_interest", root_id="0", target_id="7")
    assert r["status"] == "COMPLETE"
    value = r["result"]["requested_interest"]["interest"]
    assert Decimal(value) == Decimal("1e-154")
    assert "E" in value
    assert len(value) < 100


def authority_policy():
    return {"mode": "configured_rules", "version": "reviewed-v1", "rule_coverage": "complete",
        "rule_relations": [{"type": "GP", "target_id": "T", "holder_id": "GP", "snapshot_id": "2026-09-23",
                            "authority": "partnership registry", "evidence_refs": ["document:123#page=4"]}]}


@pytest.mark.parametrize("mode", [None, "equity_only"])
def test_equity_only_rejects_authority_relations_instead_of_silently_applying(mode):
    policy = authority_policy()
    policy.pop("rule_coverage")
    if mode is None:
        policy.pop("mode")
    else:
        policy["mode"] = mode
    r = run(graph([("A", "T", 60)], persons=["GP"]), "first_holder", target_id="T", policy=policy)
    assert r["status"] == "INVALID_ARGUMENT"


def test_authoritative_first_holder_returns_actual_selected_rule_evidence():
    policy = authority_policy()
    policy["rule_relations"].append({**policy["rule_relations"][0], "evidence_refs": ["document:456#page=2"]})
    r = run(graph([("A", "T", 60)], persons=["GP"]), "first_holder", target_id="T", policy=policy, display_limit=1)
    assert r["result"]["confirmed_id"] == "GP"
    assert "direct_facts" not in r["evidence"]
    assert r["evidence"]["selected_rule_relation_count"] == 2
    assert r["evidence"]["complete"] is False
    selected = r["evidence"]["selected_rule_relations"][0]
    assert selected["authority"] == "partnership registry"
    assert selected["evidence_refs"] == ["document:123#page=4"]
    assert selected["snapshot_id"] == "2026-09-23"
    assert selected["holder_entity_id"] == "P:GP"


@pytest.mark.parametrize("operation,selector,excluded", [
    ("first_holder", {"target_id": "T"}, "T"),
    ("control_chain", {"target_id": "I:T"}, "I:T"),
    ("group_tree", {"root_id": "A"}, "A"),
    ("economic_interest", {"target_id": "T", "root_id": "A"}, "A")])
def test_explicitly_excluded_query_subject_cannot_be_confirmed(operation, selector, excluded):
    policy = {"mode": "configured_rules", "version": "reviewed-v1", "rule_coverage": "complete",
              "exclude_entity_ids": [excluded]}
    r = run(graph([("A", "T", 60)]), operation, policy=policy, **selector)
    assert r["status"] == "EXCLUDED_BY_POLICY"
    assert r["result"]["confirmed_id"] is None
    assert r["result"]["primary_faction_id"] is None


def test_256_character_business_ids_accept_their_258_character_typed_form():
    target = "T" * 256
    g = graph([("A", target, 60)])
    assert run(g, "first_holder", target_id="I:" + target)["result"]["first_holder_id"] == "A"
    assert run(g, "snapshot", target_id="I:" + target)["result"]["entities"][0]["business_id"] == target
    r = run(g, "first_holder", target_id=target, policy={"exclude_entity_ids": ["I:" + target]})
    assert r["status"] == "EXCLUDED_BY_POLICY"
    policy = authority_policy()
    policy["rule_relations"][0]["target_id"] = "I:" + target
    g = graph([("A", target, 60)], persons=["GP"])
    assert run(g, "first_holder", target_id=target, policy=policy)["result"]["first_holder_id"] == "GP"


def test_a_root_without_strict_control_cannot_create_a_primary_group():
    policy = {"mode": "configured_rules", "version": "reviewed-v1", "rule_coverage": "complete"}
    r = run(graph([("A", "T", 30)]), "group_tree", root_id="A", policy=policy)
    assert r["status"] == "NO_CONTROLLED_MEMBERS"
    assert r["result"]["member_count"] == 1
    assert r["result"]["control_dag"] == []
    assert r["result"]["primary_faction_id"] is None


def test_uncontrolled_self_cycle_keeps_review_without_inventing_control():
    r = run(graph([("A", "A", 20)]), "group_tree", root_id="A")
    assert r["status"] == "NO_CONTROLLED_MEMBERS"
    assert r["scc_review_required"]
    assert r["review_reasons"] == ["SCC_REVIEW"]
    assert r["result"]["primary_faction_id"] is None


@pytest.mark.parametrize("excluded", ["GP", "P:GP"])
def test_authority_holder_exclusion_is_honored_before_first_holder_selection(excluded):
    policy = authority_policy()
    policy["exclude_entity_ids"] = [excluded]
    r = run(graph([("A", "T", 60)], persons=["GP"]), "first_holder", target_id="T", policy=policy)
    assert r["status"] == "COMPLETE"
    assert r["result"]["rule_applied"] == "DIRECT_STRICT_MAJORITY"
    assert r["result"]["first_holder_id"] == "A"
    assert r["result"]["confirmed_id"] == "A"


def test_control_sum_retains_tiny_votes_that_cross_strict_half():
    g = graph([("R", "T", 50), ("R", "A", 60), ("A", "T", "1e-98")])
    r = run(g, "control_chain", target_id="T")
    assert r["status"] == "CONFIRMED_CONTROL"
    assert r["result"]["ultimate_controller_ids"] == ["R"]
    row = next(v for v in r["result"]["controllers"] if v["business_id"] == "R")
    assert Decimal(row["controllable_voting_stake"]) == Decimal("0.5" + "0" * 98 + "1")
    assert r["numeric_policy"]["control_and_quality_precision"] == 110
    assert r["numeric_policy"]["economic_precision"] == 50


def test_quality_sum_retains_tiny_ownership_that_exceeds_100_percent():
    g = graph([("A", "T", 100), ("B", "T", "1e-98")])
    r = run(g, "first_holder", target_id="T")
    assert r["status"] == "DATA_CONFLICT"
    assert r["quality"]["issue_counts"]["OWNERSHIP_TOTAL_EXCEEDED"] == 1
    assert r["result"]["confirmed_id"] is None


def test_requested_upstream_evidence_does_not_mix_in_boundary_root_paths():
    g = graph([("U", "R", 40), ("R", "A", "57.1429"), ("A", "T", 60)])
    r = run(g, "economic_interest", root_id="R", target_id="T")
    assert r["result"]["root_interests_scope"] == "boundary_roots"
    assert r["result"]["root_interests"][0]["business_id"] == "U"
    assert r["result"]["requested_interest"]["scope"] == "requested_upstream"
    assert r["evidence"]["path_scope"] == "requested_upstream"
    assert r["evidence"]["root_entity_ids"] == ["I:R"]
    assert r["evidence"]["requested_root_entity_id"] == "I:R"
    assert r["evidence"]["paths"][0]["nodes"] == ["R", "A", "T"]
    assert Decimal(r["evidence"]["paths"][0]["product"]) == Decimal(".3428574")
    boundary = run(g, "economic_interest", target_id="T")
    assert boundary["evidence"]["path_scope"] == "boundary_roots"
    assert boundary["evidence"]["paths"][0]["nodes"][0] == "U"

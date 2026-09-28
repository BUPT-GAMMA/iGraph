"""Portable regression against the 13 user cases and an independent oracle."""
from decimal import Decimal

import pytest

from graph_data_service.finance import analyze_finance
from scripts.validate_finance_cases import (
    build_construction_document, compare_results, load_cases, run_local,
)


CASES = load_cases()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
def test_reference_cases_preserve_values_structure_and_quality(case):
    report = compare_results(case, run_local(case))
    assert report["passed"], report


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case_id"])
def test_requested_interests_match_independent_global_equation_oracle(case):
    document = build_construction_document(case)
    oracle = case["independent_oracle"]
    if oracle["singular"]:
        result = analyze_finance(document, "economic_interest", target_id=case["target"])
        assert result["analysis_status"] == "NON_CONVERGENT"
        assert result["result"]["target_interest"] is None
        return
    # Includes intermediate companies and every SCC member, not only roots.
    # Values were obtained by rational global elimination outside finance.py.
    for node_id, expected in oracle["node_interest"].items():
        result = analyze_finance(document, "economic_interest", target_id=case["target"],
                                 root_id=node_id, evidence_top_k=0)
        actual = result["result"]["requested_interest"]
        assert actual["complete"], (case["case_id"], node_id, result)
        assert abs(Decimal(actual["interest"]) - Decimal(expected)) <= Decimal("1e-10"), (case["case_id"], node_id)


def test_institution_only_case_does_not_need_a_fabricated_person():
    case = next(case for case in CASES if case["case_id"] == "pyramid")
    document = build_construction_document(case)
    assert not any(row["type"] == "individual_info" for row in document["graph"]["nodes"])
    result = analyze_finance(document, "snapshot")
    assert result["complete"] and result["quality"]["source_projection_complete"]
    assert result["result"]["entity_count"] == 4
    assert result["result"]["individual_count"] == 0


def test_missing_required_person_table_is_still_a_schema_error():
    case = next(case for case in CASES if case["case_id"] == "person-aligned-network")
    document = build_construction_document(case)
    document["graph"]["nodes"] = [row for row in document["graph"]["nodes"] if row["type"] != "individual_info"]
    result = analyze_finance(document, "snapshot")
    assert result["status"] == "FINANCE_SCHEMA_REQUIRED"
    assert result["missing_source_tables"] == ["individual_info"]
    assert not result["complete"]


def test_missing_single_referenced_person_is_still_a_source_conflict():
    case = next(case for case in CASES if case["case_id"] == "person-aligned-network")
    document = build_construction_document(case)
    document["graph"]["nodes"] = [row for row in document["graph"]["nodes"] if row["id"] != "person-P1"]
    result = analyze_finance(document, "snapshot")
    assert result["status"] == "DATA_CONFLICT"
    assert not result["quality"]["source_projection_complete"]
    assert result["quality"]["issue_counts"]["POLYMORPHIC_REFERENCE_INVALID"] > 0


def test_nonconvergent_direct_majority_does_not_escape_economic_review():
    case = next(case for case in CASES if case["case_id"] == "non-convergent-cycle")
    result = analyze_finance(build_construction_document(case), "first_holder", target_id=case["target"])
    assert result["status"] == "NON_CONVERGENT"
    assert result["result"]["economic_status"] == "NON_CONVERGENT"
    assert result["result"]["first_holder_id"] is None
    assert result["result"]["first_holder_ids"] == []
    assert not result["complete"]
    assert {"NON_CONVERGENT", "SCC_REVIEW"} <= set(result["review_reasons"])
    assert result["evidence"]["direct_facts"]


def test_nonconvergence_and_ownership_conflict_keep_all_review_reasons():
    original = next(case for case in CASES if case["case_id"] == "non-convergent-cycle")
    case = {**original, "nodes": original["nodes"] + [{"id": "X", "type": "institution"}],
            "edges": original["edges"] + [["X", "B", "0.1", "0"]]}
    result = analyze_finance(build_construction_document(case), "first_holder", target_id="B")
    assert result["status"] == "DATA_CONFLICT"
    assert result["analysis_status"] == "NON_CONVERGENT"
    assert {"DATA_CONFLICT", "NON_CONVERGENT", "SCC_REVIEW"} <= set(result["review_reasons"])
    assert result["result"]["first_holder_id"] is None
    assert result["result"]["first_holder_ids"] == []
    assert result["result"]["confirmed_id"] is None
    assert not result["complete"]


def test_converged_cycle_keeps_control_review_and_reports_economic_diagnosis():
    case = next(case for case in CASES if case["case_id"] == "cross-holding")
    result = analyze_finance(build_construction_document(case), "first_holder", target_id=case["target"])
    assert result["result"]["economic_status"] == "CONVERGED"
    assert result["status"] == "SCC_REVIEW"
    assert result["result"]["first_holder_id"] == "B"
    assert result["result"]["confirmed_id"] is None
    assert result["result"]["component_solutions"]
    assert result["evidence"]["direct_facts"]


def _institution_document(edges):
    """Build new graphs without borrowing the acceptance cases' expected values."""
    return build_construction_document({
        "case_id": "scc-boundary", "target": "T",
        "nodes": [{"id": node, "type": "institution"}
                  for node in sorted({node for source, target, _ in edges for node in (source, target)})],
        "edges": [[source, target, ratio, "0"] for source, target, ratio in edges],
    })


def _large_ring():
    return [("A" + str(index), "A" + str((index + 1) % 129), "0.1")
            for index in range(129)]


def test_nonconvergent_minority_does_not_clear_direct_majority():
    document = _institution_document([("B", "T", "0.6"), ("A", "A", "1"), ("A", "T", "0.1")])
    result = analyze_finance(document, "first_holder", target_id="T")
    assert result["status"] == "COMPLETE"
    assert result["complete"]
    assert result["result"]["first_holder_id"] == "B"
    assert result["result"]["first_holder_ids"] == ["B"]
    assert result["result"]["rule_applied"] == "DIRECT_STRICT_MAJORITY"
    # The separate complete economic query is genuinely blocked by the loop.
    economic = analyze_finance(document, "economic_interest", target_id="T")
    assert economic["status"] == "NON_CONVERGENT"


def test_oversized_minority_component_does_not_clear_direct_majority():
    document = _institution_document(_large_ring() + [("B", "T", "0.6"), ("A0", "T", "0.1")])
    result = analyze_finance(document, "first_holder", target_id="T")
    assert result["status"] == "COMPLETE"
    assert result["complete"]
    assert result["result"]["first_holder_id"] == "B"
    assert result["result"]["first_holder_ids"] == ["B"]
    assert result["result"]["rule_applied"] == "DIRECT_STRICT_MAJORITY"


@pytest.mark.parametrize("operation", ["first_holder", "economic_interest"])
def test_oversized_direct_majority_component_is_truncated_not_nonconvergent(operation):
    document = _institution_document(_large_ring() + [("A0", "T", "0.6")])
    result = analyze_finance(document, operation, target_id="T")
    assert result["status"] == "TRUNCATED"
    assert result["analysis_status"] == "TRUNCATED"
    assert not result["complete"]
    assert not result["completeness"]["computation"]
    assert result["result"]["economic_status"] == "TRUNCATED"
    assert any(component["status"] == "TOO_LARGE" for component in result["result"]["component_solutions"])
    assert "NON_CONVERGENT" not in result["review_reasons"]
    assert "TRUNCATED" in result["review_reasons"]
    if operation == "first_holder":
        assert "SCC_REVIEW" in result["review_reasons"]
        assert result["result"]["first_holder_id"] is None
        assert result["result"]["first_holder_ids"] == []
        assert result["evidence"]["direct_facts"]


@pytest.mark.parametrize("case_id", ["same-ratio", "cumulative-root", "parallel-root-contest", "multi-scc-economic-target"])
def test_reference_rankings_are_independent_of_display_evidence(case_id):
    case = next(case for case in CASES if case["case_id"] == case_id)
    document = build_construction_document(case)
    expected = {key: Decimal(value) for key, value in case["reference"]["economic_roots"].items()}
    results = [analyze_finance(document, "economic_interest", target_id=case["target"], evidence_top_k=k)
               for k in (0, 1, 30)]
    for result in results:
        actual = {row["business_id"]: Decimal(row["interest"]) for row in result["result"]["root_interests"]}
        assert actual.keys() == expected.keys()
        assert all(abs(actual[key] - expected[key]) <= Decimal("1e-10") for key in expected)
    assert results[0]["result"]["root_interests"] == results[1]["result"]["root_interests"] == results[2]["result"]["root_interests"]

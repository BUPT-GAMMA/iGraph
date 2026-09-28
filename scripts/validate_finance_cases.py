#!/usr/bin/env python3
"""Replay the 13 ownership cases against the public deterministic finance entry.

The fixture contains the original cases and recorded independent/reference
results, so neither the external finance folder nor third-party packages are
needed. ``build_construction_document`` and ``compare_results`` also support
HTTP harnesses: pass the same document to a session, then compare its five
finance tool responses without using the local implementation as the oracle.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "finance_reference_cases.json"
OPERATIONS = ("snapshot", "first_holder", "control_chain", "economic_interest", "group_tree")
TOOL_NAMES = {
    "snapshot": "finance_inspect_ownership",
    "first_holder": "finance_first_holder",
    "control_chain": "finance_control_chain",
    "economic_interest": "finance_economic_interest",
    "group_tree": "finance_group_tree",
}


def load_cases(path=DEFAULT_FIXTURE):
    return json.loads(Path(path).read_text(encoding="utf-8"))["cases"]


def build_construction_document(case):
    """Create original three-table records, preserving every original entity.

    An empty individual table is represented by zero rows; no dummy person or
    synthetic ownership edge is added. Fractions are converted with Decimal.
    """
    nodes = []

    def row(source_id, table, data):
        nodes.append({"id": source_id, "type": table,
                      "properties": {"data": {"STATC_DT": "2026-09-27", **data}}})

    for node in case["nodes"]:
        bid = node["id"]
        if node["type"] == "individual":
            row("person-" + bid, "individual_info", {"IDV_ID": bid, "IDV_NM": bid})
        else:
            row("institution-" + bid, "institution_main",
                {"UUID": bid, "CHN_NM": bid, "ENTP_OPR_ST": "存续"})
    for index, (holder, target, ratio, kind) in enumerate(case["edges"], 1):
        row("case-e" + str(index), "institution_shareholder_relationship", {
            "UUID": target, "REL_SHRHLR_CUST_ID": holder,
            "REL_SHRHLR_TP_CODE": kind,
            "REL_SHRHLR_HOLD_SHR_RTO": format(Decimal(ratio) * 100, "f") + "%",
        })
    return {"dataset_id": "finance-case-" + case["case_id"], "revision": 1,
            "graph": {"directed": True, "nodes": nodes, "edges": []}}


def operation_arguments(case, operation, evidence_top_k=30):
    args = {"evidence_top_k": evidence_top_k, "display_limit": 500}
    if operation == "group_tree":
        reference = case["reference"]
        roots = reference["ultimate"] or reference["holder"] or [case["target"]]
        args["root_id"] = roots[0]
    elif operation != "snapshot":
        args["target_id"] = case["target"]
    return args


def run_local(case, evidence_top_k=30):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from graph_data_service.finance import analyze_finance

    document = build_construction_document(case)
    return {operation: analyze_finance(document, operation,
                                      **operation_arguments(case, operation, evidence_top_k))
            for operation in OPERATIONS}


def _components(values):
    return sorted(sorted(component) for component in values)


def compare_results(case, responses, tolerance=Decimal("1e-10")):
    """Separate numeric/structural regressions from intentional policy statuses."""
    reference = case["reference"]
    inspected = responses["snapshot"]
    first = responses["first_holder"]
    control = responses["control_chain"]
    economic = responses["economic_interest"]
    economic_result = economic.get("result", {})
    rows = economic_result.get("root_interests", [])
    actual_roots = {row["business_id"]: Decimal(row["interest"]) for row in rows}
    reference_roots = {key: Decimal(value) for key, value in reference["economic_roots"].items()}
    errors = {key: abs(actual_roots[key] - value) for key, value in reference_roots.items()
              if key in actual_roots}
    expected_order = sorted(reference_roots, key=lambda key: (-reference_roots[key], key))
    actual_order = [row["business_id"] for row in rows]
    oracle_values = {key: Decimal(value) for key, value in case["independent_oracle"]["node_interest"].items()}
    returned_interests = {row["business_id"]: Decimal(row["interest"])
                          for component in economic_result.get("component_solutions", [])
                          for row in component.get("member_interests", [])}
    target_interest = economic_result.get("target_interest")
    if target_interest is not None:
        returned_interests[case["target"]] = Decimal(target_interest)
    expected_interest_nodes = {member for component in reference["cyclic_sccs"] for member in component}
    expected_interest_nodes.add(case["target"])
    expected_interest_nodes &= oracle_values.keys()
    member_errors = {key: abs(returned_interests[key] - oracle_values[key])
                     for key in expected_interest_nodes if key in returned_interests}
    converged = reference["economic_status"] == "CONVERGED"
    statuses = {row["status"] for row in economic_result.get("component_solutions", [])}
    control_status = control.get("control_status", control.get("analysis_status", control.get("status")))
    normalized_control_status = "TIED_CANDIDATES" if control_status == "MULTIPLE_CONTROLLERS" else control_status
    checks = {
        "all_tools_succeeded": all(responses.get(op, {}).get("success") is True for op in OPERATIONS),
        "source_projection_complete": inspected.get("quality", {}).get("source_projection_complete") is True,
        "original_entity_count": inspected.get("result", {}).get("entity_count") == len(case["nodes"]),
        "original_fact_count": inspected.get("result", {}).get("ownership_fact_count") == len(case["edges"]),
        "scc_members": _components(inspected.get("result", {}).get("cyclic_components", [])) == _components(reference["cyclic_sccs"]),
        "economic_root_ids": set(actual_roots) == set(reference_roots),
        "economic_root_values": len(errors) == len(reference_roots) and all(value <= tolerance for value in errors.values()),
        "economic_ranking_order": actual_order == expected_order,
        "target_and_scc_member_values": (expected_interest_nodes == returned_interests.keys() and
                                         len(member_errors) == len(expected_interest_nodes) and
                                         all(value <= tolerance for value in member_errors.values())),
        "economic_convergence": (economic.get("complete") is True and statuses <= {"CONVERGED", "ACYCLIC"}) if converged else economic.get("analysis_status") == "NON_CONVERGENT" and economic.get("complete") is False,
        "control_diagnosis": normalized_control_status == reference["control_status"],
        "first_holder_candidates": sorted(first.get("result", {}).get("first_holder_ids", [])) == sorted(reference["holder"]),
    }
    if reference["control_status"] != "SCC_REVIEW":
        checks["ultimate_controllers"] = set(control.get("result", {}).get("ultimate_controller_ids", [])) == set(reference["ultimate"])
        checks["intermediate_controllers"] = set(control.get("result", {}).get("intermediate_controller_ids", [])) == set(reference["intermediate"])
    # All overfull records in these cases are in the target's ancestor region.
    conflicts = case["independent_oracle"]["incoming_ratio_over_one"]
    if conflicts:
        checks["quality_conflict_preserved"] = all(
            responses[op].get("status") == "DATA_CONFLICT" and
            responses[op].get("result", {}).get("confirmed_id") is None
            for op in ("first_holder", "control_chain", "economic_interest"))
    else:
        checks["no_spurious_quality_conflict"] = all(
            responses[op].get("status") != "DATA_CONFLICT"
            for op in ("snapshot", "first_holder", "control_chain", "economic_interest"))
    if not converged:
        checks["nonconvergent_first_holder_is_unconfirmed"] = (
            first.get("analysis_status") == "NON_CONVERGENT" and
            first.get("result", {}).get("first_holder_id") is None and
            first.get("complete") is False)
    if reference["control_status"] == "SCC_REVIEW":
        checks["cyclic_control_remains_unconfirmed"] = (
            control.get("result", {}).get("confirmed_id") is None and
            "SCC_REVIEW" in control.get("review_reasons", []))
    minimum_paths = case["expected_annotations"].get("minimum_paths")
    if minimum_paths:
        checks["equal_weight_paths_retained"] = len(economic.get("evidence", {}).get("paths", [])) >= minimum_paths
    details = []
    if conflicts:
        details.append({"kind": "quality_policy", "incoming_ratio_over_one": conflicts,
                        "explanation": "Reference arithmetic permits synthetic totals above one; system confirmation remains blocked."})
    if reference["outdated_annotation_keys"]:
        details.append({"kind": "outdated_expected_annotations", "keys": reference["outdated_annotation_keys"],
                        "explanation": "These literal case expectations disagree with the supplied reference engine itself."})
    if first.get("analysis_status") != reference["holder_status"]:
        details.append({"kind": "holder_status", "system": first.get("analysis_status"),
                        "reference": reference["holder_status"]})
    if control.get("result", {}).get("ultimate_controller_ids", []) != reference["ultimate"]:
        details.append({"kind": "control_roots", "system": control.get("result", {}).get("ultimate_controller_ids", []),
                        "reference": reference["ultimate"], "requires_review": reference["control_status"] == "SCC_REVIEW"})
    return {
        "case_id": case["case_id"], "title": case["title"], "passed": all(checks.values()),
        "checks": checks, "failed_checks": [key for key, value in checks.items() if not value],
        "economic_values_match": all(checks[key] for key in ("economic_root_ids", "economic_root_values", "economic_ranking_order", "target_and_scc_member_values", "economic_convergence")),
        "max_economic_absolute_difference": str(max(errors.values(), default=Decimal(0))) if converged else None,
        "max_target_and_scc_absolute_difference": str(max(member_errors.values(), default=Decimal(0))) if converged else None,
        "system_statuses": {op: responses[op].get("status") for op in OPERATIONS},
        "analysis_statuses": {op: responses[op].get("analysis_status") for op in OPERATIONS},
        "first_holder_ids": first.get("result", {}).get("first_holder_ids", []),
        "economic_roots": {key: str(value) for key, value in actual_roots.items()},
        "differences": details,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "finance-case-validation")
    parser.add_argument("--prefix", default="local", help="Output file prefix, e.g. baseline or updated")
    parser.add_argument("--case", action="append", dest="selected", help="Only run the selected case ID; repeatable")
    args = parser.parse_args(argv)
    if not args.prefix or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in args.prefix):
        parser.error("--prefix must contain only letters, numbers, '-' or '_'")
    cases = load_cases(args.fixture)
    if args.selected:
        missing = set(args.selected) - {case["case_id"] for case in cases}
        if missing:
            parser.error("Unknown cases: " + ", ".join(sorted(missing)))
        cases = [case for case in cases if case["case_id"] in args.selected]
    results, summaries = {}, []
    for case in cases:
        responses = run_local(case)
        results[case["case_id"]] = responses
        summary = compare_results(case, responses)
        summaries.append(summary)
        print("{case}: {state}; economics={economics}; failed={failed}".format(
            case=case["case_id"], state="PASS" if summary["passed"] else "FAIL",
            economics=summary["economic_values_match"], failed=",".join(summary["failed_checks"]) or "none"))
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": "local_public_finance_entry",
              "case_count": len(cases), "passed": sum(row["passed"] for row in summaries),
              "economic_matches": sum(row["economic_values_match"] for row in summaries), "cases": summaries}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for suffix, data in (("responses", results), ("summary", report)):
        path = args.output_dir / (args.prefix + "_" + suffix + ".json")
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("{}/{} cases passed; {}/{} economic comparisons passed".format(
        report["passed"], len(cases), report["economic_matches"], len(cases)))
    return 0 if report["passed"] == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Finance analysis follows the current, lossless construction representation."""
from copy import deepcopy
from decimal import Decimal

import pytest

from graph_data_service.construction import optimize_graph
from graph_data_service.finance import analyze_finance
from graph_engine.construction.sources import initial_graph, load_source
from graph_engine.construction.tools import transform


@pytest.fixture
def source_graph(tmp_path):
    path = tmp_path / "ownership.sql"
    path.write_text("""
        CREATE TABLE institution_main(UUID TEXT PRIMARY KEY, CHN_NM TEXT, ENTP_OPR_ST TEXT, STATC_DT TEXT);
        CREATE TABLE individual_info(IDV_ID TEXT PRIMARY KEY, IDV_NM TEXT, STATC_DT TEXT);
        CREATE TABLE institution_shareholder_relationship(
            UUID TEXT REFERENCES institution_main(UUID),
            REL_SHRHLR_CUST_ID TEXT REFERENCES institution_main(UUID),
            REL_SHRHLR_TP_CODE TEXT, REL_SHRHLR_HOLD_SHR_RTO TEXT, STATC_DT TEXT);
        INSERT INTO institution_main VALUES
            ('R','Root','存续','2026-09-23'),('A','Holder','注销','2026-09-23'),('T','Target','存续','2026-09-23');
        INSERT INTO individual_info VALUES ('P','Person','2026-09-23');
        INSERT INTO institution_shareholder_relationship VALUES
            ('A','R','0','60%','2026-09-23'),('T','A','0','60%','2026-09-23');
    """)
    source = load_source(path)
    try:
        yield source, initial_graph(source)
    finally:
        source.close()


def ownership_row(graph):
    return next(n for n in graph["nodes"] if n["properties"].get("table") == "institution_shareholder_relationship"
                and n["properties"]["data"]["UUID"] == "T")


@pytest.mark.parametrize("operation", ["rename_type", "extract_property_node", "node_property_to_edge", "collapse_node"])
def test_lossless_graph_edits_preserve_finance_results(source_graph, operation):
    source, graph = source_graph
    row = ownership_row(graph)
    if operation == "rename_type":
        operations = [{"op": operation, "target": "node", "old_type": name, "new_type": "renamed_" + name}
                      for name in source.tables]
        edited, _ = optimize_graph(graph, operations)
    elif operation == "collapse_node":
        edited = transform(deepcopy(graph), source, "optimize_graph", {"operations": [
            {"op": operation, "node_id": row["id"], "edge_type": "ownership"}]})
    else:
        edit = {"op": operation, "property": "REL_SHRHLR_HOLD_SHR_RTO"}
        if operation == "extract_property_node":
            edit.update(node_ids=[row["id"]], node_type="Ratio", edge_type="has_ratio")
        else:
            edit.update(node_id=row["id"], edge_ids=[next(e["id"] for e in graph["edges"] if e["source"] == row["id"])])
        edited, _ = optimize_graph(graph, [edit])
    before = deepcopy(edited)
    result = analyze_finance(edited, "economic_interest", target_id="T", root_id="R")
    assert result["status"] == "COMPLETE", result
    assert Decimal(result["result"]["requested_interest"]["interest"]) == Decimal("0.36")
    assert analyze_finance(edited, "first_holder", target_id="T")["result"]["first_holder_id"] == "A"
    assert edited == before
    if operation == "collapse_node":
        inspected = analyze_finance(edited, "snapshot", target_id="T")
        fact = inspected["result"]["facts"][0]
        edge_id = next(e["id"] for e in edited["edges"] if e["type"] == "ownership")
        assert fact["source_edge_ids"] == [edge_id]
        assert fact["source_node_ids"] == []


def test_extracted_operating_status_still_stops_at_the_configured_boundary(source_graph):
    _, graph = source_graph
    holder = next(n for n in graph["nodes"] if n["properties"].get("table") == "institution_main"
                  and n["properties"]["data"]["UUID"] == "A")
    edited, _ = optimize_graph(graph, [{"op": "extract_property_node", "node_ids": [holder["id"]],
                                      "property": "ENTP_OPR_ST", "node_type": "Status", "edge_type": "has_status"}])
    result = analyze_finance(edited, "control_chain", target_id="T", policy={"stop_statuses": ["注销"]})
    assert result["result"]["ultimate_controller_ids"] == ["A"]


@pytest.mark.parametrize("keep_original", [False, True])
def test_finance_reads_live_attributes_and_rejects_conflicting_storage(source_graph, keep_original):
    _, graph = source_graph
    row = ownership_row(graph)
    edited, _ = optimize_graph(graph, [{"op": "extract_property_node", "node_ids": [row["id"]],
        "property": "REL_SHRHLR_HOLD_SHR_RTO", "node_type": "Ratio", "edge_type": "has_ratio",
        "remove_from_node": not keep_original}])
    next(n for n in edited["nodes"] if n["type"] == "Ratio")["properties"]["value"] = "40%"
    result = analyze_finance(edited, "economic_interest", target_id="T", root_id="A")
    if keep_original:
        assert result["status"] == "INVALID_ARGUMENT"
        assert not result["complete"]
    else:
        assert result["status"] == "COMPLETE", result
        assert Decimal(result["result"]["requested_interest"]["interest"]) == Decimal("0.4")


def test_long_source_ids_remain_usable_in_finance_evidence(source_graph):
    _, graph = source_graph
    row = ownership_row(graph)
    old_id, new_id = row["id"], "ownership-record-" + "x" * 300
    row["id"] = new_id
    for edge in graph["edges"]:
        for field in ("source", "target"):
            if edge[field] == old_id:
                edge[field] = new_id
    result = analyze_finance(graph, "snapshot", target_id="T")
    assert result["result"]["facts"][0]["source_node_ids"] == [new_id]


def test_current_record_resolution_obeys_budget_and_restores_context(source_graph, monkeypatch):
    from graph_engine.construction.budget import ConstructionBudgetExceeded, current_deadline
    from graph_engine.construction import representation
    _, graph = source_graph
    before = current_deadline()

    def exhausted(_):
        assert current_deadline() is not None
        raise ConstructionBudgetExceeded("expired")

    monkeypatch.setattr(representation, "resolve_rows", exhausted)
    result = analyze_finance(graph, "first_holder", target_id="T")
    assert result["status"] == "TRUNCATED"
    assert not result["complete"]
    assert result["result"]["confirmed_id"] is None
    assert current_deadline() == before

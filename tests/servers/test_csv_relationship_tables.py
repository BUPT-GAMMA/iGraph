"""CSV primary keys select nodes; keyless foreign-key rows are live edges."""
import copy
import csv
import json
import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from graph_data_service.construction import ConstructionStore, validate_graph
from graph_engine.construction import register_builtin_algorithms
from graph_engine.construction.agent import TabularConstructionSubAgent
from graph_engine.construction.representation import resolve_rows
from graph_engine.construction.sources import SourceError, initial_graph, load_source, source_integrity_errors
from graph_engine.construction.workload import evaluate_workload, generate_workload


def relationship_source(tmp_path, *, rows=None, primary_key=None):
    path = tmp_path / "holdings.csv"
    path.write_text("id,company,holder,share\n" + (rows or "r1,C1,P1,25%\nr2,C1,C2,75%\nr3,C1,C2,75%\n"), encoding="utf-8")
    options = {
        "primary_key": primary_key or [],
        "foreign_keys": [
            {"columns": ["company"], "ref_table": "companies", "ref_columns": ["id"]},
            {"columns": ["holder"], "ref_table": "people", "ref_columns": ["id"]},
            {"columns": ["holder"], "ref_table": "companies", "ref_columns": ["id"]},
        ],
        "tables": [
            {"name": "companies", "columns": ["id"], "primary_key": ["id"], "rows": [{"id": "C1"}, {"id": "C2"}]},
            {"name": "people", "columns": ["id"], "primary_key": ["id"], "rows": [{"id": "P1"}]},
        ],
    }
    return path, options


def test_keyless_rows_become_edges_with_attributes_and_alternative_target_types(tmp_path):
    path, options = relationship_source(tmp_path)
    source = load_source(path, options)
    graph = initial_graph(source)
    assert source.tables["holdings"].primary_key == []  # Even with a unique id column.
    assert {node["type"] for node in graph["nodes"]} == {"companies", "people"}
    assert len(graph["nodes"]) == 3
    assert len(graph["edges"]) == 3
    assert len({edge["id"] for edge in graph["edges"]}) == 3
    assert {edge["type"] for edge in graph["edges"]} == {"holdings"}
    assert [edge["properties"]["data"]["share"] for edge in graph["edges"]] == ["25%", "75%", "75%"]
    assert len(resolve_rows(graph)) == 6
    assert source_integrity_errors(source, graph) == []
    assert validate_graph(graph) == graph
    assert not source.warnings
    # Queries must read live edge records, including all parallel relationships.
    queries = generate_workload(source, 15)
    assert evaluate_workload(source, graph, queries, repeats=1, min_query_seconds=0)["accuracy"] == 1
    graph["edges"][0]["properties"]["data"]["share"] = "wrong"
    assert source_integrity_errors(source, graph)


def test_rules_mode_publishes_relationship_edges_without_a_model(tmp_path):
    path, options = relationship_source(tmp_path)
    result = TabularConstructionSubAgent().run(path, {**options, "mode": "rules"})
    assert result.report["final"]["source_fidelity"]
    assert result.report["final_size"] == {"nodes": 3, "edges": 3}
    assert not result.report["llm_used"]


def test_configuring_primary_key_turns_relationship_table_into_nodes(tmp_path):
    path, options = relationship_source(tmp_path, primary_key=["id"])
    graph = initial_graph(load_source(path, options))
    assert len(graph["nodes"]) == 6
    assert len(graph["edges"]) == 6
    assert len([node for node in graph["nodes"] if node["type"] == "holdings"]) == 3


@pytest.mark.parametrize("keys", [None, []])
def test_csv_does_not_infer_unconfigured_primary_keys(tmp_path, keys):
    path = tmp_path / "people.csv"
    path.write_text("id,name\n1,Alice\n", encoding="utf-8")
    source = load_source(path, {} if keys is None else {"primary_key": keys})
    assert source.tables["people"].primary_key == []
    with pytest.raises(SourceError, match="Configure a primary key"):
        initial_graph(source)


def test_missing_configuration_is_actionable_in_public_build_status(tmp_path):
    path = tmp_path / "people.csv"
    path.write_text("id,name\n1,Alice\n", encoding="utf-8")
    store = register_builtin_algorithms(ConstructionStore(tmp_path))
    result = store.build("u1", "d1", path, path.name, {"mode": "rules"})
    assert result["status"] == "failed"
    assert result["failure_code"] == "csv_configuration_required"
    assert "主外键配置" in result["message"]


def test_bundle_preserves_explicit_empty_pk_even_with_unique_id(tmp_path):
    path, options = relationship_source(tmp_path)
    tables = [{"name": "holdings", "columns": ["id", "company", "holder", "share"],
               "primary_key": [], "foreign_keys": options["foreign_keys"]}, *options["tables"]]
    bundle = tmp_path / "bundle.csv"
    manifest = {"version": "csv_bundle.v1", "tables": []}
    with zipfile.ZipFile(bundle, "w") as archive:
        for table in tables:
            name = table["name"]
            entry = f"tables/{name}.csv"
            manifest["tables"].append({"file_name": f"{name}.csv", "entry": entry,
                **{key: table[key] for key in ("name", "columns", "primary_key")},
                "foreign_keys": table.get("foreign_keys", [])})
            content = path.read_text(encoding="utf-8") if name == "holdings" else "id\n" + "\n".join(row["id"] for row in table["rows"])
            archive.writestr(entry, content)
        archive.writestr("manifest.json", json.dumps(manifest))
    source = load_source(bundle)
    assert source.tables["holdings"].primary_key == []
    assert len(initial_graph(source)["nodes"]) == 3


@pytest.mark.parametrize("rows", ["r1,C1,,25%\n", "r1,C1,missing,25%\n"])
def test_missing_relationship_endpoint_fails_without_losing_rows(tmp_path, rows):
    path, options = relationship_source(tmp_path, rows=rows)
    with pytest.raises(SourceError, match="row 2.*0 matching endpoints"):
        initial_graph(load_source(path, options))


def test_ambiguous_alternative_target_fails(tmp_path):
    path, options = relationship_source(tmp_path)
    options["tables"][0]["rows"].append({"id": "P1"})
    with pytest.raises(SourceError, match="2 matching endpoints"):
        initial_graph(load_source(path, options))


def test_reference_to_keyless_table_requires_configuration(tmp_path):
    path, options = relationship_source(tmp_path)
    options["tables"][0]["primary_key"] = []
    with pytest.raises(SourceError, match="targets a table without a primary key"):
        initial_graph(load_source(path, options))


def test_edges_use_edge_budget_and_support_self_loops_and_duplicate_rows(tmp_path):
    path, options = relationship_source(tmp_path, rows="r1,C1,C1,25%\nr1,C1,C1,25%\n")
    source = load_source(path, {**options, "max_graph_nodes": 3, "max_graph_edges": 2})
    graph = initial_graph(source)
    assert len(graph["edges"]) == 2
    assert all(edge["source"] == edge["target"] for edge in graph["edges"])
    assert source_integrity_errors(source, graph) == []
    source.resource_limits = replace(source.resource_limits, max_graph_edges=1)
    with pytest.raises(SourceError, match="edge construction limit"):
        initial_graph(source)


def test_composite_foreign_keys_and_empty_relationship_table(tmp_path):
    path = tmp_path / "links.csv"
    path.write_text("a_region,a_id,b_region,b_id,note\nEU,1,US,1,kept\n", encoding="utf-8")
    options = {"foreign_keys": [
        {"columns": ["a_region", "a_id"], "ref_table": "entities", "ref_columns": ["region", "id"]},
        {"columns": ["b_region", "b_id"], "ref_table": "entities", "ref_columns": ["region", "id"]},
    ], "tables": [{"name": "entities", "primary_key": ["region", "id"],
                    "rows": [{"region": "EU", "id": "1"}, {"region": "US", "id": "1"}]}]}
    source = load_source(path, options)
    original = copy.deepcopy(source.schema())
    graph = initial_graph(source)
    assert len(graph["nodes"]) == 2 and len(graph["edges"]) == 1
    assert graph["edges"][0]["properties"]["data"]["note"] == "kept"
    assert source_integrity_errors(source, graph) == []
    assert source.schema() == original
    path.write_text("a_region,a_id,b_region,b_id,note\n", encoding="utf-8")
    source = load_source(path, options)
    assert initial_graph(source)["edges"] == []
    assert source_integrity_errors(source, initial_graph(source)) == []


@pytest.mark.skipif(not os.getenv("IGRAPH_FINANCE_CSV"), reason="Set IGRAPH_FINANCE_CSV to the finance_csv directory")
def test_real_finance_csv_preserves_every_record_as_nodes_or_edges(tmp_path):
    root = Path(os.environ["IGRAPH_FINANCE_CSV"])
    names = ["institution_main", "individual_info", "institution_shareholder_relationship"]
    rows = {name: list(csv.DictReader((root / (name + ".csv")).open(encoding="utf-8-sig", newline=""))) for name in names}
    manifest = {"version": "csv_bundle.v1", "tables": []}
    bundle = tmp_path / "finance.csv"
    with zipfile.ZipFile(bundle, "w") as archive:
        for name in names:
            entry = f"tables/{name}.csv"
            manifest["tables"].append({"file_name": name + ".csv", "entry": entry, "name": name,
                "columns": list(rows[name][0]), "primary_key": ["UUID"] if name == names[0] else ["IDV_ID"] if name == names[1] else [],
                "foreign_keys": []})
            archive.write(root / (name + ".csv"), entry)
        manifest["tables"][2]["foreign_keys"] = [
            {"columns": ["REL_SHRHLR_CUST_ID"], "ref_table": names[0], "ref_columns": ["UUID"]},
            {"columns": ["REL_SHRHLR_CUST_ID"], "ref_table": names[1], "ref_columns": ["IDV_ID"]},
            {"columns": ["UUID"], "ref_table": names[0], "ref_columns": ["UUID"]},
        ]
        archive.writestr("manifest.json", json.dumps(manifest))
    result = TabularConstructionSubAgent().run(bundle, {"mode": "rules"})
    graph = result.graph
    assert len(graph["nodes"]) == len(rows[names[0]]) + len(rows[names[1]])
    assert len(graph["edges"]) == len(rows[names[2]])
    assert {node["type"] for node in graph["nodes"]} == set(names[:2])
    assert {edge["type"] for edge in graph["edges"]} == {names[2]}
    nodes = {node["id"]: node for node in graph["nodes"]}
    for edge in graph["edges"]:
        row = rows[names[2]][edge["properties"]["row_index"]]
        assert edge["properties"]["data"] == row
        start, end = nodes[edge["source"]], nodes[edge["target"]]
        assert start["properties"]["data"]["UUID" if start["type"] == names[0] else "IDV_ID"] == row["REL_SHRHLR_CUST_ID"]
        assert end["properties"]["data"]["UUID"] == row["UUID"]
    assert result.report["final"]["source_fidelity"]

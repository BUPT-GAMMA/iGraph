"""The shipped graph retains finance facts through loading and restart."""
import csv
from concurrent.futures import ThreadPoolExecutor

import pytest
from backend.server_datasets import ServerDatasetDirectory
from graph_data_service.dataset_registry import DatasetRegistry
from graph_data_service.finance import analyze_finance
from graph_data_service.graph_data import GraphData
from graph_engine.finance_preset import DATASET_ID, FILENAME, RESOURCES, build_finance_graph, install_finance_preset


def test_bundled_graph_is_in_both_catalogs_and_retains_finance_evidence(tmp_path):
    install_finance_preset(tmp_path)
    registry = DatasetRegistry(str(tmp_path / "global" / "datasets"))
    graph = registry.load_graph(DATASET_ID)
    assert (graph.num_nodes, graph.num_edges) == (6000, 13436)
    assert registry.load_graph(DATASET_ID, "v001").num_edges == 13436
    sources = ServerDatasetDirectory(tmp_path / "server_datasets").list()
    assert len(sources) == 1 and sources[0].original_filename == FILENAME
    loaded = GraphData.load(sources[0].storage_path)
    document = loaded.graph_metadata["construction_graph"]
    snapshot = analyze_finance(document, "snapshot")
    assert snapshot["complete"]
    assert snapshot["result"]["entity_count"] == 6000
    assert snapshot["result"]["ownership_fact_count"] == 13436
    assert snapshot["quality"]["issue_counts"]["OWNERSHIP_TOTAL_EXCEEDED"] == 13
    first = analyze_finance(document, "first_holder", target_id="SYN-ENT-000044")
    assert first["result"]["first_holder_id"] == "SYN-ENT-000040"
    control = analyze_finance(document, "control_chain", target_id="SYN-ENT-000044")
    assert control["result"]["ultimate_controller_ids"] == ["SYN-ENT-000001"]
    assert document["edges"][0]["source"].startswith(("I:", "P:"))
    assert document["edges"][0]["target"].startswith("I:")
    assert document["edges"][0]["properties"]["data"]["REL_SHRHLR_HOLD_SHR_RTO"]


def test_install_is_concurrent_idempotent_and_preserves_existing_data(tmp_path):
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda _: install_finance_preset(tmp_path), range(3)))
    registry = DatasetRegistry(str(tmp_path / "global" / "datasets"))
    meta = registry.get(DATASET_ID)
    meta.description = "用户保留的说明"
    registry._save_meta(meta)
    public = tmp_path / "server_datasets" / FILENAME
    before = public.stat().st_mtime_ns
    install_finance_preset(tmp_path)
    assert registry.get(DATASET_ID).description == "用户保留的说明"
    assert public.stat().st_mtime_ns == before
    assert len(registry.list_datasets()) == 1
    assert not list((tmp_path / "server_datasets").glob(".finance-*"))


def test_corrupt_bundle_is_not_installed(tmp_path):
    resources = tmp_path / "assets"
    resources.mkdir()
    (resources / "manifest.json").write_bytes((RESOURCES / "manifest.json").read_bytes())
    (resources / FILENAME).write_bytes(b"broken")
    with pytest.raises(ValueError, match="checksum"):
        install_finance_preset(tmp_path / "data", resources=resources)
    assert not (tmp_path / "data" / "server_datasets").exists()


def test_csv_builder_rejects_dangling_or_unknown_shareholder(tmp_path):
    tables = {
        "institution_main": [{"UUID": "A", "CHN_NM": "机构", "STATC_DT": "2026-09-23"}],
        "individual_info": [{"IDV_ID": "P", "IDV_NM": "个人", "STATC_DT": "2026-09-23"}],
        "institution_shareholder_relationship": [{"UUID": "A", "REL_SHRHLR_CUST_ID": "missing",
            "REL_SHRHLR_TP_CODE": "0", "REL_SHRHLR_HOLD_SHR_RTO": "60%", "STATC_DT": "2026-09-23"}]}
    def write():
        for table, rows in tables.items():
            with (tmp_path / f"{table}.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    write()
    with pytest.raises(ValueError, match="Missing ownership endpoint"):
        build_finance_graph(tmp_path)
    tables["institution_shareholder_relationship"][0]["REL_SHRHLR_TP_CODE"] = "unknown"
    write()
    with pytest.raises(ValueError, match="Unknown shareholder type"):
        build_finance_graph(tmp_path)

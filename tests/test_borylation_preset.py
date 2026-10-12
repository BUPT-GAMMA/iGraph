"""Deployable resources, evidence, background isolation and safe reinstallation."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path
import pytest
from backend.project_datasets import ProjectDatasetRecord
from backend.user_cases import background_for_dataset
from graph_engine.borylation_preset import RESOURCES, install_borylation_preset
from graph_engine.tool_execution import get_tool_execution_metadata
from graph_data_service.dataset_registry import DatasetRegistry
from graph_data_service.borylation import analyze_borylation

def test_fresh_install_needs_no_model_and_all_steps_work(tmp_path, monkeypatch):
    monkeypatch.delenv("GRAPH_CONSTRUCTION_API_KEY", raising=False)
    receipt = install_borylation_preset(tmp_path)
    registry = DatasetRegistry(str(tmp_path / "global/datasets"))
    graph = registry.load_graph(receipt["dataset_id"])
    assert (graph.num_nodes, graph.num_edges) == (22, 48)
    for step in ("properties", "rings", "structure", "symmetry", "products", "conditions", "plan"):
        assert analyze_borylation(graph, {"step": step})["success"]
    assert registry.load_graph(receipt["dataset_id"], "v001").num_nodes == 22
    public = tmp_path / "server_datasets" / receipt["server_filename"]
    assert public.read_bytes() == (RESOURCES / receipt["server_filename"]).read_bytes()
    saved = tmp_path / "global/datasets" / receipt["dataset_id"] / "graph.npz"
    saved.write_bytes(b"user changes")
    public.write_bytes(b"user public graph")
    install_borylation_preset(tmp_path)
    assert saved.read_bytes() == b"user changes"
    assert public.read_bytes() == b"user public graph"

def test_packaged_raw_data_and_graph_checksums():
    manifest = json.loads((RESOURCES / "manifest.json").read_text())
    for name, checksum in manifest["provenance"]["source_hashes"].items():
        assert hashlib.sha256((RESOURCES / "raw" / name).read_bytes()).hexdigest() == checksum
    assert len(list((RESOURCES / "graphs").glob("BO-*.json"))) == 8
    assert hashlib.sha256((RESOURCES / manifest["filename"]).read_bytes()).hexdigest() == manifest["sha256"]

def test_molecular_background_is_bound_to_verified_graph(tmp_path):
    ds = ProjectDatasetRecord(dataset_id="chem", project_id="p", user_id="admin", name="custom name",
        original_filename="borylation-story.npz", storage_path=str(RESOURCES / "borylation-story.npz"),
        source_kind="server", file_type="npz", metadata={"source_relative_path": "borylation-story.npz"})
    result = background_for_dataset(ds)
    assert result["case_id"] == "borylation"
    assert "workflow" in result["content"] and "borylation_case_analysis" in result["content"]
    assert background_for_dataset(replace(ds, source_kind="upload")) is None
    assert background_for_dataset(replace(ds, metadata={"source_relative_path": "finance-ownership.npz"})) is None
    assert background_for_dataset(replace(ds, metadata={"source_relative_path": "tianlongbabu-characters.npz"})) is None
    changed = tmp_path / "borylation-story.npz"
    changed.write_bytes(b"some other graph")
    assert background_for_dataset(replace(ds, storage_path=str(changed))) is None

def test_tool_shares_session_graph_read_lock():
    policy = get_tool_execution_metadata("borylation_case_analysis")
    assert policy["reads"] == ["graph"]
    assert not policy["writes"]

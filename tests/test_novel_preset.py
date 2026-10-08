import hashlib
import json
import pytest
from graph_engine.novel_preset import DATASET_ID, FILENAME, RESOURCES, install_novel_preset
from graph_data_service.dataset_registry import DatasetRegistry
from graph_data_service.graph_data import GraphData


def test_complete_novel_is_bundled_and_installed_without_llm(tmp_path, monkeypatch):
    monkeypatch.delenv("GRAPH_CONSTRUCTION_API_KEY", raising=False)
    manifest = json.loads((RESOURCES/"manifest.json").read_text(encoding="utf-8"))
    assert manifest["construction"]["model"] == "deepseek-v4-pro"
    extraction = manifest["construction"]["semantic_extraction"]
    assert extraction["chunks_completed"] == extraction["chunks_total"] == extraction["chunk_count"]
    assert manifest["sources"]["天龙八部.txt"]["bytes"] == 2459800
    assert hashlib.sha256((RESOURCES/FILENAME).read_bytes()).hexdigest() == manifest["sha256"]
    install_novel_preset(tmp_path)
    path = tmp_path/"global/datasets"/DATASET_ID/"graph.npz"
    data = GraphData.load(str(path))
    names = {node["properties"]["data"]["name"] for node in data.graph_metadata["construction_graph"]["nodes"]}
    assert {"段誉", "虚竹", "萧峰", "乔峰"}.issubset(names)
    assert data.num_nodes > 100 and data.num_edges > 100
    assert (tmp_path/"server_datasets"/FILENAME).is_file()
    path.write_bytes(b"saved-user-version")
    install_novel_preset(tmp_path)
    assert path.read_bytes() == b"saved-user-version"

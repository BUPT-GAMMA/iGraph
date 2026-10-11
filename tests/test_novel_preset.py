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

def test_novel_names_are_exactly_searchable_without_embedding_service():
    from graph_data_service.embedding import LexicalProvider
    from graph_data_service.search import EntitySearchIndex, NodeSearchRequest

    graph = GraphData.load(str(RESOURCES / FILENAME))
    index = EntitySearchIndex(graph, 1, LexicalProvider())
    identities = []
    for name in ("萧峰", "乔峰", "段誉", "虚竹"):
        result = index.search_nodes(NodeSearchRequest(query=name))
        assert result["success"] and not result["ambiguous"]
        assert result["best"]["confidence"] == "high"
        assert result["best"]["display_name"] == name
        assert result["best"]["score"] == 1.0
        node_id = result["best"]["node_id"]
        identity = result["best"]["node_name"]
        assert graph.id2node[node_id] == identity
        identities.append(identity)
    # The extracted graph stores these aliases separately; search must not merge them.
    assert len(set(identities)) == 4

def test_manual_lineage_is_packaged_and_survives_fresh_install(tmp_path):
    from pathlib import Path
    import json
    from graph_data_service.graph_data import GraphData
    from graph_data_service.construction import ConstructionStore
    from graph_engine.user_case_sources import seed_construction
    install_novel_preset(tmp_path)
    data = GraphData.load(str(tmp_path / "server_datasets" / FILENAME))
    graph = data.graph_metadata["construction_graph"]
    names = {n["id"]: n["properties"]["data"]["name"] for n in graph["nodes"]}
    manual = [e for e in graph["edges"] if e["properties"].get("provenance", {}).get("origin") == "manual"]
    assert {(names[e["source"]], names[e["target"]], e["type"]) for e in manual} == {
        ("无崖子", "王夫人", "生父"), ("李秋水", "王夫人", "生母"),
        ("李秋水", "梦姑", "祖母"), ("虚竹", "梦姑", "丈夫")}
    assert all("https://" in e["properties"]["provenance"]["evidence"] for e in manual)
    assert len(graph["nodes"]) == 304 and len(graph["edges"]) == 715
    manifest = json.loads((RESOURCES / "manifest.json").read_text())
    source = manifest["construction_source"]
    source_path = tmp_path / "server_datasets" / ".user-case-sources" / Path(FILENAME).stem / source["sha256"] / source["filename"]
    store = ConstructionStore(tmp_path)
    seed_construction(store, "u", "new-novel", source_path, source["filename"], "tianlongbabu")
    assert len(store.graph("u", "new-novel")["graph"]["edges"]) == 715

"""Install the first molecular graph with its experimental evidence as metadata."""
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

RESOURCES = Path(__file__).resolve().parent / "resources" / "borylation"


def build_borylation_graph(resources: Path = RESOURCES, *, case: dict | None = None):
    """Load BO-01 only; products and experiments never become graph nodes."""
    from graph_data_service.construction import to_graph_data
    from graph_data_service.graph_data import GraphData

    case = case or json.loads((resources / "processed/system_case.json").read_text(encoding="utf-8"))
    source = GraphData.load(str(resources / "graphs/BO-01.json"))
    if case["molecule_id"] != "BO-01" or source.graph_metadata["input_smiles"] != case["smiles"]:
        raise ValueError("First molecule and case evidence do not match")
    atoms = sorted(case["atoms"], key=lambda atom: atom["atom_index"])
    if [atom["atom_index"] for atom in atoms] != list(range(source.num_nodes)):
        raise ValueError("First molecule atom map does not match")
    names = [f'BO-01:atom:{atom["atom_index"]}' for atom in atoms]
    nodes = [{"id": name, "type": "atom", "properties": {
        **atom, "label": f'{atom["element"]}{atom["display_number"]}'}}
        for name, atom in zip(names, atoms)]
    bonds = {(min(b["begin"], b["end"]), max(b["begin"], b["end"])): b
             for b in case["chemical_bonds"]}
    if {(min(int(a), int(b)), max(int(a), int(b))) for a, b in source.edge_index.T} != set(bonds):
        raise ValueError("First molecule bonds and evidence do not match")
    # One property relationship per chemical bond. Its two storage entries
    # share an ID so degree and edge-count tools do not double-count bonds.
    edges = [{"id": f"bond:{a}:{b}", "type": "chemical_bond", "source": names[a], "target": names[b],
              "properties": bond} for (a, b), bond in bonds.items()]
    edge_names = [f"bond:{min(int(a), int(b))}:{max(int(a), int(b))}" for a, b in source.edge_index.T]
    topology = to_graph_data({"dataset_id": "ds_borylation_story_v1", "revision": 2,
                              "graph": {"directed": False, "nodes": nodes, "edges": edges}})
    return GraphData(num_nodes=source.num_nodes, edge_index=source.edge_index,
        id2node=topology.id2node, node2id=topology.node2id, edge_names=edge_names,
        edge_weight=source.edge_weight, node_feat=source.node_feat, edge_feat=source.edge_feat,
        node_type=topology.node_type, edge_type=["chemical_bond"] * source.num_edges, node_text=source.node_text,
        directed=False, semantic_type="undirected", storage_type="bidirectional",
        name="分子硼化反应示例", graph_metadata={**source.graph_metadata, **topology.graph_metadata,
            "borylation_case": case, "selected_graph": "BO-01", "loaded_graph_count": 1},
        graph_desc="仅加载第一张 BO-01 分子结构图：22 个重原子、24 条无向化学键（48 条双向存储边）。产物和96条实验记录保存在图的背景元数据中，不作为节点或额外图加载。当前案例是公开实验复盘，非模型预测。")


def _upgrade_snapshot(path: Path, asset: Path, previous: set[str]) -> bool:
    """Replace only a byte-identical former preset; preserve edits and a backup."""
    if not path.is_file():
        return False
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    if checksum not in previous:
        return False
    backup = path.parent / ".preset-backups" / checksum / path.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        shutil.copyfile(path, backup)
    fd, temporary = tempfile.mkstemp(prefix=".preset-", dir=path.parent)
    os.close(fd)
    try:
        shutil.copyfile(asset, temporary)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def install_borylation_preset(data_root: Path, *, resources: Path = RESOURCES,
                              server_root: Path | None = None) -> dict:
    from graph_engine.bundled_presets import install_bundled_preset
    from graph_data_service.construction import _atomic_json

    receipt = install_bundled_preset(data_root, resources=resources, server_root=server_root)
    manifest = json.loads((resources / "manifest.json").read_text(encoding="utf-8"))
    previous = set(manifest.get("previous_sha256", []))
    asset = resources / manifest["filename"]
    directory = data_root / "global/datasets" / receipt["dataset_id"]
    if _upgrade_snapshot(directory / "graph.npz", asset, previous):
        metadata = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        versions = list(metadata.get("versions", []))
        version = f"v{len(versions) + 1:03d}"
        while (directory / "versions" / f"{version}.npz").exists():
            version = f"v{int(version[1:]) + 1:03d}"
        shutil.copyfile(asset, directory / "versions" / f"{version}.npz")
        _atomic_json(directory / "meta.json", {**metadata, **manifest["dataset"],
            "name": metadata["name"], "created_at": metadata["created_at"], "versions": [*versions, version]})
    public_root = server_root or Path(os.getenv("GRAPH_ENGINE_SERVER_DATASETS_DIR") or data_root / "server_datasets")
    _upgrade_snapshot(public_root / manifest["filename"], asset, previous)
    return receipt

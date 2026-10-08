"""Build and install the bundled, lossless finance ownership graph."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DATASET_ID = "ds_finance_ownership_v1"
FILENAME = "finance-ownership.npz"
RESOURCES = Path(__file__).resolve().parent / "resources" / "finance"
TABLES = ("institution_main", "individual_info", "institution_shareholder_relationship")


def build_finance_graph(source_dir: Path) -> tuple[dict, dict]:
    """Keep all CSV cells and join typed business IDs, never names.

    Ownership points from shareholder to investee. Edge row endpoints retain
    the source relationships expected by the existing finance tools.
    """
    nodes, edges, sources, rows = [], [], {}, {}
    ids = set()
    for table in TABLES:
        path = source_dir / f"{table}.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"STATC_DT", "IDV_ID" if table == TABLES[1] else "UUID"}
            if table == TABLES[2]:
                required |= {"REL_SHRHLR_CUST_ID", "REL_SHRHLR_TP_CODE", "REL_SHRHLR_HOLD_SHR_RTO"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError(f"{path.name}: missing required columns")
            rows[table] = list(reader)
        if not rows[table] or any(None in row or None in row.values() for row in rows[table]):
            raise ValueError(f"{path.name}: empty or malformed CSV table")
        sources[path.name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "rows": len(rows[table])}
    for table, prefix, key, label in ((TABLES[0], "I:", "UUID", "CHN_NM"),
                                      (TABLES[1], "P:", "IDV_ID", "IDV_NM")):
        for index, row in enumerate(rows[table]):
            business_id = row[key].strip()
            identity = prefix + business_id
            if not business_id or identity in ids:
                raise ValueError(f"{table}: missing or duplicate identity at CSV line {index + 2}")
            ids.add(identity)
            nodes.append({"id": identity, "type": table, "properties": {
                "table": table, "row_index": index, "data": row, "label": row.get(label, business_id)}})
    for index, row in enumerate(rows[TABLES[2]]):
        kind = row["REL_SHRHLR_TP_CODE"].strip()
        if kind not in {"0", "1"}:
            raise ValueError(f"Unknown shareholder type at CSV line {index + 2}: {kind!r}")
        holder_table, holder_key = (TABLES[0], "UUID") if kind == "0" else (TABLES[1], "IDV_ID")
        holder = ("I:" if kind == "0" else "P:") + row["REL_SHRHLR_CUST_ID"].strip()
        target = "I:" + row["UUID"].strip()
        if holder not in ids or target not in ids:
            raise ValueError(f"Missing ownership endpoint at CSV line {index + 2}")
        endpoints = [{"node_id": identity, "foreign_key": {
            "table": TABLES[2], "columns": [column], "ref_table": ref_table, "ref_columns": [ref_key]}}
            for identity, column, ref_table, ref_key in (
                (holder, "REL_SHRHLR_CUST_ID", holder_table, holder_key), (target, "UUID", TABLES[0], "UUID"))]
        edges.append({"id": f"ownership:line:{index + 2}", "type": "ownership", "source": holder,
            "target": target, "properties": {"table": TABLES[2], "row_index": index,
            "data": row, "row_endpoints": endpoints, "ratio": row["REL_SHRHLR_HOLD_SHR_RTO"]}})
    return {"directed": True, "nodes": nodes, "edges": edges}, sources


def prepare_finance_preset(source_dir: Path, destination: Path = RESOURCES) -> dict:
    from graph_data_service.construction import to_graph_data
    from graph_data_service.finance import analyze_finance
    from graph_data_service.dataset_registry import DatasetMeta
    import numpy as np

    graph, sources = build_finance_graph(source_dir)
    inspection = analyze_finance(graph, "snapshot", max_seconds=60)
    if not inspection.get("complete"):
        raise ValueError(f"Finance projection is incomplete: {inspection.get('quality')}")
    destination.mkdir(parents=True, exist_ok=True)
    data = to_graph_data({"dataset_id": DATASET_ID, "revision": 1, "graph": graph})
    # Retain the existing NPZ contract while compressing the bundled asset.
    with tempfile.TemporaryDirectory(dir=destination) as stage:
        uncompressed = Path(stage) / FILENAME
        data.save(str(uncompressed))
        with np.load(uncompressed, allow_pickle=True) as archive:
            np.savez_compressed(destination / FILENAME, **{key: archive[key] for key in archive.files})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = DatasetMeta(dataset_id=DATASET_ID, name="金融股权三表（预置图）",
        description="机构、个人和原始持股三表构成的有向属性图；股东指向被投资机构。保留原始字段和证据，可直接使用金融分析工具。",
        source_type="finance_preset", node_count=data.num_nodes, edge_count=data.num_edges,
        is_directed=True, created_at=now, updated_at=now, versions=["v001"],
        edge_entry_count=data.num_edge_entries, unique_edge_count=data.num_unique_edges,
        num_unique_directed_edges=data.num_unique_directed_edges,
        num_unique_undirected_edges=data.num_unique_undirected_edges,
        semantic_type=data.semantic_type, storage_type=data.storage_type,
        is_symmetric=data.is_symmetric, has_self_loops=data.has_self_loops,
        has_duplicate_entries=data.has_duplicate_entries)
    manifest = {"version": 1, "filename": FILENAME, "dataset": meta.to_dict(), "sources": sources,
        "sha256": hashlib.sha256((destination / FILENAME).read_bytes()).hexdigest(),
        "inspection": {"entity_count": inspection["result"]["entity_count"],
            "ownership_fact_count": inspection["result"]["ownership_fact_count"],
            "quality_issue_counts": inspection["quality"]["issue_counts"]}}
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def install_finance_preset(data_root: Path, *, resources: Path = RESOURCES,
                           server_root: Path | None = None) -> dict:
    """Publish complete snapshots once; restarts never replace saved user data."""
    manifest = json.loads((resources / "manifest.json").read_text(encoding="utf-8"))
    asset = resources / FILENAME
    if hashlib.sha256(asset.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("Bundled finance graph checksum mismatch")
    registry = data_root / "global" / "datasets"
    registry.mkdir(parents=True, exist_ok=True)
    target = registry / DATASET_ID
    if not target.exists():
        stage = Path(tempfile.mkdtemp(prefix=".finance-", dir=registry))
        try:
            (stage / "versions").mkdir()
            shutil.copyfile(asset, stage / "graph.npz")
            shutil.copyfile(asset, stage / "versions" / "v001.npz")
            (stage / "meta.json").write_text(json.dumps(manifest["dataset"], ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.rename(stage, target)
            except OSError:
                if not (target / "meta.json").is_file():
                    raise
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    server_root = server_root or Path(os.getenv("GRAPH_ENGINE_SERVER_DATASETS_DIR") or data_root / "server_datasets")
    server_root.mkdir(parents=True, exist_ok=True)
    public_asset = server_root / FILENAME
    if not public_asset.exists():
        fd, temporary = tempfile.mkstemp(prefix=".finance-", dir=server_root)
        os.close(fd)
        try:
            shutil.copyfile(asset, temporary)
            # Publish the complete file exclusively; registry copies stay independent.
            try:
                os.link(temporary, public_asset)
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {"dataset_id": DATASET_ID, "server_filename": FILENAME,
        "node_count": manifest["dataset"]["node_count"], "edge_count": manifest["dataset"]["edge_count"]}

"""Publish the source files and saved construction behind bundled examples."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from graph_data_service.construction import ConstructionError, _atomic_json, validate_graph


def install_source(resources: Path, manifest: dict, server_root: Path):
    source = manifest.get("construction_source")
    if source is None:
        return
    for value in (source["asset"], source["filename"]):
        if Path(value).name != value or "/" in value or "\\" in value:
            raise ValueError("Invalid bundled source filename")
    raw = gzip.decompress((resources / source["asset"]).read_bytes())
    if hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise ValueError("Bundled source checksum mismatch")
    parent = server_root / ".user-case-sources" / Path(manifest["filename"]).stem
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / source["sha256"]
    if target.exists():
        _atomic_json(parent / "manifest.json", source)
        return
    stage = Path(tempfile.mkdtemp(prefix=".source-", dir=parent))
    try:
        (stage / source["filename"]).write_bytes(raw)
        (stage / "manifest.json").write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        try:
            os.rename(stage, target)
        except OSError:
            if not (target / "manifest.json").is_file():
                raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    _atomic_json(parent / "manifest.json", source)


def seed_construction(store, user_id, dataset_id, source_path, source_filename, case_id):
    # Fixed installed assets only: an API caller cannot select an arbitrary file.
    if case_id not in {"finance", "tianlongbabu"}:
        raise ConstructionError("unknown_user_case", "示例不存在", 404)
    resources = Path(__file__).resolve().parent / "resources" / case_id
    manifest = json.loads((resources / "manifest.json").read_text(encoding="utf-8"))
    source = manifest["construction_source"]
    if source_filename != source["filename"] or hashlib.sha256(Path(source_path).read_bytes()).hexdigest() != source["sha256"]:
        raise ConstructionError("preset_source_mismatch", "示例源文件与数据集不匹配", 409)
    with store._write_lock(user_id, dataset_id) as directory:
        record = store._read(directory, dataset_id)
        if record.get("graph") is not None or record.get("draft_graph") is not None or record.get("revision", 0):
            return {"success": True, **store._status(record, directory), "reused": True}
        asset = resources / manifest["filename"]
        if hashlib.sha256(asset.read_bytes()).hexdigest() != manifest["sha256"]:
            raise ValueError("Bundled graph checksum mismatch")
        from graph_data_service.graph_data import GraphData
        graph = validate_graph(GraphData.load(str(asset)).graph_metadata["construction_graph"])
        record = store._register(directory, record, source_path, source_filename)
        record.update(graph=graph, revision=1, status="ready", last_build_status="ready",
            source_tables=source.get("metadata", {}).get("csv_bundle", {}).get("tables", []),
            user_case_id=case_id, message="预置示例图已保存，可查看、优化或用于分析。",
            updated_at=datetime.now(timezone.utc).isoformat())
        _atomic_json(directory / "record.json", record)
        return {"success": True, **store._status(record, directory)}

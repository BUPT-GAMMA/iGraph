"""Immutable graph snapshot copying after both source and target authorization."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from graph_data_service.construction import ConstructionError, _atomic_json
from graph_data_service.construction_schema import source_schema_tables


def copy_constructed_graph(store, user_id, source_dataset_id, target_dataset_id, source_path, source_filename):
    if source_dataset_id == target_dataset_id:
        raise ConstructionError("invalid_import", "请选择不同的目标数据集", 422)
    # A source edit/build and an import must not publish a mixed revision.
    with store._write_lock(user_id, source_dataset_id) as source_dir:
        source = store._read(source_dir, source_dataset_id)
        if source.get("status") != "ready" or source.get("graph") is None:
            raise ConstructionError("graph_not_ready", "源数据集尚未完成构图", 409)
        snapshot = copy.deepcopy(source["graph"])
        source_tables = source_schema_tables(source_dir, source.get("source_tables"))
        revision = source["revision"]
    with store._write_lock(user_id, target_dataset_id) as target_dir:
        target = store._read(target_dir, target_dataset_id)
        if target.get("graph") is not None or target.get("revision", 0):
            raise ConstructionError("import_conflict", "目标已有构图结果，不能覆盖", 409)
        target = store._register(target_dir, target, source_path, source_filename)
        target.update(graph=snapshot, revision=1, status="ready", last_build_status="ready",
                      source_tables=source_tables,
                      message="已从个人构图数据集创建独立副本。",
                      imported_from={"dataset_id": source_dataset_id, "revision": revision},
                      updated_at=datetime.now(timezone.utc).isoformat())
        _atomic_json(target_dir / "record.json", target)
        return {"success": True, **store._status(target, target_dir), "source_revision": revision}

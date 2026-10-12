"""Fail-closed validation for catalog-bound snapshot and artifact manifests."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from graphscope_runtime.storage_codec import SUPPORTED_SNAPSHOT_FORMATS


_SNAPSHOT_ID = re.compile(r"snap_[0-9a-f]{16}")
_ARTIFACT_ID = re.compile(r"artifact_[0-9a-f]{16}")
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
# An algorithm may expose several logical result datasets (for example all
# distances plus the complete paths selected for preview).  Each dataset is
# partitioned independently, so the file bound is a small multiple of the
# snapshot partition count rather than exactly one file per partition.
_MAX_RESULT_FILE_GROUPS = 16
_SNAPSHOT_STORAGE_KINDS = ("nodes", "edges", "adjacency", "node_order")


class ManifestBindingError(ValueError):
    """A manifest is not the immutable object named by its catalog row."""


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestBindingError(f"{field} 缺失或类型错误")
    return value


def _required_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ManifestBindingError(f"{field} 类型错误")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ManifestBindingError(f"{field} 类型错误") from exc
    if str(value).strip() != str(parsed) and not isinstance(value, int):
        raise ManifestBindingError(f"{field} 不是规范整数")
    return parsed


def _read_managed_manifest(
    raw_path: Any,
    *,
    managed_root: str | Path,
    object_id: str,
    id_pattern: re.Pattern[str],
    graph_id: str,
    graph_version: int,
    expected_scope: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    if id_pattern.fullmatch(object_id) is None:
        raise ManifestBindingError("对象 ID 格式非法")
    try:
        root = Path(managed_root).resolve(strict=True)
        manifest_path = Path(_required_text(raw_path, "manifest_path")).resolve(
            strict=True
        )
    except (OSError, RuntimeError) as exc:
        raise ManifestBindingError("清单路径不可读") from exc
    if not root.is_dir() or not manifest_path.is_file():
        raise ManifestBindingError("清单不是常规文件")
    try:
        relative_parts = manifest_path.relative_to(root).parts
    except ValueError as exc:
        raise ManifestBindingError("清单越出受管根目录") from exc

    # Published layout is root/scope/graph/vNNNNNNNN/object/manifest.json.
    # Requiring the exact depth prevents a catalog path from being rebound to
    # a different scope or arbitrary file which merely happens to live below
    # the same broad storage root.
    expected_version = f"v{int(graph_version):08d}"
    if (
        len(relative_parts) != 5
        or relative_parts[-1] != "manifest.json"
        or relative_parts[-2] != object_id
        or relative_parts[-3] != expected_version
        or relative_parts[-4] != graph_id
    ):
        raise ManifestBindingError("清单路径与图版本或对象 ID 不绑定")
    if expected_scope is not None and not (
        relative_parts[0] == expected_scope
        or relative_parts[0].startswith(f"{expected_scope}__lease_")
    ):
        raise ManifestBindingError("清单路径与租户会话目录不绑定")
    try:
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ManifestBindingError("清单大小超过控制面上限")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestBindingError("清单内容不可读") from exc
    if not isinstance(payload, dict):
        raise ManifestBindingError("清单必须是 JSON 对象")
    return manifest_path, payload


def _bound_regular_file(object_root: Path, relative: Any, *, kind: str) -> Path:
    text = _required_text(relative, kind)
    pure = PurePosixPath(text)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or "\\" in text
    ):
        raise ManifestBindingError(f"{kind} 使用了非法相对路径")
    candidate = object_root.joinpath(*pure.parts)
    # Published snapshots/artifacts contain regular files, never links. This
    # closes the check/open gap where a link could point at another tenant's
    # otherwise valid partition below the same managed root.
    cursor = object_root
    for part in pure.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ManifestBindingError(f"{kind} 不允许符号链接")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(object_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ManifestBindingError(f"{kind} 越出对象目录或不存在") from exc
    if not resolved.is_file():
        raise ManifestBindingError(f"{kind} 不是常规文件")
    return resolved


def measure_snapshot_storage(
    object_root: str | Path,
    *,
    node_files: list[str],
    edge_files: list[str],
    adjacency_files: list[str],
    node_order_file: str | None,
) -> dict[str, Any]:
    """Return a bounded, per-shard accounting of immutable snapshot bytes.

    ``manifest.json`` itself is intentionally excluded because including its
    own serialised size would be recursive.  Snapshot partition counts are
    capped at 128, so these arrays remain control-plane bounded while allowing
    integrity checks to detect a size change in any individual shard.
    """

    try:
        root = Path(object_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ManifestBindingError("快照对象目录不可读") from exc

    groups: dict[str, list[int]] = {}
    for name, relatives in (
        ("nodes", node_files),
        ("edges", edge_files),
        ("adjacency", adjacency_files),
    ):
        try:
            sizes = [
                int(
                    _bound_regular_file(
                        root, relative, kind=f"{name}_storage"
                    ).stat().st_size
                )
                for relative in relatives
            ]
        except OSError as exc:
            raise ManifestBindingError(f"{name} 快照分片大小不可读") from exc
        groups[name] = sizes
    try:
        groups["node_order"] = (
            [
                int(
                    _bound_regular_file(
                        root, node_order_file, kind="node_order_storage"
                    ).stat().st_size
                )
            ]
            if node_order_file is not None
            else []
        )
    except OSError as exc:
        raise ManifestBindingError("节点顺序索引大小不可读") from exc
    payload: dict[str, Any] = {
        name: {"total": int(sum(groups[name])), "files": groups[name]}
        for name in _SNAPSHOT_STORAGE_KINDS
    }
    payload["total"] = int(
        sum(int(payload[name]["total"]) for name in _SNAPSHOT_STORAGE_KINDS)
    )
    return payload


def _validate_snapshot_storage_bytes(
    manifest_path: Path, manifest: dict[str, Any]
) -> None:
    declared = manifest.get("storage_bytes")
    if not isinstance(declared, dict):
        # Storage-aware admission cannot safely estimate older snapshots from
        # V/E counts alone (long identifiers and SQLite indexes vary widely).
        raise ManifestBindingError("快照缺少可验证的 storage_bytes，请重新准备")
    if set(declared) != {*_SNAPSHOT_STORAGE_KINDS, "total"}:
        raise ManifestBindingError("storage_bytes 字段集合不规范")
    partition_count = int(manifest["partitions"])
    expected_file_counts = {
        "nodes": partition_count,
        "edges": partition_count,
        "adjacency": len(manifest.get("adjacency_files") or []),
        "node_order": 1 if manifest.get("node_order_file") is not None else 0,
    }
    for name in _SNAPSHOT_STORAGE_KINDS:
        group = declared.get(name)
        if not isinstance(group, dict) or set(group) != {"total", "files"}:
            raise ManifestBindingError(f"storage_bytes.{name} 结构错误")
        sizes = group.get("files")
        if not isinstance(sizes, list) or len(sizes) != expected_file_counts[name]:
            raise ManifestBindingError(f"storage_bytes.{name}.files 数量错误")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in sizes
        ):
            raise ManifestBindingError(f"storage_bytes.{name}.files 类型错误")
        total = group.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ManifestBindingError(f"storage_bytes.{name}.total 类型错误")
        if total != sum(sizes):
            raise ManifestBindingError(f"storage_bytes.{name}.total 与分片不一致")
    total = declared.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ManifestBindingError("storage_bytes.total 类型错误")
    if total != sum(int(declared[name]["total"]) for name in _SNAPSHOT_STORAGE_KINDS):
        raise ManifestBindingError("storage_bytes.total 与分类总量不一致")

    actual = measure_snapshot_storage(
        manifest_path.parent,
        node_files=list(manifest.get("node_files") or []),
        edge_files=list(manifest.get("edge_files") or []),
        adjacency_files=list(manifest.get("adjacency_files") or []),
        node_order_file=manifest.get("node_order_file"),
    )
    if declared != actual:
        raise ManifestBindingError("storage_bytes 与快照分片实际大小不一致")


def _validate_snapshot_partitions(
    manifest_path: Path, manifest: dict[str, Any], partitions: int
) -> None:
    object_root = manifest_path.parent
    expected_fields = {
        "node_files": [
            f"nodes/part-{index:05d}.jsonl" for index in range(partitions)
        ],
        "edge_files": [
            f"edges/part-{index:05d}.jsonl" for index in range(partitions)
        ],
    }
    if manifest.get("adjacency_format") == "igraph-sharded-sqlite-adjacency-v1":
        expected_fields["adjacency_files"] = [
            f"adjacency/part-{index:05d}.sqlite3"
            for index in range(partitions)
        ]
    elif "adjacency_files" in manifest:
        raw_adjacency = manifest.get("adjacency_files")
        if not isinstance(raw_adjacency, list) or len(raw_adjacency) != partitions:
            raise ManifestBindingError("adjacency_files 与分区数不一致")
        for relative in raw_adjacency:
            _bound_regular_file(object_root, relative, kind="adjacency_files")

    for field, expected in expected_fields.items():
        actual = manifest.get(field)
        if actual != expected:
            raise ManifestBindingError(f"{field} 未绑定到规范分区集合")
        for relative in actual:
            _bound_regular_file(object_root, relative, kind=field)

    node_order = manifest.get("node_order_file")
    if node_order is not None:
        if node_order != "node-order.jsonl":
            raise ManifestBindingError("node_order_file 未绑定到规范索引")
        _bound_regular_file(object_root, node_order, kind="node_order_file")


def validate_snapshot_manifest(
    snapshot: dict[str, Any],
    *,
    snapshot_root: str | Path,
    graph_id: str,
    graph_version: int,
    expected_user_id: str | None = None,
    expected_session_id: str | None = None,
    expected_scope: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Bind a catalog snapshot, job identity and every advertised shard."""
    if not isinstance(snapshot, dict):
        raise ManifestBindingError("快照目录记录类型错误")
    snapshot_id = _required_text(snapshot.get("snapshot_id"), "snapshot_id")
    expected_graph = _required_text(graph_id, "graph_id")
    expected_version = _required_int(graph_version, "graph_version")
    if snapshot.get("graph_id") != expected_graph:
        raise ManifestBindingError("目录快照与任务 graph_id 不一致")
    if (
        _required_int(snapshot.get("graph_version"), "catalog.graph_version")
        != expected_version
    ):
        raise ManifestBindingError("目录快照与任务 graph_version 不一致")
    if snapshot.get("state") != "published":
        raise ManifestBindingError("目录快照尚未发布")
    if expected_user_id is not None and snapshot.get("user_id") != expected_user_id:
        raise ManifestBindingError("目录快照与任务 user_id 不一致")
    if (
        expected_session_id is not None
        and snapshot.get("session_id") != expected_session_id
    ):
        raise ManifestBindingError("目录快照与任务 session_id 不一致")

    manifest_path, manifest = _read_managed_manifest(
        snapshot.get("manifest_path"),
        managed_root=snapshot_root,
        object_id=snapshot_id,
        id_pattern=_SNAPSHOT_ID,
        graph_id=expected_graph,
        graph_version=expected_version,
        expected_scope=expected_scope,
    )
    if manifest.get("state") != "published":
        raise ManifestBindingError("快照清单尚未发布")
    if manifest.get("format") not in SUPPORTED_SNAPSHOT_FORMATS:
        raise ManifestBindingError("快照清单格式不受支持")
    identity_fields = ("snapshot_id", "graph_id", "format")
    for field in identity_fields:
        if manifest.get(field) != snapshot.get(field):
            raise ManifestBindingError(f"快照清单与目录字段 {field} 不一致")
    if (
        _required_int(manifest.get("graph_version"), "manifest.graph_version")
        != expected_version
    ):
        raise ManifestBindingError("快照清单与任务 graph_version 不一致")

    integer_fields = ("node_count", "edge_count", "partitions")
    for field in integer_fields:
        if _required_int(manifest.get(field), f"manifest.{field}") != _required_int(
            snapshot.get(field), f"catalog.{field}"
        ):
            raise ManifestBindingError(f"快照清单与目录字段 {field} 不一致")
    partitions = _required_int(manifest.get("partitions"), "partitions")
    if not 1 <= partitions <= 128:
        raise ManifestBindingError("快照分区数越界")
    for field in ("directed", "has_weights"):
        if not isinstance(manifest.get(field), bool):
            raise ManifestBindingError(f"manifest.{field} 类型错误")
        if bool(snapshot.get(field)) != manifest[field]:
            raise ManifestBindingError(f"快照清单与目录字段 {field} 不一致")

    manifest_quality = manifest.get("quality")
    if not isinstance(manifest_quality, dict):
        raise ManifestBindingError("快照质量元数据类型错误")
    catalog_quality = snapshot.get("quality")
    if catalog_quality is not None and manifest_quality != catalog_quality:
        raise ManifestBindingError("快照质量元数据与目录不一致")
    catalog_policy_id = snapshot.get("policy_id")
    if (
        catalog_policy_id is not None
        and manifest_quality.get("policy_id") != catalog_policy_id
    ):
        raise ManifestBindingError("快照质量策略 ID 与目录不一致")
    catalog_policy = snapshot.get("policy")
    manifest_policy = manifest_quality.get("policy")
    if catalog_policy is not None and manifest_policy != catalog_policy:
        raise ManifestBindingError("快照质量策略与目录不一致")

    _validate_snapshot_partitions(manifest_path, manifest, partitions)
    _validate_snapshot_storage_bytes(manifest_path, manifest)
    return manifest_path, manifest


def validate_artifact_manifest(
    artifact: dict[str, Any],
    *,
    artifact_root: str | Path,
    expected_scope: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Bind an artifact catalog row to its manifest, metadata and result files."""
    if not isinstance(artifact, dict):
        raise ManifestBindingError("Artifact 目录记录类型错误")
    artifact_id = _required_text(artifact.get("artifact_id"), "artifact_id")
    graph_id = _required_text(artifact.get("graph_id"), "graph_id")
    graph_version = _required_int(artifact.get("graph_version"), "graph_version")
    manifest_path, manifest = _read_managed_manifest(
        artifact.get("path"),
        managed_root=artifact_root,
        object_id=artifact_id,
        id_pattern=_ARTIFACT_ID,
        graph_id=graph_id,
        graph_version=graph_version,
        expected_scope=expected_scope,
    )
    if manifest.get("format") not in {
        "igraph-large-algorithm-artifact-v1",
        "igraph-node-score-artifact-v1",
    }:
        raise ManifestBindingError("Artifact 清单格式不受支持")
    if manifest.get("artifact_id") != artifact_id:
        raise ManifestBindingError("Artifact 清单 ID 与目录不一致")
    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict) or metadata != artifact.get("metadata"):
        raise ManifestBindingError("Artifact 元数据与目录不一致")
    if (
        metadata.get("graph_id") != graph_id
        or _required_int(metadata.get("graph_version"), "metadata.graph_version")
        != graph_version
        or metadata.get("algorithm") != artifact.get("algorithm")
    ):
        raise ManifestBindingError("Artifact 元数据与目录图身份不一致")
    partitions = _required_int(metadata.get("partitions"), "metadata.partitions")
    if not 1 <= partitions <= 128:
        raise ManifestBindingError("Artifact 元数据分区数越界")
    if manifest.get("summary") != artifact.get("summary"):
        raise ManifestBindingError("Artifact 摘要与目录不一致")

    result_files = manifest.get("result_files")
    if (
        not isinstance(result_files, list)
        or len(result_files) > partitions * _MAX_RESULT_FILE_GROUPS
    ):
        raise ManifestBindingError("Artifact 结果分区列表非法")
    if len(set(map(str, result_files))) != len(result_files):
        raise ManifestBindingError("Artifact 结果分区存在重复绑定")
    for relative in result_files:
        text = _required_text(relative, "result_files")
        pure = PurePosixPath(text)
        if not pure.parts or pure.parts[0] != "results" or pure.suffix != ".jsonl":
            raise ManifestBindingError("Artifact 结果文件不在规范 results 目录")
        _bound_regular_file(manifest_path.parent, relative, kind="result_files")
    return manifest_path, manifest

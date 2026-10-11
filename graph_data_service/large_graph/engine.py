"""Local multi-process algorithms over immutable partitioned snapshots."""

from __future__ import annotations

import heapq
import json
import os
import sqlite3
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import islice
from pathlib import Path
from typing import Any, Callable

from .algorithms import LargeGraphAlgorithms
from .disk_graph import PartitionedDiskGraph
from .durability import durable_publish_tree, remove_tree_and_fsync
from .integrity import validate_snapshot_manifest
from .processes import process_context
from .router import ComputeRouter
from .compatibility import GRAPHSCOPE_ONLY_ALGORITHM_NAMES
from .snapshot import (
    DerivedSnapshotBuilder,
    _AppendHandlePool,
    _configured_int,
    _stable_partition,
)
from graphscope_runtime.storage_codec import decode_edge_row, decode_node_row


ProgressCallback = Callable[[str, float, str, dict[str, Any]], None]
ExternalAlgorithmRunner = Callable[..., dict[str, Any]]
_MAX_CONTROL_PAYLOAD_BYTES = 2 * 1024 * 1024


def _median_from_histogram(
    histogram: dict[float, int], result_count: int
) -> float:
    """Return the exact median of a bounded degree-score histogram."""
    if result_count <= 0:
        return 0.0
    if sum(histogram.values()) != result_count:
        raise ValueError("度数直方图与结果节点数不一致")

    lower_index = (result_count - 1) // 2
    upper_index = result_count // 2
    seen = 0
    lower: float | None = None
    upper: float | None = None
    for score, frequency in sorted(histogram.items()):
        next_seen = seen + int(frequency)
        if lower is None and lower_index < next_seen:
            lower = float(score)
        if upper_index < next_seen:
            upper = float(score)
            break
        seen = next_seen
    if lower is None or upper is None:
        raise ValueError("度数直方图无法覆盖中位数位置")
    return (lower + upper) / 2.0


def _validate_control_payload(summary: dict[str, Any], preview: list[Any]) -> None:
    """Fail closed before a result crosses the Worker/API control plane."""
    try:
        encoded = json.dumps(
            {"summary": summary, "preview": preview},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("大图算法控制面结果不是有限、可序列化 JSON") from exc
    if len(encoded) > _MAX_CONTROL_PAYLOAD_BYTES:
        raise ValueError(
            f"大图算法控制面结果超过 {_MAX_CONTROL_PAYLOAD_BYTES} 字节上限；"
            "完整结果必须写入 Artifact"
        )


def _map_degree_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    edge_path_raw, map_root_raw, map_index, partitions, algorithm = args
    edge_path = Path(edge_path_raw)
    map_root = Path(map_root_raw) / f"map-{map_index:05d}"
    map_root.mkdir(parents=True, exist_ok=True)
    handles = _AppendHandlePool(
        map_root,
        _configured_int(
            "LARGE_GRAPH_MAP_OPEN_FILES", 64, minimum=4, maximum=128
        ),
    )
    contribution_count = 0

    def write(node_id: str, value: float) -> None:
        nonlocal contribution_count
        partition = _stable_partition(node_id, partitions)
        handles.get("part", partition).write(
            json.dumps([node_id, value], ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        contribution_count += 1

    try:
        with edge_path.open("r", encoding="utf-8") as source:
            for line in source:
                src, dst, _, _ = decode_edge_row(line)
                if algorithm == "in_degree":
                    write(dst, 1.0)
                elif algorithm == "out_degree":
                    write(src, 1.0)
                else:
                    # Total degree counts a self loop twice, matching the
                    # established NetworkX/GraphData degree semantics.
                    write(src, 1.0)
                    write(dst, 1.0)
    finally:
        handles.close()
    return {"map_root": str(map_root), "contributions": contribution_count}


def _reduce_degree_partition(args: tuple[Any, ...]) -> dict[str, Any]:
    (
        partition, contribution_paths_raw, node_path_raw, output_path_raw,
        sqlite_path_raw, algorithm, node_count, top_k,
    ) = args
    # A node partition may itself contain millions of rows. Keep its complete
    # aggregation state on disk and retain only one bounded write batch plus
    # the requested top-k heap in Python memory.
    batch_rows = _configured_int(
        "LARGE_GRAPH_REDUCE_BATCH_ROWS", 4096, minimum=128, maximum=65_536
    )
    sqlite_path = Path(sqlite_path_raw)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute(
            """
            CREATE TABLE scores (
                node_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                score REAL NOT NULL
            ) WITHOUT ROWID
            """
        )
    except BaseException:
        connection.close()
        raise

    def flush_nodes(rows: list[tuple[str, str, float]]) -> None:
        if not rows:
            return
        connection.executemany(
            """
            INSERT INTO scores(node_id, name, score) VALUES (?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET name = excluded.name
            """,
            rows,
        )
        rows.clear()

    def flush_contributions(rows: list[tuple[str, str, float]]) -> None:
        if not rows:
            return
        connection.executemany(
            """
            INSERT INTO scores(node_id, name, score) VALUES (?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET score = score + excluded.score
            """,
            rows,
        )
        rows.clear()

    node_path = Path(node_path_raw)
    try:
        node_batch: list[tuple[str, str, float]] = []
        if node_path.exists():
            with node_path.open("r", encoding="utf-8") as nodes:
                for line in nodes:
                    node_id, name, _ = decode_node_row(line)
                    node_batch.append((node_id, name, 0.0))
                    if len(node_batch) >= batch_rows:
                        flush_nodes(node_batch)
            flush_nodes(node_batch)

        contribution_batch: list[tuple[str, str, float]] = []
        for path_raw in contribution_paths_raw:
            path = Path(path_raw)
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8") as source:
                for line in source:
                    node_id, value = json.loads(line)
                    node_id = str(node_id)
                    contribution_batch.append((node_id, node_id, float(value)))
                    if len(contribution_batch) >= batch_rows:
                        flush_contributions(contribution_batch)
            flush_contributions(contribution_batch)
        connection.commit()

        divisor = max(1, int(node_count) - 1)
        output_path = Path(output_path_raw)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        local_top: list[tuple[float, str, str]] = []
        score_sum = 0.0
        minimum: float | None = None
        maximum: float | None = None
        score_histogram: dict[float, int] = {}
        count = 0
        with output_path.open("w", encoding="utf-8") as output:
            for node_id, name, stored_score in connection.execute(
                "SELECT node_id, name, score FROM scores ORDER BY node_id"
            ):
                score = float(stored_score)
                if algorithm == "degree_centrality":
                    score = 1.0 if int(node_count) <= 1 else score / divisor
                count += 1
                score_sum += score
                minimum = score if minimum is None else min(minimum, score)
                maximum = score if maximum is None else max(maximum, score)
                score_histogram[score] = score_histogram.get(score, 0) + 1
                node_id = str(node_id)
                name = str(name)
                output.write(
                    json.dumps(
                        {"node_id": node_id, "node": name, "score": score},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                candidate = (score, node_id, name)
                if len(local_top) < top_k:
                    heapq.heappush(local_top, candidate)
                elif candidate > local_top[0]:
                    heapq.heapreplace(local_top, candidate)
        return {
            "partition": partition,
            "count": count,
            "sum": score_sum,
            "min": minimum if minimum is not None else 0.0,
            "max": maximum if maximum is not None else 0.0,
            "histogram": score_histogram,
            "top": local_top,
            "state_backend": "sqlite",
        }
    finally:
        connection.close()


class LocalParallelGraphEngine:
    """Explicit engine adapter for every mapped partition/disk algorithm."""

    ENGINE_NAME = ComputeRouter.ENGINE_NAME
    ENGINE_VERSION = ComputeRouter.ENGINE_VERSION

    def __init__(
        self,
        artifact_root: str | Path,
        *,
        snapshot_root: str | Path | None = None,
    ) -> None:
        self.artifact_root = Path(artifact_root)
        # Production always supplies the configured root. The optional value
        # preserves the low-level engine API used by isolated algorithm tests;
        # in that mode the canonical five-level snapshot layout is used to
        # infer the root before the same binding checks run.
        self.snapshot_root = Path(snapshot_root) if snapshot_root is not None else None

    def execute(
        self,
        *,
        snapshot: dict[str, Any],
        algorithm: str,
        parameters: dict[str, Any],
        user_scope: str,
        graph_id: str,
        graph_version: int,
        workers: int,
        progress: ProgressCallback | None = None,
        expected_user_id: str | None = None,
        expected_session_id: str | None = None,
        snapshot_scope: str | None = None,
        derived_target: dict[str, Any] | None = None,
        external_runner: ExternalAlgorithmRunner | None = None,
    ) -> dict[str, Any]:
        algorithm = str(algorithm).strip().lower()
        # Added: retain the optimized degree mapper and route the complete
        # small-graph compute catalogue to the disk-backed algorithm kernel.
        degree_algorithms = {"degree", "in_degree", "out_degree", "degree_centrality"}
        supported = (
            degree_algorithms
            | set(LargeGraphAlgorithms.SUPPORTED)
            | set(GRAPHSCOPE_ONLY_ALGORITHM_NAMES)
        )
        if algorithm not in supported:
            raise ValueError(
                f"并行引擎不支持算法 {algorithm!r}；禁止回退到 NetworkX"
            )
        if self.snapshot_root is None:
            unresolved_manifest = Path(str(snapshot["manifest_path"])).resolve()
            try:
                validation_root = unresolved_manifest.parents[4]
            except IndexError as exc:
                raise ValueError("快照路径不符合受管目录布局") from exc
        else:
            validation_root = self.snapshot_root
        expected_scope = str(snapshot_scope or user_scope)
        if "__lease_" in expected_scope:
            expected_scope = expected_scope.rsplit("__lease_", 1)[0]
        snapshot_graph_id = str(snapshot.get("graph_id") or graph_id)
        snapshot_graph_version = int(
            snapshot.get("graph_version") or graph_version
        )
        manifest_path, manifest = validate_snapshot_manifest(
            snapshot,
            snapshot_root=validation_root,
            graph_id=snapshot_graph_id,
            graph_version=snapshot_graph_version,
            expected_user_id=expected_user_id,
            expected_session_id=expected_session_id,
            expected_scope=(
                expected_scope
                if expected_user_id is not None or expected_session_id is not None
                else None
            ),
        )

        partitions = int(manifest["partitions"])
        workers = max(1, min(int(workers), partitions, os.cpu_count() or 1))
        raw_top_k = parameters.get("top_k", 20)
        # "all" is valid on legacy small-graph tools, but the Agent-facing
        # summary of a large graph is always bounded; the Artifact still holds all rows.
        top_k = 200 if str(raw_top_k).strip().lower() == "all" else int(raw_top_k)
        top_k = max(1, min(top_k, 200))
        artifact_id = f"artifact_{uuid.uuid4().hex[:16]}"
        graph_root = self.artifact_root / user_scope / graph_id / f"v{int(graph_version):08d}"
        staging = graph_root / f".{artifact_id}.staging"
        final = graph_root / artifact_id
        result_root = staging / "results"
        map_root = staging / "map"
        staging.mkdir(parents=True, exist_ok=False)
        derived_snapshot: dict[str, Any] | None = None

        def emit(stage: str, value: float, message: str, detail: dict[str, Any] | None = None) -> None:
            if progress is not None:
                progress(stage, value, message, detail or {})

        try:
            if algorithm not in degree_algorithms or external_runner is not None:
                if (
                    algorithm in GRAPHSCOPE_ONLY_ALGORITHM_NAMES
                    and external_runner is None
                ):
                    raise ValueError(f"{algorithm} 仅支持 GraphScope 大图后端")
                if external_runner is None:
                    emit(
                        "algorithm", 0.10, "开始执行分区外存算法",
                        {"algorithm": algorithm, "partitions": partitions, "workers": workers},
                    )
                    with PartitionedDiskGraph(manifest_path) as graph:
                        kernel = LargeGraphAlgorithms(
                            graph,
                            staging / "work",
                            result_root,
                            workers=workers,
                            progress=progress,
                        )
                        output = kernel.run(algorithm, parameters)
                else:
                    emit(
                        "algorithm", 0.10, "提交 GraphScope 原生算法",
                        {"algorithm": algorithm, "partitions": partitions, "workers": workers},
                    )
                    output = external_runner(
                        manifest_path=manifest_path,
                        manifest=manifest,
                        staging_root=staging,
                        result_root=result_root,
                        work_root=staging / "work",
                        algorithm=algorithm,
                        parameters=parameters,
                        workers=workers,
                        top_k=top_k,
                        progress=progress,
                    )
                algorithm_summary = dict(output["summary"])
                if algorithm_summary.get("success") is False:
                    raise ValueError(
                        str(
                            algorithm_summary.get("error")
                            or algorithm_summary.get("message")
                            or f"{algorithm} 执行失败"
                        )
                    )
                summary = {"algorithm": algorithm, **algorithm_summary}
                if (
                    algorithm == "subgraph_extraction"
                    and parameters.get("create_version", True)
                    and derived_target is not None
                ):
                    if not isinstance(derived_target, dict):
                        raise ValueError("子图派生版本目标记录无效")
                    if self.snapshot_root is None:
                        raise ValueError("子图派生版本需要受管 snapshot_root")
                    target_version = int(derived_target["graph_version"])
                    derived_snapshot = DerivedSnapshotBuilder(self.snapshot_root).build(
                        source_manifest_path=manifest_path,
                        selection_files=(
                            staging / relative
                            for relative in output.get("result_files", [])
                        ),
                        user_scope=user_scope,
                        graph_id=graph_id,
                        graph_version=target_version,
                        workers=workers,
                        method=str(parameters.get("method") or ""),
                        progress=progress,
                    )
                    if (
                        int(derived_snapshot["node_count"])
                        != int(algorithm_summary.get("num_nodes", -1))
                        or int(derived_snapshot["edge_count"])
                        != int(algorithm_summary.get("num_edges", -1))
                    ):
                        raise ValueError("派生快照计数与子图算法结果不一致")
                    summary.update(
                        {
                            "new_graph_version": target_version,
                            "derived_snapshot_id": derived_snapshot["snapshot_id"],
                            "derived_version_published": True,
                            "current_graph_replaced": False,
                        }
                    )
                control_preview = list(
                    islice(iter(output.get("preview") or ()), 20)
                )
                _validate_control_payload(summary, control_preview)
                backend_metadata = dict(output.get("backend_metadata") or {})
                metadata = {
                    "graph_id": graph_id,
                    "graph_version": int(graph_version),
                    "snapshot_id": manifest["snapshot_id"],
                    "data_quality_policy_id": manifest["quality"]["policy_id"],
                    "algorithm": algorithm,
                    "parameters": parameters,
                    "engine": backend_metadata.get("engine", self.ENGINE_NAME),
                    "engine_version": backend_metadata.get(
                        "engine_version", self.ENGINE_VERSION
                    ),
                    "partitions": partitions,
                    "workers": workers,
                    # Added audit marker: this path did not construct GraphData/NX.
                    "topology_access": backend_metadata.get(
                        "topology_access", "sqlite-sharded-v1"
                    ),
                    # Added: persist the actual execution mode instead of
                    # implying that every algorithm used all worker slots.
                    "execution_mode": output.get("summary", {}).get(
                        "execution_mode", "capability-defined"
                    ),
                    "complexity_class": output.get("summary", {}).get(
                        "complexity_class", "capability-defined"
                    ),
                    "parallel_strategy": output.get("summary", {}).get(
                        "parallel", {}
                    ).get("strategy", "adjacency-shard-map/disk-state-reduce"),
                    "result_processing": output.get("summary", {}).get(
                        "result_processing",
                        {
                            "strategy": "no-materialized-partition-result",
                            "workers": 0,
                        },
                    ),
                }
                for field in (
                    "backend",
                    "adapter_version",
                    "protocol_version",
                    "native_app",
                    "semantic_profile",
                    "graph_cache_hit",
                ):
                    if field in backend_metadata:
                        metadata[field] = backend_metadata[field]
                if derived_snapshot is not None:
                    metadata.update(
                        {
                            "derived_snapshot_id": derived_snapshot["snapshot_id"],
                            "derived_graph_version": int(
                                derived_snapshot["graph_version"]
                            ),
                        }
                    )
                artifact_manifest = {
                    "format": "igraph-large-algorithm-artifact-v1",
                    "artifact_id": artifact_id,
                    "metadata": metadata,
                    "summary": summary,
                    "result_files": list(output.get("result_files", [])),
                }
                manifest_tmp = staging / "manifest.json.tmp"
                manifest_tmp.write_text(
                    json.dumps(artifact_manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                manifest_tmp.replace(staging / "manifest.json")
                remove_tree_and_fsync(staging / "work")
                graph_root.mkdir(parents=True, exist_ok=True)
                durable_publish_tree(
                    staging,
                    final,
                    durability_root=self.artifact_root.parent,
                )
                emit("artifact", 0.95, "Artifact 已原子写出", {"artifact_id": artifact_id})
                result = {
                    "artifact_id": artifact_id,
                    "algorithm": algorithm,
                    "path": str(final / "manifest.json"),
                    "summary": summary,
                    "preview": control_preview,
                    "metadata": metadata,
                }
                if derived_snapshot is not None:
                    result["derived_snapshot"] = derived_snapshot
                return result

            snapshot_root = manifest_path.parent
            edge_files = [snapshot_root / path for path in manifest["edge_files"]]
            node_files = [snapshot_root / path for path in manifest["node_files"]]
            emit("algorithm_map", 0.10, "开始并行扫描边分区", {"partitions": partitions})
            map_results: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=workers, mp_context=process_context()
            ) as pool:
                futures = [
                    pool.submit(
                        _map_degree_partition,
                        (str(path), str(map_root), index, partitions, algorithm),
                    )
                    for index, path in enumerate(edge_files)
                ]
                for completed, future in enumerate(as_completed(futures), 1):
                    map_results.append(future.result())
                    emit(
                        "algorithm_map", 0.10 + 0.40 * completed / len(futures),
                        "边分区计算进行中",
                        {"partitions_completed": completed, "partitions": len(futures)},
                    )

            emit("result_reduce", 0.55, "开始并行聚合结果分区", {})
            map_dirs = [Path(row["map_root"]) for row in map_results]
            reduce_args = [
                (
                    partition,
                    [str(root / f"part-{partition:05d}.jsonl") for root in map_dirs],
                    str(node_files[partition]),
                    str(result_root / f"part-{partition:05d}.jsonl"),
                    str(map_root / f"reduce-{partition:05d}.sqlite3"),
                    algorithm,
                    int(manifest["node_count"]),
                    top_k,
                )
                for partition in range(partitions)
            ]
            reduce_results: list[dict[str, Any]] = []
            with ProcessPoolExecutor(
                max_workers=workers, mp_context=process_context()
            ) as pool:
                futures = [pool.submit(_reduce_degree_partition, args) for args in reduce_args]
                for completed, future in enumerate(as_completed(futures), 1):
                    reduce_results.append(future.result())
                    emit(
                        "result_reduce", 0.55 + 0.35 * completed / len(futures),
                        "结果分区聚合进行中",
                        {"partitions_completed": completed, "partitions": len(futures)},
                    )

            result_count = sum(row["count"] for row in reduce_results)
            score_sum = sum(row["sum"] for row in reduce_results)
            score_histogram: dict[float, int] = {}
            for row in reduce_results:
                for score, frequency in row["histogram"].items():
                    score = float(score)
                    score_histogram[score] = (
                        score_histogram.get(score, 0) + int(frequency)
                    )
            top = heapq.nlargest(
                top_k,
                (candidate for row in reduce_results for candidate in row["top"]),
            )
            top_rows = [
                {"node_id": node_id, "node": name, "score": score}
                for score, node_id, name in top
            ]
            summary = {
                "success": True,
                "algorithm": algorithm,
                "result_count": result_count,
                "score_min": min(
                    (row["min"] for row in reduce_results if row["count"]), default=0.0
                ),
                "score_max": max(
                    (row["max"] for row in reduce_results if row["count"]), default=0.0
                ),
                "score_mean": score_sum / result_count if result_count else 0.0,
                "score_median": _median_from_histogram(
                    score_histogram, result_count
                ),
                "top": top_rows,
                "truncated": result_count > len(top_rows),
                "parallel": {
                    "strategy": "edge-map/node-reduce",
                    "workers": workers,
                    "partitions": partitions,
                },
                "result_processing": {
                    "strategy": "sqlite-node-partition-reduce-writers",
                    "workers": workers,
                },
            }
            control_preview = top_rows[: min(20, top_k)]
            _validate_control_payload(summary, control_preview)
            metadata = {
                "graph_id": graph_id,
                "graph_version": int(graph_version),
                "snapshot_id": manifest["snapshot_id"],
                "data_quality_policy_id": manifest["quality"]["policy_id"],
                "algorithm": algorithm,
                "parameters": {**parameters, "top_k": top_k},
                "engine": self.ENGINE_NAME,
                "engine_version": self.ENGINE_VERSION,
                "partitions": partitions,
                "workers": workers,
                "topology_access": "partitioned-jsonl-map-sqlite-reduce-v1",
                "execution_mode": "exact",
                "complexity_class": "O(V+E)",
                "parallel_strategy": "edge-map/node-reduce",
                "result_processing": dict(summary["result_processing"]),
            }
            artifact_manifest = {
                "format": "igraph-node-score-artifact-v1",
                "artifact_id": artifact_id,
                "metadata": metadata,
                "summary": summary,
                "result_files": [f"results/part-{index:05d}.jsonl" for index in range(partitions)],
            }
            manifest_tmp = staging / "manifest.json.tmp"
            manifest_tmp.write_text(
                json.dumps(artifact_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            manifest_tmp.replace(staging / "manifest.json")
            remove_tree_and_fsync(map_root)
            graph_root.mkdir(parents=True, exist_ok=True)
            durable_publish_tree(
                staging,
                final,
                durability_root=self.artifact_root.parent,
            )
            emit("artifact", 0.95, "Artifact 已原子写出", {"artifact_id": artifact_id})
            return {
                "artifact_id": artifact_id,
                "algorithm": algorithm,
                "path": str(final / "manifest.json"),
                "summary": summary,
                "preview": control_preview,
                "metadata": metadata,
            }
        except BaseException:
            try:
                remove_tree_and_fsync(staging)
            except (OSError, ValueError):
                # Do not mask the algorithm/cancellation failure. Lease-scoped
                # orphan cleanup will retry any staging tree that could not be
                # removed durably here.
                pass
            if derived_snapshot is not None:
                try:
                    derived_manifest = Path(
                        str(derived_snapshot["manifest_path"])
                    ).resolve()
                    allowed_root = (
                        self.snapshot_root.resolve()
                        if self.snapshot_root is not None
                        else None
                    )
                    if (
                        allowed_root is not None
                        and derived_manifest.is_relative_to(allowed_root)
                        and derived_manifest.parent.name.startswith("snap_")
                    ):
                        remove_tree_and_fsync(derived_manifest.parent)
                except (KeyError, OSError, RuntimeError, ValueError):
                    pass
            raise

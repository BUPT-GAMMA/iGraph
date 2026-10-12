from __future__ import annotations

import json
import multiprocessing
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import graph_data_service.large_graph.engine as large_graph_engine
import graph_data_service.large_graph.runtime as large_graph_runtime
from graph_data_service.large_graph.catalog import (
    LargeGraphCatalog,
    project_result_cache_key,
)
from graph_data_service.large_graph.contracts import DataQualityPolicy
from graph_data_service.large_graph.disk_graph import PartitionedDiskGraph
from graph_data_service.large_graph.engine import LocalParallelGraphEngine
from graph_data_service.large_graph.router import ComputeRouter, ResourceManager
from graph_data_service.large_graph.runtime import (
    DurableLargeGraphRuntime,
    _await_worker_registration,
)
from graph_data_service.large_graph.service import LargeGraphService
from graph_data_service.large_graph.snapshot import (
    _AppendHandlePool,
    PartitionedSnapshotBuilder,
    _bounded_json_merge,
    _write_sorted_runs,
)


def _build_snapshot(tmp_path: Path) -> dict:
    source = tmp_path / "edges.txt"
    source.write_text(
        "A B 1 friend\n"
        "a B 3 friend\n"  # same normalised edge; max weight must win
        "B C 2 friend\n"
        "C C 4 friend\n"  # skipped by the explicit self-loop policy
        "invalid\n",
        encoding="utf-8",
    )
    stat = source.stat()
    return PartitionedSnapshotBuilder(tmp_path / "snapshots").build(
        source_path=source,
        user_scope="alice__session",
        graph_id="social",
        graph_version=7,
        directed=True,
        partitions=2,
        workers=2,
        delimiter="whitespace",
        input_format="edgelist",
        policy=DataQualityPolicy(self_loops="skip"),
        expected_source={"size": stat.st_size},
    )


def test_project_result_cache_key_normalizes_stable_algorithm_defaults() -> None:
    base_request = {
        "snapshot": {
            "snapshot_id": "snap-1",
            "policy_id": "policy-1",
            "directed": True,
            "node_count": 10,
            "edge_count": 20,
            "quality": {"source_content_fingerprint": "f" * 64},
        },
        "algorithm": "node_centrality",
        "backend_plan": {
            "selected_backend": "legacy",
            "native_app": None,
            "semantic_profile": "legacy_v1",
        },
    }
    omitted = {
        **base_request,
        "parameters": {"centrality_type": "pagerank", "limit": 20},
    }
    explicit = {
        **base_request,
        "parameters": {
            "centrality_type": "PageRank",
            "limit": "20",
            "damping": 0.85,
            "tolerance": 1e-6,
            "max_iterations": 100,
        },
    }
    changed = {
        **base_request,
        "parameters": {
            "centrality_type": "pagerank",
            "limit": 20,
            "damping": 0.9,
        },
    }

    assert project_result_cache_key(omitted) == project_result_cache_key(explicit)
    assert project_result_cache_key(omitted) != project_result_cache_key(changed)

    degree_base = {**base_request, "algorithm": "in_degree"}
    assert project_result_cache_key(
        {**degree_base, "parameters": {}}
    ) == project_result_cache_key(
        {**degree_base, "parameters": {"top_k": 20}}
    )
    assert project_result_cache_key(
        {**degree_base, "parameters": {"top_k": 20}}
    ) == project_result_cache_key(
        {**degree_base, "parameters": {"top_k": 20, "limit": 20}}
    )


def _stubborn_process_tree(
    marker_path: str, ready_path: str, escape_delay: float = 0.7, escape_reader=None
) -> None:
    """Create a descendant that ignores TERM and writes if it escapes KILL."""
    os.setsid()
    child_pid = os.fork()
    if child_pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(ready_path).write_text(str(os.getpid()), encoding="utf-8")
        if escape_reader is not None:
            escape_reader.recv_bytes()
        time.sleep(float(escape_delay))
        Path(marker_path).write_text("descendant escaped", encoding="utf-8")
        os._exit(0)
    while True:
        time.sleep(0.1)


def test_worker_start_gate_fails_closed_before_graph_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClosedGate:
        def __init__(self) -> None:
            self.timeout: float | None = None

        def wait(self, timeout: float) -> bool:
            self.timeout = timeout
            return False

    monkeypatch.setenv("LARGE_GRAPH_START_GATE_SECONDS", "1")
    gate = ClosedGate()
    with pytest.raises(RuntimeError, match="启动身份未在时限内持久化"):
        _await_worker_registration(gate)
    assert gate.timeout == 1.0


def test_worker_identity_capture_waits_for_delayed_setsid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = DurableLargeGraphRuntime(
        tmp_path / "runtime",
        LargeGraphCatalog(tmp_path / "runtime" / "catalog.sqlite3"),
    )
    pid = 23456
    identity = {
        "scheme": "linux-proc-v1",
        "pid": pid,
        "state": "S",
        "pgid": pid,
        "session_id": pid,
        "start_time_ticks": 99,
        "boot_id": "boot",
    }
    observations = iter(
        [
            ("alive", {**identity, "pgid": pid - 1, "session_id": pid - 1}),
            ("alive", identity),
        ]
    )
    ticks = iter((10.0, 10.01, 10.02))
    monkeypatch.setenv("LARGE_GRAPH_WORKER_IDENTITY_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr(runtime, "_observe_worker_process", lambda _pid: next(observations))
    monkeypatch.setattr(
        "graph_data_service.large_graph.runtime.time.monotonic", lambda: next(ticks)
    )
    monkeypatch.setattr("graph_data_service.large_graph.runtime.time.sleep", lambda _: None)

    assert runtime._capture_worker_identity(pid) == identity


def test_job_wall_time_configuration_has_safe_default_and_explicit_disable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LARGE_GRAPH_MAX_JOB_SECONDS", raising=False)
    default_runtime = DurableLargeGraphRuntime(
        tmp_path / "default",
        LargeGraphCatalog(tmp_path / "default" / "catalog.sqlite3"),
    )
    assert default_runtime.max_job_seconds == 24 * 60 * 60

    monkeypatch.setenv("LARGE_GRAPH_MAX_JOB_SECONDS", "0")
    disabled_runtime = DurableLargeGraphRuntime(
        tmp_path / "disabled",
        LargeGraphCatalog(tmp_path / "disabled" / "catalog.sqlite3"),
    )
    assert disabled_runtime.max_job_seconds is None

    monkeypatch.setenv("LARGE_GRAPH_MAX_JOB_SECONDS", "not-a-number")
    invalid_runtime = DurableLargeGraphRuntime(
        tmp_path / "invalid",
        LargeGraphCatalog(tmp_path / "invalid" / "catalog.sqlite3"),
    )
    assert invalid_runtime.max_job_seconds == 24 * 60 * 60


def test_external_sort_uses_bounded_multilevel_merge_fan_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_MERGE_FAN_IN", "2")
    run_root = tmp_path / "merge-runs"
    run_root.mkdir()
    runs: list[Path] = []
    for index in range(9):
        path = run_root / f"input-{index:02d}.jsonl"
        path.write_text(
            json.dumps([index, f"row-{index}"], separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        runs.append(path)

    merged = list(
        _bounded_json_merge(
            runs,
            run_root,
            key=lambda row: int(row[0]),
            prefix="test",
        )
    )

    assert merged == [[index, f"row-{index}"] for index in range(9)]
    assert len(list(run_root.glob("*.jsonl"))) <= 2


def test_external_sort_chunk_is_bounded_by_encoded_bytes(tmp_path: Path) -> None:
    source = tmp_path / "mapped.jsonl"
    source.write_text(
        "".join(
            json.dumps(
                {"key": f"node-{index}", "payload": "x" * 400},
                separators=(",", ":"),
            )
            + "\n"
            for index in range(5)
        ),
        encoding="utf-8",
    )

    runs = _write_sorted_runs(
        [source], tmp_path / "byte-runs", chunk_rows=10_000, chunk_bytes=700
    )

    assert len(runs) == 5


def test_hash_shuffle_append_handles_have_a_shared_lru_bound(tmp_path: Path) -> None:
    pool = _AppendHandlePool(tmp_path, max_open_files=2)
    try:
        for partition in range(12):
            pool.get("edge", partition).write(f"edge-{partition}\n")
            assert len(pool._handles) <= 2
            pool.get("node", partition).write(f"node-{partition}\n")
            assert len(pool._handles) <= 2
        # Reopening an evicted partition must append, never truncate.
        pool.get("edge", 0).write("edge-0-again\n")
    finally:
        pool.close()

    assert (tmp_path / "edge-00000.jsonl").read_text(encoding="utf-8") == (
        "edge-0\nedge-0-again\n"
    )
    assert (tmp_path / "node-00011.jsonl").read_text(encoding="utf-8") == "node-11\n"


def test_degree_mapper_uses_bounded_shared_append_handle_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edge_path = tmp_path / "degree-edges.jsonl"
    edge_path.write_text(
        "".join(
            json.dumps(
                {"source": f"source-{index}", "target": f"target-{index}"},
                separators=(",", ":"),
            )
            + "\n"
            for index in range(100)
        ),
        encoding="utf-8",
    )
    seen: dict[str, int] = {"maximum": 0}

    class TrackingPool(_AppendHandlePool):
        def get(self, kind: str, partition: int):
            opened = super().get(kind, partition)
            seen["maximum"] = max(seen["maximum"], len(self._handles))
            return opened

    monkeypatch.setenv("LARGE_GRAPH_MAP_OPEN_FILES", "4")
    monkeypatch.setattr(large_graph_engine, "_AppendHandlePool", TrackingPool)
    result = large_graph_engine._map_degree_partition(
        (str(edge_path), str(tmp_path / "degree-map"), 0, 64, "degree")
    )

    mapped_rows = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in Path(result["map_root"]).glob("part-*.jsonl")
    )
    assert result["contributions"] == 200
    assert mapped_rows == 200
    assert seen["maximum"] == 4


def test_degree_reducer_uses_sqlite_state_and_preserves_statistics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nodes = [("a", "Alice"), ("b", "Bob"), ("c", "Carol")] + [
        (f"node-{index:03d}", f"Node {index}") for index in range(257)
    ]
    node_path = tmp_path / "nodes.jsonl"
    node_path.write_text(
        "".join(
            json.dumps({"id": node_id, "name": name}, separators=(",", ":"))
            + "\n"
            for node_id, name in nodes
        ),
        encoding="utf-8",
    )
    contributions = tmp_path / "contributions.jsonl"
    contributions.write_text(
        "".join(
            json.dumps(row, separators=(",", ":")) + "\n"
            for row in [("a", 1.0), ("a", 1.0), ("b", 1.0)]
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "results" / "part-00000.jsonl"
    sqlite_path = tmp_path / "map" / "reduce-00000.sqlite3"
    # Exercise multiple bounded flushes without needing a large fixture.
    monkeypatch.setenv("LARGE_GRAPH_REDUCE_BATCH_ROWS", "128")

    result = large_graph_engine._reduce_degree_partition(
        (
            0,
            [str(contributions)],
            str(node_path),
            str(output_path),
            str(sqlite_path),
            "degree",
            len(nodes),
            2,
        )
    )

    rows = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert result == {
        "partition": 0,
        "count": len(nodes),
        "sum": 3.0,
        "min": 0.0,
        "max": 2.0,
        "histogram": {0.0: 258, 1.0: 1, 2.0: 1},
        "top": [(1.0, "b", "Bob"), (2.0, "a", "Alice")],
        "state_backend": "sqlite",
    }
    assert rows[:3] == [
        {"node_id": "a", "node": "Alice", "score": 2.0},
        {"node_id": "b", "node": "Bob", "score": 1.0},
        {"node_id": "c", "node": "Carol", "score": 0.0},
    ]
    assert rows[-1] == {
        "node_id": "node-256",
        "node": "Node 256",
        "score": 0.0,
    }
    assert sqlite_path.exists()


@pytest.mark.parametrize(
    ("histogram", "count", "expected"),
    [
        ({}, 0, 0.0),
        ({0.0: 1, 1.0: 1, 2.0: 1}, 3, 1.0),
        ({0.0: 2, 1.0: 2}, 4, 0.5),
    ],
)
def test_degree_histogram_returns_exact_median(
    histogram: dict[float, int], count: int, expected: float
) -> None:
    assert large_graph_engine._median_from_histogram(histogram, count) == expected


def test_parallel_ranges_handle_crlf_utf8_oversize_and_unterminated_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_MAX_RECORD_BYTES", "1024")
    source = tmp_path / "range-boundaries.txt"
    source.write_bytes(
        "甲 乙 1\r\n".encode()
        + ("坏 " + "界" * 1000 + " 9\r\n").encode()
        + "丙 丁 2".encode()
    )
    stat = source.stat()

    snapshot = PartitionedSnapshotBuilder(tmp_path / "range-snapshots").build(
        source_path=source,
        user_scope="alice__session",
        graph_id="range-boundaries",
        graph_version=1,
        directed=True,
        partitions=4,
        workers=2,
        delimiter="whitespace",
        input_format="edgelist",
        policy=DataQualityPolicy(node_normalization="exact", invalid_records="skip"),
        expected_source={"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
    )

    assert snapshot["edge_count"] == 2
    assert snapshot["node_count"] == 4
    assert snapshot["quality"]["raw_records"] == 3
    assert snapshot["quality"]["invalid_records"] == 1


def test_edge_deduplication_key_cannot_collide_with_control_characters(
    tmp_path: Path,
) -> None:
    source = tmp_path / "control-character-ids.csv"
    source.write_text("a\x1fb,c,1\na,b\x1fc,2\n", encoding="utf-8")
    stat = source.stat()

    snapshot = PartitionedSnapshotBuilder(tmp_path / "control-snapshots").build(
        source_path=source,
        user_scope="alice__session",
        graph_id="control-ids",
        graph_version=1,
        directed=True,
        partitions=2,
        workers=1,
        delimiter="comma",
        input_format="edgelist",
        policy=DataQualityPolicy(node_normalization="exact"),
        expected_source={"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
    )

    assert snapshot["edge_count"] == 2
    assert snapshot["node_count"] == 4
    assert snapshot["quality"]["duplicate_edges_removed"] == 0


def test_snapshot_rejects_oversized_record_without_materialising_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_MAX_RECORD_BYTES", "1024")
    source = tmp_path / "oversized-record.txt"
    source.write_text("A " + "x" * 4096 + " 1\nC D 2\n", encoding="utf-8")
    stat = source.stat()

    snapshot = PartitionedSnapshotBuilder(tmp_path / "record-snapshots").build(
        source_path=source,
        user_scope="alice__session",
        graph_id="bounded-record",
        graph_version=1,
        directed=True,
        partitions=1,
        workers=1,
        delimiter="whitespace",
        input_format="edgelist",
        policy=DataQualityPolicy(invalid_records="skip"),
        expected_source={"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
    )

    assert snapshot["edge_count"] == 1
    assert snapshot["node_count"] == 2
    assert snapshot["quality"]["invalid_records"] == 1


def test_snapshot_records_and_enforces_node_identifier_byte_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_MAX_NODE_ID_BYTES", "16")
    source = tmp_path / "oversized-node-id.txt"
    source.write_text("x" * 32 + " B 1\nC D 2\n", encoding="utf-8")
    stat = source.stat()

    snapshot = PartitionedSnapshotBuilder(tmp_path / "node-id-snapshots").build(
        source_path=source,
        user_scope="alice__session",
        graph_id="bounded-node-id",
        graph_version=1,
        directed=True,
        partitions=1,
        workers=1,
        delimiter="whitespace",
        input_format="edgelist",
        policy=DataQualityPolicy(invalid_records="skip"),
        expected_source={"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
    )

    assert snapshot["edge_count"] == 1
    assert snapshot["quality"]["invalid_records"] == 1
    assert snapshot["quality"]["max_node_id_bytes"] == 16
    assert snapshot["quality"]["max_record_bytes"] == 1024 * 1024


def test_snapshot_preparation_streams_deduplicates_and_publishes(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)

    assert snapshot["graph_version"] == 7
    assert snapshot["node_count"] == 3
    assert snapshot["edge_count"] == 2
    assert snapshot["quality"]["duplicate_edges_removed"] == 1
    assert snapshot["quality"]["invalid_records"] == 1
    assert snapshot["quality"]["self_loops_skipped"] == 1
    manifest_path = Path(snapshot["manifest_path"])
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["state"] == "published"
    assert manifest["format"] == "igraph-partitioned-jsonl-v2"
    assert manifest["record_encoding"] == "compact-json-array-v1"
    edge_rows = [
        json.loads(line)
        for relative in manifest["edge_files"]
        for line in (manifest_path.parent / relative).read_text(encoding="utf-8").splitlines()
    ]
    assert all(isinstance(row, list) for row in edge_rows)
    assert manifest["adjacency_format"] == "igraph-sharded-sqlite-adjacency-v1"
    assert len(manifest["adjacency_files"]) == 2
    with PartitionedDiskGraph(manifest_path) as graph:
        assert graph.resolve_node("A") == "a"
        assert list(graph.iter_out_edges("a")) == [("b", 3.0)]
    assert not list(manifest_path.parent.glob(".*.staging"))


def test_graphscope_storage_profile_skips_duplicate_adjacency_but_keeps_degree_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_PRECOMPUTE_ADJACENCY", "false")
    snapshot = _build_snapshot(tmp_path)
    manifest_path = Path(snapshot["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["storage_profile"] == "graphscope_compact"
    assert "adjacency_format" not in manifest
    assert "adjacency_files" not in manifest
    assert manifest["storage_bytes"]["adjacency"] == {"total": 0, "files": []}
    assert not (manifest_path.parent / "adjacency").exists()

    result = LocalParallelGraphEngine(tmp_path / "artifacts").execute(
        snapshot=snapshot,
        algorithm="degree",
        parameters={"top_k": 3},
        user_scope="alice__session",
        graph_id="social",
        graph_version=7,
        workers=2,
    )
    assert result["summary"]["result_count"] == 3


def test_source_fingerprint_is_independent_of_worker_count(tmp_path: Path) -> None:
    source = tmp_path / "fingerprint.txt"
    source.write_text("\n".join(f"n{i} n{i + 1}" for i in range(20)) + "\n", encoding="utf-8")
    stat = source.stat()
    policy = DataQualityPolicy(node_normalization="exact")
    fingerprints = []
    for workers in (1, 2):
        snapshot = PartitionedSnapshotBuilder(tmp_path / f"snapshots-{workers}").build(
            source_path=source,
            user_scope="alice__session",
            graph_id="fingerprint",
            graph_version=1,
            directed=True,
            partitions=2,
            workers=workers,
            delimiter="whitespace",
            input_format="edgelist",
            policy=policy,
            expected_source={"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        )
        fingerprints.append(snapshot["quality"]["source_content_fingerprint"])
    assert fingerprints[0] == fingerprints[1]


def test_parallel_degree_writes_bounded_summary_and_partition_artifact(tmp_path: Path) -> None:
    snapshot = _build_snapshot(tmp_path)
    result = LocalParallelGraphEngine(tmp_path / "artifacts").execute(
        snapshot=snapshot,
        algorithm="degree",
        parameters={"top_k": 2},
        user_scope="alice__session",
        graph_id="social",
        graph_version=7,
        workers=2,
    )

    assert result["summary"]["result_count"] == 3
    assert result["summary"]["score_max"] == 2.0
    assert result["summary"]["score_median"] == 1.0
    assert result["summary"]["top"][0]["node_id"] == "b"
    assert len(result["preview"]) == 2
    assert result["summary"]["parallel"] == {
        "strategy": "edge-map/node-reduce",
        "workers": 2,
        "partitions": 2,
    }
    assert result["metadata"]["result_processing"] == {
        "strategy": "sqlite-node-partition-reduce-writers",
        "workers": 2,
    }
    assert result["metadata"]["topology_access"] == (
        "partitioned-jsonl-map-sqlite-reduce-v1"
    )
    artifact_path = Path(result["path"])
    artifact_manifest = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert not list(artifact_path.parent.rglob("*.sqlite3"))
    assert len(artifact_manifest["result_files"]) == 2
    assert artifact_manifest["metadata"]["graph_version"] == 7
    assert artifact_manifest["metadata"]["data_quality_policy_id"].startswith("dqp_")


def test_router_never_falls_back_for_unsupported_large_global_algorithm(tmp_path: Path) -> None:
    resources = ResourceManager(tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0)
    router = ComputeRouter(resources, large_node_threshold=10, large_edge_threshold=10)

    rejected = router.route(
        operation_scope="global",
        node_count=100,
        edge_count=100,
        algorithm="pagerank",
        snapshot_available=True,
    )
    bounded = router.route(
        operation_scope="bounded",
        node_count=100,
        edge_count=100,
        algorithm="shortest_path",
    )

    assert rejected.path == "reject"
    assert rejected.code == "LARGE_GRAPH_ALGORITHM_UNSUPPORTED"
    assert "NetworkX" in rejected.reason
    assert bounded.path == "small_graph"


def test_router_rejects_unbounded_global_link_prediction(tmp_path: Path) -> None:
    router = ComputeRouter(
        ResourceManager(tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0),
        large_node_threshold=1,
        large_edge_threshold=1,
    )
    rejected = router.route(
        operation_scope="global",
        node_count=20_001,
        edge_count=40_000,
        algorithm="link_prediction_score",
        parameters={"method": "jaccard"},
        snapshot_available=True,
    )
    assert rejected.path == "reject"
    assert rejected.code == "QUADRATIC_ALGORITHM_RISK"

    pair = router.route(
        operation_scope="global",
        node_count=20_001,
        edge_count=40_000,
        algorithm="link_prediction_score",
        parameters={"method": "jaccard", "source": "a", "target": "b"},
        snapshot_available=True,
    )
    assert pair.path in {"large_graph", "reject"}
    assert pair.code != "QUADRATIC_ALGORITHM_RISK"


def test_router_can_require_strict_small_path_semantics(tmp_path: Path) -> None:
    router = ComputeRouter(
        ResourceManager(tmp_path, memory_reserve_ratio=0, disk_reserve_bytes=0),
        large_node_threshold=1,
        large_edge_threshold=1,
    )
    decision = router.route(
        operation_scope="global",
        node_count=100,
        edge_count=200,
        algorithm="community_detection",
        parameters={"algorithm": "louvain", "strict_semantics": True},
        snapshot_available=True,
    )
    assert decision.path == "reject"
    assert decision.code == "APPROXIMATION_NOT_ALLOWED"


def test_job_state_survives_catalog_restart_and_is_requeued(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog.sqlite3"
    catalog = LargeGraphCatalog(db_path)
    policy = DataQualityPolicy()
    job_id, version = catalog.reserve_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source={"filename": "edges.txt", "size": 10},
        policy_id=policy.policy_id,
        policy=policy.to_dict(),
        request={"source_path": "/private/edges.txt"},
    )
    claimed = catalog.claim_next_job()
    assert claimed is not None and claimed["status"] == "running"

    restarted = LargeGraphCatalog(db_path)
    assert restarted.recover_interrupted_jobs(stale_after_seconds=0) == 1
    recovered = restarted.get_job(job_id, user_id="alice", session_id="s1")
    assert recovered is not None
    assert recovered["status"] == "queued"
    assert recovered["graph_version"] == version


def test_cancelled_lease_cannot_authorise_a_waiting_child(
    tmp_path: Path,
) -> None:
    catalog = LargeGraphCatalog(tmp_path / "catalog.sqlite3")
    policy = DataQualityPolicy()
    job_id, _ = catalog.reserve_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source={"filename": "edges.txt", "size": 10},
        policy_id=policy.policy_id,
        policy=policy.to_dict(),
        request={"source_path": "/private/edges.txt"},
    )
    claimed = catalog.claim_next_job()
    assert claimed is not None
    assert catalog.request_cancel(job_id, user_id="alice", session_id="s1")

    registered = catalog.set_worker_pid(
        job_id,
        12345,
        lease_id=claimed["lease_id"],
        identity={"scheme": "test", "pid": 12345},
    )
    current = catalog.get_job(job_id, user_id="alice", session_id="s1")
    assert registered is False
    assert current is not None
    assert current["status"] == "cancelling"
    assert current["worker_pid"] is None


def test_recovered_job_rejects_stale_worker_terminal_updates(tmp_path: Path) -> None:
    catalog = LargeGraphCatalog(tmp_path / "catalog.sqlite3")
    policy = DataQualityPolicy()
    job_id, _ = catalog.reserve_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source={"filename": "edges.txt", "size": 10},
        policy_id=policy.policy_id,
        policy=policy.to_dict(),
        request={"source_path": "/private/edges.txt"},
    )
    first = catalog.claim_next_job()
    assert first is not None
    first_lease = first["lease_id"]

    assert catalog.recover_interrupted_jobs(stale_after_seconds=0) == 1
    second = catalog.claim_next_job()
    assert second is not None
    second_lease = second["lease_id"]
    assert second_lease != first_lease
    assert second["attempt"] == 2

    catalog.update_progress(
        job_id,
        stage="stale",
        progress=0.9,
        message="stale update",
        lease_id=first_lease,
    )
    catalog.finish_failed(job_id, "stale failure", lease_id=first_lease)
    catalog.finish_cancelled(job_id, "stale cancellation", lease_id=first_lease)
    catalog.requeue_running_job(job_id, "stale requeue", lease_id=first_lease)
    assert catalog.publish_snapshot(job_id, {}, lease_id=first_lease) is False

    current = catalog.get_job(job_id, user_id="alice", session_id="s1")
    assert current is not None
    assert current["status"] == "running"
    assert current["stage"] == "starting"
    assert current["progress"] == 0
    assert current["lease_id"] == second_lease


def test_prepare_cancellation_is_terminal_for_job_and_version(tmp_path: Path) -> None:
    catalog = LargeGraphCatalog(tmp_path / "catalog.sqlite3")
    policy = DataQualityPolicy()

    def reserve() -> tuple[str, int]:
        return catalog.reserve_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source={"filename": "edges.txt", "size": 10},
            policy_id=policy.policy_id,
            policy=policy.to_dict(),
            request={"source_path": "/private/edges.txt"},
        )

    queued_job, queued_version = reserve()
    assert catalog.request_cancel(
        queued_job, user_id="alice", session_id="s1"
    )

    running_job, running_version = reserve()
    claimed = catalog.claim_next_job()
    assert claimed is not None and claimed["job_id"] == running_job
    assert catalog.request_cancel(
        running_job, user_id="alice", session_id="s1"
    )
    catalog.finish_cancelled(running_job)

    publish_job, publish_version = reserve()
    claimed = catalog.claim_next_job()
    assert claimed is not None and claimed["job_id"] == publish_job
    assert catalog.request_cancel(
        publish_job, user_id="alice", session_id="s1"
    )
    assert catalog.publish_snapshot(publish_job, {}) is False

    versions = {
        item["graph_version"]: item["status"]
        for item in catalog.list_versions(
            user_id="alice", session_id="s1", graph_id="g1"
        )
    }
    assert versions == {
        queued_version: "cancelled",
        running_version: "cancelled",
        publish_version: "cancelled",
    }


def test_recovery_finishes_cancelling_job_without_requeue(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog.sqlite3"
    catalog = LargeGraphCatalog(db_path)
    policy = DataQualityPolicy()
    job_id, version = catalog.reserve_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source={"filename": "edges.txt", "size": 10},
        policy_id=policy.policy_id,
        policy=policy.to_dict(),
        request={"source_path": "/private/edges.txt"},
    )
    assert catalog.claim_next_job() is not None
    assert catalog.request_cancel(job_id, user_id="alice", session_id="s1")

    restarted = LargeGraphCatalog(db_path)
    assert restarted.recover_interrupted_jobs(stale_after_seconds=0) == 1
    recovered = restarted.get_job(job_id, user_id="alice", session_id="s1")
    assert recovered is not None
    assert recovered["status"] == "cancelled"
    assert recovered["finished_at"] is not None
    assert restarted.claim_next_job() is None

    # Defend catalogs written by an interrupted/older caller that persisted
    # the flag before it changed running -> cancelling.
    flagged_job, flagged_version = restarted.reserve_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source={"filename": "edges.txt", "size": 10},
        policy_id=policy.policy_id,
        policy=policy.to_dict(),
        request={"source_path": "/private/edges.txt"},
    )
    assert restarted.claim_next_job() is not None
    with restarted._connect() as conn:
        conn.execute(
            "UPDATE jobs SET cancel_requested=1 WHERE job_id=?", (flagged_job,)
        )
    assert restarted.recover_interrupted_jobs(stale_after_seconds=0) == 1
    flagged = restarted.get_job(flagged_job, user_id="alice", session_id="s1")
    assert flagged is not None and flagged["status"] == "cancelled"
    assert restarted.claim_next_job() is None

    versions = restarted.list_versions(
        user_id="alice", session_id="s1", graph_id="g1"
    )
    assert {
        item["graph_version"]: item["status"] for item in versions
    } == {version: "cancelled", flagged_version: "cancelled"}


def test_recovery_does_not_cancel_another_live_worker_lease(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog.sqlite3"
    owner = LargeGraphCatalog(db_path)
    policy = DataQualityPolicy()
    job_id, _ = owner.reserve_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source={"filename": "edges.txt", "size": 10},
        policy_id=policy.policy_id,
        policy=policy.to_dict(),
        request={"source_path": "/private/edges.txt", "user_scope": "scope"},
    )
    claimed = owner.claim_next_job()
    assert claimed is not None
    lease_id = claimed["lease_id"]
    assert owner.request_cancel(job_id, user_id="alice", session_id="s1")

    replica = LargeGraphCatalog(db_path)
    assert replica.recover_interrupted_jobs(stale_after_seconds=60) == 0
    still_owned = replica.get_job(job_id, user_id="alice", session_id="s1")
    assert still_owned is not None
    assert still_owned["status"] == "cancelling"
    assert still_owned["lease_id"] == lease_id

    # Once the owner heartbeat really is stale, a replica may safely complete
    # the cancellation without ever requeueing the cancelled work.
    with replica._connect() as conn:
        conn.execute(
            "UPDATE jobs SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE job_id=?",
            (job_id,),
        )
    assert replica.recover_interrupted_jobs(stale_after_seconds=60) == 1
    cancelled = replica.get_job(job_id, user_id="alice", session_id="s1")
    assert cancelled is not None and cancelled["status"] == "cancelled"
    assert replica.claim_next_job() is None


def test_stale_running_recovery_durably_cleans_old_lease_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    catalog = LargeGraphCatalog(root / "catalog.sqlite3")
    policy = DataQualityPolicy()
    job_id, version = catalog.reserve_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source={"filename": "edges.txt", "size": 10},
        policy_id=policy.policy_id,
        policy=policy.to_dict(),
        request={"source_path": "/private/edges.txt", "user_scope": "scope"},
    )
    first = catalog.claim_next_job()
    assert first is not None
    first_lease = first["lease_id"]
    old_output = (
        root
        / "snapshots"
        / f"scope__{first_lease}"
        / "g1"
        / f"v{version:08d}"
        / "snap_orphan"
    )
    old_staging = old_output.parent / ".snap_orphan.staging"
    old_output.mkdir(parents=True)
    old_staging.mkdir()

    # Catalog recovery and filesystem cleanup may be performed by different
    # processes. The old lease identity must survive in the durable queue.
    assert catalog.recover_interrupted_jobs(stale_after_seconds=0) == 1
    pending = catalog.pending_attempt_cleanups()
    assert [(item["job_id"], item["lease_id"]) for item in pending] == [
        (job_id, first_lease)
    ]
    second = catalog.claim_next_job()
    assert second is not None and second["lease_id"] != first_lease
    new_output = (
        root
        / "snapshots"
        / f"scope__{second['lease_id']}"
        / "g1"
        / f"v{version:08d}"
        / "snap_keep"
    )
    new_output.mkdir(parents=True)

    replacement = DurableLargeGraphRuntime(root, catalog)
    replacement._drain_attempt_cleanups()

    assert not old_output.exists()
    assert not old_staging.exists()
    assert new_output.is_dir()
    assert catalog.pending_attempt_cleanups() == []


def test_failed_attempt_cleanup_waits_for_a_successful_directory_sync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "runtime"
    catalog = LargeGraphCatalog(root / "catalog.sqlite3")
    policy = DataQualityPolicy()
    job_id, version = catalog.reserve_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source={"filename": "edges.txt", "size": 10},
        policy_id=policy.policy_id,
        policy=policy.to_dict(),
        request={"source_path": "/private/edges.txt", "user_scope": "scope"},
    )
    claimed = catalog.claim_next_job()
    assert claimed is not None
    lease_id = claimed["lease_id"]
    orphan = (
        root
        / "snapshots"
        / f"scope__{lease_id}"
        / "g1"
        / f"v{version:08d}"
        / "snap_orphan"
    )
    orphan.mkdir(parents=True)
    catalog.finish_failed(job_id, "injected failure", lease_id=lease_id)
    assert catalog.pending_attempt_cleanups()

    runtime = DurableLargeGraphRuntime(root, catalog)
    real_sync = large_graph_runtime.fsync_directory
    monkeypatch.setattr(
        large_graph_runtime,
        "fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("injected fsync failure")),
    )
    runtime._drain_attempt_cleanups()

    assert not orphan.exists()
    assert catalog.pending_attempt_cleanups()

    monkeypatch.setattr(large_graph_runtime, "fsync_directory", real_sync)
    runtime._drain_attempt_cleanups()
    assert catalog.pending_attempt_cleanups() == []


def test_stale_cancelling_recovery_durably_cleans_terminal_attempt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    catalog = LargeGraphCatalog(root / "catalog.sqlite3")
    policy = DataQualityPolicy()
    job_id, version = catalog.reserve_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source={"filename": "edges.txt", "size": 10},
        policy_id=policy.policy_id,
        policy=policy.to_dict(),
        request={"source_path": "/private/edges.txt", "user_scope": "scope"},
    )
    claimed = catalog.claim_next_job()
    assert claimed is not None
    lease_id = claimed["lease_id"]
    orphan = (
        root
        / "artifacts"
        / f"scope__{lease_id}"
        / "g1"
        / f"v{version:08d}"
        / "artifact_orphan"
    )
    orphan.mkdir(parents=True)
    assert catalog.request_cancel(job_id, user_id="alice", session_id="s1")

    replacement = DurableLargeGraphRuntime(root, catalog)
    assert replacement._recover_interrupted_jobs(stale_after_seconds=0) == 1

    status = catalog.get_job(job_id, user_id="alice", session_id="s1")
    assert status is not None and status["status"] == "cancelled"
    assert not orphan.exists()
    assert catalog.pending_attempt_cleanups() == []


def test_close_fallback_requeues_and_cleans_only_the_old_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "runtime"
    catalog = LargeGraphCatalog(root / "catalog.sqlite3")
    policy = DataQualityPolicy()
    job_id, version = catalog.reserve_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source={"filename": "edges.txt", "size": 10},
        policy_id=policy.policy_id,
        policy=policy.to_dict(),
        request={"source_path": "/private/edges.txt", "user_scope": "scope"},
    )
    claimed = catalog.claim_next_job()
    assert claimed is not None
    lease_id = claimed["lease_id"]
    runtime = DurableLargeGraphRuntime(root, catalog)

    attempt_root = (
        runtime.snapshot_root
        / f"scope__{lease_id}"
        / "g1"
        / f"v{version:08d}"
    )
    staging = attempt_root / ".snap_old.staging"
    renamed = attempt_root / "snap_old"
    staging.mkdir(parents=True)
    renamed.mkdir()
    other_lease = (
        runtime.snapshot_root
        / "scope__lease_other"
        / "g1"
        / f"v{version:08d}"
        / "snap_keep"
    )
    other_lease.mkdir(parents=True)

    class _StuckThread:
        def is_alive(self) -> bool:
            return True

        def join(self, timeout: float | None = None) -> None:
            return None

    class _Process:
        pid = 12345

    runtime._thread = _StuckThread()  # type: ignore[assignment]
    runtime._current_process = _Process()  # type: ignore[assignment]
    runtime._current_job_id = job_id
    runtime._current_lease_id = lease_id
    runtime._current_job = dict(claimed)
    monkeypatch.setattr(runtime, "_terminate_process_tree", lambda process: None)

    runtime.close()

    recovered = catalog.get_job(job_id, user_id="alice", session_id="s1")
    assert recovered is not None and recovered["status"] == "queued"
    assert not staging.exists()
    assert not renamed.exists()
    assert other_lease.is_dir()
    assert catalog.pending_attempt_cleanups() == []


def test_cancel_after_child_result_discards_physically_renamed_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "runtime"
    catalog = LargeGraphCatalog(root / "catalog.sqlite3")
    policy = DataQualityPolicy()
    job_id, version = catalog.reserve_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source={"filename": "edges.txt", "size": 10},
        policy_id=policy.policy_id,
        policy=policy.to_dict(),
        request={"source_path": "/private/edges.txt", "user_scope": "scope"},
    )
    claimed = catalog.claim_next_job()
    assert claimed is not None
    lease_id = claimed["lease_id"]
    runtime = DurableLargeGraphRuntime(root, catalog)
    generated = (
        runtime.snapshot_root
        / f"scope__{lease_id}"
        / "g1"
        / f"v{version:08d}"
        / "snap_race"
    )
    generated.mkdir(parents=True)
    manifest = generated / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    result = {"manifest_path": str(manifest)}

    class _Messages:
        def __init__(self) -> None:
            self.sent = False

        def get(self, timeout: float | None = None) -> dict:
            if self.sent:
                raise queue.Empty
            self.sent = True
            return {"type": "result", "result": result}

        def get_nowait(self) -> dict:
            raise queue.Empty

        def close(self) -> None:
            return None

    class _Process:
        pid: int | None = None
        exitcode = 0

        def __init__(self) -> None:
            self.alive_checks = 0

        def start(self) -> None:
            self.pid = 23456

        def is_alive(self) -> bool:
            self.alive_checks += 1
            return self.alive_checks <= 2

        def join(self, timeout: float | None = None) -> None:
            return None

    messages = _Messages()
    process = _Process()

    class _Context:
        def Queue(self) -> _Messages:
            return messages

        def Event(self):
            return type("Gate", (), {"set": lambda self: None})()

        def Process(self, **kwargs) -> _Process:
            return process

    monkeypatch.setattr(
        "graph_data_service.large_graph.runtime.process_context", lambda: _Context()
    )
    monkeypatch.setattr(runtime, "_terminate_process_tree", lambda process: None)
    monkeypatch.setattr(
        runtime,
        "_capture_worker_identity",
        lambda pid: {"scheme": "test", "pid": pid},
    )
    monkeypatch.setattr(
        runtime,
        "_admit_claimed_job",
        lambda job, request: (True, "ok", {}),
    )
    real_cancellation_requested = catalog.cancellation_requested
    checks = 0

    def cancel_after_result(current_job_id: str) -> bool:
        nonlocal checks
        checks += 1
        if checks == 6:
            assert messages.sent
            assert catalog.request_cancel(
                current_job_id, user_id="alice", session_id="s1"
            )
        return real_cancellation_requested(current_job_id)

    monkeypatch.setattr(catalog, "cancellation_requested", cancel_after_result)

    runtime._execute(claimed)

    status = catalog.get_job(job_id, user_id="alice", session_id="s1")
    assert status is not None and status["status"] == "cancelled"
    assert not generated.exists()
    assert catalog.pending_attempt_cleanups() == []


def test_runtime_wall_time_expires_process_tree_and_cleans_derived_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "timeout-runtime"
    catalog = LargeGraphCatalog(root / "catalog.sqlite3")
    source_manifest = root / "snapshots" / "published-source" / "manifest.json"
    source_snapshot = {
        "snapshot_id": "snap_source",
        "manifest_path": str(source_manifest),
        "graph_id": "g1",
        "graph_version": 1,
        "node_count": 2,
        "edge_count": 1,
        "partitions": 1,
        "directed": True,
        "has_weights": False,
        "format": "jsonl-v1",
    }
    with catalog._connect() as conn:
        conn.execute(
            """INSERT INTO graph_versions(
                   user_id,session_id,graph_id,graph_version,status,source_json,
                   policy_id,policy_json,quality_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "alice", "s1", "g1", 1, "published", "{}", "dqp_test",
                "{}", "{}", "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            """INSERT INTO snapshots(
                   snapshot_id,user_id,session_id,graph_id,graph_version,state,
                   manifest_path,node_count,edge_count,partitions,directed,
                   has_weights,format,created_at
               ) VALUES(?,?,?,?,?,'published',?,?,?,?,?,?,?,?)""",
            (
                "snap_source", "alice", "s1", "g1", 1,
                str(source_manifest), 2, 1, 1, 1, 0, "jsonl-v1",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            """UPDATE graph_versions SET snapshot_id='snap_source'
               WHERE user_id='alice' AND session_id='s1'
                 AND graph_id='g1' AND graph_version=1"""
        )

    job_id, derived_version, reused = catalog.enqueue_compute(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        graph_version=1,
        request={
            "algorithm": "subgraph_extraction",
            "parameters": {
                "method": "node_subset",
                "nodes": ["A"],
                "create_version": True,
            },
            "snapshot": source_snapshot,
            "workers": 1,
            "user_scope": "scope",
        },
    )
    assert reused is False
    assert derived_version == 2
    claimed = catalog.claim_next_job()
    assert claimed is not None and claimed["job_id"] == job_id
    lease_id = claimed["lease_id"]

    partial_artifact = (
        root / "artifacts" / f"scope__{lease_id}" / "g1" / "v00000001"
        / "artifact_partial"
    )
    partial_derived_snapshot = (
        root / "snapshots" / f"scope__{lease_id}" / "g1" / "v00000002"
        / "snap_partial"
    )
    other_lease_output = (
        root / "snapshots" / "scope__lease_other" / "g1" / "v00000002"
        / "snap_keep"
    )
    partial_artifact.mkdir(parents=True)
    partial_derived_snapshot.mkdir(parents=True)
    other_lease_output.mkdir(parents=True)

    class _Messages:
        def get(self, timeout: float | None = None) -> dict:
            raise AssertionError("已到 wall-time 时不应继续等待 Worker 消息")

        def get_nowait(self) -> dict:
            raise queue.Empty

        def close(self) -> None:
            return None

    class _Process:
        pid: int | None = None
        exitcode: int | None = None
        alive = True

        def start(self) -> None:
            self.pid = 34567

        def is_alive(self) -> bool:
            return self.alive

        def join(self, timeout: float | None = None) -> None:
            return None

    messages = _Messages()
    process = _Process()
    gate_state = {"opened": False}

    class _Context:
        def Queue(self) -> _Messages:
            return messages

        def Event(self):
            return type(
                "Gate",
                (),
                {"set": lambda self: gate_state.__setitem__("opened", True)},
            )()

        def Process(self, **kwargs) -> _Process:
            return process

    runtime = DurableLargeGraphRuntime(root, catalog)
    runtime.max_job_seconds = 0.5
    termination_calls: list[_Process] = []

    def terminate_tree(current: _Process) -> None:
        termination_calls.append(current)
        current.alive = False

    ticks = iter((10.0, 10.0, 10.75))
    monkeypatch.setattr(
        "graph_data_service.large_graph.runtime.process_context", lambda: _Context()
    )
    monkeypatch.setattr(
        "graph_data_service.large_graph.runtime.time.monotonic", lambda: next(ticks)
    )
    monkeypatch.setattr(runtime, "_terminate_process_tree", terminate_tree)
    monkeypatch.setattr(
        runtime,
        "_capture_worker_identity",
        lambda pid: {"scheme": "test", "pid": pid},
    )
    monkeypatch.setattr(
        runtime, "_admit_claimed_job", lambda job, request: (True, "ok", {})
    )

    runtime._execute(claimed)

    status = catalog.get_job(job_id, user_id="alice", session_id="s1")
    assert status is not None
    assert status["status"] == "failed"
    assert status["stage"] == "timeout"
    assert status["error"].startswith("LARGE_GRAPH_JOB_TIMEOUT:")
    assert status["summary"] == {
        "code": "LARGE_GRAPH_JOB_TIMEOUT",
        "elapsed_seconds": 0.75,
        "max_job_seconds": 0.5,
    }
    versions = {
        item["graph_version"]: item["status"]
        for item in catalog.list_versions(
            user_id="alice", session_id="s1", graph_id="g1"
        )
    }
    assert versions == {1: "published", 2: "failed"}
    assert gate_state["opened"] is True
    assert termination_calls and all(item is process for item in termination_calls)
    assert not partial_artifact.exists()
    assert not partial_derived_snapshot.exists()
    assert other_lease_output.is_dir()
    assert catalog.pending_attempt_cleanups() == []


def test_timeout_terminal_transition_prefers_persisted_cancellation(
    tmp_path: Path,
) -> None:
    catalog = LargeGraphCatalog(tmp_path / "catalog.sqlite3")
    policy = DataQualityPolicy()
    job_id, version = catalog.reserve_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source={"filename": "edges.txt", "size": 10},
        policy_id=policy.policy_id,
        policy=policy.to_dict(),
        request={"source_path": "/private/edges.txt", "user_scope": "scope"},
    )
    claimed = catalog.claim_next_job()
    assert claimed is not None
    assert catalog.finish_timeout(
        job_id,
        max_job_seconds=1,
        elapsed_seconds=2,
        lease_id="stale-lease",
    ) is None
    assert catalog.request_cancel(job_id, user_id="alice", session_id="s1")

    assert catalog.finish_timeout(
        job_id,
        max_job_seconds=1,
        elapsed_seconds=2,
        lease_id=claimed["lease_id"],
    ) == "cancelled"
    status = catalog.get_job(job_id, user_id="alice", session_id="s1")
    assert status is not None
    assert status["status"] == "cancelled"
    assert status["error"] is None
    versions = catalog.list_versions(
        user_id="alice", session_id="s1", graph_id="g1"
    )
    assert versions[0]["graph_version"] == version
    assert versions[0]["status"] == "cancelled"


def test_prepare_is_failed_when_worker_resource_readmission_rejects(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "resource-gated.txt"
    source.write_text("A B\n", encoding="utf-8")
    service = LargeGraphService(tmp_path / "runtime")
    calls = 0

    def admit(estimate: dict[str, int]) -> tuple[bool, str, dict[str, int]]:
        nonlocal calls
        calls += 1
        observed = {**estimate, "memory_budget_bytes": 1, "disk_budget_bytes": 1}
        if calls == 1:
            return True, "submission admitted", observed
        return False, "worker resources changed", observed

    monkeypatch.setattr(service.resources, "admit", admit)
    try:
        submitted = service.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="resource-gated",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert submitted["success"] is True
        status = _wait_for_job(service, submitted["job_id"])
        assert status["status"] == "failed"
        assert status["stage"] == "resource_rejected"
        assert status["error"].startswith("INSUFFICIENT_RESOURCES:")
        assert status["summary"]["code"] == "INSUFFICIENT_RESOURCES"
        assert calls == 2
        versions = service.list_versions(
            user_id="alice", session_id="s1", graph_id="g1"
        )
        assert versions[0]["status"] == "failed"
    finally:
        service.close()


def test_compute_is_failed_when_worker_resource_readmission_rejects(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "compute-resource-gated.txt"
    source.write_text("A B\nB C\n", encoding="utf-8")
    service = LargeGraphService(tmp_path / "runtime")
    service.resources.disk_reserve_bytes = 0
    try:
        prepared = service.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="compute-resource-gated",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert _wait_for_job(service, prepared["job_id"])["status"] == "done"

        calls = 0

        def admit(estimate: dict[str, int]) -> tuple[bool, str, dict[str, int]]:
            nonlocal calls
            calls += 1
            observed = {**estimate, "memory_budget_bytes": 1, "disk_budget_bytes": 1}
            if calls == 1:
                return True, "submission admitted", observed
            return False, "worker resources changed", observed

        monkeypatch.setattr(service.resources, "admit", admit)
        computed = service.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=prepared["graph_version"],
            algorithm="degree",
            parameters={"top_k": 2},
            workers=1,
        )
        assert computed["success"] is True
        status = _wait_for_job(service, computed["job_id"])
        assert status["status"] == "failed"
        assert status["stage"] == "resource_rejected"
        assert status["summary"]["code"] == "INSUFFICIENT_RESOURCES"
        assert calls == 2
        versions = service.list_versions(
            user_id="alice", session_id="s1", graph_id="g1"
        )
        assert versions[0]["status"] == "published"
    finally:
        service.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are unavailable")
def test_runtime_terminates_the_entire_posix_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "escaped.txt"
    ready = tmp_path / "grandchild-ready.txt"
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_stubborn_process_tree,
        args=(str(marker), str(ready)),
    )
    process.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not ready.exists():
        time.sleep(0.01)
    assert ready.exists()

    runtime = DurableLargeGraphRuntime(
        tmp_path / "runtime",
        LargeGraphCatalog(tmp_path / "runtime" / "catalog.sqlite3"),
    )
    try:
        runtime._terminate_process_tree(process, grace_seconds=0.1)
        assert not process.is_alive()
        time.sleep(0.8)
        assert not marker.exists()
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=1)


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc").is_dir(),
    reason="Linux process identity and groups are required",
)
@pytest.mark.parametrize("unrelated_process_exits", [False, True], ids=["stable-proc", "vanished-proc"])
def test_stale_recovery_kills_descendants_after_session_leader_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unrelated_process_exits: bool
) -> None:
    marker = tmp_path / "stale-descendant-escaped.txt"
    ready = tmp_path / "stale-descendant-ready.txt"
    context = multiprocessing.get_context("fork")
    escape_reader, escape_writer = context.Pipe(duplex=False)
    process = context.Process(
        target=_stubborn_process_tree,
        args=(str(marker), str(ready), 0.0, escape_reader),
    )
    process.start()
    escape_reader.close()
    process_group = int(process.pid or 0)
    runtime_root = tmp_path / "recovery-runtime"
    catalog = LargeGraphCatalog(runtime_root / "catalog.sqlite3")
    runtime = DurableLargeGraphRuntime(runtime_root, catalog)
    policy = DataQualityPolicy()
    unrelated = None
    fault_triggered = False
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.01)
        assert ready.exists()
        identity = runtime._capture_worker_identity(process.pid)
        assert identity is not None

        job_id, _ = catalog.reserve_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source={"filename": "edges.txt", "size": 10},
            policy_id=policy.policy_id,
            policy=policy.to_dict(),
            request={"source_path": "/private/edges.txt", "user_scope": "alice__s1"},
        )
        claimed = catalog.claim_next_job()
        assert claimed is not None
        assert catalog.set_worker_pid(
            job_id,
            process.pid,
            lease_id=claimed["lease_id"],
            identity=identity,
        )

        # Simulate an owner crash that takes out only the session leader. Its
        # TERM-resistant pool child remains in the same session/process group.
        process.kill()
        process.join(timeout=2)
        assert not process.is_alive()
        real_terminate = runtime._terminate_verified_process_group
        monkeypatch.setattr(
            runtime,
            "_terminate_verified_process_group",
            lambda expected: real_terminate(expected, grace_seconds=0.1),
        )

        if unrelated_process_exits:
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            stat_path = Path(f"/proc/{unrelated.pid}/stat")
            original_read = Path.read_text
            reads = 0

            def read_with_unrelated_exit(path, *args, **kwargs):
                nonlocal reads, fault_triggered
                if path == stat_path:
                    reads += 1
                    if reads == 2:
                        # Open a real proc entry, then reap our unrelated child
                        # before reading. Linux produces the real ESRCH race.
                        with path.open(encoding="utf-8") as handle:
                            fault_triggered = True
                            unrelated.kill()
                            unrelated.wait(timeout=2)
                            return handle.read()
                return original_read(path, *args, **kwargs)

            monkeypatch.setattr(Path, "read_text", read_with_unrelated_exit)

        recovered_count = runtime._recover_interrupted_jobs(stale_after_seconds=0)
        if unrelated_process_exits:
            assert fault_triggered
        assert recovered_count == 1
        recovered = catalog.get_job(job_id, user_id="alice", session_id="s1")
        assert recovered is not None
        assert recovered["status"] == "queued"
        # Only a descendant surviving recovery can write the marker. Setup,
        # join and process-table inspection have no wall-clock deadline here.
        try:
            escape_writer.send_bytes(b"recovery complete")
        except BrokenPipeError:
            pass  # Recovery closed every descendant's read end.
        time.sleep(0.8)
        assert not marker.exists()
    finally:
        escape_writer.close()
        if unrelated is not None and unrelated.poll() is None:
            unrelated.kill()
            unrelated.wait(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        if process_group > 1:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc").is_dir(),
    reason="Linux process identity and groups are required",
)
@pytest.mark.parametrize(
    ("fault", "read_number", "expected_recovered"),
    [
        pytest.param("vanished", 1, 1, id="observe-vanished"),
        pytest.param("vanished", 3, 1, id="pidfd-vanished"),
        pytest.param("permission", 2, 0, id="scan-permission-denied"),
        pytest.param("io", 2, 0, id="scan-io-error"),
        pytest.param("malformed", 2, 0, id="scan-malformed-stat"),
        pytest.param("permission", 3, 0, id="pidfd-permission-denied"),
        pytest.param("io", 3, 0, id="pidfd-io-error"),
        pytest.param("malformed", 3, 0, id="pidfd-malformed-stat"),
    ],
)
def test_stale_recovery_preserves_proc_read_safety(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    fault: str, read_number: int, expected_recovered: int,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    catalog = LargeGraphCatalog(tmp_path / "catalog.sqlite3")
    runtime = DurableLargeGraphRuntime(tmp_path, catalog)
    policy = DataQualityPolicy()
    try:
        identity = runtime._capture_worker_identity(process.pid)
        assert identity is not None
        job_id, _ = catalog.reserve_preparation(
            user_id="alice", session_id="s1", graph_id="g1",
            source={"filename": "edges.txt", "size": 10},
            policy_id=policy.policy_id, policy=policy.to_dict(),
            request={"source_path": "/private/edges.txt", "user_scope": "alice__s1"},
        )
        claimed = catalog.claim_next_job()
        assert claimed is not None
        assert catalog.set_worker_pid(
            job_id, process.pid, lease_id=claimed["lease_id"], identity=identity,
        )
        stat_path = Path(f"/proc/{process.pid}/stat")
        original_read = Path.read_text
        reads = 0
        fault_triggered = False

        def read_at_process_boundary(path, *args, **kwargs):
            nonlocal reads, fault_triggered
            if path == stat_path:
                reads += 1
                # Recovery observes the leader, scans the group, then rechecks
                # its identity after opening the pidfd. Keep all real behavior
                # except the chosen filesystem boundary's fault.
                if reads == read_number:
                    fault_triggered = True
                    if fault == "vanished":
                        with path.open(encoding="utf-8") as handle:
                            process.kill()
                            process.wait(timeout=2)
                            return handle.read()
                    if fault == "permission":
                        raise PermissionError("proc entry is unreadable")
                    if fault == "io":
                        raise OSError("proc filesystem read failed")
                    return "invalid proc stat"
            return original_read(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_at_process_boundary)
        recovered_count = runtime._recover_interrupted_jobs(stale_after_seconds=0)
        assert fault_triggered
        assert recovered_count == expected_recovered
        recovered = catalog.get_job(job_id, user_id="alice", session_id="s1")
        assert recovered is not None
        if expected_recovered:
            assert recovered["status"] == "queued"
        else:
            assert recovered["status"] == "running"
            assert recovered["stage"] == "recovery_blocked"
            assert recovered["lease_id"] == claimed["lease_id"]
            assert process.poll() is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def test_durable_service_runs_prepare_then_compute(monkeypatch, tmp_path: Path) -> None:
    # Exercise the production-safe start method: the child imports a narrow,
    # top-level worker entry point instead of inheriting API threads.
    monkeypatch.setenv("LARGE_GRAPH_MP_START_METHOD", "spawn")
    source = tmp_path / "registered-upload.txt"
    source.write_text("A B\nB C\nC A\nA B\n", encoding="utf-8")
    service = LargeGraphService(tmp_path / "runtime")
    service.resources.disk_reserve_bytes = 0
    try:
        submitted = service.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="upload-1",
            directed=True,
            partitions=2,
            workers=2,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert submitted["success"] is True
        prepare_status = _wait_for_job(service, submitted["job_id"])
        assert prepare_status["status"] == "done"
        prepare_result = service.job_result(
            submitted["job_id"], user_id="alice", session_id="s1"
        )
        assert prepare_result is not None
        assert prepare_result["representation"] == "partitioned_snapshot"
        assert prepare_result["current_graph_replaced"] is False

        compute = service.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=submitted["graph_version"],
            algorithm="degree_centrality",
            parameters={"top_k": 2},
            workers=2,
        )
        assert compute["success"] is True
        compute_status = _wait_for_job(service, compute["job_id"])
        assert compute_status["status"] == "done"
        result = service.job_result(
            compute["job_id"], user_id="alice", session_id="s1"
        )
        assert result is not None
        assert result["artifact_id"]
        assert len(result["preview"]) <= 2

        # Added regression coverage: the durable Worker uses spawn in
        # production, and source-batch algorithms must still be able to create
        # their own bounded process pool inside that child.
        centrality = service.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=submitted["graph_version"],
            algorithm="node_centrality",
            parameters={"centrality_type": "betweenness", "sample_sources": 10},
            workers=2,
        )
        assert centrality["success"] is True
        centrality_status = _wait_for_job(service, centrality["job_id"])
        assert centrality_status["status"] == "done"

        # Cycle enumeration adds another nested spawn pool. Exercise it
        # through the real durable queue instead of only calling the engine
        # adapter in-process.
        for algorithm, parameters, expected_key, expected_value in (
            ("find_cycles", {"limit": 10}, "count_returned", 1),
            (
                "maximum_cycle",
                {"optimize_by": "weight", "max_cycles": 10},
                "cycle_length",
                3,
            ),
        ):
            cycle_job = service.submit_compute(
                user_id="alice",
                session_id="s1",
                graph_id="g1",
                graph_version=submitted["graph_version"],
                algorithm=algorithm,
                parameters=parameters,
                workers=2,
            )
            assert cycle_job["success"] is True
            assert _wait_for_job(service, cycle_job["job_id"])["status"] == "done"
            cycle_result = service.job_result(
                cycle_job["job_id"], user_id="alice", session_id="s1"
            )
            assert cycle_result is not None
            assert cycle_result["summary"][expected_key] == expected_value
            assert cycle_result["summary"]["parallel"]["workers"] == 2
    finally:
        service.close()


def test_project_sessions_share_equivalent_large_graph_results(tmp_path: Path) -> None:
    source = tmp_path / "project-shared.edgelist"
    source.write_text("A B\nB C\nC A\nA C\n", encoding="utf-8")
    root = tmp_path / "runtime"
    service = LargeGraphService(root)
    service.resources.disk_reserve_bytes = 0
    prepared: dict[str, dict] = {}
    try:
        for session_id in ("conversation-1", "conversation-2"):
            submitted = service.submit_preparation(
                user_id="alice",
                session_id=session_id,
                graph_id=session_id,
                source_path=str(source),
                source_name=source.name,
                source_token=f"upload-{session_id}",
                directed=True,
                partitions=1,
                workers=1,
                delimiter="whitespace",
                input_format="edgelist",
                quality_policy=None,
            )
            assert _wait_for_job(
                service,
                submitted["job_id"],
                session_id=session_id,
            )["status"] == "done"
            prepared[session_id] = submitted

        original = service.submit_compute(
            user_id="alice",
            session_id="conversation-1",
            graph_id="conversation-1",
            graph_version=prepared["conversation-1"]["graph_version"],
            algorithm="node_centrality",
            parameters={
                "centrality_type": "pagerank",
                "limit": 2,
                "damping": 0.85,
                "tolerance": 1e-6,
                "max_iterations": 100,
            },
            workers=1,
        )
        assert _wait_for_job(
            service,
            original["job_id"],
            session_id="conversation-1",
        )["status"] == "done"
        original_result = service.job_result(
            original["job_id"],
            user_id="alice",
            session_id="conversation-1",
        )
        assert original_result is not None

        # Existing jobs are backfilled when Backend replays project ownership
        # after an upgrade or restart.
        for session_id in ("conversation-1", "conversation-2"):
            service.bind_session_project(
                user_id="alice",
                session_id=session_id,
                project_id="project-1",
            )

        # Opening a catalog after an upgrade rewrites legacy project cache
        # identities, so historical completed jobs immediately participate in
        # default-equivalent reuse.
        with service.catalog._connect() as conn:
            row = conn.execute(
                "SELECT request_json FROM jobs WHERE job_id=?",
                (original["job_id"],),
            ).fetchone()
            assert row is not None
            legacy_request = json.loads(row["request_json"])
            legacy_request["project_result"]["cache_key"] = "legacy-cache-key"
            conn.execute(
                "UPDATE jobs SET request_json=? WHERE job_id=?",
                (
                    json.dumps(
                        legacy_request,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    original["job_id"],
                ),
            )
        LargeGraphCatalog(service.catalog.db_path)
        reindexed = service.catalog.get_job(
            original["job_id"],
            user_id="alice",
            session_id="conversation-1",
        )
        assert reindexed is not None
        assert (
            reindexed["request"]["project_result"]["cache_key"]
            == project_result_cache_key(reindexed["request"])
        )

        reused = service.submit_compute(
            user_id="alice",
            session_id="conversation-2",
            graph_id="conversation-2",
            graph_version=prepared["conversation-2"]["graph_version"],
            algorithm="node_centrality",
            parameters={"centrality_type": "pagerank", "limit": 2},
            workers=1,
        )
        assert reused["success"] is True
        assert reused["async"] is False
        assert "job_id" not in reused
        assert reused["project_result_reused"] is True
        assert reused["source_job_id"] == original["job_id"]
        assert reused["source_session_id"] == "conversation-1"
        assert reused["graph_id"] == "conversation-2"
        assert reused["source_graph_id"] == "conversation-1"
        assert reused["summary"] == original_result["summary"]

        shared_status = service.job_status(
            original["job_id"],
            user_id="alice",
            session_id="conversation-2",
        )
        assert shared_status is not None
        assert shared_status["project_result_reused"] is True
        assert shared_status["source_session_id"] == "conversation-1"
        shared_result = service.job_result(
            original["job_id"],
            user_id="alice",
            session_id="conversation-2",
        )
        assert shared_result is not None
        assert shared_result["project_result_reused"] is True
        artifact = service.get_artifact(
            str(original_result["artifact_id"]),
            user_id="alice",
            session_id="conversation-2",
        )
        assert artifact is not None
        assert artifact["project_result_reused"] is True
        assert artifact["source_session_id"] == "conversation-1"

        assert service.get_artifact(
            str(original_result["artifact_id"]),
            user_id="bob",
            session_id="conversation-2",
        ) is None
    finally:
        service.close()

    # An equivalent queued job is shared as one unit of work. Moving the
    # requester to another trusted project immediately restores isolation.
    queued_service = LargeGraphService(root, embedded_worker=False)
    queued_service.resources.disk_reserve_bytes = 0
    try:
        queued = queued_service.submit_compute(
            user_id="alice",
            session_id="conversation-1",
            graph_id="conversation-1",
            graph_version=prepared["conversation-1"]["graph_version"],
            algorithm="degree",
            parameters={"top_k": 3},
            workers=1,
        )
        attached = queued_service.submit_compute(
            user_id="alice",
            session_id="conversation-2",
            graph_id="conversation-2",
            graph_version=prepared["conversation-2"]["graph_version"],
            algorithm="degree",
            parameters={"top_k": 3},
            workers=1,
        )
        assert attached["async"] is True
        assert attached["project_result_reused"] is True
        assert attached["job_id"] == queued["job_id"]
        assert attached["source_session_id"] == "conversation-1"

        queued_service.bind_session_project(
            user_id="alice",
            session_id="conversation-2",
            project_id="project-2",
        )
        isolated = queued_service.submit_compute(
            user_id="alice",
            session_id="conversation-2",
            graph_id="conversation-2",
            graph_version=prepared["conversation-2"]["graph_version"],
            algorithm="degree",
            parameters={"top_k": 3},
            workers=1,
        )
        assert isolated["async"] is True
        assert isolated["project_result_reused"] is False
        assert isolated["job_id"] != queued["job_id"]
        assert queued_service.job_status(
            queued["job_id"],
            user_id="alice",
            session_id="conversation-2",
        ) is None
    finally:
        queued_service.close()


def test_server_snapshot_is_prepared_idempotently_and_shared_by_session_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_MP_START_METHOD", "spawn")
    source = tmp_path / "server-source.edgelist"
    source.write_text("A B\nB C\nC A\n", encoding="utf-8")
    revision = "a" * 64
    server_dataset_id = "server-" + "b" * 64
    service = LargeGraphService(tmp_path / "runtime", embedded_worker=False)
    service.resources.disk_reserve_bytes = 0
    source_session_id, _ = service._server_dataset_scope_ids(server_dataset_id)
    try:
        first = service.ensure_server_dataset_prepared(
            server_dataset_id=server_dataset_id,
            revision=revision,
            source_path=str(source),
            source_name=source.name,
            relative_path=source.name,
            directed=False,
        )
        duplicate = service.ensure_server_dataset_prepared(
            server_dataset_id=server_dataset_id,
            revision=revision,
            source_path=str(source),
            source_name=source.name,
            relative_path=source.name,
            directed=False,
        )
        assert first["status"] == duplicate["status"] == "queued"
        assert first["job_id"] == duplicate["job_id"]
        assert first["graph_version"] == duplicate["graph_version"] == 1

        service.runtime.start()
        assert _wait_for_job(
            service,
            first["job_id"],
            user_id="__server_datasets__",
            session_id=source_session_id,
        )["status"] == "done"
        ready = service.server_dataset_preparation_status(
            server_dataset_id=server_dataset_id,
            revision=revision,
        )
        assert ready["status"] == "ready"
        assert (ready["num_nodes"], ready["num_edges"]) == (3, 3)
        assert ready["directed"] is False

        alice = service.attach_server_dataset_snapshot(
            user_id="alice",
            session_id="alice-session",
            graph_id="alice-session",
            graph_version=1,
            server_dataset_id=server_dataset_id,
            revision=revision,
        )
        bob = service.attach_server_dataset_snapshot(
            user_id="bob",
            session_id="bob-session",
            graph_id="bob-session",
            graph_version=1,
            server_dataset_id=server_dataset_id,
            revision=revision,
        )
        assert alice["success"] is bob["success"] is True
        alice_snapshot = service.catalog.get_published_snapshot(
            user_id="alice",
            session_id="alice-session",
            graph_id="alice-session",
            graph_version=1,
        )
        bob_snapshot = service.catalog.get_published_snapshot(
            user_id="bob",
            session_id="bob-session",
            graph_id="bob-session",
            graph_version=1,
        )
        assert alice_snapshot is not None and bob_snapshot is not None
        assert alice_snapshot["snapshot_id"] == bob_snapshot["snapshot_id"] == ready["snapshot_id"]
        assert alice_snapshot["manifest_path"] == bob_snapshot["manifest_path"]
        assert alice_snapshot["version_source"]["kind"] == "server_dataset_snapshot"
        snapshot_dir = Path(alice_snapshot["manifest_path"]).parent

        assert service.delete_session(
            user_id="alice", session_id="alice-session"
        )["success"] is True
        assert snapshot_dir.is_dir()
        assert service.catalog.get_published_snapshot(
            user_id="bob",
            session_id="bob-session",
            graph_id="bob-session",
            graph_version=1,
        ) is not None

        computed = service.submit_compute(
            user_id="bob",
            session_id="bob-session",
            graph_id="bob-session",
            graph_version=1,
            algorithm="degree",
            parameters={"top_k": 2},
            workers=1,
        )
        assert computed["success"] is True
        service.runtime.notify()
        compute_status = _wait_for_job(
            service,
            computed["job_id"],
            user_id="bob",
            session_id="bob-session",
        )
        assert compute_status["status"] == "done", (
            compute_status.get("error"),
            compute_status.get("message"),
            compute_status.get("summary"),
        )
        assert service.job_result(
            computed["job_id"], user_id="bob", session_id="bob-session"
        )["summary"]["result_count"] == 3

        derived = service.submit_compute(
            user_id="bob",
            session_id="bob-session",
            graph_id="bob-session",
            graph_version=1,
            algorithm="subgraph_extraction",
            parameters={
                "method": "node_subset",
                "nodes": ["A", "B"],
                "create_version": True,
            },
            workers=1,
        )
        assert derived["success"] is True
        assert derived["new_graph_version"] == 2
        service.runtime.notify()
        derived_status = _wait_for_job(
            service,
            derived["job_id"],
            user_id="bob",
            session_id="bob-session",
        )
        assert derived_status["status"] == "done", (
            derived_status.get("error"),
            derived_status.get("message"),
        )
        derived_snapshot = service.catalog.get_published_snapshot(
            user_id="bob",
            session_id="bob-session",
            graph_id="bob-session",
            graph_version=2,
        )
        assert derived_snapshot is not None
        assert derived_snapshot["snapshot_id"] != ready["snapshot_id"]
        assert derived_snapshot["user_id"] == "bob"
        assert derived_snapshot["session_id"] == "bob-session"
        assert Path(derived_snapshot["manifest_path"]).parent != snapshot_dir

        assert service.delete_session(
            user_id="bob", session_id="bob-session"
        )["success"] is True
        assert snapshot_dir.is_dir()
    finally:
        service.close()


def test_durable_subgraph_job_publishes_queryable_derived_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_MP_START_METHOD", "spawn")
    source = tmp_path / "derived-source.txt"
    source.write_text("A B\nB C\n", encoding="utf-8")
    service = LargeGraphService(tmp_path / "derived-runtime")
    service.resources.disk_reserve_bytes = 0
    try:
        prepared = service.submit_preparation(
            user_id="alice", session_id="s1", graph_id="g1",
            source_path=str(source), source_name=source.name,
            source_token="derived-upload", directed=True, partitions=2,
            workers=2, delimiter="whitespace", input_format="edgelist",
            quality_policy=None,
        )
        assert _wait_for_job(service, prepared["job_id"])["status"] == "done"
        submitted = service.submit_compute(
            user_id="alice", session_id="s1", graph_id="g1",
            graph_version=prepared["graph_version"],
            algorithm="subgraph_extraction",
            parameters={
                "method": "node_subset", "nodes": ["A", "C"],
                "create_version": True,
            },
            workers=2,
        )
        assert submitted["success"] is True
        assert submitted["new_graph_version"] == 2
        assert _wait_for_job(service, submitted["job_id"])["status"] == "done"
        result = service.job_result(
            submitted["job_id"], user_id="alice", session_id="s1"
        )
        assert result is not None
        assert result["new_graph_version"] == 2
        derived = service.catalog.get_published_snapshot(
            user_id="alice", session_id="s1", graph_id="g1", graph_version=2
        )
        assert derived is not None
        assert (derived["node_count"], derived["edge_count"]) == (2, 0)

        # The derived version is a complete v1 partitioned snapshot, not only
        # an artifact label: submit a second graph algorithm against it.
        followup = service.submit_compute(
            user_id="alice", session_id="s1", graph_id="g1", graph_version=2,
            algorithm="degree", parameters={"top_k": 2}, workers=2,
        )
        assert followup["success"] is True
        assert _wait_for_job(service, followup["job_id"])["status"] == "done"
    finally:
        service.close()


def test_standalone_worker_mode_keeps_job_durable_until_claimed(tmp_path: Path) -> None:
    source = tmp_path / "queued.txt"
    source.write_text("A B\n", encoding="utf-8")
    service = LargeGraphService(tmp_path / "runtime", embedded_worker=False)
    service.resources.disk_reserve_bytes = 0
    try:
        submitted = service.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="upload-queued",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        status = service.job_status(
            submitted["job_id"], user_id="alice", session_id="s1"
        )
        assert status is not None and status["status"] == "queued"
        assert service.cancel(
            submitted["job_id"], user_id="alice", session_id="s1"
        )
        cancelled = service.job_status(
            submitted["job_id"], user_id="alice", session_id="s1"
        )
        assert cancelled is not None and cancelled["status"] == "cancelled"
    finally:
        service.close()


def test_session_delete_purges_only_its_catalog_and_managed_files(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LARGE_GRAPH_MP_START_METHOD", "spawn")
    source = tmp_path / "delete-session.txt"
    source.write_text("A B\nB C\n", encoding="utf-8")
    service = LargeGraphService(tmp_path / "runtime")
    service.resources.disk_reserve_bytes = 0
    try:
        prepared = service.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="delete-source",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert _wait_for_job(service, prepared["job_id"])["status"] == "done"
        snapshot = service.catalog.get_published_snapshot(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=prepared["graph_version"],
        )
        assert snapshot is not None
        snapshot_dir = Path(snapshot["manifest_path"]).parent

        computed = service.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=prepared["graph_version"],
            algorithm="degree",
            parameters={"top_k": 2},
            workers=1,
        )
        assert _wait_for_job(service, computed["job_id"])["status"] == "done"
        artifact = service.get_artifact(
            str(service.job_result(
                computed["job_id"], user_id="alice", session_id="s1"
            )["artifact_id"]),
            user_id="alice",
            session_id="s1",
            include_path=True,
        )
        assert artifact is not None
        artifact_dir = Path(artifact["path"]).parent
        assert snapshot_dir.is_dir() and artifact_dir.is_dir()

        deleted = service.delete_session(user_id="alice", session_id="s1")
        assert deleted["success"] is True
        assert not snapshot_dir.exists()
        assert not artifact_dir.exists()
        assert service.list_versions(
            user_id="alice", session_id="s1", graph_id="g1"
        ) == []
        assert service.job_status(
            prepared["job_id"], user_id="alice", session_id="s1"
        ) is None
    finally:
        service.close()


def test_session_delete_is_tenant_scoped_and_waits_for_running_lease(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tenant-scope.txt"
    source.write_text("A B\n", encoding="utf-8")
    service = LargeGraphService(tmp_path / "runtime", embedded_worker=False)
    service.resources.disk_reserve_bytes = 0
    try:
        jobs: dict[str, dict] = {}
        jobs["alice"] = service.submit_preparation(
            user_id="alice",
            session_id="same-session",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="source-alice",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        claimed = service.catalog.claim_next_job()
        assert claimed is not None and claimed["user_id"] == "alice"
        jobs["bob"] = service.submit_preparation(
            user_id="bob",
            session_id="same-session",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="source-bob",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )

        pending = service.delete_session(
            user_id="alice", session_id="same-session", wait_seconds=0
        )
        assert pending["success"] is False
        assert pending["code"] == "LARGE_GRAPH_SESSION_DELETE_PENDING"
        assert service.job_status(
            jobs["alice"]["job_id"],
            user_id="alice",
            session_id="same-session",
        )["status"] == "cancelling"
        assert service.job_status(
            jobs["bob"]["job_id"],
            user_id="bob",
            session_id="same-session",
        )["status"] == "queued"
        fenced = service.submit_preparation(
            user_id="alice",
            session_id="same-session",
            graph_id="g2",
            source_path=str(source),
            source_name=source.name,
            source_token="source-during-delete",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert fenced["success"] is False
        assert fenced["code"] == "LARGE_GRAPH_SESSION_DELETING"

        service.catalog.finish_cancelled(
            jobs["alice"]["job_id"], lease_id=claimed["lease_id"]
        )
        service.runtime._drain_attempt_cleanups()
        assert service.delete_session(
            user_id="alice", session_id="same-session", wait_seconds=0
        )["success"] is True
        recreated = service.submit_preparation(
            user_id="alice",
            session_id="same-session",
            graph_id="g2",
            source_path=str(source),
            source_name=source.name,
            source_token="source-after-delete",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert recreated["success"] is True
        assert service.cancel(
            recreated["job_id"], user_id="alice", session_id="same-session"
        )
        assert service.job_status(
            jobs["bob"]["job_id"],
            user_id="bob",
            session_id="same-session",
        )["status"] == "queued"
        assert service.delete_session(
            user_id="bob", session_id="same-session", wait_seconds=0
        )["success"] is True
    finally:
        service.close()


def test_ordinary_job_cancel_does_not_fence_future_session_submissions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ordinary-cancel.txt"
    source.write_text("A B\n", encoding="utf-8")
    service = LargeGraphService(tmp_path / "runtime", embedded_worker=False)
    service.resources.disk_reserve_bytes = 0
    try:
        first = service.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="cancel-first",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert first["success"] is True
        assert service.cancel(first["job_id"], user_id="alice", session_id="s1")

        second = service.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="cancel-second",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert second["success"] is True
        assert second["graph_version"] == first["graph_version"] + 1
    finally:
        service.delete_session(user_id="alice", session_id="s1", wait_seconds=0)
        service.close()


def test_deletion_fence_is_held_across_filesystem_cleanup_for_all_replicas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "replica-fence.txt"
    source.write_text("A B\nB C\n", encoding="utf-8")
    root = tmp_path / "runtime"
    owner = LargeGraphService(root)
    replica = LargeGraphService(root, embedded_worker=False)
    owner.resources.disk_reserve_bytes = 0
    replica.resources.disk_reserve_bytes = 0
    started = threading.Event()
    release = threading.Event()
    real_rmtree = __import__("shutil").rmtree

    try:
        prepared = owner.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="replica-source",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert _wait_for_job(owner, prepared["job_id"])["status"] == "done"

        def blocking_rmtree(path, *args, **kwargs):
            started.set()
            assert release.wait(timeout=5)
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(
            "graph_data_service.large_graph.service.shutil.rmtree", blocking_rmtree
        )
        outcome: dict = {}

        def delete_in_thread() -> None:
            outcome.update(
                owner.delete_session(
                    user_id="alice", session_id="s1", wait_seconds=0
                )
            )

        thread = threading.Thread(target=delete_in_thread, daemon=True)
        thread.start()
        assert started.wait(timeout=5)

        fenced_prepare = replica.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g2",
            source_path=str(source),
            source_name=source.name,
            source_token="during-delete",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        fenced_compute = replica.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=prepared["graph_version"],
            algorithm="degree",
            parameters={},
            workers=1,
        )
        assert fenced_prepare["code"] == "LARGE_GRAPH_SESSION_DELETING"
        assert fenced_compute["code"] == "LARGE_GRAPH_SESSION_DELETING"

        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert outcome["success"] is True
    finally:
        release.set()
        owner.close()
        replica.close()


def test_compute_enqueue_revalidates_snapshot_after_cross_replica_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "stale-compute.txt"
    source.write_text("A B\nB C\n", encoding="utf-8")
    root = tmp_path / "runtime"
    submitter = LargeGraphService(root)
    deleting_replica = LargeGraphService(root, embedded_worker=False)
    submitter.resources.disk_reserve_bytes = 0
    deleting_replica.resources.disk_reserve_bytes = 0
    try:
        prepared = submitter.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="stale-source",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert _wait_for_job(submitter, prepared["job_id"])["status"] == "done"
        original_enqueue = submitter.catalog.enqueue_compute

        def delete_then_enqueue(**kwargs):
            deleted = deleting_replica.delete_session(
                user_id="alice", session_id="s1", wait_seconds=0
            )
            assert deleted["success"] is True
            return original_enqueue(**kwargs)

        monkeypatch.setattr(submitter.catalog, "enqueue_compute", delete_then_enqueue)
        result = submitter.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=prepared["graph_version"],
            algorithm="degree",
            parameters={},
            workers=1,
        )
        assert result["success"] is False
        assert result["code"] == "LARGE_GRAPH_SNAPSHOT_REQUIRED"
    finally:
        submitter.close()
        deleting_replica.close()


def test_failed_filesystem_cleanup_keeps_fence_and_catalog_for_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "retry-delete.txt"
    source.write_text("A B\n", encoding="utf-8")
    service = LargeGraphService(tmp_path / "runtime")
    service.resources.disk_reserve_bytes = 0
    real_rmtree = __import__("shutil").rmtree
    try:
        prepared = service.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="retry-source",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert _wait_for_job(service, prepared["job_id"])["status"] == "done"

        def fail_rmtree(*_args, **_kwargs):
            raise OSError("injected cleanup failure")

        monkeypatch.setattr(
            "graph_data_service.large_graph.service.shutil.rmtree", fail_rmtree
        )
        failed = service.delete_session(
            user_id="alice", session_id="s1", wait_seconds=0
        )
        assert failed["success"] is False
        assert failed["code"] == "LARGE_GRAPH_SESSION_DELETE_FAILED"
        assert service.list_versions(
            user_id="alice", session_id="s1", graph_id="g1"
        )

        fenced = service.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g2",
            source_path=str(source),
            source_name=source.name,
            source_token="must-remain-fenced",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert fenced["code"] == "LARGE_GRAPH_SESSION_DELETING"

        monkeypatch.setattr(
            "graph_data_service.large_graph.service.shutil.rmtree", real_rmtree
        )
        retried = service.delete_session(
            user_id="alice", session_id="s1", wait_seconds=0
        )
        assert retried["success"] is True
        assert service.list_versions(
            user_id="alice", session_id="s1", graph_id="g1"
        ) == []
    finally:
        service.close()


def _wait_for_job(
    service: LargeGraphService,
    job_id: str,
    *,
    user_id: str = "alice",
    session_id: str = "s1",
) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = service.job_status(
            job_id, user_id=user_id, session_id=session_id
        )
        assert status is not None
        if status["status"] in {"done", "failed", "cancelled"}:
            return status
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")

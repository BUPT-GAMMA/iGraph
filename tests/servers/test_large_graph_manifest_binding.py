from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from graph_data_service.large_graph.contracts import DataQualityPolicy
from graph_data_service.large_graph.disk_graph import PartitionedDiskGraph
from graph_data_service.large_graph.engine import LocalParallelGraphEngine
from graph_data_service.large_graph.integrity import ManifestBindingError
from graph_data_service.large_graph.service import LargeGraphService
from graph_data_service.large_graph.snapshot import PartitionedSnapshotBuilder
from graph_data_service.large_graph.runtime import DurableLargeGraphRuntime


def _publish_snapshot(
    service: LargeGraphService,
    tmp_path: Path,
    *,
    partitions: int = 1,
    source_text: str = "A B 1\nB C 2\n",
) -> dict:
    source = tmp_path / "edges.txt"
    source.write_text(source_text, encoding="utf-8")
    submitted = service.submit_preparation(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        source_path=str(source),
        source_name=source.name,
        source_token="upload-1",
        directed=True,
        partitions=partitions,
        workers=1,
        delimiter="whitespace",
        input_format="edgelist",
        quality_policy=None,
    )
    assert submitted["success"] is True
    claimed = service.catalog.claim_next_job()
    assert claimed is not None and claimed["kind"] == "prepare"
    request = claimed["request"]
    result = PartitionedSnapshotBuilder(service.root / "snapshots").build(
        source_path=request["source_path"],
        user_scope=f"{request['user_scope']}__{claimed['lease_id']}",
        graph_id=claimed["graph_id"],
        graph_version=claimed["graph_version"],
        directed=request["directed"],
        partitions=request["partitions"],
        workers=request["workers"],
        delimiter=request["delimiter"],
        input_format=request["input_format"],
        policy=DataQualityPolicy.from_dict(request["quality_policy"]),
        expected_source=request["source"],
    )
    assert service.catalog.publish_snapshot(
        claimed["job_id"], result, lease_id=claimed["lease_id"]
    )
    snapshot = service.catalog.get_published_snapshot(
        user_id="alice", session_id="s1", graph_id="g1", graph_version=1
    )
    assert snapshot is not None
    return snapshot


def _service(tmp_path: Path) -> LargeGraphService:
    service = LargeGraphService(tmp_path / "runtime", embedded_worker=False)
    service.resources.disk_reserve_bytes = 0
    return service


def _build_compute_artifact(
    service: LargeGraphService,
    snapshot: dict,
    *,
    algorithm: str = "degree",
    parameters: dict | None = None,
    top_k: int = 2,
) -> tuple[dict, dict]:
    parameters = dict(parameters) if parameters is not None else {"top_k": top_k}
    queued = service.submit_compute(
        user_id="alice",
        session_id="s1",
        graph_id="g1",
        graph_version=1,
        algorithm=algorithm,
        parameters=parameters,
        workers=1,
    )
    assert queued["success"] is True
    claimed = service.catalog.claim_next_job()
    assert claimed is not None and claimed["kind"] == "compute"
    result = LocalParallelGraphEngine(
        service.root / "artifacts", snapshot_root=service.root / "snapshots"
    ).execute(
        snapshot=snapshot,
        algorithm=algorithm,
        parameters=parameters,
        user_scope=f"{claimed['request']['user_scope']}__{claimed['lease_id']}",
        graph_id="g1",
        graph_version=1,
        workers=1,
        expected_user_id="alice",
        expected_session_id="s1",
        derived_target=claimed["request"].get("derived_target"),
    )
    return claimed, result


def test_scope_component_hashes_original_identity_and_prevents_old_collisions() -> None:
    first = LargeGraphService._scope_component("a__b", "c")
    second = LargeGraphService._scope_component("a", "b__c")
    sanitised = LargeGraphService._scope_component("a/b", "c")
    already_safe = LargeGraphService._scope_component("a_b", "c")

    assert first != second
    assert sanitised != already_safe
    assert first == LargeGraphService._scope_component("a__b", "c")
    assert len(first.rsplit("__", 1)[-1]) == 16


def test_subgraph_compute_atomically_publishes_derived_snapshot_and_artifact(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(service, tmp_path, partitions=2)
        claimed, result = _build_compute_artifact(
            service,
            snapshot,
            algorithm="subgraph_extraction",
            parameters={
                "method": "node_subset",
                "nodes": ["A", "C"],
                "create_version": True,
            },
        )
        target_version = int(claimed["request"]["derived_target"]["graph_version"])
        assert target_version == 2
        assert service.catalog.get_published_snapshot(
            user_id="alice", session_id="s1", graph_id="g1",
            graph_version=target_version,
        ) is None

        assert service.catalog.publish_artifact(
            claimed["job_id"], result, lease_id=claimed["lease_id"]
        )
        derived = service.catalog.get_published_snapshot(
            user_id="alice", session_id="s1", graph_id="g1",
            graph_version=target_version,
        )
        assert derived is not None
        assert derived["node_count"] == 2
        assert derived["edge_count"] == 0
        with PartitionedDiskGraph(derived["manifest_path"]) as graph:
            assert list(graph.iter_nodes()) == [("A", "A"), ("C", "C")]
            assert list(graph.iter_edges()) == []
        artifact = service.get_artifact(
            result["artifact_id"], user_id="alice", session_id="s1"
        )
        assert artifact is not None
        assert artifact["summary"]["new_graph_version"] == target_version
        job_result = service.job_result(
            claimed["job_id"], user_id="alice", session_id="s1"
        )
        assert job_result is not None
        assert job_result["new_graph_version"] == target_version
        assert job_result["current_graph_replaced"] is False
        assert job_result["derived_version_published"] is True
    finally:
        service.close()


def test_directed_k_truss_derives_canonical_undirected_snapshot(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(
            service,
            tmp_path,
            partitions=2,
            source_text="B A 2\nA B 9\nC B 3\nA C 4\n",
        )
        claimed, result = _build_compute_artifact(
            service, snapshot, algorithm="subgraph_extraction",
            parameters={"method": "k_truss", "k": 3, "create_version": True},
        )
        assert service.catalog.publish_artifact(
            claimed["job_id"], result, lease_id=claimed["lease_id"]
        )
        derived = service.catalog.get_published_snapshot(
            user_id="alice", session_id="s1", graph_id="g1", graph_version=2
        )
        assert derived is not None
        assert derived["directed"] is False
        assert (derived["node_count"], derived["edge_count"]) == (3, 3)
        with PartitionedDiskGraph(derived["manifest_path"]) as graph:
            assert graph.directed is False
            assert sorted(graph.iter_edges()) == [
                ("A", "B", 9.0, ""),
                ("A", "C", 4.0, ""),
                ("B", "C", 3.0, ""),
            ]
    finally:
        service.close()


def test_cancelled_subgraph_compute_retires_reserved_derived_version(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        _publish_snapshot(service, tmp_path)
        queued = service.submit_compute(
            user_id="alice", session_id="s1", graph_id="g1", graph_version=1,
            algorithm="subgraph_extraction",
            parameters={"method": "k_core", "k": 1, "create_version": True},
            workers=1,
            minimum_derived_version=7,
        )
        assert queued["success"] is True
        assert queued["new_graph_version"] == 7
        assert service.cancel(
            queued["job_id"], user_id="alice", session_id="s1"
        )
        versions = service.list_versions(
            user_id="alice", session_id="s1", graph_id="g1"
        )
        assert [(row["graph_version"], row["status"]) for row in versions] == [
            (1, "published"), (7, "cancelled")
        ]
    finally:
        service.close()


def test_subgraph_artifact_only_does_not_reserve_or_publish_a_version(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(service, tmp_path)
        claimed, result = _build_compute_artifact(
            service, snapshot, algorithm="subgraph_extraction",
            parameters={
                "method": "node_subset", "nodes": ["A", "C"],
                "create_version": False,
            },
        )
        assert "derived_target" not in claimed["request"]
        assert "derived_snapshot" not in result
        assert service.catalog.publish_artifact(
            claimed["job_id"], result, lease_id=claimed["lease_id"]
        )
        versions = service.list_versions(
            user_id="alice", session_id="s1", graph_id="g1"
        )
        assert [(row["graph_version"], row["status"]) for row in versions] == [
            (1, "published")
        ]
    finally:
        service.close()


def test_derived_retry_cleanup_is_lease_isolated_and_failure_is_terminal(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        _publish_snapshot(service, tmp_path)
        queued = service.submit_compute(
            user_id="alice", session_id="s1", graph_id="g1", graph_version=1,
            algorithm="subgraph_extraction",
            parameters={"method": "k_core", "k": 1, "create_version": True},
            workers=1,
        )
        first = service.catalog.claim_next_job()
        assert first is not None
        target_version = first["request"]["derived_target"]["graph_version"]
        base_scope = first["request"]["user_scope"]
        old_output = (
            service.root / "snapshots" / f"{base_scope}__{first['lease_id']}"
            / "g1" / f"v{target_version:08d}" / "snap_1111111111111111"
        )
        old_output.mkdir(parents=True)

        assert service.catalog.recover_interrupted_jobs(stale_after_seconds=0) == 1
        second = service.catalog.claim_next_job()
        assert second is not None and second["lease_id"] != first["lease_id"]
        new_output = (
            service.root / "snapshots" / f"{base_scope}__{second['lease_id']}"
            / "g1" / f"v{target_version:08d}" / "snap_2222222222222222"
        )
        new_output.mkdir(parents=True)
        DurableLargeGraphRuntime(service.root, service.catalog)._drain_attempt_cleanups()
        assert not old_output.exists()
        assert new_output.is_dir()

        service.catalog.finish_failed(
            second["job_id"], "derived failure", lease_id=second["lease_id"]
        )
        versions = service.list_versions(
            user_id="alice", session_id="s1", graph_id="g1"
        )
        assert [(row["graph_version"], row["status"]) for row in versions] == [
            (1, "published"), (2, "failed")
        ]
    finally:
        service.close()


def test_submit_compute_rejects_manifest_rebound_from_catalog_snapshot(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(service, tmp_path)
        manifest_path = Path(snapshot["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["snapshot_id"] = "snap_0000000000000000"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        rejected = service.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=1,
            algorithm="degree",
            parameters={},
            workers=1,
        )
        assert rejected["success"] is False
        assert rejected["code"] == "SNAPSHOT_MANIFEST_INVALID"
    finally:
        service.close()


def test_snapshot_storage_bytes_are_per_shard_and_tamper_evident(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(
            service,
            tmp_path,
            partitions=2,
            source_text="long-node-a long-node-b 1\nlong-node-b long-node-c 2\n",
        )
        manifest_path = Path(snapshot["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        storage = manifest["storage_bytes"]
        assert len(storage["nodes"]["files"]) == 2
        assert len(storage["edges"]["files"]) == 2
        assert len(storage["adjacency"]["files"]) == 2
        assert len(storage["node_order"]["files"]) == 1
        assert storage["total"] == sum(
            storage[name]["total"]
            for name in ("nodes", "edges", "adjacency", "node_order")
        )

        queued = service.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=1,
            algorithm="degree",
            parameters={},
            workers=1,
        )
        assert queued["success"] is True
        claimed = service.catalog.claim_next_job()
        assert claimed is not None
        assert claimed["request"]["snapshot"]["storage_bytes"] == storage
        service.catalog.request_cancel(
            claimed["job_id"], user_id="alice", session_id="s1"
        )

        edge_path = manifest_path.parent / manifest["edge_files"][0]
        with edge_path.open("a", encoding="utf-8") as output:
            output.write("{}\n")
        rejected = service.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=1,
            algorithm="degree",
            parameters={},
            workers=1,
        )
        assert rejected["success"] is False
        assert rejected["code"] == "SNAPSHOT_MANIFEST_INVALID"
    finally:
        service.close()


def test_snapshot_without_storage_bytes_fails_safe(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(service, tmp_path)
        manifest_path = Path(snapshot["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("storage_bytes")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        rejected = service.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=1,
            algorithm="degree",
            parameters={},
            workers=1,
        )
        assert rejected["success"] is False
        assert rejected["code"] == "SNAPSHOT_MANIFEST_INVALID"
    finally:
        service.close()


def test_engine_rechecks_job_identity_scope_root_and_partition_binding(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(service, tmp_path)
        engine = LocalParallelGraphEngine(
            service.root / "artifacts", snapshot_root=service.root / "snapshots"
        )
        with pytest.raises(ManifestBindingError, match="user_id"):
            engine.execute(
                snapshot=snapshot,
                algorithm="degree",
                parameters={},
                user_scope=(
                    f"{service._scope_component('alice', 's1')}__lease_compute"
                ),
                graph_id="g1",
                graph_version=1,
                workers=1,
                expected_user_id="mallory",
                expected_session_id="s1",
            )

        with pytest.raises(ManifestBindingError, match="租户会话目录"):
            engine.execute(
                snapshot=snapshot,
                algorithm="degree",
                parameters={},
                user_scope="mallory__other__0000000000000000__lease_compute",
                graph_id="g1",
                graph_version=1,
                workers=1,
                expected_user_id="alice",
                expected_session_id="s1",
            )

        wrong_root = tmp_path / "other-snapshot-root"
        wrong_root.mkdir()
        with pytest.raises(ManifestBindingError, match="受管根目录"):
            LocalParallelGraphEngine(
                service.root / "artifacts", snapshot_root=wrong_root
            ).execute(
                snapshot=snapshot,
                algorithm="degree",
                parameters={},
                user_scope=(
                    f"{service._scope_component('alice', 's1')}__lease_compute"
                ),
                graph_id="g1",
                graph_version=1,
                workers=1,
                expected_user_id="alice",
                expected_session_id="s1",
            )

        manifest_path = Path(snapshot["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["edge_files"][0] = "../manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ManifestBindingError, match="edge_files"):
            engine.execute(
                snapshot=snapshot,
                algorithm="degree",
                parameters={},
                user_scope=(
                    f"{service._scope_component('alice', 's1')}__lease_compute"
                ),
                graph_id="g1",
                graph_version=1,
                workers=1,
                expected_user_id="alice",
                expected_session_id="s1",
            )
    finally:
        service.close()


def test_artifact_lookup_rejects_tampered_manifest_metadata(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(service, tmp_path)
        queued = service.submit_compute(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            graph_version=1,
            algorithm="degree",
            parameters={"top_k": 2},
            workers=1,
        )
        assert queued["success"] is True
        claimed = service.catalog.claim_next_job()
        assert claimed is not None and claimed["kind"] == "compute"
        result = LocalParallelGraphEngine(
            service.root / "artifacts", snapshot_root=service.root / "snapshots"
        ).execute(
            snapshot=snapshot,
            algorithm="degree",
            parameters={"top_k": 2},
            user_scope=(
                f"{claimed['request']['user_scope']}__{claimed['lease_id']}"
            ),
            graph_id="g1",
            graph_version=1,
            workers=1,
            expected_user_id="alice",
            expected_session_id="s1",
        )
        assert service.catalog.publish_artifact(
            claimed["job_id"], result, lease_id=claimed["lease_id"]
        )
        artifact_id = result["artifact_id"]
        artifact = service.get_artifact(
            artifact_id,
            user_id="alice",
            session_id="s1",
            include_path=True,
        )
        assert artifact is not None

        manifest_path = Path(artifact["path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["metadata"]["graph_id"] = "other-graph"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        assert service.get_artifact(
            artifact_id,
            user_id="alice",
            session_id="s1",
            include_path=True,
        ) is None
    finally:
        service.close()


def test_job_status_exposes_bounded_progress_summary_as_detail(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = tmp_path / "progress.txt"
    source.write_text("A B\n", encoding="utf-8")
    try:
        submitted = service.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="g1",
            source_path=str(source),
            source_name=source.name,
            source_token="progress-source",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        claimed = service.catalog.claim_next_job()
        assert claimed is not None
        detail = {"partition": 3, "processed_edges": 12_345}
        service.catalog.update_progress(
            submitted["job_id"],
            stage="edge_map",
            progress=0.4,
            message="mapping",
            detail=detail,
            lease_id=claimed["lease_id"],
        )

        status = service.job_status(
            submitted["job_id"], user_id="alice", session_id="s1"
        )

        assert status is not None
        assert status["summary"] == detail
        assert status["detail"] == detail
        assert status["detail"] is status["summary"]
        assert "request" not in status
    finally:
        service.close()


def test_artifact_lookup_exposes_bounded_indexed_result_file_preview(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(service, tmp_path, partitions=2)
        claimed, result = _build_compute_artifact(service, snapshot)
        manifest_path = Path(result["path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result_files = list(manifest["result_files"])
        for index in range(len(result_files), 25):
            relative = f"results/extra-{index:05d}.jsonl"
            (manifest_path.parent / relative).write_text("", encoding="utf-8")
            result_files.append(relative)
        manifest["result_files"] = result_files
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        assert service.catalog.publish_artifact(
            claimed["job_id"], result, lease_id=claimed["lease_id"]
        )

        artifact = service.get_artifact(
            result["artifact_id"],
            user_id="alice",
            session_id="s1",
            include_preview=False,
        )

        assert artifact is not None
        assert "preview" not in artifact
        assert "path" not in artifact
        assert artifact["result_file_count"] == 25
        assert artifact["result_files_truncated"] is True
        assert artifact["download_index_range"] == {"start": 0, "end": 24}
        assert artifact["result_files_preview"] == [
            {"index": index, "path": relative}
            for index, relative in enumerate(result_files[:20])
        ]
        assert len(artifact["result_files_preview"]) == 20
        assert service.artifact_partition_path(
            result["artifact_id"], 24, user_id="alice", session_id="s1"
        ) == manifest_path.parent / result_files[24]
    finally:
        service.close()


def test_artifact_publication_rejects_result_rebound_to_another_snapshot(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(service, tmp_path)
        claimed, result = _build_compute_artifact(service, snapshot)
        rebound = copy.deepcopy(result)
        rebound["metadata"]["snapshot_id"] = "snap_0000000000000000"

        with pytest.raises(ValueError, match="快照"):
            service.catalog.publish_artifact(
                claimed["job_id"], rebound, lease_id=claimed["lease_id"]
            )
        assert service.catalog.get_artifact(
            result["artifact_id"], user_id="alice", session_id="s1"
        ) is None
        assert service.catalog.get_job(
            claimed["job_id"], user_id="alice", session_id="s1"
        )["status"] == "running"
    finally:
        service.close()


def test_artifact_publication_rejects_manifest_outside_managed_root(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(service, tmp_path)
        claimed, result = _build_compute_artifact(service, snapshot)
        source_manifest = Path(result["path"])
        # Preserve the complete scope/graph/version/artifact suffix so this
        # specifically exercises the managed-root binding, not just naming.
        outside_manifest = (
            tmp_path
            / "outside"
            / source_manifest.parents[3].name
            / source_manifest.parents[2].name
            / source_manifest.parents[1].name
            / source_manifest.parent.name
            / "manifest.json"
        )
        shutil.copytree(source_manifest.parent, outside_manifest.parent)
        rebound = copy.deepcopy(result)
        rebound["path"] = str(outside_manifest)

        with pytest.raises(ManifestBindingError, match="受管根目录"):
            service.catalog.publish_artifact(
                claimed["job_id"], rebound, lease_id=claimed["lease_id"]
            )
        assert service.catalog.get_artifact(
            result["artifact_id"], user_id="alice", session_id="s1"
        ) is None
        assert service.catalog.get_job(
            claimed["job_id"], user_id="alice", session_id="s1"
        )["status"] == "running"
    finally:
        service.close()


def test_artifact_publication_accepts_algorithm_family_submode_summary(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(service, tmp_path)
        claimed, result = _build_compute_artifact(
            service,
            snapshot,
            algorithm="shortest_path",
            parameters={
                "source": "A",
                "target": "C",
                "algorithm": "yen_k_shortest",
                "use_weight": False,
                "k": 2,
            },
        )
        assert result["summary"]["algorithm"] == "yen_k_shortest"
        assert result["metadata"]["algorithm"] == "shortest_path"
        assert service.catalog.publish_artifact(
            claimed["job_id"], result, lease_id=claimed["lease_id"]
        )
        assert service.get_artifact(
            result["artifact_id"], user_id="alice", session_id="s1"
        ) is not None
    finally:
        service.close()


def test_artifact_lookup_requires_owner_completed_job_and_requested_parameters(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    try:
        snapshot = _publish_snapshot(service, tmp_path)
        claimed, result = _build_compute_artifact(service, snapshot, top_k=2)
        assert service.catalog.publish_artifact(
            claimed["job_id"], result, lease_id=claimed["lease_id"]
        )
        artifact_id = result["artifact_id"]

        assert service.get_artifact(
            artifact_id, user_id="mallory", session_id="s1"
        ) is None
        assert service.get_artifact(
            artifact_id, user_id="alice", session_id="other-session"
        ) is None
        assert service.artifact_partition_path(
            artifact_id, 0, user_id="mallory", session_id="s1"
        ) is None

        result_path = service.artifact_partition_path(
            artifact_id, 0, user_id="alice", session_id="s1"
        )
        assert result_path is not None and result_path.is_file()
        assert service.artifact_partition_path(
            artifact_id, -1, user_id="alice", session_id="s1"
        ) is None

        with service.catalog._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='running' WHERE job_id=?", (claimed["job_id"],)
            )
        assert service.catalog.get_artifact(
            artifact_id, user_id="alice", session_id="s1"
        ) is None
        assert service.get_artifact(
            artifact_id, user_id="alice", session_id="s1"
        ) is None
        assert service.artifact_partition_path(
            artifact_id, 0, user_id="alice", session_id="s1"
        ) is None
        with service.catalog._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='done' WHERE job_id=?", (claimed["job_id"],)
            )

        # Altering both the catalog metadata and manifest used to bypass the
        # file-level equality check. The durable request remains authoritative.
        rebound_metadata = copy.deepcopy(result["metadata"])
        rebound_metadata["parameters"]["top_k"] = 3
        manifest_path = Path(result["path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["metadata"] = rebound_metadata
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with service.catalog._connect() as conn:
            conn.execute(
                "UPDATE artifacts SET metadata_json=? WHERE artifact_id=?",
                (
                    json.dumps(rebound_metadata, sort_keys=True),
                    artifact_id,
                ),
            )
        assert service.get_artifact(
            artifact_id, user_id="alice", session_id="s1"
        ) is None
    finally:
        service.close()

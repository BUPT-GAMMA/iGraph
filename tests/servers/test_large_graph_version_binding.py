from __future__ import annotations

import asyncio
from pathlib import Path

from graph_data_service.app import GraphDataService, _registered_upload_path
from graph_data_service.large_graph.contracts import DataQualityPolicy
from graph_data_service.large_graph.snapshot import PartitionedSnapshotBuilder
from graph_data_service.session import GraphSession, UploadFileRecord


def _close(service: GraphDataService) -> None:
    service._job_manager.cancel_all()
    service._process_pool.shutdown(wait=True, cancel_futures=True)
    service._large_graph.close()
    service._nebula.close()


def _service(tmp_path: Path, monkeypatch) -> GraphDataService:
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("GRAPH_DATA_UPLOAD_DIR", str(tmp_path / "anonymous-uploads"))
    monkeypatch.setenv("GRAPH_STORAGE_BACKEND", "local")
    monkeypatch.setenv("LARGE_GRAPH_EMBEDDED_WORKER", "false")
    monkeypatch.setenv("LARGE_GRAPH_NODE_THRESHOLD", "1")
    monkeypatch.setenv("LARGE_GRAPH_EDGE_THRESHOLD", "1")
    return GraphDataService()


def test_large_reserved_version_advances_small_graph_after_restart(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("GRAPH_DATA_UPLOAD_DIR", str(tmp_path / "uploads"))
    session = GraphSession("s1", user_id="alice")
    source = tmp_path / "first.txt"
    source.write_text("A B 1\n", encoding="utf-8")
    session.load_graph_from_upload_path(str(source))
    assert session.graph_version == 1
    session.reserve_version_floor(2)
    session.load_graph_from_upload_path(str(source))
    assert session.graph_version == 3

    restored = GraphSession("s1", user_id="alice")
    restored.load_snapshot()
    assert restored.next_graph_version() == 4


def test_large_only_session_restores_from_catalog_after_restart(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    source = tmp_path / "large-only.txt"
    source.write_text("A B 1\n", encoding="utf-8")
    try:
        service._large_graph.resources.disk_reserve_bytes = 0
        prepared = service._large_graph.submit_preparation(
            user_id="alice",
            session_id="large-only",
            graph_id="large-only",
            source_path=str(source),
            source_name=source.name,
            source_token="large-only-token",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )
        assert prepared["success"] is True
        assert prepared["graph_version"] == 1
    finally:
        _close(service)

    restored_service = _service(tmp_path, monkeypatch)
    try:
        key = restored_service._session_key("large-only", "alice")
        assert key in restored_service._sessions
        restored = restored_service.get_required(
            "large-only", user_id="alice"
        )
        assert restored.next_graph_version() == 2

        # Startup recovery is eager, and the same catalog identity also
        # supports lazy repair if the process-local registration is lost.
        restored_service._sessions.pop(key)
        lazily_restored = restored_service.get_required(
            "large-only", user_id="alice"
        )
        assert lazily_restored.next_graph_version() == 2
    finally:
        _close(restored_service)


def test_explicit_prepare_rejects_small_version_bound_to_another_upload(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    try:
        session = service.get_or_create("s1", user_id="alice")
        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        first.write_text("A B 1\n", encoding="utf-8")
        second.write_text("X Y 1\n", encoding="utf-8")
        first_upload = UploadFileRecord("token-a", "first.txt", str(first))
        second_upload = UploadFileRecord("token-b", "second.txt", str(second))
        session.load_graph_from_upload_path(str(first))
        session.mark_version_upload_source(1, first_upload)
        session.register_upload(**second_upload.__dict__)

        result = service._dispatch_graph_tool(
            "s1",
            "prepare_large_graph",
            {"filename": "second.txt", "graph_version": 1},
            "alice",
        )
        assert result["success"] is False
        assert result["code"] == "GRAPH_VERSION_SOURCE_MISMATCH"
    finally:
        _close(service)


def test_prepare_reservation_prevents_reverse_version_collision(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    try:
        session = service.get_or_create("s1", user_id="alice")
        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        first.write_text("A B 1\n", encoding="utf-8")
        second.write_text("X Y 1\n", encoding="utf-8")
        first_upload = UploadFileRecord("token-a", "first.txt", str(first))
        session.load_graph_from_upload_path(str(first))
        session.mark_version_upload_source(1, first_upload)
        session.register_upload("token-b", "second.txt", str(second))

        prepared = service._dispatch_graph_tool(
            "s1", "prepare_large_graph", {"filename": "second.txt"}, "alice"
        )
        assert prepared["success"] is True
        assert prepared["graph_version"] == 2
        session.load_graph_from_upload_path(str(first))
        assert session.graph_version == 3
    finally:
        _close(service)


def test_legacy_auto_route_rejects_same_number_with_wrong_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    try:
        session = service.get_or_create("s1", user_id="alice")
        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        first.write_text("A B 1\nB C 1\n", encoding="utf-8")
        second.write_text("X Y 1\nY Z 1\n", encoding="utf-8")
        first_upload = UploadFileRecord("token-a", "first.txt", str(first))
        session.load_graph_from_upload_path(str(first))
        session.mark_version_upload_source(1, first_upload)

        stat = second.stat()
        policy = DataQualityPolicy(
            node_normalization="exact", weight_conflict="last"
        )
        request = {
            "source_path": str(second),
            "source": {"file_token": "token-b", "size": stat.st_size},
            "user_scope": "alice__s1",
            "directed": True,
            "partitions": 1,
            "workers": 1,
            "delimiter": "whitespace",
            "input_format": "edgelist",
            "quality_policy": policy.to_dict(),
        }
        job_id, _ = service._large_graph.catalog.reserve_preparation(
            user_id="alice",
            session_id="s1",
            graph_id="s1",
            graph_version=1,
            source=request["source"],
            policy_id=policy.policy_id,
            policy=policy.to_dict(),
            request=request,
        )
        claimed = service._large_graph.catalog.claim_next_job()
        assert claimed is not None
        snapshot = PartitionedSnapshotBuilder(tmp_path / "snapshots").build(
            source_path=second,
            user_scope="alice__s1",
            graph_id="s1",
            graph_version=1,
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            policy=policy,
            expected_source={"size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        )
        assert service._large_graph.catalog.publish_snapshot(
            job_id, snapshot, lease_id=claimed["lease_id"]
        )

        result = asyncio.run(
            service.dispatch_tool(
                "s1", "global_metrics", {"metric": "density"}, user_id="alice"
            )
        )
        assert result["success"] is False
        assert result["code"] == "GRAPH_VERSION_REPRESENTATION_MISMATCH"
    finally:
        _close(service)


def test_registered_upload_path_is_user_scoped(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    alice_file = data_root / "users" / "alice" / "uploads" / "a.txt"
    bob_file = data_root / "users" / "bob" / "uploads" / "b.txt"
    project_file = data_root / "projects" / "p1" / "source.txt"
    anonymous_file = tmp_path / "anonymous" / "anon.txt"
    for path in (alice_file, bob_file, project_file, anonymous_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("A B\n", encoding="utf-8")
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(data_root))
    monkeypatch.setenv("GRAPH_DATA_UPLOAD_DIR", str(anonymous_file.parent))

    assert _registered_upload_path(str(alice_file), "alice") == alice_file.resolve()
    assert _registered_upload_path(str(bob_file), "alice") is None
    assert _registered_upload_path(str(project_file), "alice") is None
    assert _registered_upload_path(str(anonymous_file), "") == anonymous_file.resolve()


def test_large_job_result_outer_success_tracks_execution_status(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    service._large_graph.resources.disk_reserve_bytes = 0
    source = tmp_path / "job-result.txt"
    source.write_text("A B 1\n", encoding="utf-8")

    def submit(graph_id: str) -> dict:
        return service._large_graph.submit_preparation(
            user_id="alice",
            session_id="s1",
            graph_id=graph_id,
            source_path=str(source),
            source_name=source.name,
            source_token=f"token-{graph_id}",
            directed=True,
            partitions=1,
            workers=1,
            delimiter="whitespace",
            input_format="edgelist",
            quality_policy=None,
        )

    try:
        pending = submit("pending")
        pending_result = service._dispatch_graph_tool(
            "s1", "get_job_result", {"job_id": pending["job_id"]}, "alice"
        )
        assert pending_result["success"] is True
        assert pending_result["result"]["status"] == "queued"

        assert service._large_graph.cancel(
            pending["job_id"], user_id="alice", session_id="s1"
        )
        cancelled_result = service._dispatch_graph_tool(
            "s1", "get_job_result", {"job_id": pending["job_id"]}, "alice"
        )
        assert cancelled_result["success"] is False
        assert cancelled_result["result"]["status"] == "cancelled"

        failed = submit("failed")
        claimed = service._large_graph.catalog.claim_next_job()
        assert claimed is not None and claimed["job_id"] == failed["job_id"]
        service._large_graph.catalog.finish_failed(
            failed["job_id"], "boom", lease_id=claimed["lease_id"]
        )
        failed_result = service._dispatch_graph_tool(
            "s1", "get_job_result", {"job_id": failed["job_id"]}, "alice"
        )
        assert failed_result["success"] is False
        assert failed_result["result"]["status"] == "failed"
        assert failed_result["result"]["error"] == "boom"
    finally:
        _close(service)


def test_explicit_derived_submit_uses_small_version_floor_and_reserves_result(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    try:
        session = service.get_or_create("s1", user_id="alice")
        source = tmp_path / "small-history.txt"
        source.write_text("A B 1\n", encoding="utf-8")
        for _ in range(3):
            session.load_graph_from_upload_path(str(source))
        expected_version = session.next_graph_version()
        captured: dict = {}

        def submit_compute(**kwargs):
            captured.update(kwargs)
            return {
                "success": True,
                "async": True,
                "job_id": "lg_compute_floor",
                "new_graph_version": int(kwargs["minimum_derived_version"]),
            }

        monkeypatch.setattr(service._large_graph, "submit_compute", submit_compute)
        result = service._dispatch_graph_tool(
            "s1",
            "run_large_graph_algorithm",
            {
                "graph_version": 1,
                "algorithm": "subgraph_extraction",
                "parameters": {"method": "k_core", "create_version": True},
            },
            "alice",
        )

        assert result["success"] is True
        assert captured["minimum_derived_version"] == expected_version
        assert session.next_graph_version() == expected_version + 1
    finally:
        _close(service)


def test_legacy_derived_auto_route_reserves_returned_large_version(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path, monkeypatch)
    try:
        session = service.get_or_create("s1", user_id="alice")
        source = tmp_path / "legacy-source.txt"
        source.write_text("A B 1\nB C 1\n", encoding="utf-8")
        session.load_graph_from_upload_path(str(source))
        expected_version = session.next_graph_version()
        captured: dict = {}

        monkeypatch.setattr(
            service._large_graph,
            "version_matches_small_binding",
            lambda **_kwargs: True,
        )

        def submit_compute(**kwargs):
            captured.update(kwargs)
            return {
                "success": True,
                "async": True,
                "job_id": "lg_compute_legacy_floor",
                "new_graph_version": int(kwargs["minimum_derived_version"]),
            }

        monkeypatch.setattr(service._large_graph, "submit_compute", submit_compute)
        result = asyncio.run(
            service.dispatch_tool(
                "s1",
                "subgraph_extraction",
                {"method": "k_core", "create_version": True},
                user_id="alice",
            )
        )

        assert result["success"] is True
        assert captured["minimum_derived_version"] == expected_version
        assert session.next_graph_version() == expected_version + 1
    finally:
        _close(service)

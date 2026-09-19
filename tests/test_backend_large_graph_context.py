from __future__ import annotations

import pytest

from backend.app import (
    _dataset_view,
    _large_graph_prepare_context,
    _large_graph_prepare_context_from_metadata,
    _large_graph_result_matches_prepare,
    _normalize_large_graph_prepare_context,
    _normalize_partitioned_graph_context,
    _partitioned_graph_context,
    _partitioned_version_is_published,
    _published_partitioned_version,
    _session_dataset_view,
)
from backend.project_datasets import ProjectDatasetRecord


def _dataset(metadata: dict) -> ProjectDatasetRecord:
    return ProjectDatasetRecord(
        dataset_id="dataset-1",
        project_id="project-1",
        user_id="alice",
        name="large",
        original_filename="large.edgelist",
        storage_path="/private/project/source.edgelist",
        file_type="edgelist",
        metadata=metadata,
        status="ready",
    )


def test_partitioned_dataset_view_is_fixed_size_and_never_exposes_storage_paths() -> None:
    view = _dataset_view(
        _dataset(
            {
                "dataset_kind": "partitioned_graph",
                "representation": "partitioned_snapshot",
                "graph_id": "session-1",
                "graph_version": 7,
                "num_nodes": 1_250_000,
                "num_edges": 7_500_000,
                "partitions": 32,
                "directed": True,
                "has_weights": False,
                "storage_bytes": {"total": 987_654_321, "files": [1] * 10_000},
                "preparation_status": "running",
                "manifest_path": "/private/snapshots/manifest.json",
                "shards": [f"/private/shard-{index}" for index in range(10_000)],
                "preview": [str(index) for index in range(10_000)],
            }
        )
    )

    assert view == {
        "kind": "partitioned_graph",
        "visualization": {
            "supported": False,
            "code": "PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE",
        },
        "summary": {
            "representation": "partitioned_snapshot",
            "num_nodes": 1_250_000,
            "num_edges": 7_500_000,
            "graph_version": 7,
            "partitions": 32,
            "directed": True,
            "has_weights": False,
            "storage_bytes": 987_654_321,
            "preparation_status": "running",
        },
    }


def test_partitioned_agent_context_is_session_bound_and_strips_unbounded_fields() -> None:
    context = _partitioned_graph_context(
        {
            "graph_id": "session-1",
            "graph_version": 4,
            "representation": "partitioned_snapshot",
            "num_nodes": 1_000_000,
            "num_edges": 6_000_000,
            "summary": {"directed": True},
            "manifest_path": "/private/manifest.json",
            "partition_paths": [str(index) for index in range(1000)],
        },
        dataset_id="dataset-1",
        session_id="session-1",
    )

    assert context == {
        "dataset_id": "dataset-1",
        "graph_id": "session-1",
        "graph_version": 4,
        "representation": "partitioned_snapshot",
        "current_graph_replaced": False,
        "num_nodes": 1_000_000,
        "num_edges": 6_000_000,
        "directed": True,
    }
    assert _normalize_partitioned_graph_context(
        {**context, "manifest_path": "/private/manifest.json"},
        dataset_id="dataset-1",
        session_id="session-1",
    ) == context
    assert _normalize_partitioned_graph_context(
        context,
        dataset_id="another-dataset",
        session_id="session-1",
    ) is None
    with pytest.raises(ValueError, match="当前会话不一致"):
        _partitioned_graph_context(
            {**context, "graph_id": "another-session"},
            dataset_id="dataset-1",
            session_id="session-1",
        )


def test_pending_prepare_and_terminal_result_are_fenced_to_exact_job_version() -> None:
    pending = _large_graph_prepare_context(
        {
            "job_id": "lg_prepare_1",
            "graph_id": "session-1",
            "graph_version": 9,
            "status": "RUNNING",
            "source_path": "/private/source",
            "resource_estimate": {"large": [0] * 1000},
        },
        dataset_id="dataset-1",
        session_id="session-1",
    )
    assert pending == {
        "dataset_id": "dataset-1",
        "job_id": "lg_prepare_1",
        "graph_id": "session-1",
        "graph_version": 9,
        "status": "running",
    }
    assert _normalize_large_graph_prepare_context(
        {**pending, "manifest_path": "/private/manifest"},
        dataset_id="dataset-1",
        session_id="session-1",
    ) == pending
    assert _large_graph_result_matches_prepare(
        {
            "job_id": "lg_prepare_1",
            "graph_id": "session-1",
            "graph_version": 9,
            "status": "done",
            "representation": "partitioned_snapshot",
        },
        pending,
    )
    assert not _large_graph_result_matches_prepare(
        {
            "job_id": "lg_prepare_1",
            "graph_id": "session-1",
            "graph_version": 10,
            "status": "done",
            "representation": "partitioned_snapshot",
        },
        pending,
    )


def test_pending_prepare_can_be_recovered_from_session_bound_dataset_metadata() -> None:
    dataset = _dataset(
        {
            "dataset_kind": "partitioned_graph",
            "representation": "partitioned_snapshot",
            "preparation_job_id": "lg_prepare_1",
            "preparation_status": "running",
            "graph_id": "session-1",
            "graph_version": 9,
        }
    )

    assert _large_graph_prepare_context_from_metadata(
        dataset,
        session_id="session-1",
    ) == {
        "dataset_id": "dataset-1",
        "job_id": "lg_prepare_1",
        "graph_id": "session-1",
        "graph_version": 9,
        "status": "running",
    }
    assert _large_graph_prepare_context_from_metadata(
        dataset,
        session_id="session-2",
    ) is None
    dataset.metadata.pop("preparation_job_id")
    assert _large_graph_prepare_context_from_metadata(
        dataset,
        session_id="session-1",
    ) is None


def test_published_version_check_requires_exact_graph_version_and_status() -> None:
    context = {"graph_id": "session-1", "graph_version": 3}
    payload = {
        "success": True,
        "graph_id": "session-1",
        "versions": [
            {"graph_version": 2, "status": "published"},
            {
                "graph_id": "session-1",
                "graph_version": 3,
                "status": "published",
                "node_count": 100,
            },
            {"graph_version": 4, "status": "preparing"},
        ],
    }

    assert _partitioned_version_is_published(payload, context)
    assert _published_partitioned_version(payload, context) == payload["versions"][1]
    assert not _partitioned_version_is_published(
        payload, {"graph_id": "session-1", "graph_version": 4}
    )
    assert not _partitioned_version_is_published(
        payload, {"graph_id": "another-session", "graph_version": 3}
    )
    assert _published_partitioned_version(
        {"success": True, "graph_id": "session-1", "versions": "invalid"},
        context,
    ) is None


def test_partitioned_dataset_view_uses_only_the_current_session_binding() -> None:
    dataset = _dataset(
        {
            "dataset_kind": "partitioned_graph",
            "representation": "partitioned_snapshot",
            "graph_id": "session-2",
            "graph_version": 8,
            "num_nodes": 8_000,
            "num_edges": 16_000,
            "partitions": 32,
            "storage_bytes": 999_999,
        }
    )
    context = {
        "dataset_id": "dataset-1",
        "graph_id": "session-1",
        "graph_version": 4,
        "representation": "partitioned_snapshot",
        "current_graph_replaced": False,
        "num_nodes": 4_000,
        "num_edges": 7_000,
        "directed": True,
    }

    view = _session_dataset_view(
        dataset,
        {"large_graph_context": context},
        session_id="session-1",
    )

    assert view == {
        "kind": "partitioned_graph",
        "visualization": {
            "supported": False,
            "code": "PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE",
        },
        "summary": {
            "representation": "partitioned_snapshot",
            "graph_version": 4,
            "num_nodes": 4_000,
            "num_edges": 7_000,
            "directed": True,
        },
    }

    pending = _session_dataset_view(
        dataset,
        {
            "large_graph_prepare": {
                "dataset_id": "dataset-1",
                "job_id": "lg_prepare_session_1",
                "graph_id": "session-1",
                "graph_version": 5,
                "status": "running",
            }
        },
        session_id="session-1",
    )
    assert pending["summary"] == {
        "representation": "partitioned_snapshot",
        "graph_version": 5,
        "preparation_status": "running",
    }

    not_started = _session_dataset_view(dataset, {}, session_id="session-1")
    assert not_started["summary"] == {
        "representation": "partitioned_snapshot",
        "preparation_status": "not_started",
    }

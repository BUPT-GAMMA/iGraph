from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

import backend.app as backend_app
from backend.app import create_app
from backend.config import BackendConfig
from backend.project_datasets import ProjectDatasetDirectory
from backend.server_datasets import ServerDatasetDirectory
from graph_data_service.session import GraphSession


class _PreparedServerServiceClient:
    runtime_sessions: dict[str, dict] = {}
    prepare_calls: list[dict] = []
    attach_calls: list[dict] = []

    def __init__(self, base_url: str, *_args) -> None:
        self.is_runtime = "runtime" in base_url

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict,
        params=None,
        json_body=None,
        **_kwargs,
    ):
        if self.is_runtime:
            if method == "GET" and path.endswith("/runs"):
                return {"data": []}
            if path.startswith("/sessions/"):
                session_id = path.split("/")[2]
                session = self.runtime_sessions[session_id]
                if method == "GET":
                    return session
                if method == "PATCH":
                    session.update(json_body or {})
                    return session
            raise AssertionError(f"unexpected runtime request: {method} {path}")
        if method == "POST" and path == "/server-datasets/prepare":
            self.prepare_calls.append(dict(json_body or {}))
            return {
                "success": True,
                "status": "ready",
                "progress": 1.0,
                "graph_version": 1,
                "num_nodes": 10_000_000,
                "num_edges": 100_000_000,
                "partitions": 16,
                "directed": True,
            }
        if method == "POST" and path.endswith("/server-datasets/attach"):
            session_id = path.split("/")[2]
            self.attach_calls.append({"session_id": session_id, **dict(json_body or {})})
            return {
                "success": True,
                "representation": "partitioned_snapshot",
                "format_detected": "edgelist",
                "graph_id": session_id,
                "graph_version": 1,
                "num_nodes": 10_000_000,
                "num_edges": 100_000_000,
                "summary": {"directed": True, "partitions": 16},
            }
        if path.endswith("/uploads/ingest"):
            raise AssertionError("large server source must not be streamed to a session")
        if method in {"POST", "PATCH", "DELETE"} and path.startswith("/sessions/"):
            return {"success": True}
        raise AssertionError(f"unexpected graph request: {method} {path}")


def test_server_dataset_catalog_is_recursive_bounded_and_does_not_follow_links(
    tmp_path: Path,
) -> None:
    root = tmp_path / "server"
    nested = root / "nested"
    nested.mkdir(parents=True)
    source = nested / "graph.edgelist"
    source.write_bytes(b"A B\n")
    (root / "ignored.exe").write_bytes(b"ignored")
    (root / ".hidden.csv").write_bytes(b"A,B\n")
    try:
        (root / "linked.csv").symlink_to(tmp_path / "outside.csv")
    except OSError:
        pass

    catalog = ServerDatasetDirectory(root)
    rows = catalog.list()

    assert len(rows) == 1
    assert rows[0].relative_path == "nested/graph.edgelist"
    assert rows[0].size_bytes == source.stat().st_size
    assert len(rows[0].revision) == 64
    assert catalog.list()[0].revision == rows[0].revision
    assert rows[0].public_dict().get("storage_path") is None
    assert catalog.get(rows[0].server_dataset_id) == rows[0]
    assert catalog.get("server-does-not-exist") is None

    source.write_bytes(b"A B\nB C\n")
    changed = catalog.list()[0]
    assert changed.server_dataset_id == rows[0].server_dataset_id
    assert changed.revision != rows[0].revision

    metadata = source.with_name(source.name + ".meta.json")
    metadata.write_text(json.dumps({"directed": False}), encoding="utf-8")
    undirected_rows = catalog.list()
    assert len(undirected_rows) == 1
    assert undirected_rows[0].directed is False
    assert undirected_rows[0].revision != changed.revision


def test_server_reference_is_deduplicated_and_deletion_preserves_shared_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "server" / "graph.csv"
    source.parent.mkdir()
    source.write_text("A,B\n", encoding="utf-8")
    directory = ProjectDatasetDirectory(
        tmp_path / "project_datasets.json", tmp_path / "projects"
    )

    created, was_created = directory.create_reference(
        project_id="project-1",
        user_id="alice",
        name="Graph",
        original_filename=source.name,
        storage_path=source,
        source_id="server-source-1",
    )
    duplicate, duplicate_created = directory.create_reference(
        project_id="project-1",
        user_id="alice",
        name="Renamed",
        original_filename=source.name,
        storage_path=source,
        source_id="server-source-1",
    )

    assert was_created is True
    assert duplicate_created is False
    assert duplicate.dataset_id == created.dataset_id
    assert not (
        tmp_path / "projects" / "project-1" / "datasets" / created.dataset_id
    ).exists()
    assert directory.delete(created.dataset_id, "alice", "project-1") is True
    assert source.read_text(encoding="utf-8") == "A,B\n"


def test_server_dataset_http_catalog_and_project_import(
    tmp_path: Path, monkeypatch
) -> None:
    server_root = tmp_path / "server"
    server_root.mkdir()
    source = server_root / "huge.edgelist"
    source.write_bytes(b"A B\nB C\n")
    supported = tmp_path / "supported.txt"
    supported.write_text("admin\n", encoding="utf-8")
    monkeypatch.setenv("BACKEND_SERVER_DATASETS_DIR", str(server_root))
    config = BackendConfig(
        host="127.0.0.1",
        port=0,
        agent_runtime_url="http://unused-runtime",
        graph_engine_url="http://unused-graph",
        agent_id="igraph-agent",
        request_timeout=1,
        stream_timeout=1,
        users_file=tmp_path / "users.json",
        projects_file=tmp_path / "projects.json",
        supported_ids_file=supported,
        admin_user_id="admin",
        default_admin_model="qwen3.6-plus",
        default_user_model="qwen3.6-plus",
        custom_models_file=tmp_path / "custom_models.json",
    )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(config)),
            base_url="http://test",
            headers={"X-User-ID": "admin"},
        ) as client:
            project = (
                await client.post("/api/projects", json={"name": "Large graph"})
            ).json()["project"]
            listed = await client.get("/api/server-datasets")
            assert listed.status_code == 200
            public_source = listed.json()["datasets"][0]
            assert public_source["relative_path"] == "huge.edgelist"
            assert "storage_path" not in public_source

            imported = await client.post(
                f"/api/projects/{project['project_id']}/datasets/from-server",
                json={"server_dataset_id": public_source["server_dataset_id"]},
            )
            assert imported.status_code == 200
            payload = imported.json()
            assert payload["created"] is True
            assert payload["dataset"]["source_kind"] == "server"
            assert payload["activated_session_id"] is None

            duplicate = await client.post(
                f"/api/projects/{project['project_id']}/datasets/from-server",
                json={"server_dataset_id": public_source["server_dataset_id"]},
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["created"] is False
            assert duplicate.json()["dataset"]["dataset_id"] == payload["dataset"]["dataset_id"]

    asyncio.run(exercise())


def test_large_server_dataset_uses_prepared_snapshot_without_session_copy(
    tmp_path: Path, monkeypatch
) -> None:
    server_root = tmp_path / "server"
    server_root.mkdir()
    source = server_root / "huge.edgelist"
    with source.open("wb") as output:
        output.truncate(64 * 1024 * 1024)
    supported = tmp_path / "supported.txt"
    supported.write_text("admin\n", encoding="utf-8")
    monkeypatch.setenv("BACKEND_SERVER_DATASETS_DIR", str(server_root))
    monkeypatch.setattr(backend_app, "ServiceClient", _PreparedServerServiceClient)
    _PreparedServerServiceClient.runtime_sessions = {}
    _PreparedServerServiceClient.prepare_calls = []
    _PreparedServerServiceClient.attach_calls = []
    config = BackendConfig(
        host="127.0.0.1",
        port=0,
        agent_runtime_url="http://fake-runtime",
        graph_engine_url="http://fake-graph",
        agent_id="igraph-agent",
        request_timeout=1,
        stream_timeout=1,
        users_file=tmp_path / "users.json",
        projects_file=tmp_path / "projects.json",
        supported_ids_file=supported,
        admin_user_id="admin",
        default_admin_model="qwen3.6-plus",
        default_user_model="qwen3.6-plus",
        custom_models_file=tmp_path / "custom_models.json",
    )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend_app.create_app(config)),
            base_url="http://test",
            headers={"X-User-ID": "admin"},
        ) as client:
            project = (
                await client.post("/api/projects", json={"name": "Prepared graph"})
            ).json()["project"]
            _PreparedServerServiceClient.runtime_sessions["session-1"] = {
                "session_id": "session-1",
                "session_name": "Prepared graph",
                "session_state": {
                    "project_id": project["project_id"],
                    "model": "qwen3.6-plus",
                },
                "user_id": "admin",
                "agent_id": "igraph-agent",
            }
            listed = (await client.get("/api/server-datasets")).json()["datasets"][0]
            assert listed["preparation"]["status"] == "ready"

            imported = await client.post(
                f"/api/projects/{project['project_id']}/datasets/from-server",
                json={
                    "server_dataset_id": listed["server_dataset_id"],
                    "session_id": "session-1",
                },
            )
            assert imported.status_code == 200
            payload = imported.json()
            assert payload["dataset_view"]["kind"] == "partitioned_graph"
            assert payload["dataset"]["metadata"]["source_revision"] == listed["revision"]
            assert _PreparedServerServiceClient.attach_calls == [
                {
                    "session_id": "session-1",
                    "server_dataset_id": listed["server_dataset_id"],
                    "revision": listed["revision"],
                }
            ]

    asyncio.run(exercise())


def test_selected_source_versions_are_persisted_in_the_user_session(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path))
    session_id = "server-source-session"
    upload_dir = tmp_path / "users" / "alice" / "uploads" / session_id
    upload_dir.mkdir(parents=True)
    copied_source = upload_dir / "server-source_graph.txt"
    copied_source.write_text("A B\nB C\n", encoding="utf-8")

    session = GraphSession(session_id, user_id="alice")
    session.register_upload("server-source", "graph.txt", str(copied_source))
    session.load_graph_from_upload_path(str(copied_source))
    copied_source.write_text("A B\nB C\nC D\n", encoding="utf-8")
    session.load_graph_from_upload_path(str(copied_source))

    session_dir = (
        tmp_path / "users" / "alice" / "graph_sessions" / session_id
    )
    assert session.graph_version == 2
    assert (session_dir / "graph_current.npz").is_file()
    assert (session_dir / "graph_versions.json").is_file()
    assert (session_dir / "snapshots" / "v001.npz").is_file()
    assert (session_dir / "snapshots" / "v002.npz").is_file()
    assert not (upload_dir / "snapshots").exists()

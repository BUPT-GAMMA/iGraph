from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import backend.app as backend_app
from backend.config import BackendConfig
from backend.projects import ProjectDirectory


class FakeServiceClient:
    runtime_sessions: dict[str, dict] = {}
    runtime_runs: dict[str, list[dict]] = {}
    graph_loaded: dict[str, bool] = {}
    graph_sources: dict[str, str] = {}
    graph_deletes: list[str] = []

    def __init__(self, base_url: str, *_args) -> None:
        self.is_runtime = "runtime" in base_url

    async def request(self, method: str, path: str, *, headers: dict, params=None, json_body=None, **_kwargs):
        user_id = headers.get("X-User-ID", "")
        if not self.is_runtime:
            parts = path.strip("/").split("/")
            session_id = parts[1] if len(parts) > 1 and parts[0] == "sessions" else ""
            if method == "GET" and path.endswith("/graph/info"):
                loaded = self.graph_loaded.get(session_id, False)
                return {"loaded": loaded, "num_nodes": 3 if loaded else 0, "num_edges": 2 if loaded else 0}
            if method == "POST" and path.endswith("/uploads/register"):
                self.graph_sources[session_id] = str((json_body or {}).get("saved_path") or "")
                return {"success": True}
            if method == "POST" and path.endswith("/graph/load_upload"):
                self.graph_loaded[session_id] = True
                return {"success": True, "num_nodes": 3, "num_edges": 2}
            if method == "POST" and path.endswith("/graph/visualize"):
                return {"success": True, "nodes": [{"id": "A"}], "edges": [], "total_nodes": 3, "total_edges": 2}
            if method == "DELETE" and len(parts) == 2 and parts[0] == "sessions":
                self.graph_loaded.pop(session_id, None)
                self.graph_sources.pop(session_id, None)
                self.graph_deletes.append(session_id)
                return {"success": True}
            if method == "POST" and len(parts) == 2 and parts[0] == "sessions":
                self.graph_loaded.setdefault(session_id, False)
                return {"success": True}
            return {"success": True}

        if path == "/sessions" and method == "GET":
            return {"data": [item for item in self.runtime_sessions.values() if item.get("user_id") == user_id]}
        if path == "/sessions" and method == "POST":
            payload = dict(json_body or {})
            self.runtime_sessions[payload["session_id"]] = payload
            return payload
        if path.startswith("/sessions/") and path.endswith("/runs") and method == "GET":
            return {"data": self.runtime_runs.get(path.split("/")[2], [])}
        if path.startswith("/sessions/"):
            session_id = path.split("/")[2]
            session = self.runtime_sessions.get(session_id)
            if session is None or session.get("user_id") != user_id:
                raise HTTPException(status_code=404, detail="session not found")
            if method == "GET":
                return session
            if method == "PATCH":
                session.update(json_body or {})
                return session
            if method == "DELETE":
                del self.runtime_sessions[session_id]
                return None
        raise AssertionError(f"Unexpected fake request: {method} {path}")


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    FakeServiceClient.runtime_sessions = {
        "legacy-session": {
            "session_id": "legacy-session",
            "session_name": "Legacy conversation",
            "session_state": {},
            "user_id": "admin",
            "agent_id": "igraph-agent",
        }
    }
    FakeServiceClient.graph_loaded = {}
    FakeServiceClient.runtime_runs = {}
    FakeServiceClient.graph_sources = {}
    FakeServiceClient.graph_deletes = []
    seed = tmp_path / "supported.txt"
    seed.write_text("admin\nalice\n", encoding="utf-8")
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
        supported_ids_file=seed,
        admin_user_id="admin",
        default_admin_model="qwen3.6-plus",
        default_user_model="qwen3.6-plus",
    )
    monkeypatch.setattr(backend_app, "ServiceClient", FakeServiceClient)
    with TestClient(backend_app.create_app(config)) as test_client:
        yield test_client


def headers(user_id: str = "admin") -> dict[str, str]:
    return {"X-User-ID": user_id}


def test_project_directory_has_stable_per_user_general(tmp_path: Path):
    directory = ProjectDirectory(tmp_path / "projects.json")
    first = directory.ensure_default("admin")
    second = directory.ensure_default("admin")
    other = directory.ensure_default("alice")

    assert first.project_id == second.project_id
    assert first.project_id != other.project_id
    assert first.name == "General"
    assert first.is_default is True


def test_project_crud_session_move_and_safe_delete(client: TestClient):
    listed = client.get("/api/projects", headers=headers()).json()["projects"]
    general = listed[0]
    assert general["is_default"] is True
    assert general["conversation_count"] == 1

    created = client.post(
        "/api/projects",
        headers=headers(),
        json={"name": "Graph Research", "description": "Experiments"},
    ).json()["project"]
    project_id = created["project_id"]

    renamed = client.patch(
        f"/api/projects/{project_id}",
        headers=headers(),
        json={"name": "GNN Research"},
    ).json()["project"]
    assert renamed["name"] == "GNN Research"

    conversation = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project_id},
    )
    assert conversation.status_code == 200
    session_id = conversation.json()["session_id"]

    project_sessions = client.get(
        f"/api/projects/{project_id}/conversations",
        headers=headers(),
    ).json()["conversations"]
    assert [item["session_id"] for item in project_sessions] == [session_id]

    moved = client.patch(
        f"/api/sessions/{session_id}",
        headers=headers(),
        json={"project_id": general["project_id"]},
    )
    assert moved.status_code == 200
    assert FakeServiceClient.runtime_sessions[session_id]["session_state"]["project_id"] == general["project_id"]

    another = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project_id},
    ).json()["session_id"]
    deleted = client.request(
        "DELETE",
        f"/api/projects/{project_id}",
        headers=headers(),
        json={"conversation_action": "move_to_general"},
    )
    assert deleted.status_code == 200
    assert FakeServiceClient.runtime_sessions[another]["session_state"]["project_id"] == general["project_id"]


def test_project_delete_conversations_and_cross_user_isolation(client: TestClient):
    project = client.post("/api/projects", headers=headers(), json={"name": "Temporary"}).json()["project"]
    session_id = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()["session_id"]

    forbidden = client.patch(
        f"/api/sessions/{session_id}",
        headers=headers("alice"),
        json={"project_id": project["project_id"]},
    )
    assert forbidden.status_code == 404

    deleted = client.request(
        "DELETE",
        f"/api/projects/{project['project_id']}",
        headers=headers(),
        json={"conversation_action": "delete"},
    )
    assert deleted.status_code == 200
    assert session_id not in FakeServiceClient.runtime_sessions

    general = client.get("/api/projects", headers=headers()).json()["projects"][0]
    protected = client.request(
        "DELETE",
        f"/api/projects/{general['project_id']}",
        headers=headers(),
        json={},
    )
    assert protected.status_code == 400


def test_project_dataset_upload_inheritance_switch_and_isolation(client: TestClient, tmp_path: Path):
    project = client.post("/api/projects", headers=headers(), json={"name": "Cora Research"}).json()["project"]
    session_a = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()["session_id"]

    uploaded = client.post(
        f"/api/projects/{project['project_id']}/datasets",
        headers=headers(),
        data={"session_id": session_a},
        files={"file": ("cora.txt", b"A B\nB C\n", "text/plain")},
    )
    assert uploaded.status_code == 200
    dataset = uploaded.json()["dataset"]
    assert uploaded.json()["default_dataset_id"] == dataset["dataset_id"]
    assert FakeServiceClient.runtime_sessions[session_a]["session_state"]["active_dataset_id"] == dataset["dataset_id"]
    assert FakeServiceClient.graph_loaded[session_a] is True

    sources = list((tmp_path / "projects" / project["project_id"] / "datasets").rglob("cora.txt"))
    assert len(sources) == 1
    assert sources[0].read_bytes() == b"A B\nB C\n"

    session_b_response = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()
    session_b = session_b_response["session_id"]
    assert session_b_response["active_dataset_id"] == dataset["dataset_id"]
    activated = client.post(f"/api/sessions/{session_b}/activate", headers=headers())
    assert activated.status_code == 200
    assert activated.json()["has_graph"] is True
    assert FakeServiceClient.graph_sources[session_a] == FakeServiceClient.graph_sources[session_b]

    other = client.post("/api/projects", headers=headers(), json={"name": "Other"}).json()["project"]
    other_session = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": other["project_id"]},
    ).json()["session_id"]
    rejected = client.patch(
        f"/api/sessions/{other_session}/dataset",
        headers=headers(),
        json={"active_dataset_id": dataset["dataset_id"]},
    )
    assert rejected.status_code == 404


def test_project_dataset_delete_clears_sessions_but_conversation_delete_preserves_source(client: TestClient, tmp_path: Path):
    project = client.post("/api/projects", headers=headers(), json={"name": "Shared Data"}).json()["project"]
    session_id = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()["session_id"]
    dataset = client.post(
        f"/api/projects/{project['project_id']}/datasets",
        headers=headers(),
        files={"file": ("graph.csv", b"source,target\nA,B\n", "text/csv")},
    ).json()["dataset"]
    source = next((tmp_path / "projects" / project["project_id"] / "datasets").rglob("graph.csv"))

    client.delete(f"/api/sessions/{session_id}", headers=headers())
    assert source.is_file()
    assert client.get(f"/api/projects/{project['project_id']}/datasets", headers=headers()).json()["datasets"]

    inheriting = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()["session_id"]
    deleted = client.delete(
        f"/api/projects/{project['project_id']}/datasets/{dataset['dataset_id']}",
        headers=headers(),
    )
    assert deleted.status_code == 200
    assert FakeServiceClient.runtime_sessions[inheriting]["session_state"]["active_dataset_id"] is None
    assert inheriting in FakeServiceClient.graph_deletes
    assert not source.exists()


def test_activate_returns_active_run_and_new_chat_is_rejected(client: TestClient):
    session_id = client.post("/api/sessions", headers=headers(), json={}).json()["session_id"]
    FakeServiceClient.runtime_runs[session_id] = [{
        "run_id": "run-active",
        "status": "RUNNING",
        "created_at": "2026-08-31T01:00:00Z",
        "events": [{"event_index": 39}, {"event_index": 42}],
    }]

    activated = client.post(f"/api/sessions/{session_id}/activate", headers=headers())
    assert activated.status_code == 200
    assert activated.json()["active_run"] == {
        "run_id": "run-active",
        "status": "running",
        "last_event_index": 42,
    }

    rejected = client.post(
        f"/api/sessions/{session_id}/chat",
        headers=headers(),
        json={"message": "must not create a second run"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["active_run"]["run_id"] == "run-active"

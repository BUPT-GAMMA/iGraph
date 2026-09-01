from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
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
    graph_sessions: set[str] = set()
    graph_sources: dict[str, str] = {}
    graph_registered_names: dict[str, str] = {}
    graph_collection_sessions: set[str] = set()
    graph_visualize_payloads: dict[str, object] = {}
    graph_load_failures: set[str] = set()
    graph_load_delay = 0.0
    graph_active_loads = 0
    graph_max_active_loads = 0
    graph_deletes: list[str] = []
    graph_session_creates: list[str] = []
    runtime_session_creates: list[str] = []
    runtime_stream_calls: list[dict] = []
    runtime_stream_error: HTTPException | None = None

    def __init__(self, base_url: str, *_args) -> None:
        self.is_runtime = "runtime" in base_url

    async def request(self, method: str, path: str, *, headers: dict, params=None, json_body=None, **_kwargs):
        user_id = headers.get("X-User-ID", "")
        if not self.is_runtime:
            parts = path.strip("/").split("/")
            session_id = parts[1] if len(parts) > 1 and parts[0] == "sessions" else ""
            if method == "GET" and path.endswith("/graph/info"):
                if session_id not in self.graph_sessions:
                    raise HTTPException(status_code=404, detail="graph session not found")
                loaded = self.graph_loaded.get(session_id, False)
                return {"loaded": loaded, "num_nodes": 3 if loaded else 0, "num_edges": 2 if loaded else 0}
            if method == "POST" and path.endswith("/uploads/register"):
                self.graph_sources[session_id] = str((json_body or {}).get("saved_path") or "")
                self.graph_registered_names[session_id] = str((json_body or {}).get("original_filename") or "")
                return {"success": True}
            if method == "POST" and path.endswith("/graph/load_upload"):
                type(self).graph_active_loads += 1
                type(self).graph_max_active_loads = max(type(self).graph_max_active_loads, type(self).graph_active_loads)
                try:
                    if self.graph_load_delay:
                        await asyncio.sleep(self.graph_load_delay)
                    filename = str((json_body or {}).get("filename") or self.graph_registered_names.get(session_id) or "")
                    if filename in self.graph_load_failures:
                        return {"success": False, "error": "synthetic load failure"}
                    if "hiv" in filename.lower() or "pcba" in filename.lower():
                        return {
                            "success": True,
                            "async": True,
                            "job_id": f"job-{session_id}",
                            "format_detected": "graph_classification_dataset",
                            "dataset_profile": "pcba" if "pcba" in filename.lower() else "hiv",
                        }
                    self.graph_loaded[session_id] = True
                    return {"success": True, "num_nodes": 3, "num_edges": 2}
                finally:
                    type(self).graph_active_loads -= 1
            if method == "POST" and path.endswith("/tools/get_job_status"):
                self.graph_collection_sessions.add(session_id)
                profile = "pcba" if "pcba" in self.graph_registered_names.get(session_id, "").lower() else "hiv"
                num_tasks = 128 if profile == "pcba" else 1
                metric = "ap" if profile == "pcba" else "roc_auc"
                return {"job": {"status": "done", "result": {
                    "success": True,
                    "dataset": {"name": profile, "num_graphs": 41127, "num_tasks": num_tasks, "metric": metric},
                    "readiness": {
                        "supported": True,
                        "num_graphs": 41127,
                        "num_tasks": num_tasks,
                        "metric": metric,
                        "split_sizes": {"train": 32901, "val": 4113, "test": 4113},
                        "node_vocab_size": 382,
                        "edge_vocab_size": 8,
                        "missing_label_rate": 0.0,
                    },
                }}}
            if method == "POST" and path.endswith("/graph/visualize"):
                return self.graph_visualize_payloads.get(session_id, {"success": True, "nodes": [{"id": "A"}], "edges": [], "total_nodes": 3, "total_edges": 2})
            if method == "DELETE" and len(parts) == 2 and parts[0] == "sessions":
                self.graph_loaded.pop(session_id, None)
                self.graph_collection_sessions.discard(session_id)
                self.graph_sources.pop(session_id, None)
                self.graph_registered_names.pop(session_id, None)
                self.graph_sessions.discard(session_id)
                self.graph_deletes.append(session_id)
                return {"success": True}
            if method == "POST" and len(parts) == 2 and parts[0] == "sessions":
                self.graph_sessions.add(session_id)
                self.graph_loaded.setdefault(session_id, False)
                self.graph_session_creates.append(session_id)
                return {"success": True}
            return {"success": True}

        if path == "/sessions" and method == "GET":
            return {"data": [item for item in self.runtime_sessions.values() if item.get("user_id") == user_id]}
        if path == "/sessions" and method == "POST":
            payload = dict(json_body or {})
            self.runtime_sessions[payload["session_id"]] = payload
            self.runtime_session_creates.append(payload["session_id"])
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

    @asynccontextmanager
    async def stream(self, method: str, path: str, *, headers: dict, data=None):
        if not self.is_runtime:
            raise AssertionError(f"Unexpected fake stream: {method} {path}")
        self.runtime_stream_calls.append({"method": method, "path": path, "headers": headers, "data": data})
        if self.runtime_stream_error is not None:
            raise self.runtime_stream_error

        class Response:
            async def aiter_lines(self):
                if False:
                    yield ""

        yield Response()


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
    FakeServiceClient.graph_sessions = set()
    FakeServiceClient.runtime_runs = {}
    FakeServiceClient.graph_sources = {}
    FakeServiceClient.graph_registered_names = {}
    FakeServiceClient.graph_collection_sessions = set()
    FakeServiceClient.graph_visualize_payloads = {}
    FakeServiceClient.graph_load_failures = set()
    FakeServiceClient.graph_load_delay = 0.0
    FakeServiceClient.graph_active_loads = 0
    FakeServiceClient.graph_max_active_loads = 0
    FakeServiceClient.graph_deletes = []
    FakeServiceClient.graph_session_creates = []
    FakeServiceClient.runtime_session_creates = []
    FakeServiceClient.runtime_stream_calls = []
    FakeServiceClient.runtime_stream_error = None
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


def test_client_session_creation_is_idempotent_and_uses_first_prompt_title(client: TestClient):
    project = client.post("/api/projects", headers=headers(), json={"name": "Deferred"}).json()["project"]
    session_id = "2f8c47b8-22e7-4fb9-88a4-67a98db295b6"
    body = {
        "client_session_id": session_id,
        "project_id": project["project_id"],
        "model": "qwen3.6-plus",
        "initial_message_preview": "  分析一下这个图谱的社区结构  ",
    }

    first = client.post("/api/sessions", headers=headers(), json=body)
    second = client.post("/api/sessions", headers=headers(), json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["session_id"] == session_id
    assert first.json()["reused"] is False
    assert second.json()["reused"] is True
    assert first.json()["title"] == "分析一下这个图谱的社区结构"
    assert FakeServiceClient.runtime_session_creates.count(session_id) == 1
    assert FakeServiceClient.graph_session_creates.count(session_id) == 1
    assert FakeServiceClient.runtime_sessions[session_id]["metadata"]["client_session_id"] == session_id


def test_client_session_creation_rejects_stale_dataset_and_conflicting_id(client: TestClient):
    project = client.post("/api/projects", headers=headers(), json={"name": "Deferred"}).json()["project"]
    stale = client.post(
        "/api/sessions",
        headers=headers(),
        json={
            "client_session_id": "f79293eb-8bd3-49a7-b10e-bb9e4387e7c1",
            "project_id": project["project_id"],
            "active_dataset_id": "missing-dataset",
            "initial_message_preview": "test",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "DRAFT_DATASET_STALE"

    conflict_id = "051e70fd-346f-44ac-a2e4-9d606535e2d8"
    FakeServiceClient.runtime_sessions[conflict_id] = {
        "session_id": conflict_id,
        "session_name": "Existing",
        "session_state": {"project_id": project["project_id"], "model": "qwen3.6-plus"},
        "metadata": {"owner": "admin"},
        "user_id": "admin",
        "agent_id": "igraph-agent",
    }
    conflict = client.post(
        "/api/sessions",
        headers=headers(),
        json={"client_session_id": conflict_id, "project_id": project["project_id"], "initial_message_preview": "test"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "SESSION_CREATION_CONFLICT"


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


def test_graph_collection_capability_is_persisted_and_visualization_is_rejected(client: TestClient):
    project = client.post("/api/projects", headers=headers(), json={"name": "Molecule Research"}).json()["project"]
    session_id = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()["session_id"]

    uploaded = client.post(
        f"/api/projects/{project['project_id']}/datasets",
        headers=headers(),
        data={"session_id": session_id},
        files={"file": ("hiv.pt", b"synthetic-hiv", "application/octet-stream")},
    )
    assert uploaded.status_code == 200
    dataset_view = uploaded.json()["dataset_view"]
    assert dataset_view["kind"] == "graph_collection"
    assert dataset_view["visualization"] == {"supported": False, "code": "NON_SINGLE_GRAPH"}
    assert dataset_view["summary"]["num_graphs"] == 41127
    assert dataset_view["summary"]["split_sizes"] == {"train": 32901, "val": 4113, "test": 4113}

    listed = client.get(f"/api/projects/{project['project_id']}/datasets", headers=headers()).json()["datasets"]
    assert listed[0]["metadata"]["format_detected"] == "graph_classification_dataset"
    assert listed[0]["metadata"]["dataset_kind"] == "graph_collection"

    activated = client.post(f"/api/sessions/{session_id}/activate", headers=headers())
    assert activated.status_code == 200
    assert activated.json()["has_graph"] is False
    assert activated.json()["dataset_view"]["kind"] == "graph_collection"
    info = client.get(f"/api/sessions/{session_id}/info", headers=headers())
    assert info.status_code == 200
    assert info.json()["dataset_view"]["summary"]["metric"] == "roc_auc"

    visualized = client.post(
        f"/api/sessions/{session_id}/graph/visualize",
        headers=headers(),
        json={},
    )
    assert visualized.status_code == 409
    assert visualized.json()["detail"]["code"] == "GRAPH_NOT_VISUALIZABLE"
    assert visualized.json()["detail"]["message"] == "非独立图，无法直接可视化"


def test_single_graph_capability_and_visualization_response_validation(client: TestClient):
    project = client.post("/api/projects", headers=headers(), json={"name": "Single Graph"}).json()["project"]
    session_id = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()["session_id"]
    uploaded = client.post(
        f"/api/projects/{project['project_id']}/datasets",
        headers=headers(),
        data={"session_id": session_id},
        files={"file": ("cora.txt", b"A B\nB C\n", "text/plain")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["dataset_view"]["kind"] == "single_graph"

    FakeServiceClient.graph_visualize_payloads[session_id] = {"success": True, "edges": []}
    invalid = client.post(f"/api/sessions/{session_id}/graph/visualize", headers=headers(), json={})
    assert invalid.status_code == 502
    assert invalid.json()["detail"]["code"] == "INVALID_GRAPH_RESPONSE"

    FakeServiceClient.graph_visualize_payloads[session_id] = {"success": False, "error": "bad node filter"}
    failed = client.post(f"/api/sessions/{session_id}/graph/visualize", headers=headers(), json={})
    assert failed.status_code == 422
    assert failed.json()["detail"]["code"] == "GRAPH_VISUALIZATION_FAILED"


def test_failed_dataset_switch_rolls_back_selection_and_runtime(client: TestClient):
    project = client.post("/api/projects", headers=headers(), json={"name": "Rollback"}).json()["project"]
    session_id = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()["session_id"]
    cora = client.post(
        f"/api/projects/{project['project_id']}/datasets",
        headers=headers(),
        data={"session_id": session_id},
        files={"file": ("cora.txt", b"A B\n", "text/plain")},
    ).json()["dataset"]
    broken = client.post(
        f"/api/projects/{project['project_id']}/datasets",
        headers=headers(),
        files={"file": ("broken.txt", b"invalid", "text/plain")},
    ).json()["dataset"]
    FakeServiceClient.graph_load_failures.add("broken.txt")

    switched = client.patch(
        f"/api/sessions/{session_id}/dataset",
        headers=headers(),
        json={"active_dataset_id": broken["dataset_id"]},
    )
    assert switched.status_code == 422
    assert FakeServiceClient.runtime_sessions[session_id]["session_state"]["active_dataset_id"] == cora["dataset_id"]
    assert FakeServiceClient.graph_loaded[session_id] is True
    assert FakeServiceClient.graph_registered_names[session_id] == "cora.txt"


def test_active_run_blocks_dataset_switch_before_runtime_mutation(client: TestClient):
    project = client.post("/api/projects", headers=headers(), json={"name": "Busy Session"}).json()["project"]
    session_id = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()["session_id"]
    dataset = client.post(
        f"/api/projects/{project['project_id']}/datasets",
        headers=headers(),
        files={"file": ("cora.txt", b"A B\n", "text/plain")},
    ).json()["dataset"]
    FakeServiceClient.runtime_runs[session_id] = [{"run_id": "run-busy", "status": "RUNNING"}]
    deletes_before = list(FakeServiceClient.graph_deletes)

    response = client.patch(
        f"/api/sessions/{session_id}/dataset",
        headers=headers(),
        json={"active_dataset_id": dataset["dataset_id"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RUN_ACTIVE"
    assert FakeServiceClient.graph_deletes == deletes_before


def test_concurrent_dataset_switches_are_serialized_per_session(client: TestClient):
    project = client.post("/api/projects", headers=headers(), json={"name": "Serialized"}).json()["project"]
    session_id = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()["session_id"]
    datasets = [
        client.post(
            f"/api/projects/{project['project_id']}/datasets",
            headers=headers(),
            files={"file": (filename, b"A B\n", "text/plain")},
        ).json()["dataset"]
        for filename in ("first.txt", "second.txt")
    ]
    FakeServiceClient.graph_load_delay = 0.05

    def switch(dataset_id: str):
        return client.patch(
            f"/api/sessions/{session_id}/dataset",
            headers=headers(),
            json={"active_dataset_id": dataset_id},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(switch, [item["dataset_id"] for item in datasets]))

    assert [response.status_code for response in responses] == [200, 200]
    assert FakeServiceClient.graph_max_active_loads == 1
    assert FakeServiceClient.runtime_sessions[session_id]["session_state"]["active_dataset_id"] in {
        item["dataset_id"] for item in datasets
    }


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


def paused_run(run_id: str = "run-paused", call_id: str = "call-train") -> dict:
    return {
        "run_id": run_id,
        "status": "PAUSED",
        "created_at": "2026-08-31T02:00:00Z",
        "events": [{"event_index": 7}],
        "requirements": [{
            "description": "确认训练",
            "tool_execution": {
                "tool_call_id": call_id,
                "tool_name": "train",
                "tool_args": {"epochs": 2},
            },
        }],
    }


def test_paused_run_snapshot_blocks_both_chat_endpoints(client: TestClient):
    session_id = client.post("/api/sessions", headers=headers(), json={}).json()["session_id"]
    FakeServiceClient.runtime_runs[session_id] = [paused_run()]

    activated = client.post(f"/api/sessions/{session_id}/activate", headers=headers()).json()
    assert activated["active_run"]["run_id"] == "run-paused"
    assert activated["pending_feedback"] == {
        "run_id": "run-paused",
        "request_id": "run-paused",
        "call_id": "call-train",
        "tool_name": "train",
        "proposed_args": {"epochs": 2},
        "description": "确认训练",
    }

    for endpoint in ("chat", "chat/stream"):
        response = client.post(
            f"/api/sessions/{session_id}/{endpoint}",
            headers=headers(),
            json={"message": "must wait"},
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "RUN_AWAITING_FEEDBACK"
        assert detail["active_run"]["run_id"] == detail["pending_feedback"]["run_id"]
    assert FakeServiceClient.runtime_stream_calls == []


def test_feedback_is_validated_and_duplicate_continue_is_rejected(client: TestClient):
    session_id = client.post("/api/sessions", headers=headers(), json={}).json()["session_id"]
    FakeServiceClient.runtime_runs[session_id] = [paused_run()]
    body = {
        "run_id": "run-paused",
        "request_id": "run-paused",
        "decision_id": "decision-1",
        "decision": "approve",
        "tool_call_id": "call-train",
        "tool_name": "train",
        "patched_args": {"epochs": 999},
    }

    accepted = client.post(
        f"/api/sessions/{session_id}/feedback/stream",
        headers={**headers(), "X-iGraph-Stream-Version": "2"},
        json=body,
    )
    assert accepted.status_code == 200
    envelope = json.loads(accepted.text.removeprefix("data: ").strip())
    assert envelope["events"][0]["type"] == "feedback_accepted"
    assert "event_index" not in envelope
    assert len(FakeServiceClient.runtime_stream_calls) == 1
    forwarded_tools = json.loads(FakeServiceClient.runtime_stream_calls[0]["data"]["tools"])
    assert forwarded_tools[0]["tool_call_id"] == "call-train"
    assert forwarded_tools[0]["tool_args"] == {"epochs": 2}

    duplicate = client.post(
        f"/api/sessions/{session_id}/feedback/stream",
        headers=headers(),
        json={**body, "decision_id": "decision-2", "decision": "reject"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "FEEDBACK_ALREADY_ACCEPTED"
    assert len(FakeServiceClient.runtime_stream_calls) == 1


def test_stale_or_ambiguous_feedback_fails_closed_without_continue(client: TestClient):
    session_id = client.post("/api/sessions", headers=headers(), json={}).json()["session_id"]
    FakeServiceClient.runtime_runs[session_id] = [paused_run()]
    stale = client.post(
        f"/api/sessions/{session_id}/feedback/stream",
        headers=headers(),
        json={
            "run_id": "run-paused",
            "request_id": "run-paused",
            "decision_id": "stale",
            "decision": "approve",
            "tool_call_id": "old-call",
            "tool_name": "train",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "STALE_FEEDBACK"

    FakeServiceClient.runtime_runs[session_id] = [{
        **paused_run(),
        "requirements": [
            paused_run()["requirements"][0],
            {"tool_execution": {"tool_call_id": "call-clear", "tool_name": "clear_graph", "tool_args": {}}},
        ],
    }]
    multiple = client.post(
        f"/api/sessions/{session_id}/feedback/stream",
        headers=headers(),
        json={
            "run_id": "run-paused",
            "decision_id": "multiple",
            "decision": "approve",
            "tool_call_id": "call-train",
            "tool_name": "train",
        },
    )
    assert multiple.status_code == 409
    assert multiple.json()["detail"]["code"] == "MULTIPLE_FEEDBACK_UNSUPPORTED"
    assert FakeServiceClient.runtime_stream_calls == []


def test_upstream_feedback_rejection_releases_submission_guard(client: TestClient):
    session_id = client.post("/api/sessions", headers=headers(), json={}).json()["session_id"]
    FakeServiceClient.runtime_runs[session_id] = [paused_run()]
    FakeServiceClient.runtime_stream_error = HTTPException(status_code=503, detail="runtime unavailable")
    body = {
        "run_id": "run-paused",
        "decision_id": "retry-1",
        "decision": "approve",
        "tool_call_id": "call-train",
        "tool_name": "train",
    }
    first = client.post(f"/api/sessions/{session_id}/feedback/stream", headers=headers(), json=body)
    assert first.status_code == 200
    assert "feedback_submit_error" in first.text

    second = client.post(
        f"/api/sessions/{session_id}/feedback/stream",
        headers=headers(),
        json={**body, "decision_id": "retry-2"},
    )
    assert second.status_code == 200
    assert len(FakeServiceClient.runtime_stream_calls) == 2

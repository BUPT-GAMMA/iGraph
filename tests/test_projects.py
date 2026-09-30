from __future__ import annotations

import json
import io
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import backend.app as backend_app
from backend.authentication import AUTH_COOKIE
from backend.config import BackendConfig
from backend.projects import ProjectDirectory


_AUTH_TOKENS: dict[str, str] = {}
_TEST_PASSWORD = "TestPassword123!"


class FakeServiceClient:
    runtime_sessions: dict[str, dict] = {}
    runtime_runs: dict[str, list[dict]] = {}
    graph_loaded: dict[str, bool] = {}
    graph_sources: dict[str, str] = {}
    graph_ingests: dict[str, list[dict]] = {}
    graph_stream_calls: list[dict] = []
    graph_stream_closed: list[str] = []
    graph_deletes: list[str] = []
    runtime_stream_calls: list[dict] = []
    runtime_stream_error: HTTPException | None = None
    graph_job_status: dict[str, dict] = {}
    graph_job_results: dict[str, dict] = {}
    graph_load_results: dict[str, dict] = {}
    graph_status_calls: list[str] = []
    graph_result_calls: list[str] = []
    server_prepare_calls: list[dict] = []
    graph_server_attach_calls: list[dict] = []
    server_preparations: dict[str, dict] = {}
    custom_model_requests: list[dict] = []

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
        data=None,
        files=None,
        timeout=None,
        **_kwargs,
    ):
        user_id = headers.get("X-User-ID", "")
        if self.is_runtime and method == 'GET' and path.startswith('/internal/sessions/') and path.endswith('/remote-graph'):
            return {'remote_graph': None, 'remote_graph_unavailable_reason': None}
        if not self.is_runtime:
            if method == "POST" and path == "/server-datasets/prepare":
                request_body = dict(json_body or {})
                self.server_prepare_calls.append(request_body)
                server_dataset_id = str(request_body.get("server_dataset_id") or "")
                return dict(
                    self.server_preparations.get(
                        server_dataset_id,
                        {
                            "success": True,
                            "status": "ready",
                            "progress": 1.0,
                            "graph_version": 1,
                            "num_nodes": 10_000_000,
                            "num_edges": 100_000_000,
                            "partitions": 16,
                            "directed": True,
                            "storage_bytes": 1_234_567,
                        },
                    )
                )
            parts = path.strip("/").split("/")
            session_id = parts[1] if len(parts) > 1 and parts[0] == "sessions" else ""
            if method == "POST" and path.endswith("/server-datasets/attach"):
                request_body = dict(json_body or {})
                self.graph_server_attach_calls.append(
                    {"session_id": session_id, **request_body}
                )
                return {
                    "success": True,
                    "representation": "partitioned_snapshot",
                    "format_detected": "edgelist",
                    "graph_id": session_id,
                    "graph_version": 1,
                    "num_nodes": 10_000_000,
                    "num_edges": 100_000_000,
                    "summary": {
                        "directed": True,
                        "partitions": 16,
                        "storage_bytes": 1_234_567,
                    },
                }
            if method == "POST" and path.endswith("/tools/get_job_status"):
                job_id = str(((json_body or {}).get("arguments") or {}).get("job_id") or "")
                self.graph_status_calls.append(job_id)
                return self.graph_job_status.get(job_id, {"success": False, "job": None})
            if method == "POST" and path.endswith("/tools/get_job_result"):
                job_id = str(((json_body or {}).get("arguments") or {}).get("job_id") or "")
                self.graph_result_calls.append(job_id)
                return self.graph_job_results.get(
                    job_id, {"success": False, "result": None}
                )
            if method == "GET" and path.endswith("/graph/info"):
                loaded = self.graph_loaded.get(session_id, False)
                return {"loaded": loaded, "num_nodes": 3 if loaded else 0, "num_edges": 2 if loaded else 0}
            if method == "POST" and path.endswith("/uploads/register"):
                self.graph_sources[session_id] = str((json_body or {}).get("saved_path") or "")
                return {"success": True}
            if method == "POST" and path.endswith("/uploads/ingest"):
                upload = (files or {}).get("file")
                assert isinstance(upload, tuple) and len(upload) >= 2
                upload_content = (
                    upload[1]
                    if isinstance(upload[1], bytes)
                    else upload[1].read()
                )
                self.graph_ingests.setdefault(session_id, []).append({
                    "data": dict(data or {}),
                    "filename": str(upload[0]),
                    # Read while Backend still owns the open source handle.  A
                    # fake that retains the handle would hide close/lifetime bugs.
                    "content": upload_content,
                    "content_type": str(upload[2]) if len(upload) > 2 else "",
                    "timeout": timeout,
                })
                return {"success": True}
            if method == "POST" and path.endswith("/graph/load_upload"):
                custom = self.graph_load_results.get(session_id)
                if custom is not None:
                    self.graph_loaded[session_id] = not bool(custom.get("async"))
                    return dict(custom)
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
        if path.startswith('/internal/sessions/') and path.endswith('/recovery-status'):
            parts = path.split('/')
            run = next((run for run in self.runtime_runs.get(parts[3], []) if run['run_id'] == parts[5]), None)
            if run is None:
                raise HTTPException(404, 'Run not found')
            return {'run_id': run['run_id'], 'status': str(run['status']).lower()}
        if path == "/internal/models/custom" and method == "PUT":
            self.custom_model_requests.append({"method": method, "path": path, "user_id": user_id, "body": json_body})
            return {"success": True, "model_id": str((json_body or {}).get("model_id") or "")}
        if path.startswith("/internal/models/custom/") and method == "DELETE":
            self.custom_model_requests.append({"method": method, "path": path, "user_id": user_id})
            return {"success": True}
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

    async def open_stream(self, method: str, path: str, *, headers: dict, params=None):
        if self.is_runtime:
            raise AssertionError(f"Unexpected runtime stream: {method} {path}")
        self.graph_stream_calls.append({
            "method": method,
            "path": path,
            "headers": dict(headers),
            "params": params,
        })

        owner = self

        class UpstreamClient:
            async def aclose(self):
                owner.graph_stream_closed.append("client")

        class UpstreamResponse:
            headers = {
                "content-type": "application/x-ndjson; charset=utf-8",
                "content-disposition": 'attachment; filename="part-00000.jsonl"',
            }

            async def aiter_bytes(self):
                yield b'{"node":"A"}\n'
                yield b'{"node":"B"}\n'

            async def aclose(self):
                owner.graph_stream_closed.append("response")

        return UpstreamClient(), UpstreamResponse()

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
    FakeServiceClient.runtime_runs = {}
    FakeServiceClient.graph_sources = {}
    FakeServiceClient.graph_ingests = {}
    FakeServiceClient.graph_stream_calls = []
    FakeServiceClient.graph_stream_closed = []
    FakeServiceClient.graph_deletes = []
    FakeServiceClient.runtime_stream_calls = []
    FakeServiceClient.runtime_stream_error = None
    FakeServiceClient.graph_job_status = {}
    FakeServiceClient.graph_job_results = {}
    FakeServiceClient.graph_load_results = {}
    FakeServiceClient.graph_status_calls = []
    FakeServiceClient.graph_result_calls = []
    FakeServiceClient.server_prepare_calls = []
    FakeServiceClient.graph_server_attach_calls = []
    FakeServiceClient.server_preparations = {}
    FakeServiceClient.custom_model_requests = []
    seed = tmp_path / "supported.txt"
    seed.write_text("admin\nalice\n", encoding="utf-8")
    monkeypatch.setenv(
        "BACKEND_SERVER_DATASETS_DIR", str(tmp_path / "server_datasets")
    )
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
        custom_models_file=tmp_path / "custom_models.json",
    )
    monkeypatch.setenv("IGRAPH_CONTROL_SERVICE_KEY", "test-project-key")
    monkeypatch.setattr(backend_app, "ServiceClient", FakeServiceClient)
    _AUTH_TOKENS.clear()
    app = backend_app.create_app(config)
    for user_id in ("admin", "alice"):
        assert app.state.auth.bootstrap(user_id, _TEST_PASSWORD)
    with TestClient(app) as test_client:
        try:
            for user_id in ("admin", "alice"):
                response = test_client.post(
                    "/api/auth/login",
                    json={"username": user_id, "password": _TEST_PASSWORD},
                )
                assert response.status_code == 200, response.text
                _AUTH_TOKENS[user_id] = response.cookies[AUTH_COOKIE]
                # Requests without an explicit Cookie must remain unauthenticated.
                # Also avoid revoking the previous account's token on the next login.
                test_client.cookies.clear()
            yield test_client
        finally:
            test_client.cookies.clear()
            _AUTH_TOKENS.clear()


def headers(user_id: str = "admin") -> dict[str, str]:
    token = _AUTH_TOKENS.get(user_id, "invalid-session")
    return {"Cookie": f"{AUTH_COOKIE}={token}"}


def test_project_directory_reads_do_not_create_general(tmp_path: Path):
    directory = ProjectDirectory(tmp_path / "projects.json")
    assert directory.list("admin") == []
    assert directory.list("alice") == []
    assert directory.get(directory.default_project_id("admin"), "admin") is None
    assert not directory.path.exists()
    ordinary = directory.create("admin", "General")
    assert not ordinary.is_default
    assert directory.delete(ordinary.project_id, "admin")


def test_custom_model_api_validates_isolates_syncs_updates_and_deletes(client: TestClient):
    invalid = client.post(
        "/api/models/custom",
        headers=headers(),
        json={"model_id": "team/model-v1", "display_name": "Team Model", "base_url": "ftp://models.example", "api_key": "secret"},
    )
    assert invalid.status_code == 400
    assert "Base URL" in invalid.json()["detail"]

    created = client.post(
        "/api/models/custom",
        headers=headers(),
        json={"model_id": "team/model-v1", "display_name": "Team Model", "base_url": "https://models.example/v1/", "api_key": "secret"},
    )
    assert created.status_code == 200
    assert created.json()["model"] == {
        **{key: value for key, value in created.json()["model"].items() if key in {"created_at", "updated_at"}},
        "model_id": "team/model-v1",
        "display_name": "Team Model",
        "provider": "openai-compatible",
        "base_url": "https://models.example/v1",
        "has_api_key": True,
    }
    assert "api_key" not in created.json()["model"]
    assert FakeServiceClient.custom_model_requests[-1]["body"]["api_key"] == "secret"

    listed = client.get("/api/models/custom", headers=headers()).json()["models"]
    assert [item["model_id"] for item in listed] == ["team/model-v1"]
    assert client.get("/api/models/custom", headers=headers("alice")).json()["models"] == []
    assert "team/model-v1" in client.post("/api/auth/validate", headers=headers()).json()["available_models"]

    updated = client.post(
        "/api/models/custom",
        headers=headers(),
        json={"model_id": "team/model-v1", "display_name": "Renamed", "base_url": "https://new.example/v1", "api_key": ""},
    )
    assert updated.status_code == 200
    assert updated.json()["model"]["display_name"] == "Renamed"
    assert FakeServiceClient.custom_model_requests[-1]["body"]["api_key"] == "secret"

    missing = client.delete("/api/models/custom/missing", headers=headers())
    assert missing.status_code == 404
    deleted = client.delete("/api/models/custom/team/model-v1", headers=headers())
    assert deleted.status_code == 200
    assert deleted.json() == {"success": True, "model_id": "team/model-v1"}
    assert FakeServiceClient.custom_model_requests[-1]["method"] == "DELETE"
    assert FakeServiceClient.custom_model_requests[-1]["path"].endswith("/admin/team/model-v1")
    assert client.get("/api/models/custom", headers=headers()).json()["models"] == []


def test_project_crud_and_explicit_delete_contract(client: TestClient):
    assert client.get('/api/projects', headers=headers()).json()['projects'] == []
    independent = client.post('/api/sessions', headers=headers(), json={}).json()
    assert independent['project_id'] is None
    project = client.post('/api/projects', headers=headers(), json={'name': 'Research'}).json()['project']
    pid = project['project_id']
    renamed = client.patch(f'/api/projects/{pid}', headers=headers(), json={'name': 'GNN'}).json()['project']
    assert renamed['name'] == 'GNN'
    sid = client.post('/api/sessions', headers=headers(), json={'project_id': pid}).json()['session_id']
    assert [row['session_id'] for row in client.get(f'/api/projects/{pid}/conversations', headers=headers()).json()['conversations']] == [sid]
    stale = client.request('DELETE', f'/api/projects/{pid}', headers=headers(), json={'conversation_action': 'move_to_general'})
    assert stale.status_code == 409
    assert stale.json()['detail']['code'] == 'PROJECT_DELETE_ACTION_CHANGED'
    assert client.delete(f'/api/projects/{pid}', headers=headers()).status_code == 422
    assert sid in FakeServiceClient.runtime_sessions
    assert client.request('DELETE', f'/api/projects/{pid}', headers=headers(), json={'conversation_action': 'delete'}).status_code == 200
    assert sid not in FakeServiceClient.runtime_sessions
    assert independent['session_id'] in FakeServiceClient.runtime_sessions
    assert client.get('/api/projects', headers=headers()).json()['projects'] == []


def test_project_training_runs_aggregate_sessions_and_persist_job_status(client: TestClient):
    project = client.post("/api/projects", headers=headers(), json={"name": "Model comparison"}).json()["project"]
    first = client.post("/api/sessions", headers=headers(), json={"project_id": project["project_id"]}).json()["session_id"]
    second = client.post("/api/sessions", headers=headers(), json={"project_id": project["project_id"]}).json()["session_id"]
    FakeServiceClient.runtime_sessions[first]["session_name"] = "GCN experiment"
    FakeServiceClient.runtime_sessions[second]["session_name"] = "GAT experiment"
    FakeServiceClient.runtime_runs[first] = [{
        "run_id": "run-gcn",
        "status": "COMPLETED",
        "tools": [{
            "tool_name": "train",
            "tool_call_id": "call-gcn",
            "tool_args": {"task": "node_classification", "model": "GCN", "train_config": {"epochs": 200}},
            "result": json.dumps({"success": True, "async": True, "job_id": "job-gcn", "status": "queued"}),
        }],
    }]
    FakeServiceClient.runtime_runs[second] = [{
        "run_id": "run-gat",
        "status": "COMPLETED",
        "tools": [{
            "tool_name": "train",
            "tool_call_id": "call-gat",
            "tool_args": {"task": "node_classification", "model": "GAT"},
            "result": json.dumps({"success": True, "model_id": "model-gat", "metrics": {"val_acc": 0.81, "trained_epochs": 40}}),
        }],
    }]
    FakeServiceClient.graph_job_status["job-gcn"] = {
        "success": True,
        "job": {
            "job_id": "job-gcn",
            "tool_name": "train",
            "status": "done",
            "progress": 1,
            "detail": {"kind": "train_epoch", "epoch": 200, "epochs": 200, "val_acc": 0.773},
            "result": {"success": True, "model_id": "model-gcn", "metrics": {"val_acc": 0.773, "best_epoch": 1, "trained_epochs": 200}},
        },
        "progress_updates": [{"kind": "train_epoch", "epoch": 9, "epochs": 200, "val_acc": 0.773}],
    }

    response = client.get(f"/api/projects/{project['project_id']}/training-runs", headers=headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["has_running"] is False
    assert {item["session_title"] for item in payload["training_runs"]} == {"GCN experiment", "GAT experiment"}
    gcn = next(item for item in payload["training_runs"] if item["training_run_id"] == "job-gcn")
    assert gcn["done"] is True
    assert gcn["result"]["model_id"] == "model-gcn"
    assert [row["epoch"] for row in gcn["history"]] == [9, 200]
    assert FakeServiceClient.runtime_sessions[first]["session_state"]["training_job_snapshots"]["job-gcn"]["job"]["status"] == "done"

    again = client.get(f"/api/projects/{project['project_id']}/training-runs", headers=headers())
    assert again.status_code == 200
    assert FakeServiceClient.graph_status_calls == ["job-gcn"]


def test_project_training_runs_enforce_project_ownership(client: TestClient):
    project = client.post("/api/projects", headers=headers(), json={"name": "Private experiments"}).json()["project"]
    response = client.get(f"/api/projects/{project['project_id']}/training-runs", headers=headers("alice"))
    assert response.status_code == 404


def test_new_session_chat_receives_cross_session_training_summary(client: TestClient):
    project = client.post("/api/projects", headers=headers(), json={"name": "Shared results"}).json()["project"]
    trained = client.post("/api/sessions", headers=headers(), json={"project_id": project["project_id"]}).json()["session_id"]
    comparison = client.post("/api/sessions", headers=headers(), json={"project_id": project["project_id"]}).json()["session_id"]
    FakeServiceClient.runtime_sessions[trained]["session_name"] = "GCN baseline"
    FakeServiceClient.runtime_runs[trained] = [{
        "run_id": "run-trained",
        "status": "COMPLETED",
        "tools": [{
            "tool_name": "train",
            "tool_call_id": "call-trained",
            "tool_args": {"task": "node_classification", "model": "GCN", "train_config": {"epochs": 20}},
            "result": json.dumps({"success": True, "model_id": "model-gcn", "metrics": {"val_acc": 0.81, "trained_epochs": 20}}),
        }],
    }]

    response = client.post(
        f"/api/sessions/{comparison}/chat/stream",
        headers=headers(),
        json={"message": "比较项目中的模型"},
    )
    assert response.status_code == 200
    summary = FakeServiceClient.runtime_sessions[comparison]["session_state"]["project_training_summary"]
    assert summary == [{
        "training_run_id": "call-trained",
        "session_id": trained,
        "session_title": "GCN baseline",
        "dataset_id": None,
        "dataset_name": None,
        "task": "node_classification",
        "model": "GCN",
        "model_id": "model-gcn",
        "train_config": {"epochs": 20},
        "algo_config": None,
        "status": "done",
        "metrics": {"val_acc": 0.81, "trained_epochs": 20},
    }]


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

    assert client.get('/api/projects', headers=headers()).json()['projects'] == []


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
        files={"file": ("cora.edgelist", b"A B\nB C\n", "text/plain")},
    )
    assert uploaded.status_code == 200
    dataset = uploaded.json()["dataset"]
    assert uploaded.json()["default_dataset_id"] == dataset["dataset_id"]
    assert FakeServiceClient.runtime_sessions[session_a]["session_state"]["active_dataset_id"] == dataset["dataset_id"]
    assert FakeServiceClient.graph_loaded[session_a] is True

    sources = list((tmp_path / "projects" / project["project_id"] / "datasets").rglob("cora.edgelist"))
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
    ingest_a = FakeServiceClient.graph_ingests[session_a]
    ingest_b = FakeServiceClient.graph_ingests[session_b]
    assert len(ingest_a) == len(ingest_b) == 1
    assert ingest_a[0]["content"] == ingest_b[0]["content"] == b"A B\nB C\n"
    assert ingest_a[0]["filename"] == ingest_b[0]["filename"] == "cora.edgelist"
    assert ingest_a[0]["content_type"] == ingest_b[0]["content_type"] == "application/octet-stream"
    assert ingest_a[0]["data"] == ingest_b[0]["data"] == {
        "file_token": f"project-{dataset['dataset_id']}",
        "original_filename": "cora.edgelist",
    }
    assert isinstance(ingest_a[0]["timeout"], httpx.Timeout)
    assert isinstance(ingest_b[0]["timeout"], httpx.Timeout)

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


def test_server_dataset_is_listed_by_opaque_id_and_streamed_only_when_used(
    client: TestClient, tmp_path: Path
) -> None:
    server_root = tmp_path / "server_datasets"
    nested = server_root / "public"
    nested.mkdir(parents=True)
    source = nested / "large-graph.edgelist"
    source.write_bytes(b"A B\nB C\n")
    (server_root / "ignore.exe").write_bytes(b"not a graph")
    (server_root / ".hidden.csv").write_bytes(b"A,B\n")
    symlink = server_root / "outside.csv"
    try:
        symlink.symlink_to(tmp_path / "supported.txt")
    except OSError:
        pass

    listed = client.get("/api/server-datasets", headers=headers())

    assert listed.status_code == 200
    assert len(listed.json()["datasets"]) == 1
    server_dataset = listed.json()["datasets"][0]
    assert server_dataset["server_dataset_id"].startswith("server-")
    assert server_dataset["relative_path"] == "public/large-graph.edgelist"
    assert server_dataset["size_bytes"] == source.stat().st_size
    assert "storage_path" not in server_dataset
    assert FakeServiceClient.graph_ingests == {}

    project = client.post(
        "/api/projects", headers=headers(), json={"name": "Server graphs"}
    ).json()["project"]
    imported = client.post(
        f"/api/projects/{project['project_id']}/datasets/from-server",
        headers=headers(),
        json={"server_dataset_id": server_dataset["server_dataset_id"]},
    )

    assert imported.status_code == 200
    dataset = imported.json()["dataset"]
    assert imported.json()["created"] is True
    assert dataset["source_kind"] == "server"
    assert dataset["source_id"] == server_dataset["server_dataset_id"]
    assert imported.json()["default_dataset_id"] == dataset["dataset_id"]
    assert FakeServiceClient.graph_ingests == {}

    duplicate = client.post(
        f"/api/projects/{project['project_id']}/datasets/from-server",
        headers=headers(),
        json={"server_dataset_id": server_dataset["server_dataset_id"]},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["created"] is False
    assert duplicate.json()["dataset"]["dataset_id"] == dataset["dataset_id"]

    session_id = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()["session_id"]
    activated = client.post(
        f"/api/sessions/{session_id}/activate", headers=headers()
    )

    assert activated.status_code == 200
    assert activated.json()["has_graph"] is True
    assert FakeServiceClient.graph_ingests[session_id][0]["content"] == source.read_bytes()
    assert FakeServiceClient.graph_ingests[session_id][0]["data"] == {
        "file_token": f"project-{dataset['dataset_id']}",
        "original_filename": "large-graph.edgelist",
    }

    deleted = client.delete(
        f"/api/projects/{project['project_id']}/datasets/{dataset['dataset_id']}",
        headers=headers(),
    )
    assert deleted.status_code == 200
    assert source.read_bytes() == b"A B\nB C\n"


def test_large_server_dataset_is_prepared_once_and_attached_without_streaming(
    client: TestClient, tmp_path: Path
) -> None:
    server_root = tmp_path / "server_datasets"
    server_root.mkdir()
    source = server_root / "huge.edgelist"
    with source.open("wb") as output:
        output.truncate(64 * 1024 * 1024)

    listed = client.get("/api/server-datasets", headers=headers())
    assert listed.status_code == 200
    server_dataset = listed.json()["datasets"][0]
    assert len(server_dataset["revision"]) == 64
    assert server_dataset["preparation"]["status"] == "ready"
    assert FakeServiceClient.server_prepare_calls == [
        {
            "server_dataset_id": server_dataset["server_dataset_id"],
            "revision": server_dataset["revision"],
            "relative_path": "huge.edgelist",
            "original_filename": "huge.edgelist",
            "source_path": str(source),
            "directed": True,
        }
    ]

    project = client.post(
        "/api/projects", headers=headers(), json={"name": "Prepared server graph"}
    ).json()["project"]
    session_id = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()["session_id"]
    imported = client.post(
        f"/api/projects/{project['project_id']}/datasets/from-server",
        headers=headers(),
        json={
            "server_dataset_id": server_dataset["server_dataset_id"],
            "session_id": session_id,
        },
    )

    assert imported.status_code == 200
    payload = imported.json()
    assert payload["dataset"]["metadata"]["source_revision"] == server_dataset["revision"]
    assert payload["dataset_view"]["kind"] == "partitioned_graph"
    assert FakeServiceClient.graph_ingests == {}
    assert FakeServiceClient.graph_server_attach_calls == [
        {
            "session_id": session_id,
            "server_dataset_id": server_dataset["server_dataset_id"],
            "revision": server_dataset["revision"],
        }
    ]


def test_server_dataset_import_rejects_unknown_id_and_wrong_project_session(
    client: TestClient, tmp_path: Path
) -> None:
    server_root = tmp_path / "server_datasets"
    server_root.mkdir()
    (server_root / "graph.csv").write_text("A,B\n", encoding="utf-8")
    server_dataset_id = client.get(
        "/api/server-datasets", headers=headers()
    ).json()["datasets"][0]["server_dataset_id"]
    first = client.post(
        "/api/projects", headers=headers(), json={"name": "First"}
    ).json()["project"]
    second = client.post(
        "/api/projects", headers=headers(), json={"name": "Second"}
    ).json()["project"]
    second_session = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": second["project_id"]},
    ).json()["session_id"]

    missing = client.post(
        f"/api/projects/{first['project_id']}/datasets/from-server",
        headers=headers(),
        json={"server_dataset_id": "server-missing"},
    )
    wrong_session = client.post(
        f"/api/projects/{first['project_id']}/datasets/from-server",
        headers=headers(),
        json={
            "server_dataset_id": server_dataset_id,
            "session_id": second_session,
        },
    )

    assert missing.status_code == 404
    assert wrong_session.status_code == 400
    assert FakeServiceClient.graph_ingests == {}


def test_project_large_dataset_persists_pending_job_then_fetches_bounded_result(
    client: TestClient,
) -> None:
    project = client.post(
        "/api/projects", headers=headers(), json={"name": "Large graph"}
    ).json()["project"]
    session_id = client.post(
        "/api/sessions",
        headers=headers(),
        json={"project_id": project["project_id"]},
    ).json()["session_id"]
    FakeServiceClient.graph_load_results[session_id] = {
        "success": True,
        "async": True,
        "job_id": "lg_prepare_project",
        "graph_id": session_id,
        "graph_version": 4,
        "status": "queued",
        "format_detected": "edgelist",
    }
    FakeServiceClient.graph_job_status["lg_prepare_project"] = {
        "success": True,
        "job": {
            "job_id": "lg_prepare_project",
            "tool_name": "prepare_large_graph",
            "status": "done",
            "progress": 1,
        },
        "progress_updates": [],
    }
    FakeServiceClient.graph_job_results["lg_prepare_project"] = {
        "success": True,
        "result": {
            "job_id": "lg_prepare_project",
            "status": "done",
            "representation": "partitioned_snapshot",
            "graph_id": session_id,
            "graph_version": 4,
            "num_nodes": 1_250_000,
            "num_edges": 7_500_000,
            "summary": {"directed": True},
            "current_graph_replaced": False,
        },
    }

    uploaded = client.post(
        f"/api/projects/{project['project_id']}/datasets",
        headers=headers(),
        data={"session_id": session_id},
        files={"file": ("large.edgelist", b"A B\n", "text/plain")},
    )

    assert uploaded.status_code == 200
    pending_payload = uploaded.json()
    metadata = pending_payload["dataset"]["metadata"]
    assert metadata["dataset_kind"] == "partitioned_graph"
    assert metadata["representation"] == "partitioned_snapshot"
    assert metadata["graph_version"] == 4
    assert metadata["preparation_job_id"] == "lg_prepare_project"
    assert metadata["preparation_status"] == "queued"
    assert pending_payload["dataset_view"]["summary"]["preparation_status"] == "queued"
    assert FakeServiceClient.graph_status_calls == []
    assert FakeServiceClient.graph_result_calls == []

    # Simulate a Backend crash between the project metadata write and the
    # session-state write. Activation must recover and poll the same job.
    FakeServiceClient.runtime_sessions[session_id]["session_state"].pop(
        "large_graph_prepare"
    )
    activated = client.post(
        f"/api/sessions/{session_id}/activate", headers=headers()
    )

    assert activated.status_code == 200
    assert activated.json()["has_graph"] is True
    datasets = client.get(
        f"/api/projects/{project['project_id']}/datasets", headers=headers()
    ).json()["datasets"]
    metadata = next(
        item["metadata"]
        for item in datasets
        if item["dataset_id"] == pending_payload["dataset"]["dataset_id"]
    )
    assert metadata["num_nodes"] == 1_250_000
    assert metadata["num_edges"] == 7_500_000
    assert metadata["directed"] is True
    assert "preparation_job_id" not in metadata
    assert "preparation_status" not in metadata
    assert FakeServiceClient.graph_loaded[session_id] is False
    assert FakeServiceClient.graph_status_calls == ["lg_prepare_project"]
    assert FakeServiceClient.graph_result_calls == ["lg_prepare_project"]


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
        files={"file": ("graph.edgelist", b"A B\nB C\n", "text/plain")},
    ).json()["dataset"]
    source = next((tmp_path / "projects" / project["project_id"] / "datasets").rglob("graph.edgelist"))

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


def test_large_graph_artifact_download_is_streamed_through_authenticated_proxy(client: TestClient):
    session_id = client.post("/api/sessions", headers=headers(), json={}).json()["session_id"]

    response = client.get(
        f"/api/sessions/{session_id}/large-graph/artifacts/artifact-1/partitions/0",
        headers=headers(),
    )

    assert response.status_code == 200
    assert response.content == b'{"node":"A"}\n{"node":"B"}\n'
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["content-disposition"] == 'attachment; filename="part-00000.jsonl"'
    assert FakeServiceClient.graph_stream_calls == [{
        "method": "GET",
        "path": (
            f"/sessions/{session_id}/large-graph/artifacts/"
            "artifact-1/partitions/0"
        ),
        "headers": {"X-User-ID": "admin"},
        "params": None,
    }]
    assert FakeServiceClient.graph_stream_closed == ["response", "client"]


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
    previous = {'tool_call_id': 'earlier', 'tool_name': 'create_analysis_report', 'result': {'report': {'id': 'saved-report'}}}
    pending = {**paused_run()['requirements'][0]['tool_execution'], 'approval_id': 'original-approval'}
    FakeServiceClient.runtime_runs[session_id] = [{**paused_run(), 'tools': [pending, previous]}]
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
    assert forwarded_tools[0]["requires_confirmation"] is True
    assert forwarded_tools[0]["approval_id"] == 'original-approval'
    assert forwarded_tools[1] == previous

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
    assert first.status_code == 503
    assert first.json()["detail"] == "runtime unavailable"

    second = client.post(
        f"/api/sessions/{session_id}/feedback/stream",
        headers=headers(),
        json={**body, "decision_id": "retry-2"},
    )
    assert second.status_code == 503
    assert len(FakeServiceClient.runtime_stream_calls) == 2


def test_recovery_snapshot_is_complete_only_at_a_persisted_phase(client: TestClient):
    sid = client.post('/api/sessions', headers=headers(), json={}).json()['session_id']
    record = {'run_id': 'r', 'status': 'RUNNING', 'content': 'AB', 'last_event_index': 100}
    FakeServiceClient.runtime_runs[sid] = [record]
    running = client.get(f'/api/sessions/{sid}/runs/r/snapshot', headers=headers()).json()
    assert running['snapshot_complete'] is False and running['resume_cursor'] is None
    assert running['active_run']['run_id'] == 'r'
    record.update(status='COMPLETED', content='ABC')
    FakeServiceClient.runtime_runs[sid].append({'run_id': 'new', 'status': 'RUNNING', 'created_at': 1000})
    complete = client.get(f'/api/sessions/{sid}/runs/r/snapshot', headers=headers()).json()
    assert complete['snapshot_complete'] is True and complete['resume_cursor'] is None
    assert complete['active_run']['run_id'] == 'new'
    assert next(message for message in complete['messages'] if message['id'] == 'assistant:r')['content'] == 'ABC'
    assert client.get(f'/api/sessions/{sid}/runs/wrong/snapshot', headers=headers()).status_code == 404
    assert client.get(f'/api/sessions/{sid}/runs/r/snapshot', headers=headers('other-user')).status_code in {401, 403, 404}


def test_resume_propagates_structured_error_before_sse_headers(client: TestClient):
    sid = client.post('/api/sessions', headers=headers(), json={}).json()['session_id']
    FakeServiceClient.runtime_stream_error = HTTPException(409, {'code': 'REPLAY_UNAVAILABLE', 'reason': 'window_trimmed'})
    result = client.post(f'/api/sessions/{sid}/runs/r/resume?last_event_index=0', headers=headers())
    assert result.status_code == 409
    assert result.headers['content-type'] == 'application/json'
    assert result.json()['detail']['code'] == 'REPLAY_UNAVAILABLE'
    assert FakeServiceClient.runtime_stream_calls[-1]['path'] == f'/internal/sessions/{sid}/runs/r/resume'


def test_feedback_continues_in_background_and_keeps_acceptance_guard(client: TestClient):
    sid = client.post('/api/sessions', headers=headers(), json={}).json()['session_id']
    FakeServiceClient.runtime_runs[sid] = [paused_run()]
    body = {'run_id': 'run-paused', 'decision_id': 'd', 'decision': 'approve', 'tool_call_id': 'call-train', 'tool_name': 'train'}
    assert client.post(f'/api/sessions/{sid}/feedback/stream', headers=headers(), json=body).status_code == 200
    assert FakeServiceClient.runtime_stream_calls[-1]['data']['background'] == 'true'
    assert client.post(f'/api/sessions/{sid}/feedback/stream', headers=headers(), json=body).status_code == 409
    assert len(FakeServiceClient.runtime_stream_calls) == 1


def test_personal_datasets_are_reusable_explicit_and_scope_isolated(client):
    first = client.post('/api/sessions', headers=headers(), json={}).json()
    assert first['project_id'] is None and first['active_dataset_id'] is None
    uploaded = client.post('/api/personal-datasets', headers=headers(),
        files={'file': ('edges.edgelist', b'A B\nB C', 'text/plain')})
    assert uploaded.status_code == 200, uploaded.text
    dataset = uploaded.json()['dataset']
    assert dataset['project_id'] is None and not dataset['is_default']
    second = client.post('/api/sessions', headers=headers(), json={}).json()
    assert second['active_dataset_id'] is None
    selected = client.post('/api/sessions', headers=headers(), json={'active_dataset_id': dataset['dataset_id']}).json()
    assert selected['active_dataset_id'] == dataset['dataset_id']
    assert [row['dataset_id'] for row in client.get('/api/personal-datasets', headers=headers()).json()['datasets']] == [dataset['dataset_id']]
    assert client.get('/api/personal-datasets', headers=headers('alice')).json()['datasets'] == []
    project = client.post('/api/projects', headers=headers(), json={'name': 'Scoped'}).json()['project']
    assert client.post('/api/sessions', headers=headers(), json={'project_id': project['project_id'], 'active_dataset_id': dataset['dataset_id']}).status_code == 409
    foreign = client.post(f"/api/projects/{project['project_id']}/datasets", headers=headers(), files={'file': ('other.csv', b'X,Y', 'text/csv')}).json()['dataset']
    assert client.patch('/api/personal-datasets/' + foreign['dataset_id'], headers=headers(), json={'name': 'wrong'}).status_code == 404
    assert client.delete('/api/personal-datasets/' + foreign['dataset_id'], headers=headers()).status_code == 404
    # A query parameter cannot widen the personal route's ownership scope.
    assert client.get('/api/personal-datasets', headers=headers(), params={'project_id': project['project_id']}).json()['datasets'][0]['dataset_id'] == dataset['dataset_id']
    assert client.patch('/api/personal-datasets/' + dataset['dataset_id'], headers=headers(), json={'name': 'Renamed'}).json()['dataset']['name'] == 'Renamed'
    assert client.delete('/api/personal-datasets/' + dataset['dataset_id'], headers=headers()).status_code == 200
    assert FakeServiceClient.runtime_sessions[selected['session_id']]['session_state']['active_dataset_id'] is None


def test_personal_multi_csv_upload_retains_one_pk_fk_bundle(client):
    schema = {"version": "csv_bundle.v1", "tables": [
        {"file_name": "customers.csv", "name": "customers", "columns": ["id", "name"],
         "primary_key": ["id"], "foreign_keys": []},
        {"file_name": "orders.csv", "name": "orders", "columns": ["id", "customer_id"],
         "primary_key": ["id"], "foreign_keys": [
             {"columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["id"]}]},
    ]}
    response = client.post('/api/personal-datasets', headers=headers(), data={
        'name': 'Retail CSV', 'csv_schema': json.dumps(schema),
    }, files=[
        ('files', ('customers.csv', io.BytesIO(b'id,name\nc1,Alice\n'), 'text/csv')),
        ('files', ('orders.csv', io.BytesIO(b'id,customer_id\no1,c1\n'), 'text/csv')),
    ])
    assert response.status_code == 200, response.text
    dataset = response.json()['dataset']
    assert dataset['file_type'] == 'csv' and dataset['status'] == 'source_ready'
    assert dataset['metadata']['csv_bundle']['source_files'] == ['customers.csv', 'orders.csv']
    assert dataset['metadata']['csv_bundle']['tables'][1]['foreign_keys'][0]['ref_table'] == 'customers'


def test_general_migration_preserves_sources_state_and_named_user_projects(client, tmp_path):
    from backend.projects import ProjectRecord
    from backend.project_datasets import ProjectDatasetDirectory
    directory = ProjectDirectory(tmp_path / 'projects.json')
    general_id = directory.default_project_id('admin')
    directory._write({general_id: ProjectRecord(general_id, 'admin', 'General', is_default=True)})
    named = directory.create('admin', 'General')
    datasets = ProjectDatasetDirectory(tmp_path / 'project_datasets.json', tmp_path / 'projects')
    dataset = datasets.create(project_id=general_id, user_id='admin', name='Shared', original_filename='edges.csv', content=b'A,B')
    state = {'project_id': general_id, 'active_dataset_id': dataset.dataset_id, 'last_message_preview': 'keep', 'graph_version': 9}
    FakeServiceClient.runtime_sessions['legacy-session']['session_state'] = state.copy()
    first = client.get('/api/projects', headers=headers())
    assert first.status_code == 200, first.text
    assert first.json()['legacy_default_project_id'] == general_id
    assert [row['project_id'] for row in first.json()['projects']] == [named.project_id]
    assert FakeServiceClient.runtime_sessions['legacy-session']['session_state'] == {**state, 'project_id': None}
    migrated = datasets.get(dataset.dataset_id, 'admin', None)
    assert migrated.storage_path == dataset.storage_path
    assert Path(migrated.storage_path).read_bytes() == b'A,B'
    assert datasets.get(dataset.dataset_id, 'admin', general_id) is None
    backups = list((tmp_path / 'migrations').rglob('session-*.json'))
    assert len(backups) == 1 and json.loads(backups[0].read_text())['session_state'] == state
    assert client.get('/api/projects', headers=headers()).json() == first.json()
    assert Path(dataset.storage_path).exists()


def test_standalone_chat_context_excludes_other_conversations(client):
    first = client.post('/api/sessions', headers=headers(), json={}).json()['session_id']
    second = client.post('/api/sessions', headers=headers(), json={}).json()['session_id']
    FakeServiceClient.runtime_runs[first] = [{
        'run_id': 'private-training', 'status': 'COMPLETED',
        'tools': [{'tool_name': 'train', 'tool_call_id': 'private-model', 'tool_args': {'task': 'node_classification'},
                   'result': json.dumps({'success': True, 'model_id': 'private-model', 'metrics': {'val_acc': .9}})}],
    }]
    response = client.post(f'/api/sessions/{second}/chat/stream', headers=headers(), json={'message': '你好'})
    assert response.status_code == 200
    assert FakeServiceClient.runtime_sessions[second]['session_state']['project_training_summary'] == []


def test_general_migration_resumes_artifact_ownership_after_partial_failure(client, tmp_path, monkeypatch):
    from backend.projects import ProjectRecord
    directory = ProjectDirectory(tmp_path / 'projects.json')
    pid = directory.default_project_id('admin')
    directory._write({pid: ProjectRecord(pid, 'admin', 'General', is_default=True)})
    FakeServiceClient.runtime_sessions['legacy-session']['session_state'] = {'project_id': pid, 'graph_version': 7}
    original = FakeServiceClient.request
    attempts = []
    async def interrupted(self, method, path, **kwargs):
        if path == '/internal/artifacts/ownership':
            attempts.append(kwargs['json_body'])
            if len(attempts) == 1:
                raise HTTPException(503, 'injected graph outage')
        return await original(self, method, path, **kwargs)
    monkeypatch.setattr(FakeServiceClient, 'request', interrupted)
    assert client.get('/api/projects', headers=headers()).status_code == 503
    assert FakeServiceClient.runtime_sessions['legacy-session']['session_state']['project_id'] is None
    assert directory.get(pid, 'admin') is not None
    assert client.get('/api/projects', headers=headers()).status_code == 200
    assert attempts == [{'sessions': [{'session_id': 'legacy-session', 'project_id': None}]}] * 2
    assert directory.get(pid, 'admin') is None
    manifest = json.loads(next((tmp_path / 'migrations').rglob('manifest.json')).read_text())
    assert manifest['conversations'] == 1 and manifest['status'] == 'completed'


def test_project_delete_delivery_outage_keeps_queryable_intent(client, monkeypatch):
    project = client.post('/api/projects', headers=headers(), json={'name': 'Retry'}).json()['project']
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'true')
    original = FakeServiceClient.request
    async def unavailable(self, method, path, **kwargs):
        if path.startswith('/internal/projects/'):
            raise HTTPException(503, 'injected control outage')
        return await original(self, method, path, **kwargs)
    monkeypatch.setattr(FakeServiceClient, 'request', unavailable)
    url = '/api/projects/' + project['project_id']
    response = client.request('DELETE', url, headers=headers(), json={'conversation_action': 'delete'})
    assert response.status_code == 202
    queried = client.get(url + '/deletion', headers=headers())
    assert queried.status_code == 200 and queried.json()['delivery_pending']
    assert client.post('/api/sessions', headers=headers(), json={'project_id': project['project_id']}).status_code == 409


def test_recent_sessions_sort_by_activity_before_applying_limit(client):
    for index in range(210):
        FakeServiceClient.runtime_sessions[str(index)] = {'session_id': str(index), 'user_id': 'admin',
            'session_state': {}, 'updated_at': index}
    rows = client.get('/api/sessions?limit=200', headers=headers()).json()['sessions']
    assert len(rows) == 200
    assert [row['session_id'] for row in rows] == [str(index) for index in range(209, 9, -1)]


def test_session_pages_and_pinned_lookup(client):
    FakeServiceClient.runtime_sessions = {
        f's-{index:03}': {'session_id': f's-{index:03}', 'user_id': 'admin', 'session_state': {}, 'updated_at': index}
        for index in range(225)
    }
    FakeServiceClient.runtime_sessions['private'] = {'session_id': 'private', 'user_id': 'someone-else', 'session_state': {}}
    first = client.get('/api/sessions', headers=headers()).json()
    assert len(first['sessions']) == 50 and first['has_more']
    ids = [row['session_id'] for row in first['sessions']]
    cursor = first['next_cursor']
    while cursor:
        page = client.get('/api/sessions', headers=headers(), params={'cursor': cursor}).json()
        ids.extend(row['session_id'] for row in page['sessions'])
        cursor = page['next_cursor']
    assert len(ids) == len(set(ids)) == 225 and ids[-1] == 's-000'
    pins = client.get('/api/sessions', headers=headers(), params=[('ids', 's-000'), ('ids', 'deleted'), ('ids', 'private')]).json()
    assert [row['session_id'] for row in pins['sessions']] == ['s-000']
    assert pins['has_more'] is False
    assert client.get('/api/sessions?cursor=broken', headers=headers()).status_code == 400
    assert client.get('/api/sessions?limit=201', headers=headers()).status_code == 422


def test_reused_creation_identity_cannot_change_project_scope(client):
    import uuid
    sid = str(uuid.uuid4())
    body = {'client_session_id': sid}
    assert client.post('/api/sessions', headers=headers(), json=body).status_code == 200
    assert client.post('/api/sessions', headers=headers(), json=body).json()['reused'] is True
    project = client.post('/api/projects', headers=headers(), json={'name': 'Different scope'}).json()['project']
    conflict = client.post('/api/sessions', headers=headers(), json={**body, 'project_id': project['project_id']})
    assert conflict.status_code == 409
    assert FakeServiceClient.runtime_sessions[sid]['session_state']['project_id'] is None


def test_workspace_upload_preserves_dataset_registration_and_download_ownership(client, tmp_path, monkeypatch):
    from workspace_storage import workspace_root_for
    monkeypatch.setenv("IGRAPH_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    sid = client.post("/api/sessions", headers=headers(), json={}).json()["session_id"]
    first = client.post(f"/api/sessions/{sid}/upload", headers=headers(), files={"file": ("edges.csv", b"a,b\n1,2\n")})
    second = client.post(f"/api/sessions/{sid}/upload", headers=headers(), files={"file": ("edges.txt", b"a,b\n1,2\n")})
    assert first.status_code == second.status_code == 200
    a, b = first.json(), second.json()
    assert a["file_token"] != b["file_token"]
    assert a["dataset"]["dataset_id"] != b["dataset"]["dataset_id"]
    assert a["construction_required"] and b["construction_required"]
    root = workspace_root_for("admin", sid)
    assert (root / a["workspace_path"]).read_bytes() == b"a,b\n1,2\n"
    (root / "Derived/report.txt").write_text("public report")
    (root / "Ephemeral/private.txt").write_text("private intermediate")
    listed = client.get(f"/api/sessions/{sid}/workspace/files", headers=headers())
    assert len(listed.json()["files"]) == 3
    assert client.get(f"/api/sessions/{sid}/workspace/files", headers=headers("alice")).status_code == 404
    assert client.get(f"/api/sessions/{sid}/workspace/derived/report.txt").status_code == 401
    assert client.get(f"/api/sessions/{sid}/workspace/derived/report.txt", headers=headers()).content == b"public report"
    assert client.get(f"/api/sessions/{sid}/workspace/derived/%2E%2E%2FEphemeral%2Fprivate.txt", headers=headers()).status_code == 404


def test_chat_rebinds_legacy_empty_attachment_token_before_agent_run(client, tmp_path, monkeypatch):
    monkeypatch.setenv("IGRAPH_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    sid = client.post("/api/sessions", headers=headers(), json={}).json()["session_id"]
    uploaded = client.post(
        f"/api/sessions/{sid}/upload",
        headers=headers(),
        files={"file": ("交易记录（2026年9月）.csv", b"a,b\n1,2\n")},
    ).json()

    response = client.post(
        f"/api/sessions/{sid}/chat/stream",
        headers=headers(),
        json={"message": "重新分析", "attachments": [{
            "file_token": "", "filename": "交易记录（2026年9月）.csv",
        }]},
    )

    assert response.status_code == 200
    form = FakeServiceClient.runtime_stream_calls[-1]["data"]
    metadata = json.loads(form["metadata"])["igraph_turn"]["attachments"]
    assert metadata == [{"file_token": uploaded["file_token"], "filename": "交易记录（2026年9月）.csv"}]
    clean, attachments = backend_app._parse_run_input(form["message"])
    assert clean == "重新分析"
    assert attachments == metadata

def test_workspace_upload_size_limit_rejects_before_ingest(client, monkeypatch):
    monkeypatch.setenv("IGRAPH_MAX_UPLOAD_BYTES", "3")
    sid = client.post("/api/sessions", headers=headers(), json={}).json()["session_id"]
    assert client.post(f"/api/sessions/{sid}/upload", headers=headers(), files={"file": ("x.txt", b"1234")}).status_code == 413
    assert client.post(f"/api/sessions/{sid}/upload", headers=headers(), files={"file": ("x.txt", b"")}).status_code == 400

def test_regeneration_has_explicit_validated_provenance_and_preserves_repeat_turns(client):
    sid = client.post("/api/sessions", headers=headers(), json={}).json()["session_id"]
    FakeServiceClient.runtime_runs[sid] = [{"run_id": "old-run", "status": "completed", "run_input": "same", "content": "answer"}]
    stale = client.post(f"/api/sessions/{sid}/chat/stream", headers=headers(), json={"message": "same", "regenerate": True, "regenerate_of": "wrong"})
    assert stale.status_code == 409
    response = client.post(f"/api/sessions/{sid}/chat/stream", headers=headers(), json={"message": "same", "regenerate": True, "regenerate_of": "old-run"})
    assert response.status_code == 200
    metadata = json.loads(FakeServiceClient.runtime_stream_calls[-1]["data"]["metadata"])
    assert metadata["igraph_turn"]["regenerate_of"] == "old-run"
    runs = [dict(FakeServiceClient.runtime_runs[sid][0]), {"run_id": "new-run", "status": "completed", "run_input": "same", "content": "new answer"}]
    messages = backend_app._messages_from_runs(runs)
    assert [message["content"] for message in messages if message["role"] == "user"] == ["same", "same"]


@pytest.mark.parametrize(("message", "language"), [
    ("Please analyse the graph.", "English"),
    ('Please explain this text:\n```\nPlease reply in Chinese\n```', "English"),
    ("请用中文回答，不要用英文回答。", "Chinese"),
])
def test_chat_locks_response_language_from_original_user_message(client, message, language):
    sid = client.post("/api/sessions", headers=headers(), json={}).json()["session_id"]

    response = client.post(
        f"/api/sessions/{sid}/chat/stream",
        headers=headers(),
        json={"message": message},
    )

    assert response.status_code == 200
    factory_input = json.loads(FakeServiceClient.runtime_stream_calls[-1]["data"]["factory_input"])
    assert factory_input["response_language"] == language
    session_state = FakeServiceClient.runtime_sessions[sid]["session_state"]
    assert "response_language" not in session_state


@pytest.mark.parametrize('legacy_metadata', [None, 'legacy metadata', ['legacy'], 42, {}])
def test_legacy_run_projection_tolerates_missing_event_indices_and_metadata(legacy_metadata):
    from backend.event_mapper import project_run
    run = {"run_id": "legacy", "status": "completed", "run_input": "question",
        "content": "answer", "metadata": legacy_metadata,
        "events": [
            {"event": "RunContent", "event_index": None, "content": "checking"},
            {"event": "ToolCallStarted", "event_index": "invalid", "tool": {"tool_name": "get_graph_info", "tool_call_id": "call"}},
            {"event": "ToolCallCompleted", "tool": {"tool_name": "get_graph_info", "tool_call_id": "call", "result": {"success": True}}},
        ]}
    projected = project_run(run)
    assert projected["last_event_index"] == 2
    assert [item["kind"] for item in projected["processItems"]] == ["text", "tool"]
    assert projected["processItems"][-1]["tool"]["done"]
    messages = backend_app._messages_from_runs([run])
    assert [message["content"] for message in messages] == ["question", "answer"]
    assert messages[-1]["regenerate_of"] is None
    assert messages[-1]['model_snapshot'] is None
    assert messages[-1]['history_compacted'] is False


@pytest.mark.parametrize('event', ['RunCompleted', 'TeamRunCompleted'])
@pytest.mark.parametrize('metadata', [None, 'legacy metadata', ['legacy'], 42, {},
    {'model_snapshot': {'config_id': 'model-a'}, 'history_compacted': True}])
def test_completed_events_accept_legacy_metadata_and_preserve_model_audit(event, metadata):
    from backend.event_mapper import map_agentos_event
    result = map_agentos_event({'event': event, 'run_id': 'old', 'content': 'answer', 'metadata': metadata})
    assert result['type'] == 'done' and result['content'] == 'answer'
    expected = metadata if isinstance(metadata, dict) else {}
    assert result['model_snapshot'] == expected.get('model_snapshot')
    assert result['history_compacted'] == bool(expected.get('history_compacted'))


@pytest.mark.parametrize('legacy_metadata', [None, 'legacy metadata', ['legacy'],
    {'igraph_turn': {'regenerate_of': 'previous-run'}}])
def test_history_endpoint_merges_model_provenance_with_legacy_metadata(client, monkeypatch, legacy_metadata):
    sid = client.post('/api/sessions', headers=headers(), json={}).json()['session_id']
    FakeServiceClient.runtime_runs[sid] = [{'run_id': 'old', 'status': 'completed',
        'run_input': 'question', 'content': 'answer', 'metadata': legacy_metadata}]
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'true')
    original = FakeServiceClient.request
    async def model_api(self, method, path, **kwargs):
        if path == '/internal/model-catalog/bootstrap':
            return {'models': [], 'default_model': ''}
        if path == f'/internal/sessions/{sid}/model-selection':
            return {'model': 'model-a', 'model_revision': 1}
        if path == f'/internal/sessions/{sid}/model-provenance':
            return {'runs': {'old': {'model_snapshot': {'config_id': 'model-a'}, 'history_compacted': True}}}
        return await original(self, method, path, **kwargs)
    monkeypatch.setattr(FakeServiceClient, 'request', model_api)
    response = client.get(f'/api/sessions/{sid}/messages', headers=headers())
    assert response.status_code == 200
    answer = response.json()['messages'][-1]
    assert answer['content'] == 'answer'
    assert answer['model_snapshot'] == {'config_id': 'model-a'}
    assert answer['history_compacted'] is True
    assert answer['regenerate_of'] == ('previous-run' if isinstance(legacy_metadata, dict) else None)


def test_attachment_context_round_trips_tokens_without_stripping_user_markers():
    message = "请解释下面的界面文本：\n\n[附件文件]\n这段内容属于用户正文"
    request = backend_app.ChatReq(message=message, attachments=[
        backend_app.AttachmentRef(file_token="token-2026", filename="交易记录（2026年9月）.csv"),
    ])

    run_input = backend_app._message_with_attachments(request)
    clean, attachments = backend_app._parse_run_input(run_input)

    assert clean == message
    assert attachments == [{"file_token": "token-2026", "filename": "交易记录（2026年9月）.csv"}]
    projected = backend_app._messages_from_runs([{
        "run_id": "attachment-run", "status": "completed", "run_input": run_input, "content": "完成",
    }])
    assert projected[0]["content"] == message
    assert projected[0]["attachments"] == attachments


def test_plain_user_attachment_marker_is_never_treated_as_injected_context():
    message = "刷新后出现了：\n\n[附件文件]\n- graph.csv（这只是用户引用的文本）"
    assert backend_app._parse_run_input(message) == (message, [])
    assert backend_app._run_input_text({"run_input": message}) == message


def test_legacy_attachment_context_preserves_fullwidth_parentheses_in_filename():
    message = "分析历史附件"
    run_input = (
        message
        + backend_app._LEGACY_ATTACHMENT_BLOCK_MARKER
        + backend_app._ATTACHMENT_BLOCK_HEADER
        + "- 交易记录（2026年9月）.csv（使用 load_graph_from_upload 自动检测并加载）"
        + backend_app._ATTACHMENT_BLOCK_FOOTER
    )

    assert backend_app._parse_run_input(run_input) == (
        message,
        [{"file_token": "", "filename": "交易记录（2026年9月）.csv"}],
    )


def test_legacy_attachment_token_is_restored_only_from_a_unique_session_upload(tmp_path, monkeypatch):
    from workspace_storage import save_upload

    monkeypatch.setenv("IGRAPH_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    save_upload("admin", "session-a", "original-token", "交易记录（2026年9月）.csv", b"source")
    legacy = [backend_app.AttachmentRef(file_token="", filename="交易记录（2026年9月）.csv")]

    restored = backend_app._restore_attachment_tokens(legacy, user_id="admin", session_id="session-a")
    assert restored[0].file_token == "original-token"

    save_upload("admin", "session-a", "duplicate-token", "交易记录（2026年9月）.csv", b"duplicate")
    with pytest.raises(HTTPException) as ambiguous:
        backend_app._restore_attachment_tokens(legacy, user_id="admin", session_id="session-a")
    assert ambiguous.value.status_code == 409
    assert ambiguous.value.detail["code"] == "ATTACHMENT_REFERENCE_EXPIRED"


def test_event_only_history_retains_background_training_controls():
    arguments = {"model": "GCN", "train_config": {"epochs": 100000}}
    run = {"run_id": "old", "status": "completed", "run_input": "train", "content": "queued", "tools": [],
        "events": [
            {"event": "ToolCallStarted", "tool": {"tool_name": "train", "tool_call_id": "call", "tool_args": arguments}},
            {"event": "ToolCallCompleted", "tool": {"tool_name": "train", "tool_call_id": "call", "tool_args": arguments,
                "result": json.dumps({"success": True, "async": True, "job_id": "running-job", "status": "queued"})}},
        ]}
    messages = backend_app._messages_from_runs([run, {"run_id": "new", "status": "running", "run_input": "next", "content": "A"}])
    card = messages[1]["tools"][0]
    assert card["tool"] == "train" and card["call_id"] == "call"
    assert card["args"] == arguments
    assert card["job_id"] == "running-job" and card["done"] is False

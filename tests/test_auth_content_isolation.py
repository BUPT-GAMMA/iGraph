"""Public cookie authentication preserves private resource ownership."""
from copy import deepcopy

import pytest
from agno.session.agent import AgentSession
from fastapi import HTTPException

from test_projects import FakeServiceClient, client, headers
from test_memory_api import memory_stack
from test_skill_api import skill_client
from test_remote_graph_api import api as remote_api, FORM


def spoofed_headers(user_id, claimed_user_id):
    return {**headers(user_id), "X-User-ID": claimed_user_id}


@pytest.fixture
def public_skill_stack(client, skill_client, monkeypatch):
    runtime_client, _config = skill_client
    original = FakeServiceClient.request

    async def request(self, method, path, *, headers, params=None, json_body=None, **kwargs):
        if path.startswith("/internal/skills"):
            response = runtime_client.request(method, path, headers=headers, params=params, json=json_body)
            if response.status_code >= 400:
                raise HTTPException(response.status_code, response.json()["detail"])
            return response.json()
        return await original(self, method, path, headers=headers, params=params, json_body=json_body, **kwargs)

    monkeypatch.setattr(FakeServiceClient, "request", request)
    return client


@pytest.fixture
def public_remote_stack(client, remote_api, monkeypatch):
    remote_api.db.upsert_session(AgentSession(
        session_id="admin-session", user_id="admin", agent_id="igraph-agent",
        session_data={"session_state": {}}, runs=[], created_at=1, updated_at=1,
    ))
    original = FakeServiceClient.request

    async def request(self, method, path, *, headers, params=None, json_body=None, **kwargs):
        if path.startswith("/internal/remote-graphs") or path.endswith("/remote-graph"):
            response = remote_api.client.request(method, path, headers=headers, params=params, json=json_body)
            if response.status_code >= 400:
                raise HTTPException(response.status_code, response.json()["detail"])
            return response.json()
        return await original(self, method, path, headers=headers, params=params, json_body=json_body, **kwargs)

    monkeypatch.setattr(FakeServiceClient, "request", request)
    return client


def test_cookie_owner_wins_over_claimed_header_and_header_alone_is_rejected(client):
    response = client.get("/api/auth/me", headers=spoofed_headers("alice", "admin"))
    assert response.status_code == 200
    assert response.json()["user_id"] == "alice"
    assert response.json()["is_admin"] is False
    assert client.get("/api/auth/me", headers=spoofed_headers("admin", "alice")).json()["user_id"] == "admin"
    assert client.get("/api/projects", headers={"X-User-ID": "admin"}).status_code == 401
    assert client.get("/api/projects", headers=spoofed_headers("missing-user", "admin")).status_code == 401
    assert client.get("/api/projects").status_code == 401


def test_projects_sessions_and_datasets_stay_with_cookie_owner(client):
    owner = headers("admin")
    other = spoofed_headers("alice", "admin")
    project = client.post("/api/projects", headers=owner, json={"name": "Admin private"}).json()["project"]
    pid = project["project_id"]
    sid = client.post("/api/sessions", headers=owner, json={"project_id": pid}).json()["session_id"]
    personal = client.post("/api/personal-datasets", headers=owner,
        files={"file": ("private.edgelist", b"A B\nB C\n", "text/plain")})
    assert personal.status_code == 200, personal.text
    did = personal.json()["dataset"]["dataset_id"]
    project_dataset = client.post(f"/api/projects/{pid}/datasets", headers=owner,
        files={"file": ("project-private.edgelist", b"X Y\nY Z\n", "text/plain")})
    assert project_dataset.status_code == 200, project_dataset.text
    project_did = project_dataset.json()["dataset"]["dataset_id"]

    other_project = client.post("/api/projects", headers=other, json={"name": "Alice private"}).json()["project"]
    assert client.get("/api/projects", headers=other).json()["projects"][0]["project_id"] == other_project["project_id"]
    assert pid not in [row["project_id"] for row in client.get("/api/projects", headers=other).json()["projects"]]
    for path in (f"/api/projects/{pid}", f"/api/projects/{pid}/datasets", f"/api/projects/{pid}/conversations"):
        assert client.get(path, headers=other).status_code == 404
    assert client.patch(f"/api/projects/{pid}", headers=other, json={"name": "Hijacked"}).status_code == 404
    assert client.post("/api/sessions", headers=other, json={"project_id": pid}).status_code == 404
    assert sid not in [row["session_id"] for row in client.get("/api/sessions", headers=other).json()["sessions"]]
    assert client.get("/api/sessions", headers=other, params={"ids": sid}).json()["sessions"] == []
    for suffix in ("messages", "info", "workspace/files"):
        assert client.get(f"/api/sessions/{sid}/{suffix}", headers=other).status_code == 404
    assert client.post(f"/api/sessions/{sid}/activate", headers=other).status_code == 404
    assert client.delete(f"/api/sessions/{sid}", headers=other).status_code == 404

    assert client.get("/api/personal-datasets", headers=other).json()["datasets"] == []
    assert client.patch(f"/api/personal-datasets/{did}", headers=other, json={"name": "Hijacked"}).status_code == 404
    assert client.delete(f"/api/personal-datasets/{did}", headers=other).status_code == 404
    assert client.patch(f"/api/projects/{pid}/datasets/{project_did}", headers=other, json={"name": "Hijacked"}).status_code == 404
    assert client.post("/api/sessions", headers=other, json={"active_dataset_id": did}).status_code == 409
    other_sid = client.post("/api/sessions", headers=other, json={}).json()["session_id"]
    assert client.patch(f"/api/sessions/{other_sid}/dataset", headers=other, json={"active_dataset_id": did}).status_code == 404

    assert client.get(f"/api/projects/{pid}", headers=owner).json()["project"]["name"] == "Admin private"
    assert client.get(f"/api/sessions/{sid}/messages", headers=owner).status_code == 200
    assert client.get("/api/personal-datasets", headers=owner).json()["datasets"][0]["dataset_id"] == did


def test_private_skills_follow_cookie_owner_through_public_proxy(public_skill_stack):
    client = public_skill_stack
    owner = headers("admin")
    other = spoofed_headers("alice", "admin")
    body = {"name": "auth-private", "body": "Admin private instructions", "destination": "user"}
    created = client.post("/api/skills", headers=owner, json=body)
    assert created.status_code == 200, created.text
    assert client.get("/api/skills", headers=other, params={"source": "user"}).json()["skills"] == []
    assert client.get("/api/skills/auth-private", headers=other).status_code == 404
    assert client.put("/api/skills/auth-private", headers=other, json={"source": "user", "body": "Hijacked"}).status_code == 404
    assert client.delete("/api/skills/auth-private", headers=other, params={"source": "user"}).status_code == 404
    assert "Admin private instructions" in client.get("/api/skills/auth-private", headers=owner).json()["skill"]["content"]


def test_learned_memory_namespace_survives_cookie_header_spoof(memory_stack):
    client, _runtime_client, store, _legacy_calls = memory_stack
    before = deepcopy(store.recall("admin").memories)
    other = spoofed_headers("alice", "admin")
    rows = client.get("/api/memory", headers=other).json()["items"]
    assert len(rows) == 1 and rows[0]["value"] == "Alice private preference"
    assert client.patch("/api/memory/learning%3Akeep", headers=other, json={"value": "Hijacked"}).status_code == 404
    assert client.delete("/api/memory/learning%3Akeep", headers=other).status_code == 404
    # Both users have report; mutation must select Alice's namespace even when
    # she claims to be admin, rather than changing the equally named admin item.
    assert client.patch("/api/memory/learning%3Areport", headers=other, json={"value": "Alice updated"}).status_code == 200
    assert store.recall("alice").memories[0]["content"] == "Alice updated"
    assert client.delete("/api/memory/learning%3Areport", headers=other).status_code == 200
    assert store.recall("admin").memories == before


def test_remote_graph_connections_and_bindings_follow_cookie_owner(public_remote_stack):
    client = public_remote_stack
    owner = headers("alice")
    other = spoofed_headers("admin", "alice")
    created = client.post("/api/remote-graphs", headers=owner, json=FORM)
    assert created.status_code == 200, created.text
    assert FORM["password"] not in created.text
    connection = created.json()["connection"]
    cid = connection["connection_id"]
    assert client.get("/api/remote-graphs", headers=other).json()["connections"] == []
    assert client.delete(f"/api/remote-graphs/{cid}?revision=1", headers=other).status_code == 404
    assert client.put("/api/sessions/admin-session/remote-graph", headers=other, json={"connection_id": cid}).status_code == 404
    assert client.put("/api/sessions/alice-session/remote-graph", headers=other, json={"connection_id": cid}).status_code == 404
    assert client.put("/api/sessions/alice-session/remote-graph", headers=owner, json={"connection_id": cid}).status_code == 200
    assert client.get("/api/remote-graphs", headers=owner).json()["connections"][0]["connection_id"] == cid

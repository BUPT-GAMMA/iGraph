from __future__ import annotations

from copy import deepcopy

import pytest
from agno.db.sqlite import SqliteDb
from agno.learn import UserMemoryConfig
from agno.learn.schemas import Memories
from agno.learn.stores.user_memory import UserMemoryStore
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from agent_runtime.memory_api import create_memory_router
from execution_protocol import service_headers
from test_projects import FakeServiceClient, client, headers


@pytest.fixture()
def memory_stack(client, tmp_path, monkeypatch):
    db = SqliteDb(db_file=str(tmp_path / "learning.db"))
    store = UserMemoryStore(config=UserMemoryConfig(db=db))
    store.save("admin", Memories(user_id="admin", memories=[
        {"id": "report", "content": "报告标题用银杏-7319", "source": "conversation", "created_at": "2026-09-17"},
        {"id": "keep", "content": "喜欢简洁的中文回答", "category": "preference"},
    ], agent_id="igraph-agent"))
    # Reuse an item ID in another user's namespace to catch unscoped deletion.
    store.save("alice", Memories(user_id="alice", memories=[{"id": "report", "content": "Alice private preference"}]))
    native = FastAPI()
    native.include_router(create_memory_router(db))
    original = FakeServiceClient.request
    legacy_calls = []
    with TestClient(native) as runtime_client:
        async def request(self, method, path, *, headers, params=None, json_body=None, **kwargs):
            if path.startswith("/internal/memory"):
                response = runtime_client.request(method, path, headers=headers, params=params, json=json_body)
                if response.status_code >= 400:
                    raise HTTPException(response.status_code, response.json()["detail"])
                return response.json()
            if path.startswith("/memories"):
                legacy_calls.append((method, path, headers, params))
                if method == "GET":
                    rows = [{"memory_id": "legacy-1", "memory": "旧版保留内容", "topics": ["long_term", "fact", "旧记忆"]}] if headers["X-User-ID"] == "admin" else []
                    search = (params or {}).get("search_content")
                    if search:
                        rows = [row for row in rows if search in row["memory"]]
                    return {"data": rows, "meta": {"total_count": len(rows)}}
                return {"success": True}
            if path == "/internal/skills":
                return {"skills": []}
            return await original(self, method, path, headers=headers, params=params, json_body=json_body, **kwargs)
        monkeypatch.setattr(FakeServiceClient, "request", request)
        yield client, runtime_client, store, legacy_calls
    db.db_engine.dispose()


def test_management_displays_real_learning_and_keeps_legacy_entries(memory_stack):
    client, _, _, _ = memory_stack
    response = client.get("/api/memory?scope=long_term", headers=headers())
    assert response.status_code == 200
    rows = response.json()["items"]
    assert [row["memory_id"] for row in rows] == ["learning:report", "learning:keep", "legacy-1"]
    assert rows[0]["key"] == rows[0]["value"] == "报告标题用银杏-7319"
    assert rows[1]["category"] == "preference"
    assert rows[2]["value"] == "旧版保留内容"
    found = client.get("/api/memory", headers=headers(), params={"query": "银杏", "limit": 1}).json()["items"]
    assert [row["memory_id"] for row in found] == ["learning:report"]
    assert len(client.get("/api/memory?limit=1", headers=headers()).json()["items"]) == 1
    assert client.get("/api/memory?limit=0", headers=headers()).status_code == 422


def test_delete_removes_only_the_owned_entry_from_agent_recall(memory_stack):
    client, _, store, legacy_calls = memory_stack
    before = deepcopy(store.get("admin").to_dict())
    response = client.delete("/api/memory/learning%3Areport?scope=long_term", headers=headers())
    assert response.status_code == 200
    recalled = UserMemoryStore(config=UserMemoryConfig(db=store.db)).recall("admin")
    assert recalled.memories == [before["memories"][1]]
    assert recalled.agent_id == before["agent_id"]
    assert store.get("alice").memories == [{"id": "report", "content": "Alice private preference"}]
    assert not legacy_calls
    assert client.delete("/api/memory/learning%3Areport", headers=headers()).status_code == 404
    rows = client.get("/api/memory", headers=headers()).json()["items"]
    assert [row["memory_id"] for row in rows] == ["learning:keep", "legacy-1"]
    assert client.delete("/api/memory/learning%3Akeep", headers=headers()).status_code == 200
    assert store.recall("admin").memories == []


def test_user_isolation_and_internal_service_auth(memory_stack):
    client, runtime_client, store, _ = memory_stack
    rows = client.get("/api/memory", headers=headers("alice")).json()["items"]
    assert len(rows) == 1 and rows[0]["value"] == "Alice private preference"
    assert client.delete("/api/memory/learning%3Akeep", headers=headers("alice")).status_code == 404
    assert len(store.recall("admin").memories) == 2
    assert runtime_client.get("/internal/memory", headers=headers()).status_code == 403
    valid = service_headers("admin")
    valid.pop("X-User-ID")
    assert runtime_client.get("/internal/memory", headers=valid).status_code == 401
    assert client.get("/api/memory").status_code in {401, 403}


def test_update_learning_entry_keeps_siblings_and_provenance(memory_stack):
    client, _, store, _ = memory_stack
    before = deepcopy(store.recall("admin").memories)
    response = client.patch("/api/memory/learning%3Areport", headers=headers(),
                            json={"value": "报告标题改用水杉-9527", "key": "报告偏好"})
    assert response.status_code == 200
    after = store.recall("admin").memories
    assert after[0]["content"] == "报告标题改用水杉-9527"
    assert after[0]["source"] == before[0]["source"]
    assert after[1] == before[1]
    assert response.json()["item"]["key"] == "报告偏好"
    assert client.patch("/api/memory/learning%3Areport", headers=headers(), json={"value": ""}).status_code == 422


def test_counters_include_learning_entries_and_legacy_delete_still_routes(memory_stack):
    client, _, _, legacy_calls = memory_stack
    usage = client.get("/api/memory/usage", headers=headers()).json()
    assert usage["count"] == 3
    assert usage["included_item_ids"] == ["learning:report", "learning:keep", "legacy-1"]
    assert client.get("/api/metrics/knowledge", headers=headers()).json()["counters"]["agentos_memories"] == 3
    client.delete("/api/memory/learning%3Areport", headers=headers()).raise_for_status()
    assert client.get("/api/memory/usage", headers=headers()).json()["count"] == 2
    client.delete("/api/memory/legacy-1", headers=headers()).raise_for_status()
    assert legacy_calls[-1][:2] == ("DELETE", "/memories/legacy-1")


def test_runtime_failure_is_reported_not_shown_as_empty_memory(memory_stack, monkeypatch):
    client, _, _, _ = memory_stack
    original = FakeServiceClient.request
    async def fail(self, method, path, **kwargs):
        if path == "/internal/memory":
            raise HTTPException(503, "Memory storage unavailable")
        return await original(self, method, path, **kwargs)
    monkeypatch.setattr(FakeServiceClient, "request", fail)
    response = client.get("/api/memory", headers=headers())
    assert response.status_code == 503
    assert "items" not in response.json()

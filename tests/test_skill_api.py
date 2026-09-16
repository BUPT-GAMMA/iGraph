from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.skill_api import create_skill_router
from agent_runtime.skills_runtime import candidate_skills_path, user_skills_path


@pytest.fixture()
def skill_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path / "data"))
    config = AgentRuntimeConfig(
        host="127.0.0.1",
        port=0,
        database_url="postgresql+psycopg://test:test@localhost/test",
        mcp_url="http://127.0.0.1:21110/mcp",
        llm_api_key="",
        llm_base_url="",
        default_model="test-model",
        embedding_model="test-embedding",
        embedding_api_key="",
        embedding_base_url="",
        skills_root=tmp_path / "skills",
        telemetry=False,
        tracing=False,
        authorization=False,
    )
    app = FastAPI()
    app.include_router(create_skill_router(config))
    with TestClient(app) as client:
        yield client, config


def _headers(user_id: str = "alice") -> dict[str, str]:
    return {"X-User-ID": user_id}


def test_skill_api_requires_identity_and_rejects_unsafe_names(skill_client) -> None:
    client, _config = skill_client

    assert client.get("/internal/skills").status_code == 401
    response = client.post(
        "/internal/skills",
        headers=_headers(),
        json={"name": "../escape", "body": "unsafe"},
    )
    assert response.status_code == 400
    assert "lowercase" in response.json()["detail"]


def test_candidate_skill_lifecycle_preserves_metadata(skill_client) -> None:
    client, config = skill_client
    created = client.post(
        "/internal/skills",
        headers=_headers(),
        json={
            "name": "Graph_Helper",
            "body": "Use graph tools carefully.",
            "destination": "review_queue",
            "metadata": {
                "domain": "graph",
                "version": "2.0.0",
                "risk_level": "medium",
                "playbook_id": "candidate-playbook",
                "condition": {"min_nodes": 2},
            },
        },
    )
    assert created.status_code == 200
    assert created.json()["name"] == "graph-helper"

    listed = client.get(
        "/internal/skills",
        headers=_headers(),
        params={"source": "review_queue"},
    ).json()["skills"]
    assert [(row["name"], row["source"]) for row in listed] == [
        ("graph-helper", "review_queue")
    ]
    assert listed[0]["domain"] == "graph"
    assert listed[0]["condition"] == {"min_nodes": 2}

    fetched = client.get("/internal/skills/graph-helper", headers=_headers()).json()["skill"]
    assert fetched["source"] == "review_queue"
    assert "name: graph-helper" in fetched["content"]

    updated = client.put(
        "/internal/skills/graph-helper",
        headers=_headers(),
        json={
            "source": "review_queue",
            "body": "---\ndescription: Updated helper\nmetadata:\n  domain: analytics\n---\n\nUpdated body.",
        },
    )
    assert updated.status_code == 200
    candidate_file = candidate_skills_path(config, "alice") / "graph-helper" / "SKILL.md"
    assert "name: graph-helper" in candidate_file.read_text(encoding="utf-8")

    approved = client.post(
        "/internal/skills/graph-helper/approve",
        headers=_headers(),
        json={"target_name": "Approved_Helper", "playbook_id": "approved-playbook"},
    )
    assert approved.status_code == 200
    assert approved.json()["name"] == "approved-helper"
    assert not candidate_file.exists()

    approved_dir = user_skills_path(config, "alice") / "approved-helper"
    assert "name: approved-helper" in (approved_dir / "SKILL.md").read_text(encoding="utf-8")
    metadata = json.loads((approved_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["playbook_id"] == "approved-playbook"
    assert isinstance(metadata["approved_at"], float)

    missing = client.post(
        "/internal/skills/graph-helper/approve",
        headers=_headers(),
        json={},
    )
    assert missing.status_code == 404

    deleted = client.delete(
        "/internal/skills/approved-helper",
        headers=_headers(),
        params={"source": "user", "reason": "obsolete"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["reason"] == "obsolete"
    assert not approved_dir.exists()


def test_skill_conflicts_rejection_and_user_isolation(skill_client) -> None:
    client, config = skill_client
    payload = {"name": "shared-skill", "body": "body"}
    assert client.post("/internal/skills", headers=_headers("alice"), json=payload).status_code == 200
    assert client.post("/internal/skills", headers=_headers("alice"), json=payload).status_code == 409

    assert client.get("/internal/skills/shared-skill", headers=_headers("bob")).status_code == 404
    assert client.put(
        "/internal/skills/missing",
        headers=_headers("alice"),
        json={"body": "body", "source": "user"},
    ).status_code == 404
    assert client.delete("/internal/skills/missing", headers=_headers("alice")).status_code == 404

    candidate_payload = {
        "name": "candidate",
        "body": "candidate body",
        "destination": "review_queue",
    }
    assert client.post(
        "/internal/skills", headers=_headers("alice"), json=candidate_payload
    ).status_code == 200
    kept = client.post(
        "/internal/skills/candidate/reject",
        headers=_headers("alice"),
        json={"reason": "needs work", "remove_files": False},
    )
    assert kept.status_code == 200
    candidate_dir = candidate_skills_path(config, "alice") / "candidate"
    rejection = json.loads((candidate_dir / "REJECTED.json").read_text(encoding="utf-8"))
    assert rejection["reason"] == "needs work"

    removed = client.post(
        "/internal/skills/candidate/reject",
        headers=_headers("alice"),
        json={"reason": "rejected", "remove_files": True},
    )
    assert removed.status_code == 200
    assert not candidate_dir.exists()
    assert client.post(
        "/internal/skills/candidate/reject",
        headers=_headers("alice"),
        json={},
    ).status_code == 404


def test_malformed_skill_metadata_falls_back_to_safe_defaults(skill_client) -> None:
    client, config = skill_client
    skill_dir = user_skills_path(config, "alice") / "manual"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Manual instructions", encoding="utf-8")
    (skill_dir / "metadata.json").write_text("{broken", encoding="utf-8")

    skill = client.get("/internal/skills/manual", headers=_headers()).json()["skill"]
    assert skill["name"] == "manual"
    assert skill["domain"] == "general"
    assert skill["version"] == "1.0.0"
    assert skill["risk_level"] == "low"


def test_private_delete_removes_both_user_skill_roots(skill_client) -> None:
    client, config = skill_client
    for destination in ("user", "review_queue"):
        response = client.post(
            "/internal/skills",
            headers=_headers("target"),
            json={"name": f"{destination}-skill", "body": "body", "destination": destination},
        )
        assert response.status_code == 200

    response = client.delete("/internal/skills/users/target/all", headers=_headers("service"))
    assert response.status_code == 200
    assert response.json()["removed"] == [str(user_skills_path(config, "target"))]
    assert not user_skills_path(config, "target").exists()
    assert not candidate_skills_path(config, "target").exists()
    assert client.delete("/internal/skills/users/target/all").status_code == 401


def test_skill_zip_upload_enters_review_queue_and_keeps_resources(skill_client):
    import io, zipfile
    client, config = skill_client
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("graph-reader/SKILL.md", "---\nname: old-name\ndescription: inspect graphs\n---\nRead the graph.")
        archive.writestr("graph-reader/references/help.txt", "reference")
    response = client.post("/internal/skills/upload", headers=_headers(), files={"file": ("graph-reader.zip", buffer.getvalue())})
    assert response.status_code == 200
    assert response.json()["source"] == "review_queue"
    root = candidate_skills_path(config, "alice") / "graph-reader"
    assert (root / "references/help.txt").read_text() == "reference"
    assert not (user_skills_path(config, "alice") / "graph-reader").exists()
    assert "name: graph-reader" in (root / "SKILL.md").read_text()

@pytest.mark.parametrize("path", ["../escape", "graph-reader/../../escape", "graph-reader" + chr(92) + ".." + chr(92) + "escape"])
def test_skill_zip_rejects_traversal(skill_client, path):
    import io, zipfile
    client, _ = skill_client
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("graph-reader/SKILL.md", "reader")
        archive.writestr(path, "escape")
    assert client.post("/internal/skills/upload", headers=_headers(), files={"file": ("graph-reader.zip", buffer.getvalue())}).status_code == 400

def test_skill_zip_rejects_expansion_bomb(skill_client):
    import io, zipfile
    client, _ = skill_client
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("graph-reader/SKILL.md", "reader")
        archive.writestr("graph-reader/large", b"x" * (10 * 1024 * 1024))
    assert client.post("/internal/skills/upload", headers=_headers(), files={"file": ("graph-reader.zip", buffer.getvalue())}).status_code == 413

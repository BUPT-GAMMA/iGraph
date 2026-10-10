"""Bundled background identity, per-turn refresh and cross-graph isolation."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.user_cases as cases
from backend.project_datasets import ProjectDatasetRecord
from agent_runtime.construction_state import construction_tool_hook
from agent_runtime.instructions import instructions_for
from tests.test_projects import client, headers, FakeServiceClient

RESOURCES = Path(__file__).resolve().parents[1] / "graph_engine/resources/tianlongbabu"


def novel():
    return ProjectDatasetRecord(
        dataset_id="novel", project_id="p", user_id="admin", name="renamed",
        original_filename="renamed.npz", storage_path=str(RESOURCES / "tianlongbabu-characters.npz"),
        file_type="npz", source_kind="server",
        metadata={"source_relative_path": "tianlongbabu-characters.npz"},
    )


def test_background_matches_snapshot_even_after_rename():
    result = cases.background_for_dataset(novel())
    assert result["dataset_id"] == "novel"
    assert result["content"] == (RESOURCES / "background.md").read_text(encoding="utf-8").strip()
    assert "106" in result["content"]
    manifest = json.loads((RESOURCES / "manifest.json").read_text(encoding="utf-8"))
    assert str(manifest["dataset"]["node_count"]) in result["content"]
    assert str(manifest["dataset"]["edge_count"]) in result["content"]


@pytest.mark.parametrize("dataset", [
    None,
    replace(novel(), source_kind="upload"),
    replace(novel(), status="source_ready"),
    replace(novel(), file_type="txt", metadata={"user_case_id": "tianlongbabu"}),
    replace(novel(), metadata={"source_relative_path": "finance-ownership.npz"}),
    replace(novel(), metadata={"source_relative_path": "other/tianlongbabu-characters.npz"}),
])
def test_other_graphs_and_matching_user_filenames_do_not_get_background(dataset):
    assert cases.background_for_dataset(dataset) is None


def test_replaced_or_missing_graph_cannot_reuse_background(tmp_path):
    graph = tmp_path / "tianlongbabu-characters.npz"
    graph.write_bytes(b"different graph under the same filename")
    assert cases.background_for_dataset(replace(novel(), storage_path=str(graph))) is None
    graph.unlink()
    assert cases.background_for_dataset(replace(novel(), storage_path=str(graph))) is None


def test_background_is_reread_and_missing_document_is_nonfatal(tmp_path, monkeypatch):
    folder = tmp_path / "tianlongbabu"
    folder.mkdir()
    (folder / "manifest.json").write_bytes((RESOURCES / "manifest.json").read_bytes())
    monkeypatch.setattr(cases, "_BACKGROUND_RESOURCES", tmp_path)
    doc = folder / "background.md"
    assert cases.background_for_dataset(novel()) is None
    doc.write_text("version one", encoding="utf-8")
    assert cases.background_for_dataset(novel())["content"] == "version one"
    doc.write_text("version two", encoding="utf-8")
    assert cases.background_for_dataset(novel())["content"] == "version two"
    doc.write_text("", encoding="utf-8")
    assert cases.background_for_dataset(novel()) is None


def test_real_chat_path_refreshes_background_and_clears_it_on_switch(client, tmp_path, monkeypatch):
    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    root = tmp_path / "server_datasets"
    root.mkdir(exist_ok=True)
    (root / "tianlongbabu-characters.npz").write_bytes(
        (RESOURCES / "tianlongbabu-characters.npz").read_bytes())
    source = client.get("/api/server-datasets", headers=headers()).json()["datasets"][0]
    project = client.post("/api/projects", headers=headers(), json={"name": "background"}).json()["project"]
    pid = project["project_id"]
    imported = client.post(f"/api/projects/{pid}/datasets/from-server", headers=headers(),
        json={"server_dataset_id": source["server_dataset_id"], "set_as_default": True})
    assert imported.status_code == 200, imported.text
    sid = client.post("/api/sessions", headers=headers(), json={"project_id": pid}).json()["session_id"]
    # Existing sessions get the document on their next message, without migration.
    state = FakeServiceClient.runtime_sessions[sid]["session_state"]
    state.pop("data_state", None)
    for locale in ("zh-CN", "en"):
        response = client.post(f"/api/sessions/{sid}/chat/stream",
            headers={**headers(), "Accept-Language": locale}, json={"message": "Describe this graph."})
        assert response.status_code == 200, response.text
        state = FakeServiceClient.runtime_sessions[sid]["session_state"]
        assert state["data_state"]["graph_background"]["case_id"] == "tianlongbabu"
        options = json.loads(FakeServiceClient.runtime_stream_calls[-1]["data"]["factory_input"])
        assert options["response_language"] == ("Chinese" if locale == "zh-CN" else "English")

    other = client.post(f"/api/projects/{pid}/datasets", headers=headers(),
        files={"file": ("other.npz", b"fake graph")}).json()["dataset"]
    response = client.patch(f"/api/sessions/{sid}/dataset", headers=headers(),
        json={"active_dataset_id": other["dataset_id"]})
    assert response.status_code == 200, response.text
    assert FakeServiceClient.runtime_sessions[sid]["session_state"]["data_state"]["graph_background"] is None
    response = client.post(f"/api/sessions/{sid}/chat/stream", headers=headers(), json={"message": "Explain this graph."})
    assert response.status_code == 200, response.text
    assert FakeServiceClient.runtime_sessions[sid]["session_state"]["data_state"]["graph_background"] is None


def test_tool_load_removes_old_background():
    state = {"active_dataset_id": "novel", "data_state": {
        "active_dataset_id": "novel", "graph_background": cases.background_for_dataset(novel()),
        "available_constructed_datasets": [{"dataset_id": "other", "source_format": "csv"}],
    }}
    async def load(**kwargs):
        return {"success": True, "dataset_id": kwargs["dataset_id"], "revision": 1}
    asyncio.run(construction_tool_hook("load_constructed_graph", load,
        {"dataset_id": "other"}, SimpleNamespace(session_state=state)))
    assert state["active_dataset_id"] == "other"
    assert state["data_state"]["graph_background"] is None


def test_background_rule_keeps_language_and_evidence_requirements():
    text = "\n".join(instructions_for(reasoning_mode="direct", planning_mode="auto", response_language="English"))
    assert "graph_background" in text
    assert "Use English for this turn" in text
    assert "不能替代工具证据" in text

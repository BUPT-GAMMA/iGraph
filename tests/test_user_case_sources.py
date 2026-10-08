"""Bundled raw sources must be visible, editable and scoped like uploaded datasets."""
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

import backend.app as backend_app
from backend.config import BackendConfig
from backend.user_cases import USER_CASES, installed_source
from graph_engine.finance_preset import install_finance_preset
from graph_engine.novel_preset import install_novel_preset
from graph_engine.user_case_sources import seed_construction
from graph_data_service.construction import ConstructionStore, ConstructionError
from graph_data_service.graph_data import GraphData


def install(root):
    install_finance_preset(root)
    install_novel_preset(root)


@pytest.mark.parametrize("case", USER_CASES, ids=lambda case: case["id"])
def test_source_matches_saved_graph_and_seed_preserves_user_edits(tmp_path, case):
    install(tmp_path)
    path, descriptor = installed_source(tmp_path / "server_datasets", case)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == descriptor["sha256"]
    store = ConstructionStore(tmp_path)
    first = seed_construction(store, "alice", "dataset", path, descriptor["filename"], case["id"])
    expected = GraphData.load(str(tmp_path / "server_datasets" / case["filename"]))
    assert first["status"] == "ready"
    assert first["node_count"] == expected.num_nodes
    assert first["edge_count"] == expected.num_edges
    assert store.graph("alice", "dataset", saved_only=True)["graph"] == expected.graph_metadata["construction_graph"]
    graph = store.graph("alice", "dataset")["graph"]
    edit = {"op": "reverse_edges", "edge_ids": [graph["edges"][0]["id"]]}
    saved = store.optimize("alice", "dataset", 1, [edit], commit=True)
    install(tmp_path)
    second = seed_construction(store, "alice", "dataset", path, descriptor["filename"], case["id"])
    assert second["revision"] == saved["revision"]
    assert store.graph("alice", "dataset")["graph"] == saved["graph"]
    wrong = tmp_path / descriptor["filename"]
    wrong.write_bytes(b"different source")
    with pytest.raises(ConstructionError, match="示例源文件"):
        seed_construction(store, "alice", "another", wrong, descriptor["filename"], case["id"])


def test_personal_api_installs_sources_once_with_real_engine_authorization(tmp_path, monkeypatch):
    from graph_data_service.app import GraphDataService, create_app as graph_app
    from graph_data_service.construction_access import ConstructionAccessClient
    from backend.service_client import ServiceClient

    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("GRAPH_ML_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("BACKEND_SERVER_DATASETS_DIR", str(tmp_path / "server_datasets"))
    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    install(tmp_path)
    supported = tmp_path / "supported.txt"
    supported.write_text("admin\nalice\n", encoding="utf-8")
    config = BackendConfig(host="127.0.0.1", port=0, agent_runtime_url="http://runtime", graph_engine_url="http://graph",
        agent_id="igraph-agent", request_timeout=30, stream_timeout=30, users_file=tmp_path / "users.json",
        projects_file=tmp_path / "projects.json", supported_ids_file=supported, admin_user_id="admin",
        default_admin_model="test", default_user_model="test", custom_models_file=tmp_path / "custom.json")
    store = ConstructionStore(tmp_path)
    engine = GraphDataService(construction_store=store, storage_backend="local", migrate_storage=False)
    engine_app = graph_app(engine)

    class RoutedClient(ServiceClient):
        async def request(self, method, path, *, headers=None, params=None, json_body=None, **kwargs):
            if self.base_url == "http://runtime":
                assert method == "GET" and path == "/sessions"
                return {"data": []}
            assert self.base_url == "http://graph"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=engine_app), base_url="http://graph") as client:
                response = await client.request(method, path, headers=headers, params=params, json=json_body)
                response.raise_for_status()
                return response.json()

    monkeypatch.setattr(backend_app, "ServiceClient", RoutedClient)
    app = backend_app.create_app(config)
    engine._construction_access = ConstructionAccessClient("http://bff", transport=httpx.ASGITransport(app=app))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bff", headers={"X-User-ID": "admin"}) as client:
            async def sources(**kwargs):
                response = await client.get("/api/personal-datasets", **kwargs)
                assert response.status_code == 200, response.text
                return response.json()["datasets"]
            rows = await sources()
            assert {row["metadata"]["user_case_id"] for row in rows} == {"finance", "tianlongbabu"}
            assert {row["file_type"] for row in rows} == {"csv", "txt"}
            assert all(row["project_id"] is None and row["status"] == "ready" for row in rows)
            ids = {row["dataset_id"] for row in rows}
            assert {row["dataset_id"] for row in await sources()} == ids
            other = await sources(headers={"X-User-ID": "alice"})
            assert not ids.intersection(row["dataset_id"] for row in other)
            for row in rows:
                endpoint = "/api/personal-datasets/" + row["dataset_id"]
                state = await client.get(endpoint + "/construction")
                assert state.status_code == 200 and state.json()["status"] == "ready", state.text
                graph = await client.get(endpoint + "/construction/graph")
                assert graph.status_code == 200 and graph.json()["graph"]["nodes"], graph.text
                preview = await client.get(endpoint + "/construction/source-preview")
                assert preview.status_code == 200, preview.text
                assert (await client.get(endpoint + "/construction", headers={"X-User-ID": "alice"})).status_code == 404
                if row["file_type"] == "csv":
                    assert len(preview.json()["tables"]) == 3
                    schema = await client.get(endpoint + "/construction/schema")
                    assert len(schema.json()["schema"]["source_tables"]) == 3
            selected = rows[0]
            endpoint = "/api/personal-datasets/" + selected["dataset_id"]
            rename = await client.patch(endpoint, json={"name": "我的金融数据"})
            assert rename.status_code == 200, rename.text
            assert next(row for row in await sources() if row["dataset_id"] == selected["dataset_id"])["name"] == "我的金融数据"
            removed = await client.delete(endpoint)
            assert removed.status_code == 200, removed.text
            assert store.status("admin", selected["dataset_id"])["status"] == "not_built"
            assert {row["dataset_id"] for row in await sources()} == ids - {selected["dataset_id"]}
            assert len(await sources(headers={"X-User-ID": "alice"})) == 2
    asyncio.run(scenario())


def test_chinese_source_filename_keeps_txt_extension(tmp_path):
    from backend.project_datasets import ProjectDatasetDirectory
    directory = ProjectDatasetDirectory(tmp_path / "datasets.json", tmp_path / "sources")
    row = directory.create(project_id=None, user_id="alice", name="天龙八部", original_filename="天龙八部.txt", content="段誉".encode())
    assert row.file_type == "txt" and row.status == "source_ready"
    assert Path(row.storage_path).suffix == ".txt"
    assert row.original_filename == "天龙八部.txt"

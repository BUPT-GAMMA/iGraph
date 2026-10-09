import json
from pathlib import Path

import pytest

from backend.project_datasets import ProjectDatasetDirectory
from graph_data_service.construction import ConstructionStore, ConstructionError
from graph_data_service.construction_import import copy_constructed_graph
from test_projects import client, headers, FakeServiceClient


def test_import_snapshot_is_independent_and_preserves_properties(tmp_path):
    source = tmp_path / "tables.csv"
    source.write_text("id,name\n1,A\n", encoding="utf-8")
    store = ConstructionStore(tmp_path)
    store.register_algorithm("csv", lambda *_: {"directed": True,
        "nodes": [{"id": "1", "type": "Person", "properties": {"id": 1, "name": "A"}}], "edges": []})
    draft = store.build("alice", "personal", source, source.name)
    # Only saved construction results can be imported.
    store.optimize("alice", "personal", draft["revision"], [], commit=True)
    source_dir = store._directory("alice", "personal")
    report = {"source_schema": {"tables": [{
        "name": "people", "columns": ["id", "manager_id", "name"], "primary_key": ["id"],
        "column_types": {"id": "INTEGER", "manager_id": "INTEGER", "name": "TEXT"},
        "foreign_keys": [{"columns": ["manager_id"], "ref_table": "people", "ref_columns": ["id"]}],
        "rows": [{"id": "private record"}], "create_sql": "private source SQL",
    }]}, "queries": ["private query"]}
    (source_dir / "evolution-report.json").write_text(json.dumps(report), encoding="utf-8")
    expected_schema = store.schema("alice", "personal")["schema"]["source_tables"]
    result = copy_constructed_graph(store, "alice", "personal", "project-copy", source, source.name)
    # Saving the draft publishes revision 2; the independent copy starts again at 1.
    assert (result["source_revision"], result["revision"]) == (2, 1)
    assert store.graph("alice", "project-copy")["graph"] == store.graph("alice", "personal")["graph"]
    store.delete("alice", "personal")
    assert store.graph("alice", "project-copy")["graph"]["nodes"][0]["properties"]["name"] == "A"
    assert store.schema("alice", "project-copy")["schema"]["source_tables"] == expected_schema
    assert expected_schema[0]["primary_key"] == ["id"]
    assert expected_schema[0]["foreign_keys"][0]["ref_table"] == "people"
    target_dir = store._directory("alice", "project-copy")
    assert not (target_dir / "evolution-report.json").exists()
    assert "private" not in json.dumps(store._read(target_dir, "project-copy")["source_tables"])
    # A copy is a complete source for another import, without the original report.
    copy_constructed_graph(store, "alice", "project-copy", "second-copy", source, source.name)
    store.delete("alice", "project-copy")
    assert store.schema("alice", "second-copy")["schema"]["source_tables"] == expected_schema
    assert store.graph("alice", "second-copy")["graph"]["nodes"][0]["properties"]["name"] == "A"
    with pytest.raises(ConstructionError):
        copy_constructed_graph(store, "alice", "second-copy", "second-copy", source, source.name)
    with pytest.raises(ConstructionError):
        copy_constructed_graph(store, "bob", "second-copy", "other", source, source.name)


def test_import_api_filters_ownership_and_rolls_back_failure(client, monkeypatch, tmp_path):
    project = client.post("/api/projects", headers=headers(), json={"name": "Target"}).json()["project"]
    pid = project["project_id"]
    source = client.post("/api/personal-datasets", headers=headers(), files={"file": ("source.csv", b"id\n1\n", "text/csv")}, data={"description": "Customer entities"}).json()["dataset"]
    source_id = source["dataset_id"]
    # An unbuilt source must never be offered or accepted.
    assert client.get(f"/api/projects/{pid}/constructed-datasets", headers=headers()).json()["datasets"] == []
    assert client.post(f"/api/projects/{pid}/datasets/import-constructed", headers=headers(), json={"source_dataset_id": source_id}).status_code == 409
    directory = ProjectDatasetDirectory(tmp_path / "project_datasets.json", tmp_path / "projects")
    directory.update(source_id, "admin", status="ready", metadata={"construction": {"status": "ready", "revision": 2}, "csv_bundle": {"tables": []}})
    assert len(client.get(f"/api/projects/{pid}/constructed-datasets", headers=headers()).json()["datasets"]) == 1
    before = len(directory.list(pid, "admin"))
    # The default fake does not publish a graph: failed copies are removed.
    assert client.post(f"/api/projects/{pid}/datasets/import-constructed", headers=headers(), json={"source_dataset_id": source_id}).status_code == 502
    assert len(directory.list(pid, "admin")) == before
    original_request = FakeServiceClient.request
    async def publish(self, method, path, **kwargs):
        if not self.is_runtime and method == "POST" and path.endswith("/import"):
            return {"success": True, "status": "ready", "revision": 1, "source_revision": 2, "node_count": 1, "edge_count": 0}
        return await original_request(self, method, path, **kwargs)
    monkeypatch.setattr(FakeServiceClient, "request", publish)
    response = client.post(f"/api/projects/{pid}/datasets/import-constructed", headers=headers(), json={"source_dataset_id": source_id})
    assert response.status_code == 200
    copied = response.json()["dataset"]
    assert copied["dataset_id"] != source_id
    assert copied["project_id"] == pid
    assert copied["description"] == "Customer entities"
    assert copied["metadata"]["csv_bundle"] == {"tables": []}
    assert copied["metadata"]["construction"]["revision"] == 1
    assert response.json()["default_dataset_id"] == copied["dataset_id"]
    # Project sources cannot masquerade as personal graph sources.
    assert client.post(f"/api/projects/{pid}/datasets/import-constructed", headers=headers(), json={"source_dataset_id": copied["dataset_id"]}).status_code == 404
    original_path = Path(directory.get(source_id, "admin").storage_path)
    copied_path = Path(directory.get(copied["dataset_id"], "admin", pid).storage_path)
    assert copied_path != original_path and copied_path.read_bytes() == original_path.read_bytes()


def test_failed_engine_cleanup_keeps_recoverable_metadata(client, monkeypatch, tmp_path):
    from fastapi import HTTPException
    pid = client.post("/api/projects", headers=headers(), json={"name": "Target"}).json()["project"]["project_id"]
    source_id = client.post("/api/personal-datasets", headers=headers(), files={"file": ("source.csv", b"id\n1\n", "text/csv")}).json()["dataset"]["dataset_id"]
    directory = ProjectDatasetDirectory(tmp_path / "project_datasets.json", tmp_path / "projects")
    directory.update(source_id, "admin", status="ready", metadata={"construction": {"status": "ready", "revision": 1}})
    original_request = FakeServiceClient.request
    async def failing(self, method, path, **kwargs):
        if not self.is_runtime and path.startswith("/construction/"):
            raise HTTPException(503, "engine unavailable")
        return await original_request(self, method, path, **kwargs)
    monkeypatch.setattr(FakeServiceClient, "request", failing)
    assert client.post(f"/api/projects/{pid}/datasets/import-constructed", headers=headers(), json={"source_dataset_id": source_id}).status_code == 503
    failed = directory.list(pid, "admin")
    assert len(failed) == 1 and failed[0].status == "failed"
    assert Path(failed[0].storage_path).is_file()
    assert directory.get(source_id, "admin").status == "ready"


def test_dataset_description_can_be_updated_without_renaming(client):
    source = client.post("/api/personal-datasets", headers=headers(), files={"file": ("source.csv", b"id\n1\n", "text/csv")}, data={"description": "Initial description"}).json()["dataset"]
    assert source["description"] == "Initial description"
    url = f'/api/personal-datasets/{source["dataset_id"]}'
    response = client.patch(url, headers=headers(), json={"description": "PK: id"})
    assert response.status_code == 200
    assert response.json()["dataset"]["name"] == source["name"]
    assert response.json()["dataset"]["description"] == "PK: id"
    assert client.patch(url, headers=headers(), json={"description": "x" * 4001}).status_code == 422

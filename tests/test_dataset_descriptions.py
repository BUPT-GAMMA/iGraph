import io
import json

import pytest

from backend.project_datasets import ProjectDatasetDirectory
from graph_data_service.construction_schema import source_schema_tables


def test_description_persists_and_remains_owner_scoped(tmp_path):
    directory = ProjectDatasetDirectory(tmp_path / "datasets.json", tmp_path / "sources")
    dataset = directory.create(project_id=None, user_id="alice", name="People", original_filename="people.csv",
                               content=b"id,name\n1,Alice\n", description="  Customer entities  ")
    assert directory.get(dataset.dataset_id, "alice").description == "Customer entities"
    assert directory.update(dataset.dataset_id, "bob", description="changed") is None
    assert directory.update(dataset.dataset_id, "alice", project_id="other", description="changed") is None
    directory.update(dataset.dataset_id, "alice", description="Updated source context")
    reloaded = ProjectDatasetDirectory(directory.metadata_path, directory.storage_root)
    assert reloaded.get(dataset.dataset_id, "alice").description == "Updated source context"
    directory.update(dataset.dataset_id, "alice", description="")
    assert directory.get(dataset.dataset_id, "alice").description == ""
    with pytest.raises(ValueError, match="DATASET_DESCRIPTION_INVALID"):
        directory.update(dataset.dataset_id, "alice", description="a" * 4001)
    assert directory.get(dataset.dataset_id, "alice").description == ""


def test_csv_bundle_description_keeps_engine_manifest_compatible(tmp_path):
    import zipfile
    directory = ProjectDatasetDirectory(tmp_path / "datasets.json", tmp_path / "sources")
    schema = {"version": "csv_bundle.v1", "description": "Customers and their orders", "tables": [
        {"file_name": "customers.csv", "name": "customers", "columns": ["id"], "primary_key": ["id"], "foreign_keys": []},
    ]}
    dataset = directory.create_csv_bundle(project_id=None, user_id="alice", name="Customers", original_filename="bundle.csv",
                                         files=[("customers.csv", io.BytesIO(b"id\n1\n"))], schema=schema)
    assert directory.get(dataset.dataset_id, "alice").description == schema["description"]
    with zipfile.ZipFile(dataset.storage_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert set(manifest) == {"version", "tables"}
    assert manifest["tables"][0]["primary_key"] == ["id"]


def test_legacy_dataset_records_default_to_empty_description(tmp_path):
    directory = ProjectDatasetDirectory(tmp_path / "datasets.json", tmp_path / "sources")
    dataset = directory.create(project_id=None, user_id="alice", name="People", original_filename="people.csv", content=b"id\n1\n")
    payload = json.loads(directory.metadata_path.read_text(encoding="utf-8"))
    del payload["datasets"][0]["description"]
    directory.metadata_path.write_text(json.dumps(payload), encoding="utf-8")
    assert directory.get(dataset.dataset_id, "alice").description == ""


def test_public_source_schema_contains_only_structure_not_records_or_sql(tmp_path):
    table = {"name": "orders", "columns": ["order_id", "customer_id", "amount"], "primary_key": ["order_id"],
             "column_types": {"order_id": "INTEGER", "amount": "REAL"},
             "foreign_keys": [{"columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["id"], "private": "secret"}],
             "rows": [{"amount": 99}], "create_sql": "DEFAULT 'secret'", "samples": ["secret"]}
    (tmp_path / "evolution-report.json").write_text(json.dumps({"source_schema": {"tables": [table], "domain": "secret"}, "queries": ["secret"]}), encoding="utf-8")
    tables = source_schema_tables(tmp_path)
    assert tables[0]["primary_key"] == ["order_id"]
    assert tables[0]["foreign_keys"] == [{"columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["id"]}]
    assert "secret" not in json.dumps(tables)
    assert "rows" not in tables[0]
    assert source_schema_tables(tmp_path / "missing") == []
    retained = source_schema_tables(tmp_path / "missing", [table])
    assert retained == tables
    assert "secret" not in json.dumps(retained)
    # Reports take precedence over a retained snapshot when both are available.
    assert source_schema_tables(tmp_path, [{**table, "name": "old-copy"}]) == tables

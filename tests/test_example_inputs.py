import gzip, hashlib, json
from pathlib import Path
import pytest
from graph_engine.example_inputs import install_example_inputs
from backend.project_datasets import ProjectDatasetDirectory
import backend.example_inputs as inputs


def bundle(tmp_path):
    resources=tmp_path/"resources";resources.mkdir()
    pieces=[b"CREATE TABLE people (id INT);\n",b"INSERT INTO people VALUES (1);\n"]
    entry={"id":"test-sql","name":"Test SQL","description":"Build it","filename":"test.sql",
           "metadata":{},"size_bytes":sum(map(len,pieces)),"sha256":hashlib.sha256(b"".join(pieces)).hexdigest(),"parts":[]}
    for i,raw in enumerate(pieces):
        name=f"{i}.gz";packed=gzip.compress(raw);(resources/name).write_bytes(packed)
        entry["parts"].append({"file":name,"sha256":hashlib.sha256(packed).hexdigest()})
    (resources/"manifest.json").write_text(json.dumps({"inputs":[entry]}))
    return resources,entry,b"".join(pieces)


def test_offline_install_user_copy_and_deletion_are_idempotent(tmp_path,monkeypatch):
    resources,entry,raw=bundle(tmp_path)
    root=tmp_path/"data";install_example_inputs(root,resources=resources)
    monkeypatch.setattr(inputs,"RESOURCES",resources)
    directory=ProjectDatasetDirectory(root/"catalog.json",root/"users")
    inputs.ensure_example_inputs(directory,root/"server_datasets","alice")
    rows=directory.list(None,"alice");assert len(rows)==1
    row=rows[0];assert row.status=="source_ready" and row.metadata["construction"]["revision"]==0
    assert Path(row.storage_path).read_bytes()==raw
    Path(row.storage_path).write_bytes(b"user edit")
    install_example_inputs(root,resources=resources)
    inputs.ensure_example_inputs(directory,root/"server_datasets","alice")
    assert len(directory.list(None,"alice"))==1
    assert Path(row.storage_path).read_bytes()==b"user edit"
    inputs.ensure_example_inputs(directory,root/"server_datasets","bob")
    assert directory.list(None,"bob")[0].dataset_id!=row.dataset_id
    directory.delete(row.dataset_id,"alice",project_id=None)
    inputs.ensure_example_inputs(directory,root/"server_datasets","alice")
    assert directory.list(None,"alice")==[]


def test_corrupt_part_is_not_published(tmp_path):
    resources,entry,_=bundle(tmp_path)
    (resources/entry["parts"][0]["file"]).write_bytes(b"bad")
    with pytest.raises(ValueError,match="checksum"):
        install_example_inputs(tmp_path/"data",resources=resources)
    assert not (tmp_path/"data/server_datasets/.example-inputs"/entry["id"]/entry["sha256"]).exists()


def test_manifest_rejects_path_traversal(tmp_path):
    resources,entry,_=bundle(tmp_path);entry["filename"]="../escape.sql"
    (resources/"manifest.json").write_text(json.dumps({"inputs":[entry]}))
    with pytest.raises(ValueError,match="name"):
        install_example_inputs(tmp_path/"data",resources=resources)


def test_finance_risk_saved_graph_and_full_finance_values(tmp_path):
    from graph_engine.bundled_presets import install_bundled_preset
    from graph_data_service.graph_data import GraphData
    from graph_data_service.finance import analyze_finance
    resources=Path(__file__).resolve().parents[1]/"graph_engine/resources/finance_risk"
    install_bundled_preset(tmp_path,resources=resources)
    g=GraphData.load(str(tmp_path/"server_datasets/finance-risk.npz"))
    assert (g.num_nodes,g.num_edges)==(19436,26872)
    graph=g.graph_metadata["construction_graph"]
    summary=analyze_finance(graph,"snapshot",query="合成示例青岛工程技术00044控股有限公司",display_limit=1,max_seconds=60)
    target=summary["result"]["direct_ownership_summaries"][0]
    assert (target["direct_shareholder_count"],target["institution_shareholder_count"],target["individual_shareholder_count"])==(5,3,2)
    assert summary["completeness"]["display_truncated"]
    first=analyze_finance(graph,"first_holder",target_id="SYN-ENT-000044",max_seconds=60)
    assert first["result"]["first_holder_id"]=="SYN-ENT-000040"
    assert first["result"]["direct_ownership_total"]=="0.79"
    total=analyze_finance(graph,"economic_interest",root_id="SYN-ENT-000001",target_id="SYN-ENT-000044",max_seconds=60)
    assert total["result"]["requested_interest"]["interest"]=="0.34849495876800"

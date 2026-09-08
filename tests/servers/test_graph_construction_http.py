"""Real HTTP and session persistence contract for dataset property graphs."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
import httpx

from backend.project_datasets import ProjectDatasetRecord
from graph_data_service.construction_access import ConstructionAccessClient
from tests.fixtures.construction_scope import authority
from fastapi.testclient import TestClient

from graph_data_service.app import GraphDataService, create_app
from graph_data_service.construction import ConstructionStore
from graph_data_service.graph_data import GraphData
from graph_data_service.session import GraphSession


GRAPH = {"directed": True, "nodes": [
    {"id": "alice", "type": "Person", "properties": {"phone": "123", "nested": {"active": True}}},
    {"id": "bob", "type": "Person", "properties": {"name": "Bob"}},
], "edges": [{"id": "knows-1", "source": "alice", "target": "bob", "type": "KNOWS",
               "properties": {"since": 2020, "nested": {"verified": True}}}]}


@pytest.fixture
def construction_http(tmp_path, monkeypatch):
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("IGRAPH_CONTROL_SERVICE_KEY", "construction-test-secret")
    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    monkeypatch.setenv("NEBULA_ENABLED", "false")
    store = ConstructionStore(tmp_path)
    service = GraphDataService(construction_store=store, storage_backend="local", migrate_storage=False)
    source = tmp_path / "people.csv"
    source.write_text("id,phone\nalice,123\n", encoding="utf-8")
    headers = {"X-iGraph-Service-Key": "construction-test-secret", "X-User-ID": "alice"}
    auth, directory, _ = authority(tmp_path, {sid: {"session_id": sid, "user_id": "alice", "session_state": {}}
        for sid in ("session-one", "session-two", "analysis")})
    directory._write({did: ProjectDatasetRecord(dataset_id=did, user_id="alice", project_id=None, name=did,
        original_filename="people.csv", file_type="csv", storage_path=str(source)) for did in ("ds-people", "ds-analysis", "ds-invalid-weight")})
    service._construction_access = ConstructionAccessClient("http://authority", transport=httpx.ASGITransport(app=auth))
    headers["X-iGraph-Construction-Scope"] = "personal"
    with TestClient(create_app(service)) as client:
        yield client, service, store, source, headers


def build_ready(client, store, source, headers):
    store.register_algorithm("csv", lambda _source, _options: copy.deepcopy(GRAPH))
    response = client.post("/construction/ds-people/build", headers=headers,
                           json={"source_path": str(source), "source_filename": "people.csv", "options": {}})
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_construction_http_requires_service_owner_and_confined_source(construction_http, tmp_path):
    client, _service, _store, source, headers = construction_http
    assert client.get("/construction/capabilities").status_code == 403
    assert client.get("/construction/capabilities", headers={"X-iGraph-Service-Key": headers["X-iGraph-Service-Key"]}).status_code == 401
    assert client.get("/construction/capabilities", headers=headers).json()["schema"] == "construction_graph.v1"
    assert client.get("/construction/ds-people", headers=headers).json()["status"] == "not_built"
    response = client.post("/construction/ds-people/source", headers=headers,
                           json={"source_path": str(tmp_path.parent / "outside.csv"), "source_filename": "people.csv"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "invalid_source"
    response = client.post("/construction/ds-people/build", headers=headers,
                           json={"source_path": str(source), "source_filename": "people.csv"})
    assert response.status_code == 200
    assert response.json()["status"] == "algorithm_unavailable"
    assert response.json()["revision"] == 0
    assert client.get("/construction/ds-people/graph", headers=headers).status_code == 409


def test_construction_http_preview_commit_reload_properties_and_session_snapshot(construction_http, tmp_path):
    client, service, store, source, headers = construction_http
    build_ready(client, store, source, headers)
    loaded = client.post("/sessions/session-one/graph/load_construction", headers=headers, json={"dataset_id": "ds-people"})
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["construction_revision"] == 1
    session = service.get_required("session-one", "alice")
    assert session.get_graph().graph_metadata["construction_graph"] == GRAPH
    snapshot = GraphData.load(str(session._snapshots_dir / "v001.npz"))
    assert snapshot.graph_metadata["construction_graph"] == GRAPH
    network = snapshot.to_networkx()
    assert network.nodes["alice"]["properties"]["nested"] == {"active": True}
    assert network.edges["alice", "bob"]["properties"]["since"] == 2020
    info = client.get("/sessions/session-one/graph/info", headers=headers).json()
    assert info["construction_dataset_id"] == "ds-people"
    assert info["graph_info"]["construction_revision"] == 1
    visual = client.post("/sessions/session-one/graph/visualize", headers=headers, json={"arguments": {}}).json()
    assert next(node for node in visual["nodes"] if node["id"] == "alice")["attrs"]["properties"]["phone"] == "123"
    assert visual["edges"][0]["properties"]["since"] == 2020
    operation = {"base_revision": 1, "operations": [{"op": "reverse_edges", "edge_ids": ["knows-1"]}]}
    preview = client.post("/construction/ds-people/preview", headers=headers, json=operation)
    assert preview.status_code == 200
    assert preview.json()["revision"] == 2
    assert store.graph("alice", "ds-people")["revision"] == 1
    committed = client.post("/construction/ds-people/commit", headers=headers, json=operation)
    assert committed.status_code == 200
    assert committed.json()["graph"] == preview.json()["graph"]
    # Dataset commits do not replace the graph underneath an existing session.
    assert client.get("/sessions/session-one/graph/info", headers=headers).json()["construction_revision"] == 1
    conflict = client.post("/construction/ds-people/commit", headers=headers, json=operation)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "revision_conflict"
    assert client.post("/sessions/session-one/graph/load_construction", headers=headers, json={"dataset_id": "ds-people"}).json()["construction_revision"] == 2
    assert client.get("/sessions/session-one/graph/info", headers=headers).json()["construction_revision"] == 2
    restored = GraphSession("session-one", "alice")
    assert restored.load_snapshot()
    assert restored.get_graph().graph_metadata["construction_revision"] == 2
    assert ConstructionStore(tmp_path).graph("alice", "ds-people")["revision"] == 2
    assert any(row["dataset_id"] == "ds-people" for row in client.get("/datasets", headers=headers).json()["datasets"])
    assert client.post("/sessions/session-two/tools/load_dataset", headers=headers,
        json={"arguments": {"dataset_id": "ds-people"}}).json()["code"] == "USE_CONSTRUCTED_GRAPH_LOADER"
    assert service.do_load_constructed_graph("session-two", "ds-people", user_id="alice")["construction_revision"] == 2


def test_construction_http_invalid_batch_is_atomic_and_cross_user_isolated(construction_http, monkeypatch):
    client, service, store, source, headers = construction_http
    build_ready(client, store, source, headers)
    invalid = {"base_revision": 1, "operations": [{"op": "reverse_edges", "edge_ids": ["knows-1"]},
                {"op": "delete", "target": "node", "ids": ["missing"]}]}
    response = client.post("/construction/ds-people/commit", headers=headers, json=invalid)
    assert response.status_code == 422
    assert response.json()["detail"]["operation_index"] == 1
    assert store.graph("alice", "ds-people")["graph"] == GRAPH
    other = {**headers, "X-User-ID": "bob"}
    assert client.get("/construction/ds-people", headers=other).status_code == 404
    assert client.get("/construction/ds-people/graph", headers=other).status_code == 404
    monkeypatch.setattr(service._job_manager, "list_jobs", lambda _sid: [SimpleNamespace(user_id="alice", status="running")])
    assert client.post("/sessions/session-one/graph/load_construction", headers=headers,
                       json={"dataset_id": "ds-people"}).status_code == 409
    assert client.delete("/construction/ds-people", headers=other).status_code == 404
    assert client.delete("/construction/ds-people", headers=headers).json()["deleted"] is True
    assert source.exists()


def test_parallel_relationships_retain_their_properties_in_networkx():
    from graph_data_service.construction import to_graph_data
    graph = copy.deepcopy(GRAPH)
    graph["edges"].append({"id": "knows-2", "source": "alice", "target": "bob", "type": "WORKS_WITH",
                           "properties": {"since": 2024, "key": "business-key", "u_for_edge": "kept"}})
    network = to_graph_data({"dataset_id": "ds-people", "revision": 1, "graph": graph}).to_networkx()
    assert network.is_multigraph()
    assert network.number_of_edges() == 2
    assert {data["id"] for *_ends, data in network.edges(data=True)} == {"knows-1", "knows-2"}
    assert network.edges["alice", "bob", 1]["properties"]["key"] == "business-key"


def test_property_analysis_http_uses_same_relationships_after_conversion_and_reload(construction_http):
    from tests.servers.test_property_graph_analysis import property_graph
    client, service, store, source, headers = construction_http
    document = property_graph([('ab2', 'A', 'B', 2), ('ab7', 'A', 'B', 7),
                               ('bc', 'B', 'C', 12), ('ca', 'C', 'A', 1)]).graph_metadata['construction_graph']
    store.register_algorithm('csv', lambda _source, _options: copy.deepcopy(document))
    built = client.post('/construction/ds-analysis/build', headers=headers,
                        json={'source_path': str(source), 'source_filename': 'people.csv'})
    assert built.json()['status'] == 'ready'
    loaded = client.post('/sessions/analysis/graph/load_construction', headers=headers, json={'dataset_id': 'ds-analysis'})
    assert loaded.status_code == 200 and loaded.json()['success']

    def call(name, arguments):
        response = client.post(f'/sessions/analysis/tools/{name}', headers=headers, json={'arguments': arguments})
        assert response.status_code == 200, response.text
        return response.json()

    for directed in (True, False):
        shortest = call('shortest_path', {'source': 'A', 'target': 'C'})
        assert shortest['success'] and shortest['total_length'] == (14 if directed else 1)
        assert sum(e['weight'] for e in shortest['edges']) == shortest['total_length']
        flow = call('maximum_flow', {'source': 'A', 'sink': 'C'})
        cut = call('minimum_cut', {'source': 'A', 'sink': 'C'})
        assert flow['success'] and cut['success']
        assert flow['max_flow_value'] == cut['min_cut_value'] == (9 if directed else 10)
        assert call('node_centrality', {'centrality_type': 'eigenvector'})['success']
        assert call('maximum_cycle', {})['code'] == 'UNSUPPORTED_MULTIGRAPH_OPERATION'
        info = call('get_node_info', {'node_name': 'B'})
        assert info['degree'] == 3 and info['incident_edges_total'] == 3
        edge_info = call('get_edge_info', {'src_name': 'A', 'dst_name': 'B'})
        assert edge_info['edge_count'] == 2
        visual = client.post('/sessions/analysis/graph/visualize', headers=headers, json={'arguments': {}}).json()
        assert len(visual['edges']) == 4
        restored = GraphSession('analysis', 'alice')
        assert restored.load_snapshot()
        snapshot = restored.get_graph()
        assert snapshot.num_unique_edges == snapshot.to_networkx().number_of_edges() == 4
        assert snapshot.degree_by_name('B') == 3
        if directed:
            converted = client.post('/sessions/analysis/graph/to_undirected', headers=headers)
            assert converted.status_code == 200 and converted.json()['success']
            assert not service.get_required('analysis', 'alice').get_graph().directed


def test_invalid_weight_cannot_publish_ready_construction(construction_http):
    client, _service, store, source, headers = construction_http
    graph = copy.deepcopy(GRAPH)
    graph['edges'][0]['properties']['weight'] = 'heavy'
    store.register_algorithm('csv', lambda _source, _options: graph)
    built = client.post('/construction/ds-invalid-weight/build', headers=headers,
                        json={'source_path': str(source), 'source_filename': 'people.csv'})
    assert built.status_code == 200 and built.json()['status'] == 'failed'
    assert client.get('/construction/ds-invalid-weight/graph', headers=headers).status_code == 409


def test_build_without_path_reuses_authorized_registered_source(construction_http):
    client, _service, store, source, headers = construction_http
    unregistered = client.post('/construction/ds-people/build', headers=headers, json={})
    assert unregistered.status_code == 409
    assert unregistered.json()['detail']['code'] == 'source_not_registered'
    registered = client.post('/construction/ds-people/source', headers=headers,
        json={'source_path': str(source), 'source_filename': 'people.csv'})
    assert registered.status_code == 200
    unavailable = client.post('/construction/ds-people/build', headers=headers, json={})
    assert unavailable.json()['status'] == 'algorithm_unavailable'
    store.register_algorithm('csv', lambda _source, _options: copy.deepcopy(GRAPH))
    built = client.post('/construction/ds-people/build', headers=headers, json={})
    assert built.status_code == 200 and built.json()['status'] == 'ready'
    reused = client.post('/construction/ds-people/build', headers=headers, json={})
    assert reused.status_code == 200 and reused.json()['reused']
    for body in ({'source_path': '\u0000'}, {'source_filename': 'other.csv'}):
        assert client.post('/construction/ds-people/build', headers=headers, json=body).status_code == 403

import asyncio
import httpx
import backend.app as backend_app
from backend.config import BackendConfig
from tests.test_server_dataset_catalog import _PreparedServerServiceClient


def test_bundled_examples_are_available_idempotent_and_user_owned(tmp_path, monkeypatch):
    server = tmp_path / "server"
    server.mkdir()
    for filename in ("finance-ownership.npz", "tianlongbabu-characters.npz"):
        (server / filename).write_bytes(b"catalog-test-asset")
    supported = tmp_path / "supported.txt"
    supported.write_text("admin\nreader\n", encoding="utf-8")
    monkeypatch.setenv("BACKEND_SERVER_DATASETS_DIR", str(server))
    monkeypatch.setenv("IGRAPH_RUN_CANCEL_ENABLED", "false")
    monkeypatch.setattr(backend_app, "ServiceClient", _PreparedServerServiceClient)
    _PreparedServerServiceClient.runtime_sessions = {}
    config = BackendConfig(host="127.0.0.1", port=0, agent_runtime_url="http://fake-runtime", graph_engine_url="http://fake-graph", agent_id="igraph-agent", request_timeout=1, stream_timeout=1,
        users_file=tmp_path/"users.json", projects_file=tmp_path/"projects.json", supported_ids_file=supported,
        admin_user_id="admin", default_admin_model="test", default_user_model="test", custom_models_file=tmp_path/"custom.json")
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=backend_app.create_app(config)), base_url="http://test", headers={"X-User-ID":"admin"}) as client:
            listing = (await client.get("/api/user-cases")).json()["cases"]
            assert len(listing) == 2 and all(item["available"] and len(item["questions"]) >= 3 for item in listing)
            for item in listing:
                english = item['translations']['en']
                assert english['name'] and english['description']
                assert len(english['questions']) == len(item['questions'])
            first = await client.post("/api/user-cases/finance/project")
            assert first.status_code == 200, first.text
            second = (await client.post("/api/user-cases/finance/project")).json()
            assert second["project"]["project_id"] == first.json()["project"]["project_id"]
            assert second["dataset"]["dataset_id"] == first.json()["dataset"]["dataset_id"]
            novel = (await client.post("/api/user-cases/tianlongbabu/project")).json()
            assert novel["project"]["default_dataset_id"] == novel["dataset"]["dataset_id"]
            assert novel["project"]["project_id"] != second["project"]["project_id"]
            other = await client.post("/api/user-cases/finance/project", headers={"X-User-ID":"reader"})
            assert other.status_code == 200, other.text
            assert other.json()["project"]["project_id"] == second["project"]["project_id"]
            assert (await client.post("/api/user-cases/missing/project")).status_code == 404
    asyncio.run(run())

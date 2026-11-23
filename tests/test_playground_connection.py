import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from agent_runtime.execution_store import ControlError, ExecutionStore
from agent_runtime.model_api import create_model_router
from agent_runtime.model_catalog import ModelCatalog
from unittest.mock import patch


class PlaygroundConnectionTest(unittest.TestCase):
    def test_personal_credentials_and_revision(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"IGRAPH_CONTROL_SERVICE_KEY": "service-only"}):
            root = Path(tmp)
            store = ExecutionStore(create_engine(f"sqlite:///{root / 'db'}", connect_args={"check_same_thread": False}))
            store.migrate()
            catalog = ModelCatalog(store, SimpleNamespace(remote_graph_secret_key="", remote_graph_secret_key_file=root / "key"))
            app = FastAPI()
            @app.exception_handler(ControlError)
            async def error(request, exc):
                return JSONResponse({"detail": {"code": exc.code}}, status_code=exc.status)
            app.include_router(create_model_router(catalog, None))
            url = "/internal/playground/connection"
            with TestClient(app) as client:
                client.headers.update({"X-User-ID": "alice", "X-iGraph-Service-Key": "service-only"})
                self.assertFalse(client.get(url).json()["configured"])
                form = {"revision": 0, "model": "gpt-5.4-mini", "base_url": "https://model.test/v1"}
                self.assertEqual(client.put(url, json=form).status_code, 422)
                key = "alice-private-example"
                saved = client.put(url, json={**form, "api_key": key})
                self.assertTrue(saved.json()["configured"])
                self.assertNotIn(key, saved.text)
                mid = saved.json()["config_id"]
                selected = next(m for m in catalog.list("alice")["models"] if m["config_id"] == mid)
                self.assertIsNone(selected["unavailable_reason"])
                self.assertEqual(selected["context_window"], 131072)
                self.assertNotIn(key, str(selected))
                self.assertEqual(catalog.resolve("alice", {"config_id": mid, "revision": selected["revision"]})["api_key"], key)
                self.assertFalse(any(m["config_id"] == mid for m in catalog.list("bob")["models"]))
                with store.transaction("alice", "__models__") as db:
                    self.assertNotIn(key, str(catalog._preference(db, "alice")))
                self.assertEqual(client.get(url + "/resolve?revision=1").json()["api_key"], key)
                self.assertEqual(client.put(url, json={**form, "revision": 1, "base_url": "https://other.test/v1"}).status_code, 422)
                client.headers.update({"X-User-ID": "bob"})
                self.assertFalse(client.get(url).json()["configured"])
                self.assertNotIn(key, client.get(url + "/resolve?revision=1").text)
                client.headers.update({"X-User-ID": "alice"})
                self.assertEqual(client.put(url, json={**form, "api_key": "stale"}).status_code, 409)
                self.assertFalse(client.delete(url + "?revision=1").json()["configured"])

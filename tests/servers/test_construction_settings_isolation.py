import pytest

from graph_engine.construction.settings import construction_identity, public_settings, read_settings, save_settings, ConstructionSettingsWrite
from graph_engine.construction.llm import ToolCallingLLM, ConstructionLLMError


def test_personal_connections_cannot_read_overwrite_or_inherit_owner_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("IGRAPH_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("GRAPH_CONSTRUCTION_API_KEY", "server-secret")
    monkeypatch.setenv("GRAPH_CONSTRUCTION_BASE_URL", "https://owner.example/v1")
    with construction_identity("admin"):
        save_settings(ConstructionSettingsWrite(model="owner-model", base_url="https://owner.example/v1", api_key="owner-key"))
    with construction_identity("u_alice"):
        assert not public_settings()["has_api_key"]
        assert not public_settings()["has_server_config"]
        with pytest.raises(ConstructionLLMError, match="unavailable"):
            ToolCallingLLM()
        save_settings(ConstructionSettingsWrite(model="alice-model", base_url="https://alice.example/v1", api_key="alice-key"))
        assert ToolCallingLLM()._key == "alice-key"
    with construction_identity("u_bob"):
        assert read_settings() == {}
    with construction_identity("admin"):
        assert ToolCallingLLM()._key == "owner-key"
        assert read_settings()["model"] == "owner-model"

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent_runtime.custom_models as runtime_custom_models
import agent_runtime.model_factory as model_factory
from agent_runtime.config import AgentRuntimeConfig
from backend.custom_models import CustomModelDirectory, CustomModelRecord, public_custom_model


@pytest.fixture(autouse=True)
def clear_runtime_custom_models():
    runtime_custom_models._CONFIGS.clear()
    yield
    runtime_custom_models._CONFIGS.clear()


@pytest.fixture()
def runtime_config(tmp_path: Path) -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        host="127.0.0.1",
        port=0,
        database_url="sqlite:///runtime.db",
        mcp_url="http://graph-engine",
        llm_api_key="fallback-key",
        llm_base_url="https://fallback.example/v1",
        default_model="qwen3.6-plus",
        embedding_model="embedding",
        embedding_api_key="embedding-key",
        embedding_base_url="https://embedding.example/v1",
        skills_root=tmp_path / "skills",
        telemetry=False,
        tracing=False,
        authorization=False,
    )


def test_custom_model_directory_ignores_missing_and_malformed_rows(tmp_path: Path):
    path = tmp_path / "custom-models.json"
    directory = CustomModelDirectory(path)
    assert directory.list("alice") == []

    path.write_text("not-json", encoding="utf-8")
    assert directory.list("alice") == []

    path.write_text(json.dumps([{"user_id": "alice", "model_id": "ignored"}]), encoding="utf-8")
    assert directory.list("alice") == []

    path.write_text(
        json.dumps(
            {
                "models": [
                    None,
                    {},
                    {"user_id": "alice", "model_id": "broken", "unexpected": True},
                    {
                        "user_id": "alice",
                        "model_id": "valid-model",
                        "display_name": "Valid",
                        "base_url": "https://models.example/v1",
                        "api_key": "secret",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    assert [row.model_id for row in directory.list(" alice ")] == ["valid-model"]


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (CustomModelRecord("", "custom", "Custom", "https://models.example", "key"), "模型 ID"),
        (CustomModelRecord("alice", "bad model", "Custom", "https://models.example", "key"), "模型 ID"),
        (CustomModelRecord("alice", "gpt-5-4", "Custom", "https://models.example", "key"), "内置模型"),
        (CustomModelRecord("alice", "custom", "Custom", "ftp://models.example", "key"), "Base URL"),
        (CustomModelRecord("alice", "custom", "Custom", "https://models.example", ""), "API Key"),
    ],
)
def test_custom_model_directory_validates_new_records(tmp_path: Path, record: CustomModelRecord, message: str):
    with pytest.raises(ValueError, match=message):
        CustomModelDirectory(tmp_path / "models.json").save(record)


def test_custom_model_directory_persists_updates_and_deletes_per_user(tmp_path: Path):
    directory = CustomModelDirectory(tmp_path / "models.json")
    created = directory.save(
        CustomModelRecord(
            user_id=" alice ",
            model_id=" team/model-v1 ",
            display_name="",
            base_url=" https://models.example/v1/ ",
            api_key=" secret ",
        )
    )

    assert created.user_id == "alice"
    assert created.model_id == "team/model-v1"
    assert created.display_name == "team/model-v1"
    assert created.base_url == "https://models.example/v1"
    assert created.api_key == "secret"
    assert directory.get("alice", "team/model-v1") == created
    assert directory.get("bob", "team/model-v1") is None
    assert directory.list("bob") == []
    assert public_custom_model(created) == {
        "model_id": "team/model-v1",
        "display_name": "team/model-v1",
        "provider": "openai-compatible",
        "base_url": "https://models.example/v1",
        "has_api_key": True,
        "created_at": created.created_at,
        "updated_at": created.updated_at,
    }

    updated = directory.save(
        CustomModelRecord(
            user_id="alice",
            model_id="team/model-v1",
            display_name="Team Model",
            base_url="https://new.example/v1/",
        )
    )
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.updated_at
    assert updated.api_key == "secret"
    assert updated.base_url == "https://new.example/v1"
    assert len(directory.list("alice")) == 1

    assert directory.delete("bob", "team/model-v1") is False
    assert directory.delete("alice", "missing") is False
    assert directory.delete(" alice ", " team/model-v1 ") is True
    assert directory.get("alice", "team/model-v1") is None


def test_custom_model_directory_removes_temporary_file_after_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    directory = CustomModelDirectory(tmp_path / "models.json")

    def fail_replace(_source: str, _destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("backend.custom_models.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        directory.save(CustomModelRecord("alice", "custom", "Custom", "https://models.example", "key"))
    assert list(tmp_path.glob("custom-models-*.json")) == []


def test_runtime_custom_model_registry_returns_copies_and_supports_removal():
    with pytest.raises(ValueError, match="缺少用户或模型 ID"):
        runtime_custom_models.register_custom_model("", {"model_id": "custom"})
    with pytest.raises(ValueError, match="缺少用户或模型 ID"):
        runtime_custom_models.register_custom_model("alice", {})

    runtime_custom_models.register_custom_model(
        " alice ",
        {"model_id": " custom/v1 ", "base_url": " https://models.example/v1/ ", "api_key": " key ", "provider": ""},
    )
    stored = runtime_custom_models.get_custom_model("alice", "custom/v1")
    assert stored == {
        "model_id": "custom/v1",
        "base_url": "https://models.example/v1",
        "api_key": "key",
        "provider": "openai-compatible",
    }
    assert stored is not None
    stored["api_key"] = "changed"
    assert runtime_custom_models.get_custom_model("alice", "custom/v1")["api_key"] == "key"
    assert runtime_custom_models.get_custom_model("bob", "custom/v1") is None

    runtime_custom_models.remove_custom_model("bob", "missing")
    runtime_custom_models.remove_custom_model(" alice ", " custom/v1 ")
    assert runtime_custom_models.get_custom_model("alice", "custom/v1") is None


def test_create_model_uses_custom_endpoint_without_exposing_network(monkeypatch: pytest.MonkeyPatch, runtime_config: AgentRuntimeConfig):
    created: list[dict[str, str]] = []

    class FakeOpenAILike:
        def __init__(self, **kwargs: str) -> None:
            created.append(kwargs)

    monkeypatch.setattr(model_factory, "OpenAILike", FakeOpenAILike)
    model = model_factory.create_model(
        "custom/v1",
        runtime_config,
        {"model_id": " custom/v1 ", "base_url": "https://models.example/v1/", "api_key": " key "},
    )
    assert isinstance(model, FakeOpenAILike)
    assert created == [{"id": "custom/v1", "api_key": "key", "base_url": "https://models.example/v1"}]

    with pytest.raises(ValueError, match="ID 格式无效"):
        model_factory.create_model("bad model", runtime_config, {"model_id": "bad model", "base_url": "https://models.example", "api_key": "key"})
    with pytest.raises(ValueError, match="有效的 Base URL"):
        model_factory.create_model("custom", runtime_config, {"model_id": "custom", "base_url": "ftp://models.example", "api_key": "key"})
    with pytest.raises(ValueError, match="有效的 Base URL"):
        model_factory.create_model("custom", runtime_config, {"model_id": "custom", "base_url": "https://models.example", "api_key": ""})


@pytest.mark.parametrize(
    ("model_id", "key_name", "base_name"),
    [
        ("gpt-5-4", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
        ("claude-sonnet-4-6", "CLAUDE_API_KEY", "CLAUDE_BASE_URL"),
        ("deepseek-v4-pro", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
        ("doubao-seed-2-0-pro-260215", "ARK_API_KEY", "ARK_BASE_URL"),
        ("qwen3.6-plus", "QWEN_API_KEY", "QWEN_BASE_URL"),
    ],
)
def test_create_model_resolves_each_builtin_provider(
    monkeypatch: pytest.MonkeyPatch,
    runtime_config: AgentRuntimeConfig,
    model_id: str,
    key_name: str,
    base_name: str,
):
    captured: dict[str, str] = {}
    all_names = {
        "OPENAI_API_KEY", "LLM_API_KEY", "OPENAI_BASE_URL", "LLM_BASE_URL",
        "CLAUDE_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_BASE_URL", "ANTHROPIC_BASE_URL",
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "ARK_API_KEY", "DOUBAO_API_KEY",
        "ARK_BASE_URL", "DOUBAO_BASE_URL", "QWEN_API_KEY", "DASHSCOPE_API_KEY",
        "QWEN_BASE_URL", "DASHSCOPE_BASE_URL",
    }
    for name in all_names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key_name, " provider-key ")
    monkeypatch.setenv(base_name, " https://provider.example/v1 ")

    class FakeOpenAILike:
        def __init__(self, **kwargs: str) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(model_factory, "OpenAILike", FakeOpenAILike)
    model_factory.create_model(model_id, runtime_config)
    assert captured == {"id": model_id, "api_key": "provider-key", "base_url": "https://provider.example/v1"}


def test_builtin_model_falls_back_to_config_and_rejects_missing_endpoint(monkeypatch: pytest.MonkeyPatch, runtime_config: AgentRuntimeConfig):
    captured: dict[str, str] = {}
    for name in ("QWEN_API_KEY", "DASHSCOPE_API_KEY", "LLM_API_KEY", "QWEN_BASE_URL", "DASHSCOPE_BASE_URL", "LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    class FakeOpenAILike:
        def __init__(self, **kwargs: str) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(model_factory, "OpenAILike", FakeOpenAILike)
    model_factory.create_model("", runtime_config, custom_model="not-a-dict")
    assert captured == {"id": "qwen3.6-plus", "api_key": "fallback-key", "base_url": "https://fallback.example/v1"}

    empty_config = AgentRuntimeConfig(**{**runtime_config.__dict__, "llm_api_key": "", "llm_base_url": ""})
    with pytest.raises(RuntimeError, match="缺少 API Key"):
        model_factory.create_model("qwen3.6-plus", empty_config)


def test_allowed_model_enforces_builtin_claims_and_custom_identity(runtime_config: AgentRuntimeConfig):
    assert model_factory.allowed_model("qwen3.6-plus", {}, runtime_config) == "qwen3.6-plus"
    assert model_factory.allowed_model("gpt-5-4", {"allowed_models": ["gpt-5-4", "unknown"]}, runtime_config) == "gpt-5-4"
    assert model_factory.allowed_model("qwen3.6-plus", "unverified", runtime_config) == "qwen3.6-plus"
    with pytest.raises(PermissionError, match="not allowed"):
        model_factory.allowed_model("gpt-5-4", {"allowed_models": ["qwen3.6-plus", "unknown"]}, runtime_config)

    custom = {"model_id": "custom/v1"}
    assert model_factory.allowed_model("custom/v1", {}, runtime_config, custom) == "custom/v1"
    with pytest.raises(ValueError, match="不匹配"):
        model_factory.allowed_model("custom/v2", {}, runtime_config, custom)
    with pytest.raises(ValueError, match="不匹配"):
        model_factory.allowed_model("bad model", {}, runtime_config, {"model_id": "bad model"})

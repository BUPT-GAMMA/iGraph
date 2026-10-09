import json
import os

import pytest

from graph_engine.construction.llm import ConstructionLLMError, ToolCallingLLM, construction_models, selected_model
from graph_engine.construction.settings import ConstructionSettingsWrite, public_settings, read_settings, save_settings, settings_path


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv('IGRAPH_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('GRAPH_CONSTRUCTION_BASE_URL', 'https://server.test/v1')
    monkeypatch.setenv('GRAPH_CONSTRUCTION_API_KEY', 'server-secret')
    monkeypatch.setenv('GRAPH_CONSTRUCTION_RESOLVED_IP', '192.0.2.8')
    monkeypatch.delenv('GRAPH_CONSTRUCTION_MODEL', raising=False)


def test_public_settings_and_models_never_return_credentials():
    result = save_settings(ConstructionSettingsWrite(model='team/custom-model', base_url='https://gateway.test/', api_key='private-secret'))
    assert result == {'model': 'team/custom-model', 'has_base_url': True, 'has_api_key': True, 'has_server_config': True}
    assert selected_model() == 'team/custom-model'
    assert selected_model('team/custom-model') == 'team/custom-model'
    assert 'team/custom-model' in construction_models()
    assert 'secret' not in json.dumps(public_settings())
    assert 'gateway.test' not in json.dumps(public_settings())
    if os.name != 'nt':
        assert settings_path().stat().st_mode & 0o777 == 0o600


def test_blank_fields_preserve_config_and_custom_connection_reaches_llm():
    save_settings(ConstructionSettingsWrite(model='deepseek-v4-pro', base_url='https://gateway.test', api_key='private-secret'))
    save_settings(ConstructionSettingsWrite(model='claude-opus-5-5'))
    client = ToolCallingLLM()
    assert client.model == 'claude-opus-5-5'
    assert client._url == 'https://gateway.test/v1/chat/completions'
    assert client._key == 'private-secret'
    assert client._resolved_ip is None
    assert 'private-secret' not in json.dumps(client.audit_metadata())


def test_clearing_saved_key_does_not_fall_back_to_environment():
    save_settings(ConstructionSettingsWrite(model='m', base_url='https://gateway.test/v1', api_key='private-secret'))
    save_settings(ConstructionSettingsWrite(model='m', clear_api_key=True))
    assert public_settings()['has_api_key'] is False
    with pytest.raises(ConstructionLLMError, match='unavailable'):
        ToolCallingLLM()


def test_model_only_keeps_existing_environment_connection():
    save_settings(ConstructionSettingsWrite(model='claude-opus-5-5'))
    client = ToolCallingLLM()
    assert client._url == 'https://server.test/v1/chat/completions'
    assert client._key == 'server-secret'
    assert client._resolved_ip == '192.0.2.8'


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://u:p@gateway.test', 'https://gateway.test?key=secret', 'https://gateway.test/#x', 'https://gateway.test:invalid', 'https://bad host/v1'])
def test_invalid_url_does_not_change_saved_connection(url):
    save_settings(ConstructionSettingsWrite(model='m', base_url='https://good.test/v1', api_key='private-secret'))
    before = read_settings()
    with pytest.raises(ValueError):
        save_settings(ConstructionSettingsWrite(model='m', base_url=url))
    assert read_settings() == before


@pytest.mark.parametrize('values', [{'base_url': 'https://gateway.test'}, {'api_key': 'key'}])
def test_first_configuration_requires_complete_connection(values):
    with pytest.raises(ValueError):
        save_settings(ConstructionSettingsWrite(model='m', **values))
    assert read_settings() == {}

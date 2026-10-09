from pathlib import Path

import pytest

from backend.users import SingleUserDirectory
from tests.test_projects import client, FakeServiceClient


def test_owner_is_available_without_a_seed_file(tmp_path: Path):
    directory = SingleUserDirectory(tmp_path / 'users.json', tmp_path / 'absent.txt', 'admin')
    assert [user.user_id for user in directory.list()] == ['admin']
    assert directory.get('admin').enabled
    assert directory.get('reader') is None


def test_existing_multi_user_registry_is_not_destroyed(tmp_path: Path):
    users = tmp_path / 'users.json'
    original = '{"users": [{"user_id": "admin", "enabled": false}, {"user_id": "reader"}]}'
    users.write_text(original)
    directory = SingleUserDirectory(users, tmp_path / 'absent.txt', 'admin')
    assert [user.user_id for user in directory.list()] == ['admin']
    assert directory.get('admin').enabled
    assert users.read_text() == original


def test_public_api_ignores_missing_or_stale_account_headers(client):
    workspace = client.get('/api/workspace')
    assert workspace.status_code == 200
    assert workspace.json()['single_user'] is True
    assert workspace.json()['user_id'] == 'admin'
    project = client.post('/api/projects', json={'name': 'Shared workspace'}).json()['project']
    assert project['user_id'] == 'admin'
    assert client.get('/api/projects', headers={'X-User-ID': 'old-user'}).json()['projects'][0]['project_id'] == project['project_id']
    assert client.post('/api/admin/users', json={'user_id': 'another'}).status_code == 404
    assert client.delete('/api/admin/users/admin').status_code == 404
    assert client.post('/api/auth/login', json={'username': 'admin', 'password': 'unused'}).status_code in {404, 405}


def test_cross_site_writes_are_rejected_without_login_cookie(client):
    assert client.post('/api/projects', headers={'Origin': 'https://evil.example'}, json={'name': 'x'}).status_code == 403
    assert client.post('/api/projects', headers={'Sec-Fetch-Site': 'cross-site'}, json={'name': 'x'}).status_code == 403
    assert client.get('/api/projects', headers={'Origin': 'https://evil.example'}).status_code == 200
    accepted = client.post('/api/projects', headers={'Origin': 'http://testserver'}, json={'name': 'same-origin'})
    assert accepted.status_code == 200, accepted.text


@pytest.mark.parametrize(('locale', 'message', 'language'), [
    ('zh-CN,zh;q=0.9,en;q=0.7', 'Please inspect the graph.', 'Chinese'),
    ('en-US,en;q=0.9', '请用中文回答并检查图。', 'English'),
])
def test_chat_captures_interface_language(client, locale, message, language):
    import json
    sid = client.post('/api/sessions', json={}).json()['session_id']
    response = client.post(f'/api/sessions/{sid}/chat/stream', headers={'Accept-Language': locale}, json={'message': message})
    assert response.status_code == 200
    factory = json.loads(FakeServiceClient.runtime_stream_calls[-1]['data']['factory_input'])
    assert factory['response_language'] == language
    # Language belongs to each turn, so a subsequent switch must not reuse it.
    other = 'en' if language == 'Chinese' else 'zh'
    response = client.post(f'/api/sessions/{sid}/chat/stream', headers={'Accept-Language': other}, json={'message': message})
    assert response.status_code == 200
    factory = json.loads(FakeServiceClient.runtime_stream_calls[-1]['data']['factory_input'])
    assert factory['response_language'] != language

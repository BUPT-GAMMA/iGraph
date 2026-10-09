"""Attachment dataset IDs and selection state must reach the native runtime."""
from test_construction_api import engine
from test_projects import FakeServiceClient, client, headers


def test_attachment_dataset_identity_is_scoped_and_does_not_survive_unrelated_messages(client, engine):
    session = client.post('/api/sessions', headers=headers(), json={}).json()['session_id']
    upload = client.post(f'/api/sessions/{session}/upload', headers=headers(),
                         files={'file': ('relations.csv', b'raw source')}).json()
    dataset_id = upload['dataset']['dataset_id']
    assert upload['construction_required'] is True
    assert upload['dataset']['status'] == 'source_ready'
    response = client.post(f'/api/sessions/{session}/chat/stream', headers=headers(), json={
        'message': '这个文件现在可以构图吗',
        'attachments': [{'file_token': upload['file_token'], 'filename': 'relations.csv'}],
    })
    assert response.status_code == 200
    state = FakeServiceClient.runtime_sessions[session]['session_state']['data_state']
    assert state['active_dataset_id'] is None
    assert state['construction_sources'] == [{
        'dataset_id': dataset_id, 'name': 'relations', 'source_filename': 'relations.csv',
        'source_format': 'csv', 'status': 'not_built',
    }]
    message = FakeServiceClient.runtime_stream_calls[-1]['data']['message']
    assert '使用 load_graph_from_upload 自动检测并加载' not in message
    assert '构图' in message
    assert client.post(f'/api/sessions/{session}/chat/stream', headers=headers(), json={'message': '你好'}).status_code == 200
    assert FakeServiceClient.runtime_sessions[session]['session_state']['data_state']['construction_sources'] == []


def test_clearing_dataset_selection_removes_stale_construction_state(client, engine):
    states, _ = engine
    dataset = client.post('/api/personal-datasets', headers=headers(),
                          files={'file': ('graph.csv', b'raw source')}).json()['dataset']
    dataset_id = dataset['dataset_id']
    states[('admin', dataset_id)] = dict(dataset_id=dataset_id, status='ready', revision=2,
                                       node_count=2, edge_count=1, directed=True)
    client.get(f'/api/personal-datasets/{dataset_id}/construction', headers=headers())
    session = client.post('/api/sessions', headers=headers(), json={'active_dataset_id': dataset_id}).json()['session_id']
    FakeServiceClient.runtime_sessions[session]['session_state']['data_state'] = {
        'active_dataset_id': dataset_id, 'construction_status': 'ready', 'construction_revision': 2,
    }
    response = client.patch(f'/api/sessions/{session}/dataset', headers=headers(), json={'active_dataset_id': None})
    assert response.status_code == 200
    state = FakeServiceClient.runtime_sessions[session]['session_state']
    assert state['active_dataset_id'] is None
    assert state['data_state']['active_dataset_id'] is None
    assert state['data_state']['construction_status'] is None
    assert state['data_state']['construction_revision'] is None

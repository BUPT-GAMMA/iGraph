"""Deletion preserves unrelated artifacts and refuses unsafe late mutations."""
from pathlib import Path

import pytest
from fastapi import HTTPException

from graph_data_service.graph_ml.artifact_store import ArtifactStore


def test_artifact_cleanup_preserves_unknown_and_personal_files(tmp_path):
    store = ArtifactStore(str(tmp_path))
    store.record_owner('alice:project-chat', 'alice', 'project-chat', 'project')
    store.record_owner('alice:personal-chat', 'alice', 'personal-chat', None)
    project = store.create_model_dir('alice:project-chat', 'test')
    personal = store.create_model_dir('alice:personal-chat', 'test')
    legacy = tmp_path / 'alice:project-chat' / 'legacy-model'
    legacy.mkdir(parents=True)
    unknown = tmp_path / 'unknown-legacy-model'
    unknown.mkdir()
    assert store.ownership_report()['unattributed_directories'] == ['unknown-legacy-model']
    with pytest.raises(ValueError, match='ARTIFACT_OWNER_CONFLICT'):
        store.delete_project('alice', 'project', ['personal-chat'])
    assert Path(project.model_dir).exists() and Path(personal.model_dir).exists()
    store.delete_project('alice', 'project', [])  # Receipts survive a deleted chat.
    assert not Path(project.model_dir).exists() and not legacy.exists()
    assert Path(personal.model_dir).exists() and unknown.exists()
    assert store.delete_project('alice', 'project', []) == 0


def test_permanent_graph_deletion_fences_late_creation_but_reset_is_reusable(tmp_path, monkeypatch):
    monkeypatch.setenv('IGRAPH_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('GRAPH_ML_ARTIFACT_ROOT', str(tmp_path / 'models'))
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'false')
    from graph_data_service.app import GraphDataService
    async def unused(_):
        raise AssertionError('No model call should be made')
    service = GraphDataService(text_graph_llm_factory=unused, migrate_storage=False)
    try:
        service.get_or_create('chat', 'alice')
        service.delete_session('chat', 'alice')
        service.get_or_create('chat', 'alice')
        service.delete_session('chat', 'alice', permanent=True)
        with pytest.raises(HTTPException) as blocked:
            service.get_or_create('chat', 'alice')
        assert blocked.value.status_code == 409
        assert service.get_or_create('chat', 'bob').user_id == 'bob'
    finally:
        service._process_pool.shutdown(cancel_futures=True)


def test_corrupt_project_metadata_never_overwrites_deletion_intent(tmp_path):
    from backend.projects import ProjectDirectory
    path = tmp_path / 'projects.json'
    damaged = '{"projects": [{"delete_status": "requested"'
    path.write_text(damaged)
    with pytest.raises(ValueError):
        ProjectDirectory(path).create('alice', 'New')
    assert path.read_text() == damaged

"""Real worker algorithms, staged artifacts and cancellation exit evidence."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from graph_data_service.app import GraphDataService
from graph_data_service.execution_host import JobContext, job_context
from graph_data_service.isolated_compute import run_isolated
from graph_engine.paths import safe_path_component
from tests.servers.test_graph_ml_legacy_regression import _legacy_graph, _request


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv('IGRAPH_DATA_ROOT', str(tmp_path / 'data'))
    monkeypatch.setenv('GRAPH_ML_ARTIFACT_ROOT', str(tmp_path / 'models'))
    monkeypatch.setenv('IGRAPH_RUN_CANCEL_ENABLED', 'false')
    svc = GraphDataService(storage_backend='local', migrate_storage=False)
    async def boundary(_context):
        pass
    svc._execution_host = SimpleNamespace(before_start=boundary)
    svc.get_or_create('s', user_id='alice').set_graph(_legacy_graph())
    yield svc
    svc._process_pool.shutdown(wait=True, cancel_futures=True)
    svc._nebula.close()


async def isolated(svc, tool, args, progress=None):
    ctx = JobContext('alice', 's', str(uuid4()), 1, str(uuid4()), str(uuid4()))
    token = job_context.set(ctx)
    try:
        return await run_isolated(svc, 's', 'alice', tool, args, progress)
    finally:
        job_context.reset(token)


def test_graph_publication_and_history_avoid_parent_graph_sized_io(service, monkeypatch):
    from graph_data_service.graph_data import GraphData
    from graph_data_service.session import GraphSession
    from graph_data_service.graph_snapshot import GraphSnapshot
    def forbidden(*_args, **_kwargs):
        raise AssertionError('parent must not deserialize or serialize a graph')
    async def scenario():
        session = service.get_required('s', user_id='alice')
        first = session.graph_version
        monkeypatch.setattr(GraphData, 'load', forbidden)
        monkeypatch.setattr(GraphData, 'save', forbidden)
        result = await isolated(service, 'load_graph', {'edge_text': 'A B\nB C', 'directed': True})
        assert result['success'], result
        assert isinstance(session.get_graph(), GraphSnapshot)
        second = session.graph_version
        restored = GraphSession('s', user_id='alice')
        assert restored.load_snapshot() and restored.graph_version == second
        assert restored.graph_summary().num_nodes == 3
        exported = await isolated(service, 'save_as_dataset', {'name': 'bounded-export'})
        assert exported['success'], exported
        dataset = service._get_user_dataset_registry('alice').get(exported['dataset']['dataset_id'])
        assert dataset.node_count == 3
        loaded = await isolated(service, 'load_graph_version', {'graph_version': first})
        assert loaded['success'] and session.graph_version == first, loaded
        loaded = await isolated(service, 'load_graph_version', {'graph_version': second})
        assert loaded['success'] and session.graph_version == second, loaded
        undone = await isolated(service, 'undo_graph', {})
        assert undone['success'] and session.graph_version > second, undone
        discarded = await isolated(service, 'discard_graph_version', {'graph_version': session.graph_version, 'fallback_graph_version': second})
        assert discarded['success'] and session.graph_version == second, discarded
        assert all(row['graph_version'] != discarded['discarded_graph_version'] for row in session.list_versions())
    asyncio.run(scenario())


def test_failed_manifest_commit_preserves_old_graph_and_hides_orphan(service, monkeypatch):
    import graph_data_service.session as module
    from graph_data_service.session import GraphSession
    async def scenario():
        session = service.get_required('s', user_id='alice')
        # First migrate the fixture onto the authoritative manifest format.
        result = await isolated(service, 'load_graph', {'edge_text': 'A B', 'directed': True})
        assert result['success'], result
        version, graph = session.graph_version, session.get_graph()
        original = module.atomic_json
        def fail_manifest(path, value, **kwargs):
            if path.name == 'graph_versions.json':
                raise OSError('injected commit failure')
            return original(path, value, **kwargs)
        monkeypatch.setattr(module, 'atomic_json', fail_manifest)
        with pytest.raises(OSError, match='injected commit'):
            await isolated(service, 'load_graph', {'edge_text': 'X Y\nY Z', 'directed': True})
        assert session.graph_version == version and session.get_graph() is graph
        assert {row['graph_version'] for row in session.list_versions()} == {1, version}
        restored = GraphSession('s', user_id='alice')
        assert restored.load_snapshot() and restored.graph_version == version
        assert restored.graph_summary().num_nodes == 2
    asyncio.run(scenario())


def test_graph_capability_gate_requires_service_identity_and_supported_storage(service, monkeypatch):
    import httpx
    from graph_data_service.app import create_app
    monkeypatch.setenv('IGRAPH_CONTROL_SERVICE_KEY', 'capability-test-only')
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)), base_url='http://test') as client:
            assert (await client.get('/internal/capabilities')).status_code == 403
            headers = {'X-iGraph-Service-Key': 'capability-test-only'}
            response = await client.get('/internal/capabilities', headers=headers)
            assert response.status_code == 200
            assert response.json() == {'run_cancel_v1': True, 'job_cancel_v1': True}
            service._storage_backend = 'nebula'
            response = await client.get('/internal/capabilities', headers=headers)
            assert response.json() == {'run_cancel_v1': False, 'job_cancel_v1': False}
    asyncio.run(scenario())


def test_nx_worker_matches_existing_algorithm(service):
    async def scenario():
        expected = await service.dispatch_tool('s', 'degree_analysis', {}, user_id='alice')
        actual = await isolated(service, 'degree_analysis', {})
        assert actual == expected
    asyncio.run(scenario())


def test_bounded_graph_info_reuses_validated_result_and_invalidates_by_version(service, monkeypatch):
    from graph_data_service.worker_process import WorkerProcess
    from unittest.mock import patch
    async def scenario():
        first = await isolated(service, 'get_graph_info', {})
        ctx = JobContext('alice', 's', str(uuid4()), 1, str(uuid4()), str(uuid4()))
        token = job_context.set(ctx)
        try:
            with patch.object(WorkerProcess, 'run', side_effect=AssertionError('unexpected worker launch')):
                second = await service.dispatch_tool('s', 'get_graph_info', {}, user_id='alice')
                assert second == first
            service.get_required('s', user_id='alice')._graph_version += 1
            with patch.object(WorkerProcess, 'run', side_effect=AssertionError('worker launch')):
                with pytest.raises((AssertionError, FileNotFoundError)):
                    await service.dispatch_tool('s', 'get_graph_info', {}, user_id='alice')
        finally:
            job_context.reset(token)
    asyncio.run(scenario())


def test_worker_entry_point_is_independent_of_service_working_directory(service, tmp_path, monkeypatch):
    async def scenario():
        expected = await isolated(service, 'degree_analysis', {})
        # Uvicorn can be launched outside the repository, as browser integration
        # does. The managed entry point must still resolve its own package.
        monkeypatch.chdir(tmp_path)
        actual = await isolated(service, 'degree_analysis', {})
        assert actual == expected
    asyncio.run(scenario())


def test_worker_input_stays_pinned_when_retention_unlinks_source(service):
    async def scenario():
        expected = await isolated(service, 'degree_analysis', {})
        session = service.get_required('s', user_id='alice')
        source = session._snapshots_dir / f'v{session.graph_version:03d}.npz'
        async def started(_context, _identity):
            source.unlink()  # Only this fixture's temporary snapshot.
        service._execution_host.worker_started = started
        actual = await isolated(service, 'degree_analysis', {})
        assert actual == expected
    asyncio.run(scenario())


def test_document_publication_does_not_cross_filesystems(service, monkeypatch):
    import os
    from pathlib import Path
    monkeypatch.setenv('EMBEDDING_PROVIDER', 'lexical')
    original = os.replace
    session_root = service.get_required('s', user_id='alice')._session_dir
    def replace_on_same_volume(source, target):
        source, target = Path(source), Path(target)
        if target.is_relative_to(session_root):
            assert source.is_relative_to(session_root), 'document publication must stage on its target filesystem'
        return original(source, target)
    monkeypatch.setattr(os, 'replace', replace_on_same_volume)
    result = asyncio.run(isolated(service, 'upload_document', {'content': 'separate model and session volumes'}))
    assert result['success'], result
    assert not list((session_root / '.staging').iterdir())


def test_worker_train_publishes_complete_model_and_infer_can_read_it(service):
    async def scenario():
        result = await isolated(service, 'train', _request('node_classification').model_dump(mode='json'))
        assert result['success'], result
        root = service._graph_ml._store.root_dir
        from pathlib import Path
        published = Path(root) / safe_path_component('alice:s') / result['model_id']
        assert (published / 'meta.json').is_file()
        assert (published / 'metrics.json').is_file()
        response = await isolated(service, 'infer', {'model_id': result['model_id'], 'graph_version': 1,
                                                    'query': {'nodes': ['n0', 'n1']}, 'infer_config': {'device': 'cpu', 'result_limit': 2}})
        assert response['success'], response
        assert response['returned_count'] == 2
        assert not list((Path(root) / '.staging').iterdir())
    asyncio.run(scenario())


def test_cancelled_training_never_publishes_partial_artifact(service):
    async def scenario():
        from pathlib import Path
        entered = asyncio.Event()
        req = _request('node_classification').model_dump(mode='json')
        req['train_config'].update(epochs=100000, early_stop_patience=0)
        task = asyncio.create_task(isolated(service, 'train', req, lambda _: entered.set()))
        await asyncio.wait_for(entered.wait(), 40)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        root = Path(service._graph_ml._store.root_dir)
        assert not list((root / '.staging').iterdir())
        assert not list(root.glob('*/mdl_*'))
    asyncio.run(scenario())


def test_upload_worker_commits_graph_version_and_keeps_source_binding(service, tmp_path):
    async def scenario():
        source = tmp_path / 'graph.npz'
        _legacy_graph().save(str(source))
        session = service.get_required('s', user_id='alice')
        session.register_upload('token', 'graph.npz', str(source))
        result = await isolated(service, 'load_graph_from_upload', {'filename': 'graph.npz'})
        assert result['success'], result
        assert session.graph_version == 2
        assert session.get_graph().num_nodes == 8
        assert session._load_version_records()[-1]['source']['file_token'] == 'token'
    asyncio.run(scenario())


def test_explicit_classification_load_remains_required_and_worker_bundle_is_safe(service, tmp_path):
    async def scenario():
        import torch
        from tests.fixtures.graph_classification import make_graph_classification_payload
        from graph_data_service.worker_data import LazyGraphs
        source = tmp_path / 'hiv.pt'
        torch.save(make_graph_classification_payload(), source)
        session = service.get_required('s', user_id='alice')
        session.register_upload('hiv-token', 'hiv.pt', str(source))
        result = await isolated(service, 'load_graph_classification_dataset', {'filename': 'hiv.pt', 'dataset_profile': 'hiv'})
        assert result['success'], result
        dataset_id = result['dataset']['dataset_id']
        bundle = service._graph_ml.resolve_graph_classification_dataset('alice:s', dataset_id)
        assert isinstance(bundle.graphs, LazyGraphs)
        assert len(bundle.graphs) == 6 and bundle.graphs[0].x.numel() == 2
        sample = await isolated(service, 'load_graph_classification_graph', {'dataset_id': dataset_id, 'graph_index': 0})
        assert sample['success'], sample
        assert session.graph_version == 2
        assert service._graph_ml.resolve_graph_classification_dataset('alice:s', dataset_id) is bundle
        service._graph_ml.release_session('alice:s')
        with pytest.raises(ValueError):
            await isolated(service, 'train', {**_request('graph_classification').model_dump(mode='json'), 'data': {'dataset_id': dataset_id}})
    asyncio.run(scenario())


def test_document_worker_preserves_history_across_processes_and_atomic_publication(service, monkeypatch):
    monkeypatch.setenv('EMBEDDING_PROVIDER', 'lexical')
    async def scenario():
        old = await isolated(service, 'upload_document', {'content': 'alpha network document'})
        assert old['success'], old
        session = service.get_required('s', user_id='alice')
        previous = (session._session_dir / 'rag_manifest.json').read_text()
        new = await isolated(service, 'upload_document', {'content': 'unique beta research'})
        assert new['success'], new
        assert (session._session_dir / 'rag_manifest.json').read_text() != previous
        result = await isolated(service, 'retrieve_context', {'query': 'alpha network document', 'top_k': 2})
        assert result['success'] and len(result['chunks']) == 2, result
        assert result['chunks'][0]['text'] == 'alpha network document'
        assert session.graph_version == 1  # A document commit never replaces the graph.
    asyncio.run(scenario())


@pytest.mark.parametrize('tool', ['upload_document', 'retrieve_context', 'load_graph_classification_dataset'])
def test_independent_worker_succeeds_while_active_graph_is_replaced(service, tmp_path, monkeypatch, tool):
    """Resource-independent work must not acquire an implicit graph dependency."""
    from graph_data_service.graph_data import GraphData
    from graph_data_service.worker_process import WorkerProcess
    import numpy as np
    monkeypatch.setenv('EMBEDDING_PROVIDER', 'lexical')

    async def scenario():
        session = service.get_required('s', user_id='alice')
        if tool == 'retrieve_context':
            loaded = await isolated(service, 'upload_document', {'content': 'independent document content'})
            assert loaded['success'], loaded
            arguments = {'query': 'independent document content'}
        elif tool == 'load_graph_classification_dataset':
            import torch
            from tests.fixtures.graph_classification import make_graph_classification_payload
            source = tmp_path / 'parallel-hiv.pt'
            torch.save(make_graph_classification_payload(), source)
            session.register_upload('parallel-hiv', 'parallel-hiv.pt', str(source))
            arguments = {'filename': 'parallel-hiv.pt', 'dataset_profile': 'hiv'}
        else:
            arguments = {'content': 'independent document content'}

        original_version = session.graph_version
        async def graph_replaced_after_worker_start(_context, _identity):
            session.set_graph(GraphData(num_nodes=2, edge_index=np.array([[0], [1]])))
        service._execution_host.worker_started = graph_replaced_after_worker_start
        worker_run = WorkerProcess.run
        async def capture_input(worker, payload, *args, **kwargs):
            assert payload['graph_path'] is None, 'Independent tools must not copy/load a graph snapshot'
            return await worker_run(worker, payload, *args, **kwargs)
        monkeypatch.setattr(WorkerProcess, 'run', capture_input)

        result = await isolated(service, tool, arguments)
        assert result['success'], result
        assert session.graph_version == original_version + 1
        assert session.get_graph().num_nodes == 2
        if tool == 'retrieve_context':
            assert result['chunks'][0]['text'] == 'independent document content'
        elif tool == 'upload_document':
            assert (session._session_dir / 'rag_manifest.json').is_file()
        else:
            bundle = service._graph_ml.resolve_graph_classification_dataset('alice:s', result['dataset']['dataset_id'])
            assert len(bundle.graphs) == 6
    asyncio.run(scenario())


@pytest.mark.parametrize('tool,arguments', [
    ('degree_analysis', {}), ('load_graph', {'edge_text': 'A B\nB C'}),
])
def test_graph_worker_still_rejects_a_changed_input_version(service, tool, arguments):
    from graph_data_service.graph_data import GraphData
    import numpy as np
    async def scenario():
        session = service.get_required('s', user_id='alice')
        original_version = session.graph_version
        async def graph_replaced_after_worker_start(_context, _identity):
            session.set_graph(GraphData(num_nodes=2, edge_index=np.array([[0], [1]])))
        service._execution_host.worker_started = graph_replaced_after_worker_start
        with pytest.raises(ValueError, match='GRAPH_VERSION_CHANGED'):
            await isolated(service, tool, arguments)
        assert session.graph_version == original_version + 1
        assert session.get_graph().num_nodes == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('profile', ['hiv', 'pcba'])
@pytest.mark.parametrize('model', ['GCN', 'GAT', 'SAGE'])
def test_isolated_graph_classification_train_and_infer_keep_explicit_dataset_contract(service, tmp_path, profile, model):
    from graph_data_service.worker_data import write_bundle, read_bundle
    from tests.fixtures.graph_classification import make_trainable_graph_classification_bundle
    from tests.servers.test_graph_classification_train_infer import _request as classification_request
    from dataclasses import replace
    bundle = make_trainable_graph_classification_bundle(profile)
    directory = tmp_path / 'validated-dataset'
    write_bundle(bundle, directory, lambda: None)
    service._graph_ml._dataset_store._bundles['alice:s'] = read_bundle(directory)

    async def scenario():
        request = classification_request(model).model_dump(mode='json', exclude_unset=True)
        trained = await isolated(service, 'train', request)
        assert trained['success'], trained
        arguments = {'model_id': trained['model_id'], 'query': {'split': 'test'}, 'infer_config': {'device': 'cpu'}}
        expected = service._graph_ml.infer('alice:s', arguments)
        actual = await isolated(service, 'infer', arguments)
        assert expected['success'] and actual['success'], (expected, actual)
        assert actual['predictions'] == expected['predictions']
        assert actual['evaluation'] == expected['evaluation']
        resident = service._graph_ml._dataset_store._bundles['alice:s']
        service._graph_ml._dataset_store._bundles['alice:s'] = replace(resident, fingerprint='changed')
        with pytest.raises(ValueError, match='fingerprint|指纹'):
            await isolated(service, 'infer', arguments)
        service._graph_ml.release_session('alice:s')
        with pytest.raises(ValueError, match='数据集'):
            await isolated(service, 'infer', arguments)
        with pytest.raises(ValueError, match='模型不存在'):
            await isolated(service, 'infer', {**arguments, 'model_id': 'mdl_not_owned'})

    asyncio.run(scenario())


def test_property_relationship_analysis_survives_isolated_conversion_and_snapshot(service):
    from tests.servers.test_property_graph_analysis import property_graph
    from graph_data_service.compute import run_nx_tool
    from graph_data_service.session import GraphSession
    async def scenario():
        session = service.get_required('s', user_id='alice')
        session.set_graph(property_graph([('ab2', 'A', 'B', 2), ('ab7', 'A', 'B', 7),
                                          ('bc', 'B', 'C', 12), ('ca', 'C', 'A', 1)]))
        for directed in (True, False):
            network = session.get_graph().to_networkx()
            version = session.graph_version
            for name, args in [('shortest_path', {'source': 'A', 'target': 'C'}),
                               ('maximum_flow', {'source': 'A', 'sink': 'C'}),
                               ('node_centrality', {'centrality_type': 'eigenvector'}),
                               ('maximum_cycle', {})]:
                expected = run_nx_tool(name, network, args)
                actual = await isolated(service, name, args)
                assert actual == expected
                assert session.graph_version == version
            if directed:
                converted = await isolated(service, 'to_undirected', {})
                assert converted['success'], converted
                assert session.graph_version > version
                restored = GraphSession('s', user_id='alice')
                assert restored.load_snapshot()
                graph = restored.get_graph()
                assert not graph.directed
                assert graph.num_unique_edges == graph.to_networkx().number_of_edges() == 4
                assert graph.degree_by_name('B') == 3
    asyncio.run(scenario())

@pytest.mark.parametrize('imported', [False, True])
def test_isolated_graph_info_retains_snapshot_source_and_matches_http(service, imported):
    from graph_data_service.graph_data import GraphData
    from graph_data_service.session import UploadFileRecord
    from graph_engine.novel_preset import RESOURCES, FILENAME

    session = service.get_required('s', user_id='alice')
    path = RESOURCES / FILENAME
    session.set_graph(GraphData.load(str(path)))
    if imported:
        session.mark_version_upload_source(session.graph_version, UploadFileRecord('preset', path.name, str(path)))
    expected = service.do_get_graph_info('s', user_id='alice')
    actual = asyncio.run(isolated(service, 'get_graph_info', {}))
    for value in (actual, actual['graph_info']):
        assert value['construction_applicable'] is (not imported)
        assert value['construction_dataset_id'] == (None if imported else 'ds_tianlongbabu_v1')
    assert actual == expected

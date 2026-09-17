"""Fixed computation entry point. Only server-created immutable inputs enter here.

No database connection, shared session, client PID or arbitrary command crosses
the worker boundary. All published files remain in a per-job staging directory.
"""
from __future__ import annotations

import asyncio
import json
import signal
import sys
import threading
import os
from pathlib import Path
from types import SimpleNamespace
from graph_data_service.worker_signals import stopping, check_cancelled
from graph_data_service.worker_contract import ISOLATED_DATA_TOOLS

def emit(kind, value):
    sys.__stdout__.write(json.dumps({'type': kind, 'value': value}, ensure_ascii=False, default=str) + '\n')
    sys.__stdout__.flush()


def progress(value):
    check_cancelled()
    emit('progress', value)


def compute(payload):
    from graph_data_service.graph_data import GraphData
    from graph_data_service.compute import NX_TOOL_NAMES, run_nx_tool

    tool = payload['tool']
    data_tools = ISOLATED_DATA_TOOLS
    if tool not in NX_TOOL_NAMES | {'train', 'infer', 'load_graph_classification_dataset'} | data_tools:
        raise ValueError('WORKER_TOOL_NOT_ALLOWED')
    check_cancelled()
    graph = GraphData.load(payload['graph_path']) if payload.get('graph_path') else None
    check_cancelled()

    class ReadOnlySession:
        user_id = payload['user_id']
        session_id = payload['session_id']
        graph_version = payload['graph_version']

        def get_graph(self):
            return graph

        def resolve_upload_by_name(self, filename):
            record = payload.get('uploads', {}).get(filename)
            return SimpleNamespace(**record) if record else None

        def bind_graph_ml_dataset(self, **kwargs):
            # Parent commits the binding after verifying the execution boundary.
            pass

    session = ReadOnlySession()
    runtime_id = f'{session.user_id}:{session.session_id}' if session.user_id else session.session_id
    if tool in data_tools:
        from graph_data_service.worker_data import write_bundle
        import os
        os.environ['IGRAPH_DATA_ROOT'] = str(Path(payload['staging_root']) / 'runtime')
        os.environ['GRAPH_ML_ARTIFACT_ROOT'] = payload['staging_root']
        os.environ['IGRAPH_RUN_CANCEL_ENABLED'] = 'false'
        from graph_data_service.app import GraphDataService
        from graph_data_service.session import UploadFileRecord
        service = GraphDataService(storage_backend='local', migrate_storage=False)
        isolated_session = service.get_or_create(session.session_id, user_id=session.user_id)
        isolated_session._graph = graph
        isolated_session._graph_version = session.graph_version
        isolated_session._uploads_by_name = {name: UploadFileRecord(**record) for name, record in payload.get('uploads', {}).items()}
        isolated_session.llm_model = payload.get('llm_model', '')
        if payload.get('dataset_directory'):
            from graph_data_service.worker_data import read_bundle
            service._graph_ml._dataset_store._bundles[runtime_id] = read_bundle(payload['dataset_directory'])
        elif payload.get('loaded_dataset'):
            loaded = payload['loaded_dataset']
            bundle = service._graph_ml._dataset_store.load(runtime_id, isolated_session, loaded['filename'], loaded['profile'], progress_callback=progress)
            if bundle.fingerprint != loaded['fingerprint']:
                raise ValueError('DATASET_CHANGED')
        if tool in {'upload_document', 'retrieve_context'}:
            import shutil
            source = Path(payload['rag_directory'])
            for name in ('doc_index.json', 'doc_embeddings.npy', 'docs'):
                check_cancelled()
                old, new = source / name, isolated_session._session_dir / name
                if old.is_dir(): shutil.copytree(old, new, dirs_exist_ok=True)
                elif old.is_file(): shutil.copy2(old, new)

        async def execute():
            async def work():
                if tool in {'undo_graph', 'load_graph_version', 'discard_graph_version'}:
                    target = payload['history_target']
                    loaded = GraphData.load(target['path'])
                    check_cancelled()
                    isolated_session.set_graph(loaded, description='加载历史图版本')
                    return {'success': True, 'session_id': session.session_id,
                        **({'discarded_graph_version': int(payload['arguments']['graph_version'])} if tool == 'discard_graph_version' else {}),
                        'loaded_version': target['graph_version'], 'description': target.get('description', ''),
                        **service._graph_counts_payload(loaded)}
                if tool == 'save_as_dataset':
                    from graph_data_service.dataset_registry import DatasetRegistry
                    if graph is None:
                        return {'success': False, 'error': '尚未加载任何图数据。'}
                    registry = DatasetRegistry(Path(payload['staging_root']) / 'dataset-export')
                    meta = registry.create(str(payload['arguments'].get('name') or ''), str(payload['arguments'].get('description') or ''), graph)
                    for path in registry._dataset_dir(meta.dataset_id).rglob('*'):
                        check_cancelled()
                        if path.is_file():
                            with path.open('rb') as saved:
                                os.fsync(saved.fileno())
                    return {'success': True, 'dataset': dict(meta.to_dict(), scope='user' if session.user_id else 'global')}
                if tool == 'load_dataset':
                    loaded = GraphData.load(payload['dataset_load']['path'])
                    check_cancelled()
                    isolated_session.set_graph(loaded, description='加载数据集')
                    return {'success': True, 'session_id': session.session_id, 'backend': 'local',
                            'dataset_id': payload['dataset_load']['dataset_id'],
                            'dataset_version': payload['dataset_load']['version'],
                            **service._graph_counts_payload(loaded), 'features_omitted': [], 'warnings': []}
                result = await service.dispatch_tool(session.session_id, tool, payload['arguments'], user_id=session.user_id)
                if isinstance(result, dict) and result.get('async'):
                    jid = result['job_id']
                    while True:
                        check_cancelled()
                        for update in service._job_manager.drain_pending_progress(jid):
                            progress(update)
                        job = service._job_manager.status(jid)
                        if job.status in {'done', 'failed', 'cancelled'}:
                            if job.status != 'done':
                                raise ValueError(job.message)
                            result = {**result, **job.result, 'async': False}
                            break
                        await asyncio.sleep(.05)
                return result
            task = asyncio.create_task(work())
            try:
                while not task.done():
                    if stopping.is_set():
                        task.cancel()
                        service._job_manager.cancel_all()
                    await asyncio.wait((task,), timeout=.05)
                return await task
            finally:
                service._job_manager.cancel_all()
                await asyncio.gather(*(service._job_manager.wait(jid) for jid in list(service._job_manager._tasks)))
                service._process_pool.shutdown(wait=False, cancel_futures=True)
                service._nebula.close()
        result = asyncio.run(execute())
        check_cancelled()
        if tool == 'upload_document' and result.get('success'):
            import shutil
            target = Path(payload['staging_root']) / 'rag-result'
            target.mkdir()
            for name in ('doc_index.json', 'doc_embeddings.npy', 'docs'):
                check_cancelled()
                old, new = isolated_session._session_dir / name, target / name
                if old.is_dir(): shutil.copytree(old, new)
                elif old.is_file(): shutil.copy2(old, new)
            result['_worker_rag_result'] = True
        if isolated_session.graph_version != session.graph_version and isolated_session.get_graph() is not None:
            from graph_data_service.graph_snapshot import snapshot_summary
            snapshot = Path(payload['staging_root']) / 'graph-result.npz'
            isolated_session.get_graph().save(str(snapshot))
            with snapshot.open('rb') as saved:
                os.fsync(saved.fileno())
            # Re-open using the existing reader while still inside the killable
            # process; malformed/incomplete output cannot reach publication.
            validated = GraphData.load(str(snapshot))
            result['_worker_graph_summary'] = snapshot_summary(validated)
            result['_worker_graph_result'] = True
        bundle = service._graph_ml._dataset_store._bundles.get(runtime_id)
        if bundle and tool != 'load_graph_classification_graph':
            write_bundle(bundle, Path(payload['staging_root']) / 'dataset', check_cancelled)
            result['_worker_dataset_result'] = True
    elif tool in NX_TOOL_NAMES:
        if graph is None:
            raise ValueError('尚未加载任何图数据')
        result = run_nx_tool(tool, graph.to_networkx(), payload['arguments'])
    else:
        from graph_data_service.graph_ml.module import GraphMLModule
        from graph_data_service.graph_ml.artifact_store import ArtifactStore
        from graph_data_service.worker_data import read_bundle, write_bundle
        module = GraphMLModule(lambda _: session, artifact_root=payload['staging_root'])
        readonly_store = ArtifactStore(payload['artifact_root'])
        local_resolve = module._store.resolve_model
        module._store.resolve_model = lambda sid, mid: local_resolve(sid, mid) or readonly_store.resolve_model(sid, mid)
        if payload.get('dataset_directory'):
            module._dataset_store._bundles[runtime_id] = read_bundle(payload['dataset_directory'])
        elif payload.get('loaded_dataset'):
            # The parent proved an explicit load is still resident. Reconstruct
            # only in the isolated worker and verify the source fingerprint.
            loaded = payload['loaded_dataset']
            bundle = module._dataset_store.load(runtime_id, session, loaded['filename'], loaded['profile'], progress_callback=progress)
            if bundle.fingerprint != loaded['fingerprint']:
                raise ValueError('DATASET_CHANGED')
        check_cancelled()
        if tool == 'load_graph_classification_dataset':
            result = module.load_graph_classification_dataset(runtime_id, payload['arguments'], progress_callback=progress)
            bundle = module.resolve_graph_classification_dataset(runtime_id)
            write_bundle(bundle, Path(payload['staging_root']) / 'dataset', check_cancelled)
        elif tool == 'train':
            result = module.train(runtime_id, payload['arguments'], progress_callback=progress)
            if result.get('success') and result.get('model_id'):
                from graph_data_service.worker_artifacts import validate_model
                ref = module._store.resolve_model(runtime_id, result['model_id'])
                validate_model(ref.model_dir, payload['arguments']['task'])
                result['_worker_artifact_validated'] = True
        else:
            result = module.infer(runtime_id, payload['arguments'])
    check_cancelled()
    return result


def main():
    identity_path = os.getenv('IGRAPH_WORKER_IDENTITY_FILE')
    if identity_path:
        import psutil
        from execution_lifecycle import atomic_json
        pid = os.getpid()
        if os.getpgrp() != pid:
            raise RuntimeError('WORKER_REQUIRES_PRIVATE_PROCESS_GROUP')
        atomic_json(Path(identity_path), {'pid': pid, 'started_at': psutil.Process(pid).create_time(), 'process_group': pid})
    first = sys.stdin.readline()
    if not first:
        return  # Parent exited before releasing the registered execution permit.
    payload = json.loads(first)
    # Keep library diagnostics out of the framed IPC channel.
    sys.stdout = sys.stderr
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    def control_reader():
        try:
            for line in sys.stdin:
                try:
                    if json.loads(line).get('control') == 'cancel':
                        stopping.set()
                except ValueError:
                    continue
        finally:
            # A lost owner cannot leave computation running indefinitely. The
            # restarted service independently verifies and reaps its owned group.
            stopping.set()
            if identity_path:
                import time
                time.sleep(2)
                os.killpg(os.getpgrp(), signal.SIGTERM)
                time.sleep(1)
                os.killpg(os.getpgrp(), signal.SIGKILL)
    threading.Thread(target=control_reader, daemon=True).start()
    try:
        result = compute(payload)
        emit('result', result)
    except InterruptedError:
        raise SystemExit(130)
    except Exception as exc:
        emit('result', {'success': False, 'error': str(exc)})


if __name__ == '__main__':
    main()

"""Stage worker results and commit only at the current execution boundary."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import logging
import json
import stat
from dataclasses import asdict
from pathlib import Path

from graph_data_service.execution_host import job_context
from graph_data_service.worker_process import WorkerProcess
from graph_data_service.worker_data import LazyGraphs, read_bundle
from graph_engine.paths import safe_path_component
from graph_engine.tool_execution import execution_policy
from execution_lifecycle import atomic_json


async def run_isolated(service, session_id, user_id, tool, arguments, progress=None):
    context = job_context.get()
    if not context or not service._execution_host:
        raise RuntimeError('MISSING_EXECUTION_SCOPE')
    session = service.get_required(session_id, user_id=user_id)
    if progress is None:
        job = service._job_manager.status(context.job_id)
        if job:
            progress = service._job_manager._make_progress_callback(job)
    version = session.graph_version
    policy = execution_policy(tool, arguments)
    uses_graph = policy.barrier or "graph" in policy.reads | policy.writes
    runtime_id = service._cache_key(session_id, user_id)
    dataset_generation = service._graph_ml._dataset_store.generation_token(runtime_id)
    root = Path(service._graph_ml._store.root_dir)
    # Stage document publication on the session's filesystem. Models/datasets
    # publish under the artifact root. No atomic rename crosses a mount point.
    from graph_data_service.worker_contract import ISOLATED_DATA_TOOLS
    stage_parent = (session._session_dir if tool in ISOLATED_DATA_TOOLS else root) / '.staging'
    if tool == 'save_as_dataset':
        registry = service._get_user_dataset_registry(user_id) if user_id else service._dataset_registry
        stage_parent = registry._base_dir / '.staging'
    stage_parent.mkdir(exist_ok=True, parents=True)
    stage = Path(tempfile.mkdtemp(prefix=context.job_id + '-', dir=stage_parent))
    graph_path = None
    pinned = []
    def receipt(result):
        return {'run_id': context.run_id, 'job_id': context.job_id, 'generation': context.generation,
                'result': {key: value for key, value in result.items() if not key.startswith('_worker_')}}

    def prepare_commit(path):
        prepare = getattr(service._execution_host, 'prepare_commit', None)
        if prepare:
            prepare(context, path)

    def remember_committed(result):
        job = service._job_manager.status(context.job_id)
        if job:
            job.result = receipt(result)['result']

    def commit_allowed():
        signal = service._job_manager._cancel_signals.get(context.job_id)
        if signal and signal.is_set():
            raise InterruptedError('JOB_CANCELLED')
        if service.get_required(session_id, user_id=user_id) is not session:
            raise ValueError('GRAPH_VERSION_CHANGED')
        if uses_graph and session.graph_version != version:
            raise ValueError('GRAPH_VERSION_CHANGED')
    def pin(source):
        # Read-only descriptors pin the immutable inode even if version
        # retention unlinks it, without copying a large input on the event loop
        # or requiring the source and artifact roots to share a filesystem.
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        pinned.append(descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('INVALID_WORKER_INPUT')
        return f'/dev/fd/{descriptor}'
    try:
        # Documents and classification-bundle loading have their own resources.
        # They neither need a graph-sized input nor depend on its version; a
        # concurrent graph replacement must not invalidate their results.
        if uses_graph and session.get_graph() is not None:
            source = session._snapshots_dir / f'v{version:03d}.npz'
            graph_path = pin(source)
        payload = {'tool': tool, 'arguments': service._strip_internal_graph_ml_args(arguments),
                   'session_id': session_id, 'user_id': user_id, 'graph_version': version,
                   'graph_path': graph_path, 'artifact_root': str(root), 'staging_root': str(stage),
                   'graph_source': session.version_source(version) if uses_graph else {},
                   'llm_model': session.llm_model,
                   'model_run_context': asdict(context),
                   'uploads': {name: asdict(record) for name, record in session._uploads_by_name.items()}}
        if tool in {'undo_graph', 'load_graph_version', 'discard_graph_version'}:
            records = session.list_versions()
            requested_version = int(arguments.get('graph_version', -1))
            if tool == 'discard_graph_version':
                if not any(int(r['graph_version']) == requested_version for r in records):
                    return {'success': False, 'error': '图版本不存在。'}
                fallback = int(arguments.get('fallback_graph_version') or -1)
                candidates = [int(r['graph_version']) for r in records if r.get('loaded') and int(r['graph_version']) < version]
                requested_version = version if requested_version != version else (
                    fallback if any(int(r['graph_version']) == fallback and r.get('loaded') and fallback != version for r in records)
                    else max(candidates, default=-1))
            targets = [r for r in records if r.get('loaded') and (int(r['graph_version']) < version
                if tool == 'undo_graph' else int(r['graph_version']) == requested_version)]
            if not targets:
                return {'success': False, 'error': '没有可加载的历史版本。', 'graph_version': version}
            target = targets[-1]
            payload['history_target'] = {**target, 'path': pin(target['snapshot'])}
        if tool in {'upload_document', 'retrieve_context'}:
            from graph_data_service.rag import LightweightRAG
            payload['rag_directory'] = str(LightweightRAG.current_directory(session._session_dir))
        if tool == 'load_dataset':
            dataset_id = str(arguments.get('dataset_id') or '')
            if not dataset_id or safe_path_component(dataset_id) != dataset_id:
                raise ValueError('INVALID_DATASET_ID')
            registry = service._get_user_dataset_registry(user_id) if user_id else service._dataset_registry
            meta = registry.get(dataset_id, required=False)
            if meta is None:
                registry = service._dataset_registry
                meta = registry.get(dataset_id)
            if meta.storage_backend != 'local':
                return {'success': False, 'code': 'NEBULA_PRIMARY_REQUIRED', 'error': '该数据集需要 NebulaGraph 存储。'}
            selected = str(arguments.get('version') or 'latest')
            selected = meta.versions[-1] if selected == 'latest' else selected
            if selected not in meta.versions:
                raise ValueError('DATASET_VERSION_NOT_FOUND')
            source = registry._dataset_dir(dataset_id) / 'versions' / f'{selected}.npz'
            payload['dataset_load'] = {'dataset_id': dataset_id, 'version': selected, 'path': pin(source)}
        if tool in {'train', 'infer', 'load_graph_classification_graph'}:
            requested_dataset = arguments.get('dataset_id') if tool == 'load_graph_classification_graph' else (arguments.get('data') or {}).get('dataset_id')
            task = arguments.get('task')
            model_meta = None
            if tool == 'infer':
                # Inference identifies its task through the owned artifact;
                # InferRequest intentionally has no task/data fields.
                model_id = str(arguments.get('model_id') or '')
                if not model_id or safe_path_component(model_id) != model_id:
                    raise ValueError('INVALID_MODEL_ID')
                ref = service._graph_ml._store.resolve_model(runtime_id, model_id)
                if ref is None:
                    raise ValueError(f'模型不存在: {model_id}')
                model_meta = service._graph_ml._store.load_json(ref.model_dir, 'meta.json')
                task = model_meta.get('task')
                requested_dataset = model_meta.get('dataset_id') if task == 'graph_classification' else None
            if task == 'graph_classification' or requested_dataset or tool == 'load_graph_classification_graph':
                bundle = service._graph_ml.resolve_graph_classification_dataset(runtime_id, requested_dataset)
                if model_meta is not None:
                    from graph_data_service.graph_ml.infer.infer_graph_classification import GraphClassificationInfer
                    GraphClassificationInfer._validate_dataset_identity(bundle, model_meta)
                if isinstance(bundle.graphs, LazyGraphs):
                    payload['dataset_directory'] = str(bundle.graphs.directory)
                else:
                    payload['loaded_dataset'] = {'filename': bundle.source_filename, 'profile': bundle.name, 'fingerprint': bundle.fingerprint}
        worker = WorkerProcess()
        async def started(identity):
            register = getattr(service._execution_host, 'worker_started', None)
            if register:
                await register(context, identity)
        result = await worker.run(payload, progress, on_started=started, pass_fds=tuple(pinned),
                                  lease_root=getattr(service._execution_host, 'worker_root', Path(os.getenv('IGRAPH_DATA_ROOT', 'data')).resolve() / 'graph_engine' / 'executors' / 'workers'),
                                  job_id=context.job_id)
        await service._execution_host.before_start(context)
        commit_allowed()
        if (payload.get('dataset_directory') or payload.get('loaded_dataset')) and service._graph_ml._dataset_store.generation_token(runtime_id) != dataset_generation:
            raise ValueError('DATASET_GENERATION_CHANGED')
        if not result.get('success'):
            if tool != 'get_graph_info' or not result.get('loaded'):
                return result
        if tool == 'get_graph_info' and len(json.dumps(result, ensure_ascii=False)) <= 65536:
            session._controlled_graph_info = (version, result)
        if result.pop('_worker_rag_result', False):
            from graph_data_service.rag import LightweightRAG
            # Only a complete immutable document version is published. Readers
            # see all of its index, embeddings and source documents together.
            with service._graph_write_lock(session_id, user_id):
                if str(LightweightRAG.current_directory(session._session_dir)) != payload['rag_directory']:
                    raise ValueError('DOCUMENT_VERSION_CHANGED')
                target = session._session_dir / '.rag_versions' / context.job_id
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_json(stage / 'rag-result' / '_execution_commit.json', receipt(result))
                prepare_commit(session._session_dir / 'rag_manifest.json')
                os.replace(stage / 'rag-result', target)
                manifest = session._session_dir / f'.rag-manifest-{context.job_id}'
                try:
                    with manifest.open('w', encoding='utf-8') as handle:
                        json.dump({'version': context.job_id, 'commit_receipt': receipt(result)}, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                    commit_allowed()
                    os.replace(manifest, session._session_dir / 'rag_manifest.json')
                except BaseException:
                    os.replace(target, stage / 'rag-result')
                    manifest.unlink(missing_ok=True)
                    raise
                service._rag_cache.pop(runtime_id, None)
                remember_committed(result)
        if tool == 'load_graph_classification_dataset' or result.pop('_worker_dataset_result', False):
            target_root = root / '.datasets' / safe_path_component(runtime_id)
            target_root.mkdir(exist_ok=True, parents=True)
            target = target_root / context.job_id
            os.replace(stage / 'dataset', target)
            try:
                bundle = read_bundle(target)
                service._graph_ml._dataset_store.adopt_worker_bundle(runtime_id, session, bundle, expected_generation=dataset_generation)
            except BaseException:
                # The bundle never became visible; return it to this job's
                # staging area so the scoped cleanup handles the failed commit.
                os.replace(target, stage / 'dataset')
                raise
        if result.pop('_worker_graph_result', False):
            from graph_data_service.async_work import run_sync
            summary = result.pop('_worker_graph_summary')
            await service._execution_host.before_start(context)
            if session.graph_version != version:
                raise ValueError('GRAPH_VERSION_CHANGED')
            def commit():
                with service._graph_write_lock(session_id, user_id):
                    if service.get_required(session_id, user_id=user_id) is not session or session.graph_version != version:
                        raise ValueError('GRAPH_VERSION_CHANGED')
                    signal = service._job_manager._cancel_signals.get(context.job_id)
                    if signal and signal.is_set():
                        raise InterruptedError('JOB_CANCELLED')
                    prepare_commit(session._versions_path())
                    session.publish_snapshot(stage / 'graph-result.npz', summary, expected_version=version,
                        receipt=receipt(result),
                        activate_version=payload['history_target']['graph_version'] if tool in {'load_graph_version', 'discard_graph_version'} else None,
                        discard_version=int(arguments['graph_version']) if tool == 'discard_graph_version' else None,
                        before_commit=commit_allowed)
                    service._compute.invalidate(runtime_id)
                    service._search_index_cache.pop(runtime_id, None)
                    remember_committed({**result, 'graph_version': session.graph_version})
                    if tool == 'load_dataset':
                        session.bind_dataset(result['dataset_id'], result['dataset_version'])
                    if tool == 'load_graph_from_upload':
                        upload = session.resolve_upload_by_name(arguments.get('filename', ''))
                        if upload:
                            session.mark_version_upload_source(session.graph_version, upload)
            await run_sync(commit)
            result['graph_version'] = session.graph_version
            result.pop('job_id', None)  # Private worker-local child IDs never escape.
            if tool == 'undo_graph':
                result['message'] = '图已回滚到上一版本。'
            elif tool == 'load_graph_version':
                result['message'] = f'已切换到图版本 {session.graph_version}。'
            elif tool == 'discard_graph_version':
                result['message'] = f"已删除图版本 {arguments['graph_version']} 并回退到版本 {session.graph_version}。"
                result['versions'] = session.list_versions()
        if tool == 'save_as_dataset':
            dataset = result['dataset']
            dataset_id = str(dataset['dataset_id'])
            if safe_path_component(dataset_id) != dataset_id:
                raise ValueError('INVALID_WORKER_DATASET')
            source, target = stage / 'dataset-export' / dataset_id, registry._dataset_dir(dataset_id)
            if target.exists():
                raise ValueError('DATASET_ALREADY_PUBLISHED')
            atomic_json(source / '_execution_commit.json', receipt(result))
            prepare_commit(target / '_execution_commit.json')
            commit_allowed()
            os.replace(source, target)
            remember_committed(result)
            session.bind_dataset(dataset_id, dataset['versions'][-1])
        model_id = result.get('model_id') if tool == 'train' else None
        if model_id:
            if not result.pop('_worker_artifact_validated', False):
                raise ValueError('UNVALIDATED_WORKER_ARTIFACT')
            if safe_path_component(model_id) != model_id:
                raise ValueError('INVALID_WORKER_ARTIFACT')
            source = stage / safe_path_component(runtime_id) / model_id
            if not source.is_dir() or not (source / 'metrics.json').is_file() or not (source / 'meta.json').is_file():
                raise ValueError('INCOMPLETE_WORKER_ARTIFACT')
            target = root / safe_path_component(runtime_id) / model_id
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise ValueError('ARTIFACT_ALREADY_PUBLISHED')
            atomic_json(source / '_execution_commit.json', receipt(result))
            prepare_commit(target / '_execution_commit.json')
            commit_allowed()
            os.replace(source, target)
            remember_committed(result)
        return result
    finally:
        for descriptor in pinned:
            os.close(descriptor)
        # A failed cleanup is observable and never broadens to published data.
        try:
            await asyncio.to_thread(shutil.rmtree, stage)
        except OSError:
            logging.getLogger(__name__).warning('worker_staging_cleanup_pending job_id=%s', context.job_id)
            retry = getattr(service._execution_host, 'retry_cleanup', None)
            if retry:
                retry(stage, context.job_id)

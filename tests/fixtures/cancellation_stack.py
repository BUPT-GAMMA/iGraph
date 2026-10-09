"""Disposable full HTTP stack with a deterministic model provider.

Shared by pytest and browser E2E. All mutable files and databases are test-owned.
Explicit legacy_async seed requests also emulate the old tool-result contract;
the MCP transport, worker, ledger and cancellation paths remain real.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


@asynccontextmanager
async def cancellation_stack(root: Path, url: str, *, learning=False, training_epochs=100000, request_timeout=30):
    from agno.db.sqlite import SqliteDb
    from agno.run import cancel
    from backend.config import BackendConfig
    from backend.users import UserDirectory, UserRecord
    from agent_runtime.config import AgentRuntimeConfig
    from agent_runtime.model_factory import CancellableDAGOpenAILike
    from graph_data_service.app import GraphDataService, create_app as graph_app
    from tests.servers.test_graph_ml_legacy_regression import _request
    from mcp_service.graph_data_client import GraphDataClient

    settings = {'IGRAPH_DATA_ROOT': str(root), 'GRAPH_ML_ARTIFACT_ROOT': str(root / 'artifacts'),
                'IGRAPH_RUN_CANCEL_ENABLED': 'true', 'IGRAPH_CONTROL_SERVICE_KEY': 'local-cancellation-test-only',
                'EMBEDDING_API_KEY': '', 'EMBEDDING_MODEL': '',
                'AGNO_TELEMETRY': 'false', 'AGENT_RUNTIME_TRACING': 'false', 'AGENT_RUNTIME_AUTHORIZATION': 'false',
                'GRAPH_STORAGE_BACKEND': 'local', 'GRAPH_ENGINE_MCP_URL': url + '/mcp-api/mcp',
                'MCP_HOST': '127.0.0.1', 'MCP_PORT': '21110', 'GRAPH_DATA_SERVER_URL': url + '/graph',
                'AGENT_RUNTIME_URL': url + '/runtime', 'BACKEND_CONTROL_URL': url + '/bff',
                'GRAPH_ENGINE_API_URL': url + '/graph'}
    settings.update(GRAPH_CONSTRUCTION_API_KEY='local-construction-test-only',
                    GRAPH_CONSTRUCTION_BASE_URL=url + '/model/construction/v1',
                    GRAPH_CONSTRUCTION_RESOLVED_IP='',
                    # Catalogue admission checks the deployment endpoint before
                    # the patched model is constructed. Keep every repetition
                    # independent of dotenv's one-time module initialization.
                    QWEN_API_KEY='local-cancellation-test-only',
                    QWEN_BASE_URL=url + '/model/v1')
    settings['NO_PROXY'] = settings['no_proxy'] = '127.0.0.1,localhost'
    previous_manager = cancel.get_cancellation_manager()
    async with AsyncExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, settings))
        # sse-starlette's Uvicorn drain flag is process-global. A previous
        # disposable server's shutdown must not close this server's new SSEs.
        from sse_starlette.sse import AppStatus
        stack.enter_context(patch.object(AppStatus, 'should_exit', False))
        from agent_runtime import os_app, agno_factory
        from agent_runtime import graph_jobs
        from agent_runtime.execution_scope import current_scope
        legacy_async_sessions = set()
        complete_graph_result = graph_jobs.completed_graph_result
        async def fixture_graph_result(result, poll, *, interval=1.0, tool_name=""):
            scope = current_scope.get()
            # Only explicitly seeded legacy sessions return a live training job.
            # All other tools/sessions retain dev's wait-for-completion contract.
            if tool_name == "train" and scope is not None and scope.session_id in legacy_async_sessions:
                return result
            return await complete_graph_result(result, poll, interval=interval, tool_name=tool_name)
        stack.enter_context(patch.object(graph_jobs, "completed_graph_result", fixture_graph_result))
        from backend.app import create_app as backend_app
        from mcp_service import server as mcp_server
        db_url = os.getenv('IGRAPH_CONTROL_TEST_DATABASE_URL')
        if db_url:
            from agno.db.postgres import PostgresDb
            from sqlalchemy.schema import CreateSchema, DropSchema
            from uuid import uuid4
            schema = 'cancel_chain_' + uuid4().hex
            db = PostgresDb(db_url=db_url, db_schema=schema, session_table='cancel_test_sessions')
            with db.db_engine.begin() as connection:
                connection.execute(CreateSchema(schema))
            db.db_engine = db.db_engine.execution_options(schema_translate_map={None: schema})
        else:
            db = SqliteDb(db_file=str(root / 'native.db'))
        stack.enter_context(patch.object(os_app, 'create_db', lambda _: db))
        stack.enter_context(patch.object(agno_factory, 'create_model', lambda *_: CancellableDAGOpenAILike(
            id='qwen3.6-plus', api_key='test-only', base_url=url + '/model/v1', timeout=30)))
        if not learning:
            stack.enter_context(patch.object(agno_factory, 'create_learning_machine', lambda *_args, **_kwargs: None))
        stack.enter_context(patch.object(mcp_server, '_graph_data', GraphDataClient(url + '/graph')))
        runtime = os_app.create_app(replace(AgentRuntimeConfig.from_env(), skills_root=root / 'skills', tracing=False))
        runtime.state.execution_controller.learning_queue = runtime.state.background_learning
        graph = GraphDataService(migrate_storage=False)
        graph_api = graph_app(graph)
        # A fresh server owns loop-bound locks and lifespan state on every run;
        # reuse the real registered tool functions, never the previous loop.
        from fastmcp import FastMCP
        mcp = FastMCP('iGraph cancellation contract', tools=list(await mcp_server.mcp.list_tools()))
        mcp.custom_route('/internal/capabilities', methods=['GET'])(mcp_server.control_capabilities)
        mcp_api = mcp.http_app(path='/mcp', transport='streamable-http')
        # A first graph-info request starts an isolated Python/Torch worker.
        # CI cold starts can exceed eight seconds before cancellation is even
        # exercised. Keep this setup budget separate from the tests' bounded
        # cancellation/exit assertions.
        config = replace(BackendConfig.from_env(), agent_runtime_url=url + '/runtime', graph_engine_url=url + '/graph',
                         request_timeout=request_timeout, stream_timeout=120, supported_ids_file=root / 'unused.txt')
        users = UserDirectory(config.users_file, config.supported_ids_file, 'admin')
        users.save(UserRecord(user_id='admin', roles=['admin']))
        users.save(UserRecord(user_id='alice', roles=[]))
        bff = backend_app(config)
        metrics = {'calls': {}, 'closed': {}, 'tool_calls': {}, 'contexts': {}}
        model = FastAPI()

        @model.post('/construction/v1/chat/completions')
        async def construction_completion(request: Request):
            body = await request.json()
            metrics['calls']['construction'] = metrics['calls'].get('construction', 0) + 1
            return {'choices': [{'message': {'role': 'assistant', 'content':
                '<triples>\nAlice | works_at | OpenAI\nBob | works_at | OpenAI\nAlice | mentors | Bob\n</triples>'}}]}

        @model.post('/v1/embeddings')
        async def embedding(request: Request):
            await request.json()
            metrics['calls']['embedding'] = metrics['calls'].get('embedding', 0) + 1
            try:
                while not await request.is_disconnected():
                    await asyncio.sleep(.02)
            finally:
                metrics['closed']['embedding'] = metrics['closed'].get('embedding', 0) + 1
            from fastapi.responses import Response
            return Response(status_code=499)

        @model.post('/v1/chat/completions')
        async def completion(request: Request):
            body = await request.json()
            key = next((str(msg.get('content')) for msg in reversed(body['messages']) if msg.get('role') == 'user'), '')
            metrics['contexts'][key] = body['messages']
            count = metrics['calls'].get(key, 0) + 1
            metrics['calls'][key] = count
            if not body.get('stream'):
                # Real auxiliary model request held before its response headers.
                # The provider records actual client disconnect, separately from
                # the local Run's cancellation status.
                try:
                    while not await request.is_disconnected():
                        await asyncio.sleep(.02)
                finally:
                    metrics['closed'][key] = metrics['closed'].get(key, 0) + 1
                from fastapi.responses import Response
                return Response(status_code=499)
            async def stream():
                def frame(delta, finish=None):
                    return 'data: ' + json.dumps({'id': 'test', 'object': 'chat.completion.chunk', 'created': 1,
                        'model': body['model'], 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}) + '\n\n'
                try:
                    if key.startswith('large-graph') and count == 1:
                        args = {'filename': 'large.edges', 'workers': 1, 'partitions': 1}
                        yield frame({'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'large-prepare', 'type': 'function',
                            'function': {'name': 'prepare_large_graph', 'arguments': json.dumps(args)}}]})
                        yield frame({}, 'tool_calls')
                        yield 'data: [DONE]\n\n'
                    elif key.startswith('large-graph'):
                        yield frame({'role': 'assistant', 'content': '大图处理完成'})
                        yield frame({}, 'stop')
                        yield 'data: [DONE]\n\n'
                    elif (key.startswith('train') or key.startswith('background') or key.startswith('approval')) and count == 1:
                        tool = 'clear_graph' if key.startswith('approval') else 'train'
                        args = {} if tool == 'clear_graph' else _request('node_classification').model_dump(mode='json')
                        if tool == 'train': args['train_config']['epochs'] = training_epochs
                        metrics['tool_calls'][key] = metrics['tool_calls'].get(key, 0) + 1
                        yield frame({'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'call-' + str(count), 'type': 'function',
                                      'function': {'name': tool, 'arguments': json.dumps(args)}}]})
                        yield frame({}, 'tool_calls')
                        yield 'data: [DONE]\n\n'
                    elif key.startswith('reasoning-browser'):
                        if count == 1:
                            yield frame({'role': 'assistant', 'reasoning_content': '先检查'})
                            await asyncio.sleep(2)
                            yield frame({'reasoning_content': '图结构。'})
                            await asyncio.sleep(2)
                            yield frame({'tool_calls': [{'index': 0, 'id': 'reasoning-tool', 'type': 'function',
                                'function': {'name': 'get_graph_info', 'arguments': '{}'}}]})
                            yield frame({}, 'tool_calls')
                        else:
                            yield frame({'role': 'assistant', 'reasoning_content': '根据工具结果继续分析。'})
                            await asyncio.sleep(2)
                            yield frame({'content': '图结构检查完成。'})
                            yield frame({}, 'stop')
                        yield 'data: [DONE]\n\n'
                    elif key.startswith('complete') or key.startswith('background'):
                        yield frame({'role': 'assistant', 'content': 'ABC'})
                        yield frame({}, 'stop')
                        yield 'data: [DONE]\n\n'
                    else:
                        if not key.startswith('first-token'):
                            yield frame({'role': 'assistant', 'content': 'A'})
                        await asyncio.Event().wait()
                finally:
                    metrics['closed'][key] = metrics['closed'].get(key, 0) + 1
            return StreamingResponse(stream(), media_type='text/event-stream')

        for app in (runtime, graph_api, mcp_api):
            await stack.enter_async_context(app.router.lifespan_context(app))
        app = FastAPI()
        app.mount('/runtime', runtime)
        app.mount('/graph', graph_api)
        app.mount('/mcp-api', mcp_api)
        app.mount('/model', model)
        @app.get('/test-metrics')
        async def inspect_metrics():
            return {**metrics, 'jobs': [job.to_dict() for job in graph._job_manager._jobs.values()]}
        @app.post('/test-seed/{sid}')
        async def seed_graph(sid: str, legacy_async: bool = False):
            # Test-only deterministic data; this application is never deployed.
            from tests.servers.test_graph_ml_legacy_regression import _legacy_graph
            graph.get_required(sid, user_id='admin').set_graph(_legacy_graph())
            if legacy_async:
                legacy_async_sessions.add(sid)
            return {'success': True}
        app.mount('/bff', bff)
        app.mount('/', bff)
        try:
            yield app, graph, runtime.state.execution_controller, metrics
        finally:
            # Retain the coordinator until actual workers are stopped.
            for scope in runtime.state.execution_controller.manager.scopes.values():
                scope.cancel()
            await graph._execution_host.close()
            await runtime.state.background_learning.close()
            await runtime.state.execution_controller.close()
            if db_url:
                with db.db_engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
            if hasattr(db, 'close'): db.close()
            cancel.set_cancellation_manager(previous_manager)

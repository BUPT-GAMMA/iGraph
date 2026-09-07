"""Disposable full HTTP stack; only the model provider is deterministic.

Shared by pytest and browser E2E. All mutable files and databases are test-owned.
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
async def cancellation_stack(root: Path, url: str, *, learning=False, training_epochs=100000):
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
    settings['NO_PROXY'] = settings['no_proxy'] = '127.0.0.1,localhost'
    previous_manager = cancel.get_cancellation_manager()
    async with AsyncExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, settings))
        # sse-starlette's Uvicorn drain flag is process-global. A previous
        # disposable server's shutdown must not close this server's new SSEs.
        from sse_starlette.sse import AppStatus
        stack.enter_context(patch.object(AppStatus, 'should_exit', False))
        from agent_runtime import os_app, agno_factory
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
        graph = GraphDataService(migrate_storage=False)
        graph_api = graph_app(graph)
        # A fresh server owns loop-bound locks and lifespan state on every run;
        # reuse the real registered tool functions, never the previous loop.
        from fastmcp import FastMCP
        mcp = FastMCP('iGraph cancellation contract', tools=list(await mcp_server.mcp.list_tools()))
        mcp.custom_route('/internal/capabilities', methods=['GET'])(mcp_server.control_capabilities)
        mcp_api = mcp.http_app(path='/mcp', transport='streamable-http')
        config = replace(BackendConfig.from_env(), agent_runtime_url=url + '/runtime', graph_engine_url=url + '/graph',
                         request_timeout=8, stream_timeout=120, supported_ids_file=root / 'unused.txt')
        users = UserDirectory(config.users_file, config.supported_ids_file, 'admin')
        users.save(UserRecord(user_id='admin', roles=['admin']))
        users.save(UserRecord(user_id='alice', roles=[]))
        bff = backend_app(config)
        metrics = {'calls': {}, 'closed': {}, 'tool_calls': {}}
        model = FastAPI()

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
                    if (key.startswith('train') or key.startswith('background') or key.startswith('approval')) and count == 1:
                        tool = 'clear_graph' if key.startswith('approval') else 'train'
                        args = {} if tool == 'clear_graph' else _request('node_classification').model_dump(mode='json')
                        if tool == 'train': args['train_config']['epochs'] = training_epochs
                        metrics['tool_calls'][key] = metrics['tool_calls'].get(key, 0) + 1
                        yield frame({'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'call-' + str(count), 'type': 'function',
                                      'function': {'name': tool, 'arguments': json.dumps(args)}}]})
                        yield frame({}, 'tool_calls')
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
        async def seed_graph(sid: str):
            # Test-only deterministic data; this application is never deployed.
            from tests.servers.test_graph_ml_legacy_regression import _legacy_graph
            graph.get_required(sid, user_id='admin').set_graph(_legacy_graph())
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
            await runtime.state.execution_controller.close()
            if db_url:
                with db.db_engine.begin() as connection:
                    connection.execute(DropSchema(schema, cascade=True))
            if hasattr(db, 'close'): db.close()
            cancel.set_cancellation_manager(previous_manager)

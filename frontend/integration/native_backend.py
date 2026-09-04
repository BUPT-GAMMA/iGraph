"""Local-only browser fixture: real AgentFactory, buffer, DB and public BFF.

Only the model and unrelated graph service are deterministic stand-ins.
All test data lives in a disposable directory; no credentials are required.
"""
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from agno.agent import Agent, AgentFactory
from agno.db.sqlite import SqliteDb
from agno.models.base import Model
from agno.models.response import ModelResponse
from agno.os import AgentOS
from agno.os.managers import sse_subscriber_manager
from agno.tools import tool
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_runtime.recovery import create_recovery_router
from backend.app import create_app
from backend.config import BackendConfig
from backend.users import UserDirectory, UserRecord


def fixture(root: Path):
    os.environ['IGRAPH_DATA_ROOT'] = str(root)
    os.environ['AGNO_TELEMETRY'] = 'false'
    db = SqliteDb(db_file=str(root / 'native.db'))
    metrics = {'builds': 0, 'effects': 0}

    @tool(requires_confirmation=True)
    def record_once() -> str:
        """Record one deterministic side effect after approval."""
        metrics['effects'] += 1
        return 'ok'

    class DeterministicModel(Model):
        def invoke(self, *args, **kwargs):
            return ModelResponse(role='assistant', content='ABC')

        async def ainvoke(self, *args, **kwargs):
            return self.invoke()

        def invoke_stream(self, *args, **kwargs):
            for token in 'ABC':
                yield ModelResponse(role='assistant', content=token)

        async def ainvoke_stream(self, *args, **kwargs):
            messages = kwargs.get('messages', args[0] if args else [])
            approval = any(message.role == 'user' and 'approval' in str(message.content) for message in messages)
            if approval and not any(message.role == 'tool' for message in messages):
                yield ModelResponse(role='assistant', tool_calls=[{'id': 'call-once', 'type': 'function',
                    'function': {'name': 'record_once', 'arguments': '{}'}}])
            else:
                for token in self.invoke_stream():
                    await asyncio.sleep(.1)
                    yield token

        def _parse_provider_response(self, response, **kwargs):
            return response

        def _parse_provider_response_delta(self, response):
            return response

    def factory(_context):
        metrics['builds'] += 1
        return Agent(id='igraph-agent', db=db, model=DeterministicModel(id='deterministic'), tools=[record_once],
                     store_events=True, stream_events=True, telemetry=False)

    base = FastAPI()
    base.include_router(create_recovery_router(db))
    runtime = AgentOS(agents=[AgentFactory(id='igraph-agent', db=db, factory=factory)],
                      db=db, base_app=base, telemetry=False).get_app()
    graph = FastAPI()

    @graph.api_route('/{path:path}', methods=['GET', 'POST', 'PATCH', 'DELETE'])
    async def empty_graph(path: str):
        return {'success': True, 'loaded': False, 'num_nodes': 0}

    config = replace(BackendConfig.from_env(), agent_runtime_url='http://127.0.0.1:11119/runtime',
                     graph_engine_url='http://127.0.0.1:11119/graph', request_timeout=5,
                     supported_ids_file=root / 'unused-seed.txt')
    UserDirectory(config.users_file, config.supported_ids_file, 'admin').save(UserRecord(user_id='admin', roles=['admin']))

    @asynccontextmanager
    async def lifespan(_app):
        async with runtime.router.lifespan_context(runtime):
            yield

    app = FastAPI(lifespan=lifespan)

    @app.get('/test-metrics')
    async def test_metrics():
        return {**metrics, 'subscribers': sum(len(items) for items in sse_subscriber_manager._subscribers.values())}

    app.mount('/runtime', runtime)
    app.mount('/graph', graph)
    app.mount('/', create_app(config))
    return app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='igraph-native-e2e-') as directory:
        uvicorn.run(fixture(Path(directory)), host='127.0.0.1', port=11119, log_level='warning')

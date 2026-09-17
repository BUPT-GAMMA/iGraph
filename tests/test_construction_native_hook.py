"""Run construction hooks through the pinned Agno + MCP + persistence stack.

The provider and MCP server are deterministic in-process doubles: no network,
provider credentials or construction algorithm is involved.
"""
import asyncio
import inspect
import json

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.base import Model
from agno.models.response import ModelResponse
from agno.tools.function import Function
from agno.tools.mcp import MCPTools
from mcp.types import CallToolResult, TextContent, Tool

from agent_runtime.cancellation_compat import cancellable_tool_hook
from agent_runtime.construction_state import construction_tool_hook


@pytest.mark.parametrize('with_cancellation_hook', [False, True])
@pytest.mark.parametrize('outcome', ['success', 'failure', 'wrong_scope'])
def test_native_mcp_hook_injection_and_saved_dataset_state(tmp_path, outcome, with_cancellation_hook):
    async def scenario():
        db_path = str(tmp_path / 'native-construction.db')
        database = SqliteDb(db_file=db_path)
        calls, rounds = [], []
        target = 'other-project-dataset' if outcome == 'wrong_scope' else 'dataset-b'

        class Session:
            async def send_ping(self):
                return None

            async def call_tool(self, name, arguments):
                calls.append((name, arguments))
                return CallToolResult(content=[TextContent(type='text', text=json.dumps({
                    'success': outcome == 'success', 'construction_dataset_id': target,
                    'construction_revision': 3,
                }))])

        class DeterministicModel(Model):
            def invoke(self, *args, **kwargs):
                if not rounds:
                    rounds.append(True)
                    return ModelResponse(role='assistant', tool_calls=[{
                        'id': 'load-saved-graph', 'type': 'function',
                        'function': {'name': 'load_constructed_graph', 'arguments': json.dumps({'dataset_id': target})},
                    }])
                return ModelResponse(role='assistant', content='finished')

            async def ainvoke(self, *args, **kwargs):
                return self.invoke(*args, **kwargs)

            def invoke_stream(self, *args, **kwargs):
                yield self.invoke(*args, **kwargs)

            async def ainvoke_stream(self, *args, **kwargs):
                yield self.invoke(*args, **kwargs)

            def _parse_provider_response(self, response, **kwargs):
                return response

            def _parse_provider_response_delta(self, response):
                return response

        spec = Tool(name='load_constructed_graph', description='Load a saved dataset', inputSchema={
            'type': 'object', 'properties': {'dataset_id': {'type': 'string'}}, 'required': ['dataset_id'],
        })
        # Use the exact wrapper called by MCPTools.build_tools, including its
        # _agno_run_context injection and ToolResult(content=str) response.
        entrypoint = inspect.getmodule(MCPTools).get_entrypoint_for_tool(spec, Session())
        function = Function(name=spec.name, description=spec.description, parameters=spec.inputSchema,
                            entrypoint=entrypoint, skip_entrypoint_processing=True)
        hooks = ([cancellable_tool_hook] if with_cancellation_hook else []) + [construction_tool_hook]
        agent = Agent(id='igraph-native-construction-test', db=database, model=DeterministicModel(id='deterministic'),
                      tools=[function], tool_hooks=hooks, telemetry=False, session_state={
                          'project_id': 'project-a', 'active_dataset_id': 'dataset-a',
                          'data_state': {'active_dataset_id': 'dataset-a', 'construction_status': 'ready',
                                         'construction_revision': 1, 'source_format': 'csv',
                                         'available_constructed_datasets': [
                                             {'dataset_id': 'dataset-b', 'revision': 2, 'source_format': 'txt'},
                                         ]},
                      })
        try:
            response = await agent.arun('Load the saved graph', session_id='native-session', user_id='user-a')
            assert response.content == 'finished'
            assert len(calls) == (0 if outcome == 'wrong_scope' else 1)
            # Read the native database after Agent's own completion/write path.
            saved = database.get_session(session_id='native-session', user_id='user-a')
            state = saved.session_data['session_state']
            assert state['project_id'] == 'project-a'
            expected_id = 'dataset-b' if outcome == 'success' else 'dataset-a'
            assert state['active_dataset_id'] == expected_id
            assert state['data_state']['active_dataset_id'] == expected_id
            assert state['data_state']['construction_revision'] == (3 if outcome == 'success' else 1)
            assert state['data_state']['source_format'] == ('txt' if outcome == 'success' else 'csv')
            assert saved.get_run(response.run_id) is not None
        finally:
            database.close()
        reopened = SqliteDb(db_file=db_path)
        try:
            state = reopened.get_session(session_id='native-session', user_id='user-a').session_data['session_state']
            assert state['active_dataset_id'] == ('dataset-b' if outcome == 'success' else 'dataset-a')
        finally:
            reopened.close()

    asyncio.run(scenario())

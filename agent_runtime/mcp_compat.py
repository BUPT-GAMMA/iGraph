"""Agno 2.9.0 tool schemas with task-owned MCP transport lifetimes.

MCP's AnyIO scopes must open and close in the same task. Discovery and each
invocation use lexical contexts; no live context is cached across agent yields.
"""
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from agno.tools.mcp import MCPTools
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_runtime.execution_scope import current_scope


class OwnedMCPTools(MCPTools):
    def __init__(self, **kwargs):
        super().__init__(refresh_connection=False, **kwargs)

    def should_use_temporary_run_session(self, run_context=None):
        return run_context is not None

    @asynccontextmanager
    async def _owned_session(self, headers):
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, read=300), trust_env=False) as client:
            async with streamable_http_client(self.url, http_client=client) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=300)) as session:
                    await session.initialize()
                    yield session

    async def connect(self, force=False):
        if self._initialized and not force:
            return
        scope = current_scope.get()

        async def discover():
            # Discovery is read-only; a failed connection cannot leave an
            # initialized-but-empty toolkit or an unowned transport behind.
            async with self._owned_session(self._call_header_provider()) as session:
                self.session = session
                try:
                    await self.build_tools()
                finally:
                    self.session = None
            self._initialized = True

        try:
            if scope:
                await scope.wait(discover())
            else:
                await discover()
        except Exception as exc:
            if scope and not scope.stopped.is_set():
                scope.preflight_error = 'MCP_TOOL_DISCOVERY_FAILED'
            raise

    @asynccontextmanager
    async def get_temporary_session_for_run(self, run_context=None, agent=None, team=None):
        headers = self._call_header_provider(run_context=run_context, agent=agent, team=team)
        async with self._owned_session(headers) as session:
            yield session

    async def is_alive(self):
        return self._initialized

    async def close(self):
        # Each discovery/tool operation already owns and closes its resources.
        self._initialized = False

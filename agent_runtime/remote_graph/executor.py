"""Shared owner resolution, deadlines, previews and artifacts for graph adapters."""
import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from execution_lifecycle import owned_sync
from agent_runtime.execution_store import ControlError
from . import registry
from .adapters import AdapterContext
from .contracts import QueryRequest, QueryResult
from .policy import NetworkPolicy
from .results import bounded_rows, dumps
from .query_errors import UnsupportedMapError


class RemoteGraphExecutor:
    def __init__(self, catalog, settings):
        self.catalog, self.settings = catalog, settings
        self.policy = NetworkPolicy(settings.remote_graph_allowed_hosts, settings.remote_graph_allowed_cidrs)

    def _context(self, connection, deadline):
        return AdapterContext(connection=connection, deadline=deadline,
            max_rows=self.settings.remote_graph_max_rows,
            max_bytes=self.settings.remote_graph_max_bytes,
            network_policy=self.policy)

    @staticmethod
    def _error(adapter, exc):
        if isinstance(exc, UnsupportedMapError):
            return exc.code, UnsupportedMapError.safe_message
        if isinstance(exc, ControlError):
            return exc.code, None
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return 'REMOTE_GRAPH_TIMEOUT', None
        if adapter is not None and hasattr(adapter, 'error'):
            return adapter.error(exc)
        return 'REMOTE_GRAPH_QUERY_ERROR', None

    async def test_connection(self, uid, body):
        adapter = None
        try:
            timeout = self.settings.remote_graph_timeout_seconds
            deadline = time.monotonic() + timeout
            async with asyncio.timeout(timeout):
                adapter = registry.get_adapter(body.kind)
                connection = await owned_sync(self.catalog.resolve_write, uid, body)
                result = await adapter.test(self._context(connection, deadline))
                # A concurrent edit also invalidates a form test.
                await owned_sync(self.catalog.resolve_write, uid, body)
                return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code, _ = self._error(adapter, exc)
            status = exc.status if isinstance(exc, ControlError) else 422
            raise ControlError(code, status) from None

    async def query(self, uid, binding, request: QueryRequest, artifact_dir: Path | None = None) -> QueryResult:
        started = time.monotonic()
        query_id = str(uuid4())
        adapter = None
        try:
            timeout = self.settings.remote_graph_timeout_seconds
            async with asyncio.timeout(timeout):
                adapter = registry.get_adapter(binding.kind)
                connection = await owned_sync(self.catalog.resolve, uid, binding)
                rows = await adapter.execute(self._context(connection, started + timeout), request)
                # Adapters bound source consumption; this is the final output boundary.
                if bounded_rows([rows.columns], max_rows=1,
                                max_bytes=self.settings.remote_graph_preview_bytes).truncated:
                    raise ControlError('REMOTE_GRAPH_QUERY_REJECTED', 422)
                complete = bounded_rows(rows.rows, max_rows=self.settings.remote_graph_max_rows,
                                        max_bytes=self.settings.remote_graph_max_bytes)
                preview = bounded_rows(complete.rows, max_rows=self.settings.remote_graph_preview_rows,
                                       max_bytes=self.settings.remote_graph_preview_bytes)
                response = QueryResult(success=True, query_id=query_id, source=binding,
                    columns=rows.columns, rows=preview.rows, returned_rows=complete.returned_rows,
                    truncated=rows.truncated or complete.truncated, preview_truncated=preview.truncated,
                    elapsed_ms=int((time.monotonic() - started) * 1000))
                if request.save_result and artifact_dir is not None:
                    payload = {**response.model_dump(), 'rows': complete.rows, 'query': request.query,
                               'parameters': request.parameters,
                               'query_time': datetime.now(timezone.utc).isoformat()}
                    response.artifact_path = await self._save(artifact_dir, query_id, payload)
                return response
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code, message = self._error(adapter, exc)
            return QueryResult(success=False, query_id=query_id, source=binding, error_code=code,
                               error_message=message, elapsed_ms=int((time.monotonic() - started) * 1000))

    async def describe(self, uid, binding, labels=None, relationship_types=None):
        adapter = None
        try:
            timeout = self.settings.remote_graph_timeout_seconds
            deadline = time.monotonic() + timeout
            async with asyncio.timeout(timeout):
                adapter = registry.get_adapter(binding.kind)
                connection = await owned_sync(self.catalog.resolve, uid, binding)
                result = await adapter.describe(self._context(connection, deadline), labels, relationship_types)
                return {**result, 'source': binding.model_dump()}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code, _ = self._error(adapter, exc)
            status = exc.status if isinstance(exc, ControlError) else (408 if code == 'REMOTE_GRAPH_TIMEOUT' else 422)
            raise ControlError(code, status) from None

    async def _save(self, directory, query_id, payload):
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f'{query_id}.json'
        temporary = directory / f'.{query_id}.tmp'
        try:
            with temporary.open('x', encoding='utf-8') as output:
                output.write(dumps(payload))
            await asyncio.sleep(0)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return f'Ephemeral/remote_graph/{target.name}'

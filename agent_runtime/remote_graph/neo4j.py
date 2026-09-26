"""Cancellable Neo4j access with server-authorized, bounded read transactions."""
import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from neo4j import AsyncGraphDatabase, READ_ACCESS
from neo4j.exceptions import AuthError, Neo4jError, ServiceUnavailable, SessionExpired

from execution_lifecycle import owned_sync
from agent_runtime.execution_store import ControlError
from .contracts import QueryRequest, QueryResult
from .policy import NetworkPolicy, check_privileges, check_query, check_query_type
from .results import RowBudget, bounded_rows, dumps


class RemoteGraphExecutor:
    def __init__(self, catalog, settings):
        self.catalog, self.settings = catalog, settings
        self.policy = NetworkPolicy(settings.remote_graph_allowed_hosts, settings.remote_graph_allowed_cidrs)

    def _driver(self, connection):
        return AsyncGraphDatabase.driver(connection.public.uri,
            auth=(connection.public.username, connection.password.get_secret_value()),
            resolver=self.policy.resolve, max_connection_pool_size=1,
            connection_timeout=self.settings.remote_graph_timeout_seconds,
            connection_acquisition_timeout=self.settings.remote_graph_timeout_seconds,
            max_transaction_retry_time=0)

    @asynccontextmanager
    async def _transaction(self, driver, database):
        session = driver.session(database=database, default_access_mode=READ_ACCESS, fetch_size=100)
        tx = None
        try:
            tx = await session.begin_transaction(timeout=self.settings.remote_graph_timeout_seconds)
            yield tx
        except asyncio.CancelledError:
            session.cancel()
            raise
        finally:
            try:
                if tx is not None:
                    await tx.close()
            finally:
                await session.close()

    async def _metadata(self, tx, query, limit=10000):
        result = await tx.run(query)
        rows = []
        async for record in result:
            if len(rows) >= limit:
                raise ControlError('REMOTE_GRAPH_READ_ONLY_REQUIRED', 422)
            rows.append(dict(record))
        return rows

    async def _permissions(self, driver, database):
        try:
            async with self._transaction(driver, 'system') as tx:
                rows = await self._metadata(tx, 'SHOW USER PRIVILEGES')
                check_privileges(rows, database)
        except AuthError:
            raise
        except Neo4jError:
            raise ControlError('REMOTE_GRAPH_READ_ONLY_REQUIRED', 422) from None

    @staticmethod
    def _error(exc):
        if isinstance(exc, ControlError):
            return exc.code, None
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return 'REMOTE_GRAPH_TIMEOUT', None
        if isinstance(exc, AuthError):
            return 'REMOTE_GRAPH_AUTH_FAILED', None
        if isinstance(exc, (ServiceUnavailable, SessionExpired, OSError)):
            return 'REMOTE_GRAPH_UNREACHABLE', None
        if isinstance(exc, Neo4jError):
            code = getattr(exc, 'code', '') or ''
            if 'TimedOut' in code or 'Timeout' in code:
                return 'REMOTE_GRAPH_TIMEOUT', None
            if 'Security.Forbidden' in code:
                return 'REMOTE_GRAPH_READ_ONLY_REQUIRED', None
            # Preserve repair coordinates and a known code, never database text.
            position = re.search(r'line \d+, column \d+', str(exc))
            safe_code = code if re.fullmatch(r'Neo\.[A-Za-z.]+', code) else 'Cypher error'
            return 'REMOTE_GRAPH_QUERY_ERROR', safe_code + (f' ({position.group()})' if position else '')
        return 'REMOTE_GRAPH_QUERY_ERROR', None

    async def test_connection(self, uid, body):
        try:
            async with asyncio.timeout(self.settings.remote_graph_timeout_seconds):
                connection = await owned_sync(self.catalog.resolve_write, uid, body)
                async with self._driver(connection) as driver:
                    await self._permissions(driver, connection.public.database)
                    async with self._transaction(driver, connection.public.database) as tx:
                        result = await tx.run('MATCH (n) RETURN n LIMIT 1')
                        await result.consume()
                # A concurrent edit invalidates an edited form's test result too.
                await owned_sync(self.catalog.resolve_write, uid, body)
            return dict(success=True, readable=True, read_only=True, database=connection.public.database)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code, _ = self._error(exc)
            raise ControlError(code, exc.status if isinstance(exc, ControlError) else 422) from None

    async def query(self, uid, binding, request: QueryRequest, artifact_dir: Path | None = None) -> QueryResult:
        started = time.monotonic()
        query_id = str(uuid4())
        try:
            async with asyncio.timeout(self.settings.remote_graph_timeout_seconds):
                connection = await owned_sync(self.catalog.resolve, uid, binding)
                check_query(request.query)
                async with self._driver(connection) as driver:
                    await self._permissions(driver, connection.public.database)
                    async with self._transaction(driver, connection.public.database) as tx:
                        functions = await self._metadata(tx, 'SHOW FUNCTIONS YIELD name, isBuiltIn RETURN name, isBuiltIn')
                        check_query(request.query, {row['name'].lower() for row in functions if row.get('isBuiltIn') is True})
                        explained = await tx.run('EXPLAIN ' + request.query, request.parameters)
                        check_query_type((await explained.consume()).query_type)
                        records = await tx.run(request.query, request.parameters)
                        columns = list(records.keys())
                        # Column names are also untrusted result data.
                        if bounded_rows([columns], max_rows=1, max_bytes=self.settings.remote_graph_preview_bytes).truncated:
                            raise ControlError('REMOTE_GRAPH_QUERY_REJECTED', 422)
                        result = RowBudget(self.settings.remote_graph_max_rows, self.settings.remote_graph_max_bytes)
                        async for record in records:
                            if not result.add([record[column] for column in columns]):
                                break
                preview = bounded_rows(result.rows, max_rows=self.settings.remote_graph_preview_rows,
                                       max_bytes=self.settings.remote_graph_preview_bytes)
                response = QueryResult(success=True, query_id=query_id, source=binding, columns=columns,
                    rows=preview.rows, returned_rows=result.returned_rows, truncated=result.truncated,
                    preview_truncated=preview.truncated, elapsed_ms=int((time.monotonic() - started) * 1000))
                if request.save_result and artifact_dir is not None:
                    payload = {**response.model_dump(), 'rows': result.rows, 'query': request.query,
                               'query_time': datetime.now(timezone.utc).isoformat()}
                    response.artifact_path = await self._save(artifact_dir, query_id, payload)
                return response
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code, message = self._error(exc)
            return QueryResult(success=False, query_id=query_id, source=binding, error_code=code,
                               error_message=message, elapsed_ms=int((time.monotonic() - started) * 1000))

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

    async def describe(self, uid, binding, labels=None, relationship_types=None):
        try:
            async with asyncio.timeout(self.settings.remote_graph_timeout_seconds):
                return await self._describe(uid, binding, labels, relationship_types)
        except TimeoutError:
            raise ControlError('REMOTE_GRAPH_TIMEOUT', 408) from None

    async def _describe(self, uid, binding, labels, relationship_types):
        queries = {
            'labels': 'CALL db.labels() YIELD label WHERE $labels IS NULL OR label IN $labels RETURN label',
            'relationship_types': 'CALL db.relationshipTypes() YIELD relationshipType '
                                  'WHERE $types IS NULL OR relationshipType IN $types RETURN relationshipType',
            'nodes': 'CALL db.schema.nodeTypeProperties() YIELD nodeLabels, propertyName, propertyTypes '
                     'WHERE $labels IS NULL OR any(label IN nodeLabels WHERE label IN $labels) '
                     'RETURN nodeLabels, propertyName, propertyTypes',
            'relationships': 'CALL db.schema.relTypeProperties() YIELD relType, propertyName, propertyTypes '
                             'WHERE $types IS NULL OR relType IN $encoded_types '
                             'RETURN relType, propertyName, propertyTypes',
        }
        # Neo4j schema metadata uses :`escaped type`; keep filtering server-side,
        # before query() applies row/byte/preview limits. Also accept plain metadata names.
        encoded_types = None if relationship_types is None else [
            encoded for name in relationship_types
            for encoded in (name, ':' + name, ':`' + name.replace('`', '``') + '`')]
        results = {}
        for key, query in queries.items():
            result = await self.query(uid, binding, QueryRequest(query=query,
                parameters={'labels': labels, 'types': relationship_types, 'encoded_types': encoded_types}))
            if not result.success:
                raise ControlError(result.error_code, 422)
            results[key] = result
        properties = []
        for key, owner in [('nodes', 'node_labels'), ('relationships', 'relationship_type')]:
            for row in results[key].rows:
                owner_name = row[0]
                if key == 'relationships':
                    owner_name = str(owner_name).removeprefix(':')
                    if owner_name.startswith('`') and owner_name.endswith('`'):
                        owner_name = owner_name[1:-1].replace('``', '`')
                    if relationship_types is not None and owner_name not in relationship_types:
                        continue
                properties.append({owner: owner_name, 'name': row[1], 'types': row[2]})
        return {'success': True, 'source': binding.model_dump(),
                'labels': [r[0] for r in results['labels'].rows if labels is None or r[0] in labels],
                'relationship_types': [r[0] for r in results['relationship_types'].rows
                                       if relationship_types is None or r[0] in relationship_types],
                'properties': properties,
                'coverage': {'sampled': False, 'truncated': any(r.truncated or r.preview_truncated for r in results.values())}}

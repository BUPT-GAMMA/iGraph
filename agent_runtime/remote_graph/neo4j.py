"""Neo4j product adapter with compiled read-only execution and pinned egress."""
import asyncio
import re
import time
from contextlib import asynccontextmanager

from neo4j import Address, AsyncGraphDatabase, READ_ACCESS
from neo4j.exceptions import AuthError, Neo4jError, ServiceUnavailable, SessionExpired

from agent_runtime.execution_store import ControlError
from .adapters import AdapterContext, AdapterRows
from .contracts import QueryRequest
from .policy import check_privileges, check_query, check_query_type
from .results import RowBudget, bounded_rows


class _PinnedAddress(Address):
    """Use a resolved IP for the socket and the original name for TLS verification."""
    def __new__(cls, address, hostname):
        instance = tuple.__new__(cls, address)
        instance.hostname = hostname
        return instance

    @property
    def _host_name(self):
        return self.hostname


class Neo4jAdapter:
    """Stateless adapter: all connection, budget and cancellation state is per call."""
    @staticmethod
    def error(exc):
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
                return 'REMOTE_GRAPH_ACCESS_DENIED', None
            position = re.search(r'line \d+, column \d+', str(exc))
            safe_code = code if re.fullmatch(r'Neo\.[A-Za-z.]+', code) else 'Cypher error'
            return 'REMOTE_GRAPH_QUERY_ERROR', safe_code + (f' ({position.group()})' if position else '')
        return 'REMOTE_GRAPH_QUERY_ERROR', None

    @staticmethod
    def _remaining(ctx):
        remaining = ctx.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def _driver(self, ctx):
        connection = ctx.connection

        async def resolver(address):
            verified = await ctx.network_policy.resolve(address)
            return [_PinnedAddress((item.host, item.port), item.tls_hostname) for item in verified]

        timeout = self._remaining(ctx)
        return AsyncGraphDatabase.driver(connection.public.uri,
            auth=(connection.public.username, connection.password.get_secret_value()),
            resolver=resolver, max_connection_pool_size=1,
            connection_timeout=timeout, connection_acquisition_timeout=timeout,
            max_transaction_retry_time=0)

    @asynccontextmanager
    async def _transaction(self, ctx, driver, database):
        session = driver.session(database=database, default_access_mode=READ_ACCESS, fetch_size=100)
        tx = None
        try:
            tx = await session.begin_transaction(timeout=self._remaining(ctx))
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

    @staticmethod
    async def _metadata(tx, query, limit=10000):
        result = await tx.run(query)
        rows = []
        async for record in result:
            if len(rows) >= limit:
                raise ControlError('REMOTE_GRAPH_QUERY_REJECTED', 422)
            rows.append(dict(record))
        return rows

    async def _permissions(self, ctx, driver, database):
        """Privilege metadata is advisory; missing data is never read-only proof."""
        try:
            async with asyncio.timeout(min(3, self._remaining(ctx))):
                async with self._transaction(ctx, driver, 'system') as tx:
                    rows = await self._metadata(tx, 'SHOW USER PRIVILEGES')
            if any(row.get('access') == 'GRANTED' and row.get('action') in {
                'write', 'create_element', 'delete_element', 'set_property', 'set_label',
                'remove_label', 'admin', 'all', 'all_dbms_privileges', 'database_management',
                'index_management', 'constraint_management', 'user_management',
                'role_management', 'privilege_management', 'impersonate',
            } for row in rows):
                return 'not_read_only'
            check_privileges(rows, database)
            return 'verified_read_only'
        except (Neo4jError, ControlError, TimeoutError, ServiceUnavailable, SessionExpired, OSError):
            return 'unknown'

    async def test(self, ctx):
        database = ctx.connection.public.database
        async with self._driver(ctx) as driver:
            async with self._transaction(ctx, driver, database) as tx:
                result = await tx.run('MATCH (n) RETURN n LIMIT 1')
                await result.consume()
        async with self._driver(ctx) as driver:
            permission = await self._permissions(ctx, driver, database)
        return dict(success=True, connection_usable=True, database_read_only=permission, database=database)

    async def _execute_tx(self, ctx, tx, request):
        check_query(request.query)
        functions = await self._metadata(tx, 'SHOW FUNCTIONS YIELD name, isBuiltIn RETURN name, isBuiltIn')
        check_query(request.query, {row['name'].lower() for row in functions if row.get('isBuiltIn') is True})
        explained = await tx.run('EXPLAIN ' + request.query, request.parameters)
        check_query_type((await explained.consume()).query_type)
        records = await tx.run(request.query, request.parameters)
        columns = list(records.keys())
        if bounded_rows([columns], max_rows=1, max_bytes=ctx.max_bytes).truncated:
            raise ControlError('REMOTE_GRAPH_QUERY_REJECTED', 422)
        result = RowBudget(ctx.max_rows, ctx.max_bytes)
        async for record in records:
            if not result.add([record[column] for column in columns]):
                break
        return AdapterRows(columns, result.rows, result.returned_rows, result.truncated)

    async def execute(self, ctx: AdapterContext, request: QueryRequest) -> AdapterRows:
        async with self._driver(ctx) as driver:
            async with self._transaction(ctx, driver, ctx.connection.public.database) as tx:
                return await self._execute_tx(ctx, tx, request)

    async def describe(self, ctx, labels=None, relationship_types=None):
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
        encoded_types = None if relationship_types is None else [
            encoded for name in relationship_types
            for encoded in (name, ':' + name, ':`' + name.replace('`', '``') + '`')]
        parameters = {'labels': labels, 'types': relationship_types, 'encoded_types': encoded_types}
        results = {}
        async with self._driver(ctx) as driver:
            async with self._transaction(ctx, driver, ctx.connection.public.database) as tx:
                for key, query in queries.items():
                    results[key] = await self._execute_tx(ctx, tx, QueryRequest(query=query, parameters=parameters))
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
        return {'success': True,
                'labels': [r[0] for r in results['labels'].rows if labels is None or r[0] in labels],
                'relationship_types': [r[0] for r in results['relationship_types'].rows
                                       if relationship_types is None or r[0] in relationship_types],
                'properties': properties,
                'coverage': {'sampled': False, 'truncated': any(r.truncated for r in results.values())}}

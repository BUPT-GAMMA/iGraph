"""Explicit, repeatable schema migration for saved remote graph connections."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .catalog import connections


def migrate_remote_graph_connections(engine: Engine) -> None:
    """Add and backfill kind without reading or rewriting saved credentials."""
    with engine.begin() as conn:
        if engine.dialect.name == 'postgresql':
            # One migration per database at a time, including concurrent runtime starts.
            conn.execute(text('SELECT pg_advisory_xact_lock(3923238257127574837)'))
        # Inspector and textual DDL do not apply schema_translate_map, unlike
        # Table.create(). Resolve the same physical schema for every operation.
        schema = conn.schema_for_object(connections)
        preparer = engine.dialect.identifier_preparer
        table = preparer.quote(connections.name)
        if schema is not None:
            table = f'{preparer.quote_schema(schema)}.{table}'
        if not inspect(conn).has_table(connections.name, schema=schema):
            connections.create(conn, checkfirst=True)
        columns = {column['name']: column for column in inspect(conn).get_columns(connections.name, schema=schema)}
        if 'kind' not in columns:
            conn.execute(text(f"ALTER TABLE {table} "
                              "ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'neo4j'"))
        elif columns['kind']['nullable']:
            conn.execute(text(f"UPDATE {table} SET kind = 'neo4j' "
                              "WHERE kind IS NULL"))
            if engine.dialect.name == 'postgresql':
                conn.execute(text(f'ALTER TABLE {table} '
                                  'ALTER COLUMN kind SET NOT NULL'))
        if (engine.dialect.name == 'postgresql' and 'kind' in columns
                and columns['kind']['default'] is None):
            conn.execute(text(f"ALTER TABLE {table} "
                              "ALTER COLUMN kind SET DEFAULT 'neo4j'"))

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
        if not inspect(conn).has_table(connections.name):
            connections.create(conn, checkfirst=True)
        columns = {column['name']: column for column in inspect(conn).get_columns(connections.name)}
        if 'kind' not in columns:
            conn.execute(text("ALTER TABLE igraph_remote_graph_connections "
                              "ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'neo4j'"))
        elif columns['kind']['nullable']:
            conn.execute(text("UPDATE igraph_remote_graph_connections SET kind = 'neo4j' "
                              "WHERE kind IS NULL"))
            if engine.dialect.name == 'postgresql':
                conn.execute(text('ALTER TABLE igraph_remote_graph_connections '
                                  'ALTER COLUMN kind SET NOT NULL'))
        if (engine.dialect.name == 'postgresql' and 'kind' in columns
                and columns['kind']['default'] is None):
            conn.execute(text("ALTER TABLE igraph_remote_graph_connections "
                              "ALTER COLUMN kind SET DEFAULT 'neo4j'"))

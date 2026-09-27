"""Explicit product admission shared by execution and selectable type metadata."""
from agent_runtime.execution_store import ControlError

from .adapters import GraphAdapter
from .neo4j import Neo4jAdapter


_REGISTRY: dict[str, dict] = {
    'neo4j': {
        'adapter': Neo4jAdapter(), 'enabled': True,
        'display_name': 'Neo4j', 'query_language': 'cypher',
        'uri_example': 'bolt+s://db.example.com:7687', 'target_label': 'Database',
        'username_required': True, 'support_status': 'verified',
        'verified_versions': '5.26.31 community', 'parameters': True,
        'schema_coverage': 'database metadata', 'server_timeout': True,
        'cancellation': True,
    },
    'falkordb': {
        'adapter': None, 'enabled': False,
        'display_name': 'FalkorDB', 'query_language': 'cypher',
        'uri_example': 'rediss://db.example.com:6379', 'target_label': 'Graph',
        'username_required': False, 'support_status': 'pending',
        'verified_versions': '', 'parameters': False,
        'schema_coverage': '', 'server_timeout': False,
        'cancellation': False,
    },
}


def get_adapter(kind: str) -> GraphAdapter:
    entry = _REGISTRY.get(kind)
    if entry is None or not entry['enabled'] or entry['adapter'] is None:
        raise ControlError('REMOTE_GRAPH_INVALID_CONFIG', 422)
    return entry['adapter']


def list_database_types() -> list[dict]:
    return [{'kind': kind, **{field: value for field, value in entry.items()
                            if field not in {'adapter', 'enabled'}}}
            for kind, entry in _REGISTRY.items() if entry['enabled'] and entry['adapter'] is not None]

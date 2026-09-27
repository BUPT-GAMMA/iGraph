import asyncio
import socket
import time
from types import SimpleNamespace

import pytest

from agent_runtime.execution_store import ControlError


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_network_policy_checks_all_dns_answers_and_routing_targets(monkeypatch):
    from agent_runtime.remote_graph.policy import NetworkPolicy
    policy = NetworkPolicy(allowed_hosts=('db.example',), allowed_cidrs=())
    answers = ['93.184.216.34']
    async def dns(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port)) for ip in answers]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
    resolved = await policy.resolve(('db.example', 7687))
    assert resolved[0].host == '93.184.216.34'
    assert resolved[0].tls_hostname == 'db.example'  # TLS checks the original name.
    answers.append('127.0.0.1')
    with pytest.raises(ControlError, match='REMOTE_GRAPH_INVALID_CONFIG'):
        await policy.resolve(('db.example', 7687))
    with pytest.raises(ControlError, match='REMOTE_GRAPH_INVALID_CONFIG'):
        await policy.resolve(('unapproved-routing.example', 7687))


@pytest.mark.anyio
async def test_loopback_requires_explicit_cidr_and_default_denies():
    from agent_runtime.remote_graph.policy import NetworkPolicy
    with pytest.raises(ControlError, match='REMOTE_GRAPH_INVALID_CONFIG'):
        await NetworkPolicy().resolve(('127.0.0.1', 7687))
    result = await NetworkPolicy(allowed_cidrs=('127.0.0.1/32',)).resolve(('127.0.0.1', 7687))
    assert result[0].host == '127.0.0.1'


@pytest.mark.anyio
async def test_neo4j_checks_each_secondary_connection_and_keeps_tls_name(monkeypatch):
    from agent_runtime.remote_graph.adapters import AdapterContext
    from agent_runtime.remote_graph.neo4j import Neo4jAdapter
    from agent_runtime.remote_graph.policy import NetworkPolicy
    from pydantic import SecretStr

    answers = ['93.184.216.34']
    async def dns(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port)) for ip in answers]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
    captured = {}
    monkeypatch.setattr('agent_runtime.remote_graph.neo4j.AsyncGraphDatabase.driver',
                        lambda *args, **kwargs: captured.update(kwargs))
    ctx = AdapterContext(connection=SimpleNamespace(public=SimpleNamespace(
        uri='bolt+s://db.example:7687', username='reader'), password=SecretStr('secret')),
        deadline=time.monotonic() + 5, max_rows=10, max_bytes=1000,
        network_policy=NetworkPolicy(allowed_hosts=('db.example',)))
    Neo4jAdapter()._driver(ctx)
    first = await captured['resolver'](('db.example', 7687))
    assert first[0].host == '93.184.216.34'
    assert first[0]._host_name == 'db.example'
    answers[:] = ['127.0.0.1']
    with pytest.raises(ControlError, match='REMOTE_GRAPH_INVALID_CONFIG'):
        await captured['resolver'](('db.example', 7687))


@pytest.mark.parametrize('query', [
    "MATCH (n) RETURN n.set, 'SET DELETE; LOAD CSV'",
    'MATCH (n:`set`) RETURN n',
    'CALL { MATCH (n) RETURN count(n) AS c } RETURN c',
    'MATCH (n) CALL (n) { RETURN n AS x } RETURN x',
    'CALL db.labels() YIELD label RETURN label',
    'MATCH p=(a)-[*1..2]->(b) RETURN p LIMIT 4;',
    'MATCH (n) /* LOAD CSV */ RETURN n // CALL evil()\n',
])
def test_valid_query_entries_are_not_keyword_substring_filtered(query):
    from agent_runtime.remote_graph.policy import check_query
    check_query(query)


@pytest.mark.parametrize('query', [
    'RETURN 1; RETURN 2',
    "LOAD CSV FROM 'https://private/' AS row RETURN row",
    'CALL apoc.cypher.run($query, {}) YIELD value RETURN value',
    'CALL `apoc`.`load`.`json`($url) YIELD value RETURN value',
    'RETURN apoc.load.json($url)',
    'SHOW USERS',
    'USE other MATCH (n) RETURN n',
    'CYPHER runtime=slotted MATCH (n) RETURN n',
])
def test_unknown_procedures_management_and_outbound_entries_are_rejected(query):
    from agent_runtime.remote_graph.policy import check_query
    with pytest.raises(ControlError, match='REMOTE_GRAPH_QUERY_REJECTED'):
        check_query(query)


def test_plan_metadata_is_required_even_after_lexical_check():
    from agent_runtime.remote_graph.policy import check_query_type
    check_query_type('r')
    for query_type in ['w', 'rw', 's', None, 'unknown']:
        with pytest.raises(ControlError, match='REMOTE_GRAPH_QUERY_REJECTED'):
            check_query_type(query_type)


def test_readonly_privileges_require_evidence_and_reject_write_or_admin_grants():
    from agent_runtime.remote_graph.policy import check_privileges
    reader = [dict(access='GRANTED', action=action, graph='*', resource='all_properties', segment='NODE(*)')
              for action in ['access', 'match']]
    check_privileges(reader, 'neo4j')
    for actions in [[], [dict(access='GRANTED', action='write', graph='neo4j')],
                    reader + [dict(access='GRANTED', action='role_management', graph='*')],
                    reader + [dict(access='GRANTED', action='execute_boosted', graph='*')]]:
        with pytest.raises(ControlError, match='REMOTE_GRAPH_READ_ONLY_REQUIRED'):
            check_privileges(actions, 'neo4j')

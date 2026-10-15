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
async def test_database_and_routing_hosts_resolve_without_allowlists(monkeypatch):
    from agent_runtime.remote_graph.policy import NetworkPolicy
    policy = NetworkPolicy()
    answers = ['93.184.216.34', '127.0.0.1', '10.0.0.1', '169.254.169.254', '93.184.216.34']
    async def dns(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port)) for ip in answers]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
    for host in ('db.example', 'routing.example'):
        resolved = await policy.resolve((host, 7687))
        assert [item.host for item in resolved] == list(dict.fromkeys(answers))
        assert all(item.port == 7687 and item.tls_hostname == host for item in resolved)


@pytest.mark.anyio
@pytest.mark.parametrize('host', ['93.184.216.34', '127.0.0.1', '192.168.71.2',
                                '169.254.169.254', '::1', 'fd00::1'])
async def test_ip_addresses_connect_without_allowlists(host):
    from agent_runtime.remote_graph.policy import NetworkPolicy
    result = await NetworkPolicy().resolve((host, 7687))
    assert result[0].host == host
    assert result[0].tls_hostname == host


@pytest.mark.anyio
async def test_neo4j_resolves_secondary_connections_and_keeps_tls_names(monkeypatch):
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
        network_policy=NetworkPolicy())
    Neo4jAdapter()._driver(ctx)
    first = await captured['resolver'](('db.example', 7687))
    assert first[0].host == '93.184.216.34'
    assert first[0]._host_name == 'db.example'
    answers[:] = ['127.0.0.1']
    second = await captured['resolver'](('routing.example', 7687))
    assert second[0].host == '127.0.0.1'
    assert second[0]._host_name == 'routing.example'


@pytest.mark.anyio
async def test_empty_dns_result_is_reported_as_unreachable(monkeypatch):
    from agent_runtime.remote_graph.policy import NetworkPolicy

    async def dns(*args, **kwargs):
        return []

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', dns)
    with pytest.raises(ControlError, match='REMOTE_GRAPH_UNREACHABLE'):
        await NetworkPolicy().resolve(('missing.example', 7687))


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

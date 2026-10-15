"""Initial address fallback must end before the first application command."""
import asyncio
import io
import socket
import ssl
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from agent_runtime.execution_store import ControlError
from agent_runtime.remote_graph import falkor_protocol as protocol
from agent_runtime.remote_graph.adapters import AdapterContext
from agent_runtime.remote_graph.policy import NetworkPolicy, VerifiedAddress


@pytest.fixture
def transport(monkeypatch):
    """Control network/time at the socket boundary, keeping AUTH and RESP real."""
    state = SimpleNamespace(now=100.0, sockets=[], attempts=[], resolutions=0, creations=0,
                            outcomes=['refused', 'ok'], tls_names=[], phase=None)
    addresses = [VerifiedAddress('2001:db8::1', 6379, 'graph.example'),
                 VerifiedAddress('192.0.2.1', 6379, 'graph.example')]
    state.addresses = addresses

    class Policy:
        async def resolve(self, target):
            assert target == ('graph.example', 6379)
            state.resolutions += 1
            assert state.resolutions == 1, 'Fallback must not resolve again'
            return addresses

    class Sock:
        def __init__(self, family, kind):
            self.index = state.creations
            state.creations += 1
            self.outcome = state.outcomes[self.index]
            if self.outcome == 'unsupported':
                raise OSError('address family unavailable')
            self.closed = False
            self.sent = []
            self.timeout = None
            self.wire = io.BytesIO(b'+OK\r\n:7\r\n')
            assert family == (socket.AF_INET6 if self.index == 0 else socket.AF_INET)
            state.sockets.append(self)

        def settimeout(self, value):
            assert value > 0
            self.timeout = value

        def connect(self, target):
            assert target == (addresses[self.index].host, 6379)
            assert all(s.closed for s in state.sockets if s is not self)
            state.attempts.append(target)
            if self.outcome == 'refused':
                raise ConnectionRefusedError('unreachable candidate')
            if self.outcome in {'timeout', 'expired'}:
                state.now += self.timeout if self.outcome == 'timeout' else 10
                raise TimeoutError('connect timed out')
            if self.outcome == 'tcp_budget_used':
                state.now += self.timeout

        def do_handshake(self):
            if self.outcome == 'tls_timeout':
                state.now += self.timeout
                raise TimeoutError('handshake timed out')
            if self.outcome == 'bad_certificate':
                raise ssl.SSLCertVerificationError('hostname mismatch')

        def sendall(self, payload):
            self.sent.append(payload)
            if state.phase == 'auth_send':
                raise OSError('partial AUTH send')
            if state.phase == 'query_send' and len(self.sent) == 2:
                raise OSError('partial query send')

        def recv(self, count):
            if state.phase == 'auth_recv':
                raise OSError('AUTH response lost')
            if state.phase == 'query_recv' and len(self.sent) == 2:
                raise OSError('query response lost')
            return self.wire.read(count)

        def shutdown(self, how):
            self.closed = True

        def close(self):
            self.closed = True

    class TLS:
        def wrap_socket(self, sock, *, server_hostname, do_handshake_on_connect):
            assert not do_handshake_on_connect
            state.tls_names.append(server_hostname)
            return sock

    monkeypatch.setattr(protocol, 'time', SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(protocol, 'socket', SimpleNamespace(
        socket=Sock, AF_INET6=socket.AF_INET6, AF_INET=socket.AF_INET,
        SOCK_STREAM=socket.SOCK_STREAM, SHUT_RDWR=socket.SHUT_RDWR))
    monkeypatch.setattr(protocol.ssl, 'create_default_context', lambda: TLS())
    state.ctx = AdapterContext(
        connection=SimpleNamespace(public=SimpleNamespace(
            uri='redis://graph.example:6379', username='', database='graph'),
            password=SecretStr('private-test-value')),
        deadline=110.0, max_rows=128, max_bytes=65536, network_policy=Policy())
    return state


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['refused', 'timeout'])
async def test_first_address_failure_leaves_time_for_next_approved_address(transport, failure):
    transport.outcomes = [failure, 'ok']
    async with protocol.open_connection(transport.ctx) as connection:
        assert await connection.command('GRAPH.RO_QUERY', 'graph', 'RETURN 7') == 7
        assert transport.now < transport.ctx.deadline
        assert connection.deadline == transport.ctx.deadline
    assert transport.attempts == [('2001:db8::1', 6379), ('192.0.2.1', 6379)]
    assert transport.sockets[0].sent == []
    assert len(transport.sockets[1].sent) == 2
    assert all(sock.closed for sock in transport.sockets)


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['tls_timeout', 'bad_certificate', 'tcp_budget_used'])
async def test_tls_fallback_retains_original_hostname_and_authenticates_once(transport, failure):
    transport.outcomes = [failure, 'ok']
    transport.ctx.connection.public.uri = 'rediss://graph.example:6379'
    async with protocol.open_connection(transport.ctx):
        pass
    assert len(transport.attempts) == 2
    assert transport.tls_names == ['graph.example', 'graph.example']
    assert [len(sock.sent) for sock in transport.sockets] == [0, 1]
    assert all(sock.closed for sock in transport.sockets)


@pytest.mark.anyio
async def test_unavailable_ipv6_socket_family_falls_back_to_ipv4(transport):
    transport.outcomes = ['unsupported', 'ok']
    async with protocol.open_connection(transport.ctx) as connection:
        assert await connection.command('GRAPH.RO_QUERY', 'graph', 'RETURN 7') == 7
    assert transport.attempts == [('192.0.2.1', 6379)]
    assert transport.creations == 2
    assert transport.sockets[0].closed


@pytest.mark.anyio
@pytest.mark.parametrize('failure,error', [('refused', ConnectionRefusedError), ('timeout', TimeoutError)])
async def test_all_approved_addresses_fail_within_original_deadline(transport, failure, error):
    transport.outcomes = [failure, failure]
    with pytest.raises(error):
        async with protocol.open_connection(transport.ctx):
            pytest.fail('failed addresses yielded a connection')
    assert len(transport.attempts) == 2
    assert transport.now <= transport.ctx.deadline
    assert all(sock.closed and not sock.sent for sock in transport.sockets)


@pytest.mark.anyio
async def test_expired_overall_deadline_prevents_another_address(transport):
    transport.outcomes = ['expired', 'ok']
    with pytest.raises(TimeoutError):
        async with protocol.open_connection(transport.ctx):
            pytest.fail('expired call yielded a connection')
    assert len(transport.attempts) == 1
    assert transport.sockets[0].closed


@pytest.mark.anyio
async def test_waiting_for_connect_worker_also_spends_attempt_budget(transport, monkeypatch):
    from dataclasses import replace

    transport.ctx = replace(transport.ctx, deadline=100.08)
    to_thread = asyncio.to_thread

    async def queued_connect(function, *args):
        if function.__name__ == 'connect' and args[0][0] == '2001:db8::1':
            try:
                await asyncio.Event().wait()
            finally:
                transport.now += transport.sockets[0].timeout
        return await to_thread(function, *args)

    monkeypatch.setattr(protocol, 'asyncio', SimpleNamespace(
        to_thread=queued_connect, timeout=asyncio.timeout))

    async def run():
        async with protocol.open_connection(transport.ctx) as connection:
            return await connection.command('GRAPH.RO_QUERY', 'graph', 'RETURN 7')

    assert await asyncio.wait_for(run(), .5) == 7
    assert transport.attempts == [('192.0.2.1', 6379)]
    assert all(sock.closed for sock in transport.sockets)


@pytest.mark.anyio
@pytest.mark.parametrize('phase', ['auth_send', 'auth_recv', 'query_send', 'query_recv'])
async def test_no_address_retry_after_auth_or_query_send_begins(transport, phase):
    transport.outcomes = ['ok', 'ok']
    transport.phase = phase
    with pytest.raises(OSError):
        async with protocol.open_connection(transport.ctx) as connection:
            await connection.command('GRAPH.RO_QUERY', 'graph', 'RETURN 7')
    assert len(transport.attempts) == 1
    assert len(transport.sockets[0].sent) == (2 if phase.startswith('query') else 1)
    assert transport.sockets[0].closed


@pytest.mark.anyio
async def test_cancellation_during_fallback_closes_current_socket_without_more_attempts(transport, monkeypatch):
    transport.addresses.append(VerifiedAddress('192.0.2.2', 6379, 'graph.example'))
    transport.outcomes = ['refused', 'ok', 'ok']
    started, closed, finished = threading.Event(), threading.Event(), threading.Event()
    factory = protocol.socket.socket

    def socket_factory(*args):
        sock = factory(*args)
        if sock.index == 1:
            def connect(target):
                transport.attempts.append(target)
                started.set()
                try:
                    assert closed.wait(2), 'cancel did not close owned socket'
                    raise OSError('closed')
                finally:
                    finished.set()

            original_close = sock.close

            def close():
                original_close()
                closed.set()

            sock.connect, sock.close = connect, close
        return sock

    monkeypatch.setattr(protocol.socket, 'socket', socket_factory)

    async def run():
        async with protocol.open_connection(transport.ctx):
            pytest.fail('cancelled connection yielded')

    task = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(finished.wait, 1)
        assert len(transport.attempts) == 2
        assert all(sock.closed and not sock.sent for sock in transport.sockets)
    finally:
        closed.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_empty_dns_answer_prevents_all_connection_attempts(transport, monkeypatch):
    from dataclasses import replace

    async def resolve(*args, **kwargs):
        return []

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    policy = NetworkPolicy()
    with pytest.raises(ControlError, match='REMOTE_GRAPH_UNREACHABLE'):
        async with protocol.open_connection(replace(transport.ctx, network_policy=policy)):
            pytest.fail('empty DNS answer yielded a connection')
    assert transport.sockets == []


@pytest.mark.anyio
async def test_approved_address_fallback_uses_real_tcp_and_one_dns_snapshot(monkeypatch):
    loop = asyncio.get_running_loop()
    peer_result = loop.create_future()
    auth = b'*2\r\n$4\r\nAUTH\r\n$7\r\nprivate\r\n'
    query = b'*3\r\n$14\r\nGRAPH.RO_QUERY\r\n$5\r\ngraph\r\n$8\r\nRETURN 7\r\n'

    async def peer(reader, writer):
        try:
            assert await reader.readexactly(len(auth)) == auth
            writer.write(b'+OK\r\n')
            await writer.drain()
            assert await reader.readexactly(len(query)) == query
            writer.write(b':7\r\n')
            await writer.drain()
            assert await reader.read() == b''
            peer_result.set_result(True)
        except Exception as exc:
            peer_result.set_exception(exc)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(peer, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    resolutions = []

    async def resolve(host, service, **kwargs):
        resolutions.append((host, service))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, port))
                for ip in ['127.0.0.2', '127.0.0.1']]

    monkeypatch.setattr(loop, 'getaddrinfo', resolve)
    ctx = AdapterContext(
        connection=SimpleNamespace(public=SimpleNamespace(
            uri=f'redis://fallback.test:{port}', username='', database='graph'),
            password=SecretStr('private')),
        deadline=time.monotonic() + 3, max_rows=128, max_bytes=65536,
        network_policy=NetworkPolicy())
    try:
        async with protocol.open_connection(ctx) as connection:
            assert await connection.command('GRAPH.RO_QUERY', 'graph', 'RETURN 7') == 7
        assert await asyncio.wait_for(peer_result, 2)
        assert resolutions == [('fallback.test', port)]
    finally:
        server.close()
        await server.wait_closed()

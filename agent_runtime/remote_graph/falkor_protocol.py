"""RESP2 over one owned socket; reject lengths before reading/allocating bodies.

Blocking socket operations run off-loop, while cancellation shuts down that same
socket. There is no pool, discovery, retry, redirect, or unbounded Redis parser.
"""
import asyncio
import ipaddress
import socket
import ssl
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from agent_runtime.execution_store import ControlError


def reject():
    raise ControlError('REMOTE_GRAPH_QUERY_REJECTED', 422)


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError
    return value


def read_response(sock, *, max_bytes, deadline, max_rows=None):
    spent = 0

    def take(count):
        nonlocal spent
        if count < 0 or spent + count > max_bytes:
            reject()
        chunks = bytearray()
        while len(chunks) < count:
            sock.settimeout(remaining(deadline))
            chunk = sock.recv(min(4096, count - len(chunks)))
            if not chunk:
                raise OSError('Incomplete response')
            chunks.extend(chunk)
        spent += count
        return bytes(chunks)

    def parse(depth=0, row_array=False):
        if depth > 32:
            reject()
        line = bytearray()
        while not line.endswith(b'\r\n'):
            if len(line) >= 4096:
                reject()
            line.extend(take(1))
        marker, value = line[:1], bytes(line[1:-2])
        if marker == b'-':
            # Never return server text, which can echo queries or credentials.
            lowered = value.lower()
            code = ('REMOTE_GRAPH_AUTH_FAILED' if lowered.startswith((b'wrongpass', b'noauth'))
                    else 'REMOTE_GRAPH_TIMEOUT' if b'timed out' in lowered
                    else 'REMOTE_GRAPH_QUERY_REJECTED')
            raise ControlError(code, 422)
        if marker == b'+':
            return value
        if marker == b':':
            return int(value)
        if marker in (b'$', b'*'):
            size = int(value)
            if size == -1:
                return None
            if size < 0:
                reject()
            if marker == b'$':
                if size + 2 > max_bytes - spent:
                    reject()
                data = take(size + 2)
                if not data.endswith(b'\r\n'):
                    reject()
                return data[:-2]
            # Each child needs at least three wire bytes, even an empty string.
            if size > (max_bytes - spent) // 3 or (row_array and max_rows is not None and size > max_rows):
                reject()
            return [parse(depth + 1, depth == 0 and index == 1) for index in range(size)]
        reject()

    try:
        return parse()
    except (ValueError, UnicodeError):
        reject()


def pack_command(args):
    encoded = [str(arg).encode('utf-8') for arg in args]
    return f'*{len(encoded)}\r\n'.encode() + b''.join(
        f'${len(arg)}\r\n'.encode() + arg + b'\r\n' for arg in encoded)


class Connection:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    async def command(self, *args, max_bytes=65536, max_rows=None):
        def exchange():
            self.sock.settimeout(remaining(self.deadline))
            self.sock.sendall(pack_command(args))
            return read_response(self.sock, max_bytes=max_bytes,
                                 deadline=self.deadline, max_rows=max_rows)
        return await asyncio.to_thread(exchange)

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


@asynccontextmanager
async def open_connection(ctx):
    remaining(ctx.deadline)
    uri = urlsplit(ctx.connection.public.uri)
    addresses = await ctx.network_policy.resolve((uri.hostname, uri.port or 6379))
    address = addresses[0]
    # The socket exists before the first cancellable connect operation.
    ip = ipaddress.ip_address(address.host)
    sock = socket.socket(socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_STREAM)
    connection = Connection(sock, ctx.deadline)
    try:
        sock.settimeout(remaining(ctx.deadline))
        await asyncio.to_thread(sock.connect, (address.host, address.port))
        if uri.scheme == 'rediss':
            # No await between wrap and ownership update: cancellation cannot lose
            # the SSLSocket after wrap_socket detaches the original descriptor.
            connection.sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=address.tls_hostname, do_handshake_on_connect=False)
            connection.sock.settimeout(remaining(ctx.deadline))
            await asyncio.to_thread(connection.sock.do_handshake)
        password = ctx.connection.password.get_secret_value()
        user = ctx.connection.public.username
        await connection.command('AUTH', *([user, password] if user else [password]))
        yield connection
    finally:
        connection.close()

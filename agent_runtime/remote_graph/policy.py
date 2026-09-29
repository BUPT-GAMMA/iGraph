"""Explicit egress and executable-entry policy, backed by server read metadata."""
import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass

from agent_runtime.execution_store import ControlError


@dataclass(frozen=True)
class VerifiedAddress:
    host: str
    port: int
    tls_hostname: str


class NetworkPolicy:
    def __init__(self, allowed_hosts=(), allowed_cidrs=()):
        self.hosts = {h.lower().rstrip('.') for h in allowed_hosts}
        try:
            self.networks = [ipaddress.ip_network(cidr, strict=False) for cidr in allowed_cidrs]
        except ValueError:
            raise ControlError('REMOTE_GRAPH_INVALID_CONFIG', 422) from None

    async def resolve(self, address):
        host, port = address[:2]
        hostname = str(host).lower().rstrip('.')
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            literal = None
        if literal is None:
            if hostname not in self.hosts:
                raise ControlError('REMOTE_GRAPH_INVALID_CONFIG', 422)
            entries = await asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(entry[4] for entry in entries))
        else:
            addresses = [(str(literal), port)]
        if not addresses:
            raise ControlError('REMOTE_GRAPH_UNREACHABLE', 502)
        for item in addresses:
            ip = ipaddress.ip_address(item[0])
            if not (any(ip in net for net in self.networks)
                    or (ip.is_global and hostname in self.hosts)):
                raise ControlError('REMOTE_GRAPH_INVALID_CONFIG', 422)
        return [VerifiedAddress(str(item[0]), int(item[1]), hostname) for item in addresses]


ALLOWED_PROCEDURES = {'db.labels', 'db.relationshiptypes', 'db.schema.nodetypeproperties',
                      'db.schema.reltypeproperties'}
# Namespaced functions are limited to built-ins without external execution.
SAFE_NAMESPACED_FUNCTIONS = {'point.distance', 'point.withinbbox', 'duration.between',
    'duration.inmonths', 'duration.indays', 'duration.inseconds',
    'datetime.transaction', 'datetime.statement', 'datetime.realtime',
    'date.transaction', 'date.statement', 'date.realtime',
    'localdatetime.transaction', 'localdatetime.statement', 'localdatetime.realtime',
    'time.transaction', 'time.statement', 'time.realtime',
    'localtime.transaction', 'localtime.statement', 'localtime.realtime',
    'vector.similarity.cosine', 'vector.similarity.euclidean'}
_TOKENS = re.compile(r"\s+|//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:``|[^`])*`|[^\W\d]\w*|.", re.DOTALL)


def check_query(query, builtins=None):
    tokens = []
    for match in _TOKENS.finditer(query):
        token = match.group()
        if token.isspace() or token.startswith(('//', '/*')):
            continue
        if token.startswith(("'", '"')):
            tokens.append('<literal>')
        else:
            tokens.append(token.strip('`').replace('``', '`').lower())
    if tokens and tokens[-1] == ';':
        tokens.pop()
    if not tokens or tokens[0] not in {'match', 'optional', 'with', 'return', 'unwind', 'call'} or ';' in tokens:
        raise ControlError('REMOTE_GRAPH_QUERY_REJECTED', 422)
    for i, token in enumerate(tokens):
        previous = tokens[i - 1] if i else None
        if token in {'load', 'use'} and previous not in {'.', ':', '$'}:
            if token == 'use' or tokens[i + 1:i + 2] == ['csv']:
                raise ControlError('REMOTE_GRAPH_QUERY_REJECTED', 422)
        if token == 'call' and previous not in {'.', ':', '$'}:
            if tokens[i + 1:i + 2] in [['{'], ['(']]:
                continue
            j, parts = i + 1, []
            while j < len(tokens) and tokens[j] != '(':
                parts.append(tokens[j])
                j += 1
            if ''.join(parts) not in ALLOWED_PROCEDURES:
                raise ControlError('REMOTE_GRAPH_QUERY_REJECTED', 422)
        if token == '(' and i >= 3 and tokens[i - 2] == '.':
            j = i - 1
            while j >= 2 and tokens[j - 1] == '.':
                j -= 2
            name = ''.join(tokens[j:i])
            if name not in SAFE_NAMESPACED_FUNCTIONS and not (j > 0 and tokens[j - 1] == 'call' and name in ALLOWED_PROCEDURES):
                raise ControlError('REMOTE_GRAPH_QUERY_REJECTED', 422)
        if token == '(' and i > 0 and re.fullmatch(r'[^\W\d]\w*', tokens[i - 1]):
            j = i - 1
            while j >= 2 and tokens[j - 1] == '.':
                j -= 2
            name = ''.join(tokens[j:i])
            syntax = {'match', 'optional', 'where', 'with', 'return', 'call', 'in',
                      'and', 'or', 'xor', 'not', 'when', 'then', 'else',
                      'all', 'any', 'none', 'single'}
            procedure = j > 0 and tokens[j - 1] == 'call' and name in ALLOWED_PROCEDURES
            if builtins is not None and name not in syntax and not procedure and name not in builtins:
                raise ControlError('REMOTE_GRAPH_QUERY_REJECTED', 422)


def check_query_type(query_type):
    if query_type != 'r':
        raise ControlError('REMOTE_GRAPH_QUERY_REJECTED', 422)


def check_privileges(rows, database):
    readable, accessible = False, False
    allowed = {'access', 'match', 'read', 'traverse', 'show_index', 'show_constraint',
               'execute', 'execute_function', 'load', 'show_transaction', 'show_connection'}
    for row in rows:
        if row.get('access') == 'DENIED':
            continue
        if row.get('access') != 'GRANTED' or row.get('action') not in allowed:
            raise ControlError('REMOTE_GRAPH_READ_ONLY_REQUIRED', 422)
        if row.get('graph') in {'*', database}:
            accessible |= row['action'] == 'access'
            readable |= row['action'] in {'read', 'match'}
    if not (readable and accessible):
        raise ControlError('REMOTE_GRAPH_READ_ONLY_REQUIRED', 422)

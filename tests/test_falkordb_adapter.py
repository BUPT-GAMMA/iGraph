"""Falkor safety boundaries: real wire parser and invocation-owned sockets."""
import asyncio
import base64
import importlib
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from agent_runtime.execution_store import ControlError
from agent_runtime.remote_graph.adapters import AdapterContext
from agent_runtime.remote_graph.contracts import QueryRequest
from agent_runtime.remote_graph.policy import NetworkPolicy
from agent_runtime.remote_graph.results import bounded_rows


def module(name='falkordb'):
    try:
        return importlib.import_module('agent_runtime.remote_graph.' + name)
    except ModuleNotFoundError:
        pytest.fail('FalkorDB bounded adapter is not implemented')


def context(**kwargs):
    public = SimpleNamespace(uri='redis://127.0.0.1:21188', username='', database='test', kind='falkordb')
    return AdapterContext(connection=SimpleNamespace(public=public, password=SecretStr('private')),
        deadline=time.monotonic()+5, max_rows=kwargs.get('max_rows',128), max_bytes=kwargs.get('max_bytes',65536),
        network_policy=NetworkPolicy(allowed_cidrs=['127.0.0.1/32']))


class Wire:
    def __init__(self, data):
        self.data=io.BytesIO(data)
        self.reads=[]
    def recv(self, count):
        self.reads.append(count)
        return self.data.read(count)
    def settimeout(self, value):
        assert value > 0


def fixture():
    payload=json.loads(Path('tests/fixtures/falkordb_compact_path.resp2.json').read_text())
    return base64.b64decode(payload['response_base64'])


def test_result_preserves_entity_types_and_large_integers():
    wire=Wire(fixture())
    raw=module('falkor_protocol').read_response(wire, max_bytes=65536, deadline=time.monotonic()+2)
    values=module('falkor_values').decode_rows(raw, {'labels':['WireA','WireB'], 'relationships':['WIRE_R'], 'properties':['name','weight']})
    node, edge, path, floating, boolean, integer=values[1][0]
    assert node['labels']==['WireA'] and node['properties']=={'name':'甲'}
    assert edge['start_node']=='0' and edge['end_node']=='1'
    assert path['nodes'][1]['element_id']=='1' and path['relationships'][0]==edge
    assert floating==2.5 and boolean is True and integer==9007199254740993
    encoded=bounded_rows(values[1],max_rows=10,max_bytes=65536)
    assert encoded.rows[0][-1]=={'type':'integer','value':'9007199254740993'}


@pytest.mark.parametrize('frame',[b'$999999999\r\n',b'*999999999\r\n', b'*1\r\n'*34+b':1\r\n',b'*3\r\n$30\r\n'+b'x'*30+b'\r\n'+b'$30\r\n'])
def test_byte_limit_applies_before_unbounded_materialization(frame):
    wire=Wire(frame)
    with pytest.raises(ControlError,match='REMOTE_GRAPH_QUERY_REJECTED'):
        module('falkor_protocol').read_response(wire,max_bytes=64,deadline=time.monotonic()+2)
    assert max(wire.reads)<=64


def test_unknown_mapping_and_type_fail_explicitly():
    decode=module('falkor_values').decode_rows
    for value in ([8,[1,[50],[]]],[999,'x']):
        with pytest.raises(ControlError,match='REMOTE_GRAPH_QUERY_REJECTED'):
            decode([[[1,b'x']],[[value]],[]],{'labels':[], 'relationships':[], 'properties':[]})


@pytest.mark.parametrize('query',[
    'RETURN range(1,1000000000)', 'RETURN `range`(1,2)', 'RETURN RaNgE /*x*/ (1,2)',
    'CALL db.index.fulltext.queryNodes("x","x")', 'CALL { RETURN 1 }',
    'RETURN split("x", "")','RETURN custom(1)', 'LOAD CSV FROM "x" AS x RETURN x',
    'RETURN 1; CREATE ()', 'RETURN apoc.cypher.run("x",{})',
])
def test_unsafe_entrypoints_rejected(query):
    with pytest.raises(ControlError,match='REMOTE_GRAPH_QUERY_REJECTED'):
        module().check_query(query)


def test_literals_comments_and_identifiers_are_not_functions():
    module().check_query("MATCH (n:`range`) WHERE n.name='range(1,2)' /* range( */ RETURN count(n)")


def test_params_are_encoded_not_concatenated_as_syntax():
    encode=module('falkor_values').parameter_prefix
    value='\" // 注释\n O\'Reilly \\ /* */'
    result=encode({'x':value,'y':[None,True,2]})
    assert result=='CYPHER x='+json.dumps(value,ensure_ascii=False)+' y=[null,true,2] '
    with pytest.raises(ControlError):
        encode({'x CREATE () //':1})


@pytest.mark.anyio
async def test_never_falls_back_to_writable_command(monkeypatch):
    mod=module()
    commands=[]
    class Connection:
        async def command(self,*args,**kwargs):
            commands.append(args[0])
            raise ControlError('REMOTE_GRAPH_QUERY_REJECTED',422)
        def close(self): pass
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def connect(ctx): yield Connection(),{'TIMEOUT_MAX':3000,'RESULTSET_SIZE':128}
    monkeypatch.setattr(mod,'connect',connect)
    with pytest.raises(ControlError):
        await mod.FalkorDBAdapter().execute(context(),QueryRequest(query='MATCH (n) SET n.x=1 RETURN n'))
    assert commands==['GRAPH.RO_QUERY']


@pytest.mark.anyio
async def test_cancel_closes_owned_connection_only(monkeypatch):
    mod=module()
    from contextlib import asynccontextmanager
    entered=[asyncio.Event(),asyncio.Event()]
    release=asyncio.Event()
    closed=[]
    calls=[]
    class Connection:
        def __init__(self,index): self.index=index
        async def command(self,*args,**kwargs):
            entered[self.index].set()
            await release.wait()
            return [[[1,b'v']],[[[3,self.index]]],[]]
    @asynccontextmanager
    async def connect(ctx):
        index=len(calls); calls.append(ctx)
        try: yield Connection(index),{'TIMEOUT_MAX':3000,'RESULTSET_SIZE':128}
        finally: closed.append(index)
    monkeypatch.setattr(mod,'connect',connect)
    adapter=mod.FalkorDBAdapter()
    one=asyncio.create_task(adapter.execute(context(),QueryRequest(query='RETURN 1')))
    await entered[0].wait()
    two=asyncio.create_task(adapter.execute(context(),QueryRequest(query='RETURN 2')))
    await entered[1].wait()
    one.cancel()
    with pytest.raises(asyncio.CancelledError): await one
    assert closed==[0] and not two.done()
    release.set()
    assert (await two).rows==[[1]]
    assert closed==[0,1]


@pytest.mark.anyio
async def test_tls_uses_checked_ip_and_original_hostname(monkeypatch):
    mod=module('falkor_protocol')
    from agent_runtime.remote_graph.policy import VerifiedAddress
    seen={}
    class Sock:
        def settimeout(self,value): assert 0<value<=5
        def connect(self,target): seen['target']=target
        def do_handshake(self): seen['handshake']=True
        def shutdown(self,how): pass
        def close(self): seen['closed']=True
    sock=Sock()
    class TLS:
        def wrap_socket(self,original,**kwargs):
            assert original is sock
            seen.update(kwargs)
            return sock
    class Policy:
        async def resolve(self,target):
            assert target==('graph.example',6379)
            return [VerifiedAddress('203.0.113.7',6379,'graph.example')]
    monkeypatch.setattr(mod.socket,'socket',lambda *args:sock)
    monkeypatch.setattr(mod.ssl,'create_default_context',lambda:TLS())
    async def command(self,*args,**kwargs): return b'OK'
    monkeypatch.setattr(mod.Connection,'command',command)
    ctx=context()
    ctx.connection.public.uri='rediss://graph.example:6379'
    from dataclasses import replace
    async with mod.open_connection(replace(ctx,network_policy=Policy())): pass
    assert seen=={'target':('203.0.113.7',6379),'server_hostname':'graph.example',
                  'do_handshake_on_connect':False,'handshake':True,'closed':True}


@pytest.mark.anyio
async def test_cancellation_during_connect_closes_socket_and_worker(monkeypatch):
    import threading
    mod=module('falkor_protocol')
    started,closed,finished=threading.Event(),threading.Event(),threading.Event()
    class Sock:
        def settimeout(self,value): pass
        def connect(self,target):
            started.set()
            closed.wait(2)
            finished.set()
            raise OSError('closed')
        def shutdown(self,how): closed.set()
        def close(self): closed.set()
    monkeypatch.setattr(mod.socket,'socket',lambda *args:Sock())
    async def run():
        async with mod.open_connection(context()): pytest.fail('cancelled connection yielded')
    task=asyncio.create_task(run())
    assert await asyncio.to_thread(started.wait,1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert closed.is_set() and await asyncio.to_thread(finished.wait,1)


@pytest.mark.anyio
async def test_cancellation_during_recv_unblocks_owned_worker():
    import socket
    import threading
    mod=module('falkor_protocol')
    client,server=socket.socketpair()
    received=threading.Event()
    connection=mod.Connection(client,time.monotonic()+3)
    def peer():
        server.recv(4096)
        received.set()
        assert server.recv(4096)==b''
        server.close()
    worker=asyncio.create_task(asyncio.to_thread(peer))
    async def run():
        try: await connection.command('PING')
        finally: connection.close()
    task=asyncio.create_task(run())
    assert await asyncio.to_thread(received.wait,1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    await asyncio.wait_for(worker,1)


@pytest.mark.anyio
@pytest.mark.parametrize('bad_key,bad_value',[('TIMEOUT_MAX',0),('TIMEOUT_DEFAULT',1001),('RESULTSET_SIZE',129),('QUERY_MEM_CAPACITY',0),('version',42008)])
async def test_effective_server_caps_and_version_required(monkeypatch,bad_key,bad_value):
    mod=module()
    from contextlib import asynccontextmanager
    limits={'TIMEOUT_MAX':3000,'TIMEOUT_DEFAULT':1000,'RESULTSET_SIZE':128,'QUERY_MEM_CAPACITY':8388608}
    limits[bad_key]=bad_value
    class Connection:
        async def command(self,*args,**kwargs):
            if args[0]=='MODULE': return [[b'name',b'graph',b'ver',limits.get('version',42007)]]
            return [args[-1].encode(),limits[args[-1]]]
    @asynccontextmanager
    async def open_connection(ctx): yield Connection()
    monkeypatch.setattr(mod,'open_connection',open_connection)
    with pytest.raises(ControlError,match='REMOTE_GRAPH_INVALID_CONFIG'):
        async with mod.connect(context()): pytest.fail('Unsafe server admitted')


def test_resp_row_count_checked_before_reading_rows():
    wire=Wire(b'*3\r\n*0\r\n*129\r\n')
    with pytest.raises(ControlError):
        module('falkor_protocol').read_response(wire,max_bytes=65536,max_rows=128,deadline=time.monotonic()+1)


@pytest.mark.anyio
async def test_truncated_metadata_is_not_used_for_entity_ids(monkeypatch):
    mod=module()
    class Connection:
        async def command(self,*args,**kwargs):
            return [[[1,b'label']],[[[2,b'A']]]*128,[]]
    with pytest.raises(ControlError):
        await mod.FalkorDBAdapter()._mappings(context(),Connection(),{'TIMEOUT_MAX':3000,'RESULTSET_SIZE':128})


@pytest.mark.anyio
async def test_expired_budget_does_not_resolve_or_dial():
    from dataclasses import replace
    class Policy:
        async def resolve(self,address): pytest.fail('expired call resolved network')
    ctx=replace(context(),deadline=time.monotonic()-1,network_policy=Policy())
    with pytest.raises(TimeoutError):
        async with module('falkor_protocol').open_connection(ctx): pass


@pytest.mark.parametrize('query',['MATCH p=(n)-[*]->(m) RETURN p','MATCH (n) RETURN [(n)-->(m) | m]'])
def test_unverified_eager_path_expansion_is_rejected(query):
    with pytest.raises(ControlError): module().check_query(query)


@pytest.mark.anyio
async def test_real_tls_certificate_validates_original_name(tmp_path,monkeypatch):
    import datetime
    import socket
    import ssl
    from dataclasses import replace
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from agent_runtime.remote_graph.policy import VerifiedAddress
    mod=module('falkor_protocol')
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'graph.test')])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
          .not_valid_after(now+datetime.timedelta(days=1))
          .add_extension(x509.SubjectAlternativeName([x509.DNSName('graph.test')]),False).sign(key,hashes.SHA256()))
    certfile=tmp_path/'cert.pem';keyfile=tmp_path/'key.pem'
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certfile,keyfile)
    client_context=ssl.create_default_context(cafile=str(certfile))
    monkeypatch.setattr(mod.ssl,'create_default_context',lambda:client_context)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(3)
    port=listener.getsockname()[1]
    def server():
        try:
            for _ in range(2):
                peer,_=listener.accept()
                try:
                    with server_context.wrap_socket(peer,server_side=True) as secured:
                        assert b'AUTH' in secured.recv(4096)
                        secured.sendall(b'+OK\r\n')
                except ssl.SSLError: peer.close()
        finally: listener.close()
    worker=asyncio.create_task(asyncio.to_thread(server))
    class Policy:
        async def resolve(self,address): return [VerifiedAddress('127.0.0.1',port,address[0])]
    ctx=context();ctx.connection.public.uri=f'rediss://graph.test:{port}'
    ctx=replace(ctx,network_policy=Policy())
    async with mod.open_connection(ctx): pass
    ctx.connection.public.uri=f'rediss://wrong.test:{port}'
    with pytest.raises(ssl.SSLCertVerificationError):
        async with mod.open_connection(ctx): pass
    await worker


@pytest.mark.parametrize('query',[r'RETURN `ra\u006ege`(1,2)', 'RETURN "range"(1,2)'])
def test_nonstandard_function_names_cannot_bypass_allowance(query):
    with pytest.raises(ControlError): module().check_query(query)


def test_resp_depth_limit_is_independent_of_byte_budget():
    wire=Wire(b'*1\r\n'*34+b':1\r\n')
    with pytest.raises(ControlError):
        module('falkor_protocol').read_response(wire,max_bytes=65536,deadline=time.monotonic()+1)

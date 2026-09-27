"""Real writable-account adapter acceptance; never targets user databases."""
import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from pydantic import SecretStr

from agent_runtime.execution_store import ControlError
from agent_runtime.remote_graph.adapters import AdapterContext
from agent_runtime.remote_graph.contracts import QueryRequest
from agent_runtime.remote_graph.falkordb import FalkorDBAdapter
from agent_runtime.remote_graph.policy import NetworkPolicy


def capture(name,value):
    path=Path(os.environ.get('IGRAPH_TEST_FALKORDB_EVIDENCE_DIR','output/multi-database-phase1/task4-falkordb'))
    path.mkdir(parents=True,exist_ok=True)
    (path/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


@pytest.fixture(scope='module')
def live(request):
    keys=['IGRAPH_TEST_FALKORDB_URI','IGRAPH_TEST_FALKORDB_PASSWORD','IGRAPH_TEST_FALKORDB_GRAPH']
    missing=[key for key in keys if not os.environ.get(key)]
    if missing:
        if request.config.getoption('--require-live'): pytest.fail('Missing live FalkorDB prerequisites: '+','.join(missing))
        pytest.skip('Missing live FalkorDB prerequisites')
    import redis
    uri=urlsplit(os.environ[keys[0]])
    admin=redis.Redis(host=uri.hostname,port=uri.port,password=os.environ[keys[1]],socket_timeout=10)
    graph='igraph_task4_'+uuid4().hex[:10]
    admin.execute_command('GRAPH.QUERY',graph,"CREATE (:Fixture:Multi {name:'甲', flag:true, big:9007199254740993})-[:EDGE {weight:2.5}]->(:Empty)")
    # More than preview and server row caps; a 1MiB single scalar is separate.
    admin.execute_command('GRAPH.QUERY',graph,'UNWIND range(1,140) AS x CREATE (:Budget {number:x})')
    admin.execute_command('GRAPH.QUERY',graph,"CREATE (:Bulk {value:'"+'x'*1048576+"'})")
    def ctx(target=None,**kwargs):
        return AdapterContext(connection=SimpleNamespace(public=SimpleNamespace(uri=os.environ[keys[0]],
            username='',database=target or graph),password=SecretStr(os.environ[keys[1]])),
            deadline=time.monotonic()+kwargs.get('timeout',8),max_rows=kwargs.get('max_rows',128),
            max_bytes=kwargs.get('max_bytes',262144),network_policy=NetworkPolicy(allowed_cidrs=['127.0.0.1/32']))
    try: yield admin,graph,ctx
    finally:
        admin.execute_command('GRAPH.DELETE',graph)
        admin.close()


@pytest.mark.anyio
async def test_nonexistent_graph_test_does_not_create_graph(live):
    admin,graph,ctx=live
    target=graph+'_missing'
    with pytest.raises(ControlError): await FalkorDBAdapter().test(ctx(target))
    assert admin.exists(target)==0
    capture('missing_graph',{'created':False})


@pytest.mark.anyio
async def test_typed_values_parameters_and_empty_results(live):
    admin,graph,ctx=live
    adapter=FalkorDBAdapter()
    values={'quoted':'\" // 注释\n O\'Reilly \\ /* */','items':[None,True,2,'雪']}
    result=await adapter.execute(ctx(),QueryRequest(query='RETURN $quoted,$items,null',parameters=values))
    assert result.rows==[[values['quoted'],values['items'],None]]
    entities=await adapter.execute(ctx(),QueryRequest(query='MATCH p=(b:Empty)<-[r:EDGE]-(a:Fixture) RETURN a,r,p,2.5,true,9007199254740993'))
    node,edge,path,floating,boolean,big=entities.rows[0]
    assert node['labels']==['Fixture','Multi'] and node['properties']['big']=={'type':'integer','value':'9007199254740993'}
    assert edge['start_node']==node['element_id'] and edge['end_node']==path['nodes'][0]['element_id']
    assert path['nodes'][0]['labels']==['Empty'] and path['nodes'][0]['properties']=={}
    assert floating==2.5 and boolean is True and big=={'type':'integer','value':'9007199254740993'}
    empty=await adapter.execute(ctx(),QueryRequest(query='MATCH (n:Missing) RETURN n'))
    assert empty.rows==[] and empty.columns==['n']
    capture('types_and_parameters',{'rows':entities.rows,'parameters':result.rows,'empty':empty.rows})


@pytest.mark.anyio
async def test_sampling_is_explicit(live):
    _,_,ctx=live
    result=await FalkorDBAdapter().describe(ctx(),labels=['Fixture'],relationship_types=['EDGE'])
    assert result['coverage']['sampled'] is True
    assert result['labels']==['Fixture'] and result['relationship_types']==['EDGE']
    assert any(p['name']=='name' for p in result['properties'])
    capture('schema',result)


@pytest.mark.anyio
async def test_writable_account_rejects_mutations_and_entrypoints(live):
    admin,graph,ctx=live
    before=admin.execute_command('GRAPH.RO_QUERY',graph,'MATCH (n) RETURN count(n)')[1]
    cases=['CREATE ()','MATCH (n:Fixture) SET n.name="bad" RETURN n','MATCH (n:Fixture) DELETE n',
           'CREATE INDEX FOR (n:Fixture) ON (n.name)','MATCH (n) REMOVE n.name RETURN n',
           'RETURN 1; CREATE ()','CALL db.idx.fulltext.createNodeIndex("Fixture","name")',
           'RETURN range(1,1000000000)','CALL db.unknown()','RETURN custom(1)']
    for query in cases:
        with pytest.raises(ControlError): await FalkorDBAdapter().execute(ctx(),QueryRequest(query=query))
    assert admin.execute_command('GRAPH.RO_QUERY',graph,'MATCH (n) RETURN count(n)')[1]==before
    assert admin.execute_command('GRAPH.RO_QUERY',graph,'MATCH (n:Fixture) RETURN n.name')[1]==[[b'\xe7\x94\xb2']]
    capture('write_rejection',{'cases':cases,'rejected':len(cases),'node_count':before})


@pytest.mark.anyio
async def test_result_rows_bytes_and_server_timeout(live):
    _,_,ctx=live
    adapter=FalkorDBAdapter()
    result=await adapter.execute(ctx(),QueryRequest(query='MATCH (n:Budget) RETURN n.number'))
    assert result.returned_rows==128 and result.truncated
    small=await adapter.execute(ctx(max_rows=3),QueryRequest(query='MATCH (n:Budget) RETURN n.number'))
    assert small.returned_rows==3 and small.truncated
    with pytest.raises(ControlError):
        await adapter.execute(ctx(max_bytes=8192),QueryRequest(query='MATCH (n:Bulk) RETURN n.value'))
    started=time.monotonic()
    with pytest.raises((ControlError,TimeoutError)):
        await adapter.execute(ctx(timeout=.1),QueryRequest(query='MATCH (a:Budget),(b:Budget),(c:Budget),(d:Budget) RETURN sum(a.number+b.number+c.number+d.number)'))
    assert time.monotonic()-started<2
    capture('budgets',{'server_rows':128,'truncated':result.truncated,'single_bulk_rejected':True,'deadline_seconds':time.monotonic()-started})


@pytest.mark.anyio
async def test_cora_matches_independent_manifest(live):
    _,_,ctx=live
    manifest=Path('output/multi-database-phase1/task4-falkordb/cora-manifest.json')
    assert manifest.exists(),'Run administrative Cora import first'
    expected=json.loads(manifest.read_text())
    target=os.environ['IGRAPH_TEST_FALKORDB_GRAPH']
    adapter=FalkorDBAdapter()
    nodes=await adapter.execute(ctx(target),QueryRequest(query='MATCH (n:Paper) RETURN count(n)'))
    edges=await adapter.execute(ctx(target),QueryRequest(query='MATCH ()-[r:LINKS_TO]->() RETURN count(r)'))
    assert nodes.rows==[[expected['node_count']]] and edges.rows==[[expected['edge_count']]]
    labels=await adapter.execute(ctx(target),QueryRequest(query='MATCH (n:Paper) RETURN n.label,count(n) ORDER BY n.label'))
    assert {str(k):v for k,v in labels.rows}==expected['label_histogram']
    for identity,neighbors in expected['neighbors'].items():
        result=await adapter.execute(ctx(target),QueryRequest(query='MATCH (n:Paper)-[:LINKS_TO]-(m:Paper) WHERE n.cora_id=$id RETURN m.cora_id ORDER BY m.cora_id',parameters={'id':int(identity)}))
        assert [r[0] for r in result.rows]==neighbors
    capture('cora_comparison',{'nodes':nodes.rows,'edges':edges.rows,'histogram':labels.rows,'neighbors_verified':list(expected['neighbors'])})


@pytest.mark.anyio
async def test_cancel_disconnects_client_and_server_finishes_by_deadline(live):
    admin,graph,ctx=live
    baseline={row['id'] for row in admin.client_list()}
    adapter=FalkorDBAdapter()
    slow='MATCH (a:Budget),(b:Budget),(c:Budget),(d:Budget) RETURN sum(a.number+b.number+c.number+d.number)'
    task=asyncio.create_task(adapter.execute(ctx(timeout=1),QueryRequest(query=slow)))
    def running(): return len(admin.execute_command('GRAPH.INFO')[1])
    for _ in range(100):
        if running(): break
        await asyncio.sleep(.005)
    assert running()>0 and not task.done()
    active={row['id'] for row in admin.client_list()}-baseline
    assert len(active)==1
    started=time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not active.intersection({row['id'] for row in admin.client_list()})
    observed=[running()]
    while observed[-1] and time.monotonic()-started<2:
        await asyncio.sleep(.025)
        observed.append(running())
    assert observed[-1]==0
    capture('cancel',{'client_closed':True,'server_active_after_client_cancel':observed[0],
                     'server_finished_seconds':time.monotonic()-started,'active_observations':observed})


@pytest.mark.anyio
async def test_shared_executor_artifact_preserves_source_and_preview(live,tmp_path,monkeypatch):
    _,graph,ctx=live
    from agent_runtime.remote_graph import registry
    from agent_runtime.remote_graph.contracts import RemoteBinding
    from agent_runtime.remote_graph.executor import RemoteGraphExecutor
    # Exercise shared contract before registry admission.
    monkeypatch.setattr(registry,'get_adapter',lambda kind:FalkorDBAdapter())
    binding=RemoteBinding(connection_id='falkor-fixture',revision=1,name='Falkor fixture',kind='falkordb',database=graph)
    class Catalog:
        def resolve(self,uid,actual):
            assert uid=='owner' and actual==binding
            return ctx().connection
    settings=SimpleNamespace(remote_graph_timeout_seconds=5,remote_graph_max_rows=128,remote_graph_max_bytes=262144,
        remote_graph_preview_rows=3,remote_graph_preview_bytes=8192,remote_graph_allowed_hosts=(),remote_graph_allowed_cidrs=['127.0.0.1/32'])
    request=QueryRequest(query='MATCH (n:Budget) WHERE n.number >= $min RETURN n.number',parameters={'min':2},save_result=True)
    result=await RemoteGraphExecutor(Catalog(),settings).query('owner',binding,request,tmp_path)
    assert result.success and result.returned_rows==128 and result.truncated and result.preview_truncated and len(result.rows)==3
    artifact=json.loads((tmp_path/(result.query_id+'.json')).read_text())
    assert artifact['source']['kind']=='falkordb' and artifact['source']['database']==graph
    assert artifact['parameters']=={'min':2} and len(artifact['rows'])==128
    capture('executor_artifact',{'result':result.model_dump(),'saved_rows':len(artifact['rows']),'parameters':artifact['parameters']})


def test_admin_import_foreign_target_refused_and_exact_batch_resumes(live):
    from scripts.import_cora_falkordb import import_graph
    admin,graph,_=live
    with pytest.raises(ValueError,match='incompatible'):
        import_graph(admin,graph,[{'cora_id':1}],[],'other-hash')
    dedicated=graph+'_import'
    try:
        nodes=[{'cora_id':0,'label':1},{'cora_id':1,'label':2}]
        edges=[{'source':0,'target':1}]
        import_graph(admin,dedicated,nodes,edges,'same-hash')
        import_graph(admin,dedicated,nodes,edges,'same-hash')
        assert admin.execute_command('GRAPH.RO_QUERY',dedicated,'MATCH (n) RETURN count(n)')[1]==[[2]]
        assert admin.execute_command('GRAPH.RO_QUERY',dedicated,'MATCH ()-[r]->() RETURN count(r)')[1]==[[1]]
        with pytest.raises(ValueError): import_graph(admin,dedicated,nodes,edges,'different-hash')
    finally: admin.execute_command('GRAPH.DELETE',dedicated)

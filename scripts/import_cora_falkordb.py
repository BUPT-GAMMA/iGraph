#!/usr/bin/env python3
"""Administrative Cora import into a dedicated FalkorDB graph.

Uses writable commands only here, never from runtime tools. Batches are resumable
for this exact source hash; use an exclusively owned target during import.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

from scripts.import_cora_neo4j import convert_cora, load_cora
from agent_runtime.remote_graph.falkor_values import parameter_prefix


def import_graph(admin, graph, nodes, edges, source_hash, batch_size=10):
    if batch_size < 1:
        raise ValueError('batch_size must be positive')

    def query(command, text, **parameters):
        return admin.execute_command(command, graph, parameter_prefix(parameters)+text)

    if admin.execute_command('EXISTS',graph):
        bad_nodes=query('GRAPH.RO_QUERY','MATCH (n) WHERE NOT n:Paper OR n._igraph_import IS NULL OR n._igraph_import <> $batch RETURN count(n)',batch=source_hash)[1][0][0]
        bad_edges=query('GRAPH.RO_QUERY','MATCH ()-[r]->() WHERE type(r) <> "LINKS_TO" OR r._igraph_import IS NULL OR r._igraph_import <> $batch RETURN count(r)',batch=source_hash)[1][0][0]
        if bad_nodes or bad_edges:
            raise ValueError('Target contains incompatible data; use a dedicated empty graph')
    from redis.exceptions import ResponseError
    try:
        query('GRAPH.QUERY','CREATE INDEX FOR (n:Paper) ON (n.cora_id)')
    except ResponseError as exc:
        if str(exc) != "Attribute 'cora_id' is already indexed":
            raise
    for start in range(0,len(nodes),batch_size):
        query('GRAPH.QUERY','UNWIND $rows AS row MERGE (n:Paper {cora_id:row.cora_id}) SET n += row, n._igraph_import=$batch',rows=nodes[start:start+batch_size],batch=source_hash)
    for start in range(0,len(edges),batch_size):
        query('GRAPH.QUERY','UNWIND $rows AS row MATCH (a:Paper {cora_id:row.source}), (b:Paper {cora_id:row.target}) MERGE (a)-[r:LINKS_TO]->(b) SET r._igraph_import=$batch',rows=edges[start:start+batch_size],batch=source_hash)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--expected-sha256',required=True)
    args=parser.parse_args(argv)
    source_hash=hashlib.sha256(args.input.read_bytes()).hexdigest()
    if source_hash!=args.expected_sha256:
        parser.error('Input hash differs from trusted Cora manifest')
    required=['URI','PASSWORD','GRAPH']
    if any(not os.environ.get('IGRAPH_TEST_FALKORDB_'+key) for key in required):
        parser.error('Set IGRAPH_TEST_FALKORDB_URI/PASSWORD/GRAPH')
    import redis
    nodes,edges,manifest=convert_cora(load_cora(args.input))
    with redis.Redis.from_url(os.environ['IGRAPH_TEST_FALKORDB_URI'],password=os.environ['IGRAPH_TEST_FALKORDB_PASSWORD'],socket_timeout=10) as admin:
        import_graph(admin,os.environ['IGRAPH_TEST_FALKORDB_GRAPH'],nodes,edges,source_hash)
        manifest.update(source_sha256=source_hash,server_agent=admin.info('server')['redis_version'],
                        module_version=42007,graph=os.environ['IGRAPH_TEST_FALKORDB_GRAPH'],
                        import_atomic=False,import_recovery='Resumable batches; exclusive target ownership required')
    args.manifest.parent.mkdir(parents=True,exist_ok=True)
    args.manifest.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'manifest':str(args.manifest),'nodes':len(nodes),'edges':len(edges)}))


if __name__=='__main__': main()

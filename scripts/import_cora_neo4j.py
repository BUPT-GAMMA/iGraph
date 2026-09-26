#!/usr/bin/env python3
"""Import a restricted-load Cora file into a dedicated Neo4j database.

Never called by the upload API. Credentials are accepted only through environment.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path


def _list(value):
    return value.tolist() if hasattr(value, 'tolist') else value


def convert_cora(data):
    count = int(data.num_nodes)
    if count < 1:
        raise ValueError('Cora must contain nodes')
    raw_edges = _list(data.edge_index)
    if not isinstance(raw_edges, (list, tuple)) or len(raw_edges) != 2 or not all(isinstance(v, (list, tuple)) for v in raw_edges) or len(raw_edges[0]) != len(raw_edges[1]):
        raise ValueError('edge_index must have shape [2, E]')
    pairs = set()
    for source, target in zip(*raw_edges):
        if type(source) is not int or type(target) is not int or not (0 <= source < count and 0 <= target < count):
            raise ValueError('Edge endpoints must be valid integer node IDs')
        pairs.add((source, target))
    directed = not all((target, source) in pairs for source, target in pairs)
    if not directed:
        pairs = {(min(a, b), max(a, b)) for a, b in pairs}
    nodes = [{'cora_id': index} for index in range(count)]
    fields = ['cora_id']
    for attribute, property_name in [('y', 'label'), ('title', 'title'), ('raw_texts', 'text'), ('x', 'x')]:
        values = _list(getattr(data, attribute, None))
        if values is None:
            continue
        if not isinstance(values, (list, tuple)) or len(values) != count:
            raise ValueError(f'{attribute} must have one entry per node')
        present = False
        for node, value in zip(nodes, values):
            if value is None:
                continue
            if attribute == 'x':
                if not isinstance(value, (list, tuple)) or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in value):
                    raise ValueError('Features must be finite numeric vectors')
                value = list(value)
            elif attribute == 'y':
                if isinstance(value, (list, tuple)) and len(value) == 1:
                    value = value[0]
                if type(value) not in (str, int, float, bool) or isinstance(value, float) and not math.isfinite(value):
                    raise ValueError('Labels must be finite scalars or strings')
            elif not isinstance(value, str):
                raise ValueError('Title and text must be strings')
            node[property_name] = value
            present = True
        if present:
            fields.append(property_name)
    neighbors = {str(index): set() for index in sorted({0, min(1, count - 1), count - 1})}
    for source, target in pairs:
        if str(source) in neighbors:
            neighbors[str(source)].add(target)
        if not directed and str(target) in neighbors:
            neighbors[str(target)].add(source)
    edges = [{'source': a, 'target': b} for a, b in sorted(pairs)]
    manifest = {
        'node_count': count, 'edge_count': len(edges), 'directed': directed,
        'direction_rule': 'preserve unique ordered pairs' if directed else 'symmetric pairs deduplicated as (min, max)',
        'relationship_type': 'LINKS_TO', 'fields': fields,
        'label_histogram': dict(sorted(Counter(str(n['label']) for n in nodes if 'label' in n).items())),
        'neighbors': {key: sorted(value) for key, value in neighbors.items()},
    }
    return nodes, edges, manifest


def import_graph(driver, database, nodes, edges, source_hash, batch_size=500):
    """Atomic import; only an empty DB or this exact import batch is allowed."""
    if batch_size < 1:
        raise ValueError('batch_size must be positive')
    with driver.session(database=database) as session:
        with session.begin_transaction() as tx:
            incompatible = tx.run('MATCH (n) WHERE NOT n:Paper OR n._igraph_import IS NULL OR n._igraph_import <> $batch RETURN count(n) AS count', batch=source_hash).single()['count']
            incompatible += tx.run('MATCH ()-[r]->() WHERE type(r) <> "LINKS_TO" OR r._igraph_import IS NULL OR r._igraph_import <> $batch RETURN count(r) AS count', batch=source_hash).single()['count']
            if incompatible:
                raise ValueError('Target contains incompatible data; use a dedicated empty database')
            for start in range(0, len(nodes), batch_size):
                tx.run('UNWIND $rows AS row MERGE (n:Paper {cora_id: row.cora_id}) SET n += row, n._igraph_import = $batch', rows=nodes[start:start + batch_size], batch=source_hash).consume()
            for start in range(0, len(edges), batch_size):
                tx.run('UNWIND $rows AS row MATCH (a:Paper {cora_id: row.source}), (b:Paper {cora_id: row.target}) MERGE (a)-[r:LINKS_TO]->(b) SET r._igraph_import = $batch', rows=edges[start:start + batch_size], batch=source_hash).consume()
            tx.commit()


def load_cora(path):
    """Restricted PyTorch loader; never fall back to arbitrary pickle execution.

    Only the reviewed data containers used by Cora are allowed. Unknown globals
    fail closed. PyG Data/Storage __setstate__ only restore their data mappings.
    """
    import numpy as np
    import torch
    from numpy.core.multiarray import _reconstruct
    from torch_geometric.data.data import Data, DataEdgeAttr, DataTensorAttr
    from torch_geometric.data.storage import GlobalStorage
    allowed = [Data, DataEdgeAttr, DataTensorAttr, GlobalStorage, np.ndarray, np.dtype,
               _reconstruct, (_reconstruct, 'numpy.core.multiarray._reconstruct')]
    allowed.extend(type(np.dtype(dtype)) for dtype in ('int64', 'float64', 'float32', 'O', 'str'))
    with torch.serialization.safe_globals(allowed):
        return torch.load(path, map_location='cpu', weights_only=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--trusted-input', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    required = ['URI', 'DATABASE', 'USER', 'PASSWORD']
    if any(not os.environ.get('IGRAPH_TEST_NEO4J_' + name) for name in required):
        parser.error('Set IGRAPH_TEST_NEO4J_URI/DATABASE/USER/PASSWORD in the environment')
    from neo4j import GraphDatabase
    source_hash = hashlib.sha256(args.input.read_bytes()).hexdigest()
    graph = load_cora(args.input)
    nodes, edges, manifest = convert_cora(graph)
    manifest['source_sha256'] = source_hash
    with GraphDatabase.driver(os.environ['IGRAPH_TEST_NEO4J_URI'], auth=(os.environ['IGRAPH_TEST_NEO4J_USER'], os.environ['IGRAPH_TEST_NEO4J_PASSWORD'])) as driver:
        driver.verify_connectivity()
        info = driver.get_server_info()
        import_graph(driver, os.environ['IGRAPH_TEST_NEO4J_DATABASE'], nodes, edges, source_hash)
        manifest['server_agent'] = info.agent
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'manifest': str(args.manifest), 'node_count': len(nodes), 'edge_count': len(edges)}))


if __name__ == '__main__':
    main()

"""Small repeatable CPU baseline; run each mode in a fresh process.

Reports timings and sampled process-tree RSS only. All graph files are temporary;
the benchmark never connects to a model, database, or deployment service.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def measure(mode, directory):
    os.environ.update(IGRAPH_DATA_ROOT=directory, GRAPH_ML_ARTIFACT_ROOT=directory + '/models',
                      IGRAPH_RUN_CANCEL_ENABLED='false', GRAPH_STORAGE_BACKEND='local', GRAPH_COMPUTE_CONCURRENCY='1')
    import numpy as np
    import psutil
    from graph_data_service.app import GraphDataService
    from graph_data_service.graph_data import GraphData
    from graph_data_service.execution_host import JobContext, job_context
    from graph_data_service.isolated_compute import run_isolated
    service = GraphDataService(storage_backend='local', migrate_storage=False)
    nodes = np.arange(500)
    graph = GraphData(num_nodes=500, edge_index=np.vstack([np.tile(nodes, 2), np.concatenate([(nodes + 1) % 500, (nodes + 7) % 500])]))
    service.get_or_create('benchmark', user_id='benchmark').set_graph(graph)
    async def before_start(_context): pass
    service._execution_host = SimpleNamespace(before_start=before_start)
    parent, peak = psutil.Process(), 0
    async def sample():
        nonlocal peak
        while True:
            rss = 0
            for process in [parent, *parent.children(recursive=True)]:
                try: rss += process.memory_info().rss
                except psutil.NoSuchProcess: pass
            peak = max(peak, rss)
            await asyncio.sleep(.02)
    sampler = asyncio.create_task(sample())
    timings, results = [], []
    try:
        for _ in range(3):
            started = time.perf_counter()
            if mode == 'isolated':
                context = JobContext('benchmark', 'benchmark', str(uuid4()), 1, str(uuid4()), str(uuid4()))
                token = job_context.set(context)
                try: value = await run_isolated(service, 'benchmark', 'benchmark', 'degree_analysis', {})
                finally: job_context.reset(token)
            else:
                value = await service.dispatch_tool('benchmark', 'degree_analysis', {}, user_id='benchmark')
            results.append(value)
            timings.append(round((time.perf_counter() - started) * 1000, 2))
        assert results[0] == results[1] == results[2]
        import hashlib
        digest = hashlib.sha256(json.dumps(results[0], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        print(json.dumps({'mode': mode, 'nodes': 500, 'edges': 1000, 'elapsed_ms': timings,
                          'sampled_tree_peak_rss_mb': round(peak / 1024 ** 2, 2), 'result_sha256': digest}))
    finally:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
        service._process_pool.shutdown(wait=True, cancel_futures=True)
        service._nebula.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['baseline', 'isolated'], required=True)
    args = parser.parse_args()
    with TemporaryDirectory(prefix='igraph-cancel-benchmark-') as directory:
        asyncio.run(measure(args.mode, directory))

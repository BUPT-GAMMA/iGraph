import asyncio
import os
import signal
import sys
import time

from graph_data_service.worker_process import WorkerProcess


def test_uncooperative_worker_and_child_actually_exit():
    async def scenario():
        entered = asyncio.Event()
        ids = []
        command = [sys.executable, '-u', '-c', '''
import json, os, signal, subprocess, sys, time
sys.stdin.readline()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)'])
print(json.dumps({'type':'progress','value':{'pid':os.getpid(),'child':child.pid}}), flush=True)
time.sleep(120)
''']
        def progress(frame):
            ids.extend([frame['pid'], frame['child']])
            entered.set()
        worker = WorkerProcess(grace=.05, terminate_grace=.05, reap_grace=1)
        task = asyncio.create_task(worker.run({}, progress, command=command))
        await asyncio.wait_for(entered.wait(), 3)
        started = time.monotonic()
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError), repr(results[0])
        assert time.monotonic() - started < 2
        assert worker.process.returncode is not None
        # The process group cannot continue doing work after cancellation.
        for pid in ids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            # Orphaned dead children can briefly be zombies until init reaps.
            import subprocess
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
            assert not state or state.startswith('Z')
    asyncio.run(scenario())

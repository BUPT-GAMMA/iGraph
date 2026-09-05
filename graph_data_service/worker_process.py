"""One managed process group per computation, with bounded stop escalation."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import anyio
import psutil
from anyio.streams.text import TextReceiveStream
from execution_lifecycle import ProcessLease, atomic_json

logger = logging.getLogger(__name__)


class WorkerExitUnconfirmed(RuntimeError):
    pass


class WorkerProcess:
    def __init__(self, *, grace=2.0, terminate_grace=1.0, reap_grace=2.0):
        self.grace, self.terminate_grace, self.reap_grace = grace, terminate_grace, reap_grace
        self.process = None
        self.pid = None
        self._stop_task = None
        self._members: dict[int, psutil.Process] = {}
        self._tracker = None
        self.peak_rss = 0
        self._identity_path = None
        self._persisted_members = set()

    def _save_identity(self):
        if self._identity_path and self._members and self._persisted_members != set(self._members):
            members = [{'pid': item.pid, 'started_at': item.create_time()} for item in self._members.values()]
            atomic_json(self._identity_path, {'pid': self.pid, 'process_group': self.pid,
                        'started_at': self._members[self.pid].create_time(), 'members': members})
            self._persisted_members = set(self._members)

    def _live_members(self):
        live = []
        group_rss = 0
        for member in list(self._members.values()):
            try:
                # psutil compares both PID and creation time. A reused PID
                # cannot become evidence or a signal target for this job.
                if member.is_running() and member.status() != psutil.STATUS_ZOMBIE:
                    live.append(member)
                    group_rss += member.memory_info().rss
                    for child in member.children(recursive=True):
                        if os.getpgid(child.pid) == self.pid:
                            self._members.setdefault(child.pid, child)
            except (psutil.NoSuchProcess, ProcessLookupError):
                continue
        self.peak_rss = max(self.peak_rss, group_rss)
        self._save_identity()
        return live

    def _executing_group(self):
        if self._live_members():
            return True
        try:
            os.killpg(self.pid, 0)  # Read-only existence probe, never a signal.
        except ProcessLookupError:
            return False
        # An unexpectedly dead leader can leave an unobserved orphan. Its
        # absence from our handle registry is not exit evidence. Unknown group
        # members keep confirmation pending; they never grant kill authority.
        for process in psutil.process_iter(['pid', 'status']):
            try:
                if process.info['status'] != psutil.STATUS_ZOMBIE and os.getpgid(process.pid) == self.pid:
                    return True
            except (psutil.NoSuchProcess, ProcessLookupError):
                continue
        return False

    async def _track(self):
        while True:
            try:
                self._live_members()
            except (OSError, psutil.Error):
                logger.warning('worker_identity_check_pending pid=%s', self.pid)
            await asyncio.sleep(.05)

    def _signal_group(self, sig):
        proc = self.process
        if proc is None or self.pid != proc.pid:
            raise WorkerExitUnconfirmed('WORKER_IDENTITY_MISMATCH')
        # Signal a group only while an identity-checked member proves its
        # lifetime. A dead leader's numeric PID alone grants no authority.
        for member in self._live_members():
            try:
                if member.is_running() and os.getpgid(member.pid) == self.pid:
                    os.killpg(self.pid, sig)
                    logger.info('worker_stop_signal pid=%s signal=%s', self.pid, int(sig))
                    return
            except (psutil.NoSuchProcess, ProcessLookupError):
                continue

    async def stop(self):
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        with anyio.CancelScope(shield=True):
            while not self._stop_task.done():
                try:
                    await asyncio.shield(self._stop_task)
                except asyncio.CancelledError:
                    # Repeated disconnect/shutdown signals cannot detach cleanup.
                    continue
        await self._stop_task

    async def _stop(self):
        while True:
            try:
                await self._stop_attempt()
                return
            except (OSError, psutil.Error, WorkerExitUnconfirmed):
                logger.error('worker_exit_confirmation_pending pid=%s', self.pid)
                await asyncio.sleep(1)

    async def _stop_attempt(self):
        proc = self.process
        if proc is None:
            return
        if proc.stdin:
            with suppress(Exception):
                await proc.stdin.send(b'{"control":"cancel"}\n')
        waiter = asyncio.create_task(proc.wait())
        try:
            for duration, sig in [(self.grace, signal.SIGTERM), (self.terminate_grace, signal.SIGKILL), (self.reap_grace, signal.SIGKILL)]:
                deadline = asyncio.get_running_loop().time() + duration
                while asyncio.get_running_loop().time() < deadline:
                    if waiter.done() and not self._executing_group():
                        break
                    await asyncio.sleep(.02)
                if waiter.done() and not self._executing_group():
                    break
                self._signal_group(sig)
            if not waiter.done() or self._executing_group():
                logger.error('worker_exit_unconfirmed pid=%s', self.pid)
            # A timeout remains pending, retaining the job, staging directory
            # and semaphore. Only observed exit can acknowledge cancellation.
            while not waiter.done() or self._executing_group():
                self._signal_group(signal.SIGKILL)
                await asyncio.sleep(1)
            await waiter
        finally:
            if not waiter.done():
                # A live waiter must remain owned; a timeout is not an exit ack.
                waiter.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    async def run(self, payload: dict, progress=None, *, command=None, on_started=None, pass_fds=(), lease_root=None, job_id=None):
        command = command or [sys.executable, '-m', 'graph_data_service.compute_worker']
        lease = ProcessLease(lease_root, job_id) if lease_root is not None else None
        environment = dict(os.environ)
        if lease:
            pass_fds = (*pass_fds, lease.fd)
            self._identity_path = lease.root / f'{lease.identity}.identity.json'
            environment['IGRAPH_WORKER_IDENTITY_FILE'] = str(self._identity_path)
        try:
            proc = self.process = await anyio.open_process(command, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
                    cwd=str(Path(__file__).resolve().parent.parent), pass_fds=pass_fds, env=environment)
        except BaseException:
            if lease:
                lease.close()
            raise
        self.pid = proc.pid
        started = time.monotonic()
        result = None
        try:
            # The fixed worker waits for input before it can spawn children.
            # Keep process discovery inside cleanup ownership too.
            self._members[proc.pid] = psutil.Process(proc.pid)
            self._save_identity()
            self._tracker = asyncio.create_task(self._track())
            if on_started:
                await on_started({'pid': self.pid, 'started_at': self._members[self.pid].create_time(), 'process_group': self.pid})
            await proc.stdin.send(json.dumps(payload, ensure_ascii=False).encode() + b'\n')
            pending = ''
            async for chunk in TextReceiveStream(proc.stdout):
                pending += chunk
                if len(pending) > 8 * 1024 * 1024:
                    raise RuntimeError('WORKER_FRAME_TOO_LARGE')
                while '\n' in pending:
                    line, pending = pending.split('\n', 1)
                    frame = json.loads(line)
                    if frame.get('type') == 'progress' and progress:
                        progress(frame['value'])
                    elif frame.get('type') == 'result':
                        result = frame['value']
            code = await proc.wait()
            if code != 0 or result is None:
                raise RuntimeError('WORKER_FAILED')
            # Clean up DataLoader/other descendants on successful completion too.
            if self._executing_group():
                await self.stop()
            return result
        except BaseException:
            if self._members:
                await self.stop()
            else:
                with anyio.CancelScope(shield=True):
                    with suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
            raise
        finally:
            if self._tracker:
                self._tracker.cancel()
                await asyncio.gather(self._tracker, return_exceptions=True)
            if proc.returncode is not None:
                await proc.aclose()
            if lease:
                lease.close()
            logger.info('worker_exit pid=%s elapsed_ms=%s peak_rss_bytes=%s', self.pid, int((time.monotonic() - started) * 1000), self.peak_rss)


async def recover_worker(lease_root, job_id, registered_identity=None):
    """Reap a prior executor's group using its inherited lease and identities."""
    root = Path(lease_root)
    state = ProcessLease.inspect(root, job_id)
    if state == 'unknown':
        # The verified dead service creates this lease before spawning. Without
        # a lease or an older worker registration it never spawned this worker.
        return not (root / f'{job_id}.lock').exists() and not registered_identity
    try:
        identity = json.loads((root / f'{job_id}.identity.json').read_text())
    except FileNotFoundError:
        # Both sides save identity before the child receives work. An exited
        # inherited lease with no identity cannot have started descendants.
        if registered_identity is None:
            return state == 'exited'
        identity = registered_identity
    except (OSError, ValueError):
        return False
    if identity.get('pid') != identity.get('process_group'):
        return False
    if registered_identity and any(identity.get(field) != registered_identity.get(field) for field in ('pid', 'started_at', 'process_group')):
        return False
    if state == 'exited':
        probe = WorkerProcess()
        probe.pid = identity['pid']
        if not probe._executing_group():
            return True
    members = {}
    for item in identity.get('members', [identity]):
        try:
            member = psutil.Process(item['pid'])
            if member.create_time() == item['started_at'] and os.getpgid(member.pid) == identity['process_group']:
                members[member.pid] = member
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
    if not members:
        return False

    class RecoveredProcess:
        pid = identity['pid']
        stdin = None
        async def wait(self):
            while True:
                member = members.get(self.pid)
                try:
                    if member is None or not member.is_running() or member.status() == psutil.STATUS_ZOMBIE:
                        return 0
                except psutil.NoSuchProcess:
                    return 0
                await asyncio.sleep(.02)

    worker = WorkerProcess()
    worker.pid = identity['pid']
    worker.process = RecoveredProcess()
    worker._members = members
    await worker.stop()
    return ProcessLease.inspect(root, job_id) == 'exited'

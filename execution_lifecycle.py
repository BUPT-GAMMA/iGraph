"""Local execution lifetime evidence, retained across service restarts.

A kernel-held file lock proves the executor is still alive. Its release is
evidence only for a previously created lease. Missing/invalid files stay unknown.
Worker leases are inherited across spawn, closing the spawn/registration gap.
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import tempfile
import weakref
from pathlib import Path
from uuid import UUID, uuid4


async def owned_sync(function, *args, **kwargs):
    """Retain a synchronous operation through cancellation until actual exit."""
    import asyncio
    import anyio
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
        if not worker.cancelled():
            worker.exception()
        raise


def atomic_json(path: Path, payload, *, before_replace=None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace:
            before_replace()
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ProcessLease:
    def __init__(self, root: Path, identity=None):
        self.identity = str(UUID(str(identity))) if identity else str(uuid4())
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / (self.identity + '.lock')
        # Never replace/reuse a lifetime token: a late process may still own it.
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self._finalize = weakref.finalize(self, os.close, self.fd)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(self.fd, json.dumps({'identity': self.identity, 'pid': os.getpid(), 'host': socket.gethostname()}).encode())
        os.fsync(self.fd)

    @staticmethod
    def inspect(root, identity):
        try:
            path = Path(root) / (str(UUID(str(identity))) + '.lock')
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except (OSError, ValueError, TypeError):
            return 'unknown'
        try:
            record = json.loads(os.read(fd, 4096))
            if record.get('identity') != str(identity):
                return 'unknown'
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return 'exited'
            except BlockingIOError:
                return 'alive'
        except (OSError, ValueError):
            return 'unknown'
        finally:
            os.close(fd)

    def close(self):
        # Do not LOCK_UN: an inherited worker descriptor shares the lock and
        # must keep it held even after its parent exits or closes its copy.
        self._finalize()

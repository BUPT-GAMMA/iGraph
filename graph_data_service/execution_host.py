"""Graph Engine is the single owner of job execution and exit acknowledgement."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from contextvars import ContextVar
from dataclasses import dataclass, replace

import httpx
from fastapi import HTTPException

from execution_protocol import service_headers
from execution_lifecycle import ProcessLease, atomic_json
from graph_data_service.async_work import run_sync

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobContext:
    user_id: str
    session_id: str
    run_id: str
    generation: int
    operation_id: str
    job_id: str | None = None


job_context: ContextVar[JobContext | None] = ContextVar('igraph_job_context', default=None)


class GraphExecutionHost:
    def __init__(self, manager, *, runtime_url=None, execution_root=None):
        self.manager = manager
        self.runtime_url = (runtime_url or os.getenv('AGENT_RUNTIME_URL', 'http://agent-runtime:21112')).rstrip('/')
        root = Path(execution_root) if execution_root else Path(os.getenv('IGRAPH_DATA_ROOT', 'data')).resolve() / 'graph_engine' / 'executors'
        self.lease = ProcessLease(root / 'services')
        self.instance_id = self.lease.identity
        self.worker_root = root / 'workers'
        self.job_root = root / 'jobs'
        self.recoveries: dict[str, asyncio.Task] = {}
        self.scanner = None
        self.monitors: dict[str, asyncio.Task] = {}
        self.cleanups: set[asyncio.Task] = set()
        self.launches: dict[tuple, tuple[str, asyncio.Task]] = {}
        self.closed = False
        manager.execution_host = self
        manager.load_saved = self.load_job

    def save_job(self, job):
        atomic_json(self.job_root / f'{uuid.UUID(job.job_id)}.json', job.to_dict())

    def prepare_commit(self, ctx, proof_path):
        # A locator grants no success by itself. Recovery also requires the
        # matching receipt inside the published manifest/immutable directory.
        atomic_json(self.job_root / f'{uuid.UUID(ctx.job_id)}.commit.json',
                    {**self.identity(ctx), 'job_id': ctx.job_id, 'proof_path': str(proof_path)})

    def committed_result(self, ctx):
        try:
            index = json.loads((self.job_root / f'{uuid.UUID(ctx.job_id)}.commit.json').read_text())
            if any(index.get(k) != v for k, v in self.identity(ctx).items()):
                raise ValueError('COMMIT_OWNER_MISMATCH')
            proof = json.loads(Path(index['proof_path']).read_text())
            candidates = [proof, proof.get('commit_receipt', {})]
            candidates += [row.get('commit_receipt', {}) for row in proof.get('versions', [])]
            for receipt in candidates:
                if (receipt.get('run_id'), receipt.get('job_id'), receipt.get('generation')) == (ctx.run_id, ctx.job_id, ctx.generation):
                    return receipt.get('result')
        except FileNotFoundError:
            return None  # Locator persisted before publication; no side effect.
        return None

    def load_job(self, jid):
        from graph_data_service.job_manager import Job
        try:
            value = json.loads((self.job_root / f'{uuid.UUID(jid)}.json').read_text())
            if value.get('job_id') != jid or value.get('status') not in {'done', 'cancelled', 'failed'}:
                return None
            return Job(**value)
        except (OSError, ValueError, TypeError):
            return None

    async def restore(self):
        if self.scanner is None:
            self.scanner = asyncio.create_task(self._scan())

    async def _scan(self):
        while not self.closed:
            try:
                async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                    response = await client.get(self.runtime_url + '/internal/execution/jobs/pending', headers=service_headers(''))
                    response.raise_for_status()
                for row in response.json():
                    jid = row['job_id']
                    if jid not in self.recoveries and ProcessLease.inspect(self.lease.root, row['state'].get('executor_instance')) == 'exited':
                        task = asyncio.create_task(self.recover(row))
                        self.recoveries[jid] = task
                        def done(result, job_id=jid):
                            self.recoveries.pop(job_id, None)
                            if not result.cancelled() and result.exception() is not None:
                                logger.warning('job_recovery_pending job_id=%s', job_id)
                        task.add_done_callback(done)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('job_recovery_inventory_pending')
            await asyncio.sleep(5)

    async def recover(self, row):
        from graph_data_service.worker_process import recover_worker
        from graph_data_service.job_manager import Job
        if ProcessLease.inspect(self.lease.root, row['state'].get('executor_instance')) != 'exited':
            return False
        ctx = JobContext(row['user_id'], row['session_id'], row['run_id'], row['generation'], row['operation_id'], row['job_id'])
        if not await recover_worker(self.worker_root, ctx.job_id, row['state'].get('worker')):
            logger.warning('job_executor_exit_pending job_id=%s', ctx.job_id)
            return False
        control = await self.request(ctx, 'GET', f'/jobs/{ctx.job_id}/control')
        job = self.load_job(ctx.job_id)
        if job is None:
            cancelled = control.get('cancel_requested')
            job = Job(job_id=ctx.job_id, session_id=ctx.session_id, user_id=ctx.user_id, run_id=ctx.run_id,
                      execution_generation=ctx.generation, operation_id=ctx.operation_id,
                      parent_job_id=row['parent_id'] if row['parent_id'] != ctx.run_id else None,
                      tool_name=row['state'].get('tool_name') or '任务',
                      status='cancelled' if cancelled else 'failed', message='执行者已退出')
            job.result = self.committed_result(ctx)
        if (job.user_id, job.session_id, job.run_id, job.execution_generation) != (ctx.user_id, ctx.session_id, ctx.run_id, ctx.generation):
            raise ValueError('RECOVERY_OWNER_MISMATCH')
        self.manager._jobs[ctx.job_id] = job
        await self.request(ctx, 'POST', f'/jobs/{ctx.job_id}/ack',
                           {'generation': ctx.generation, 'status': job.status, 'execution_stopped': True, 'terminal_persisted': False})
        await run_sync(self.save_job, job)
        await self.request(ctx, 'POST', f'/jobs/{ctx.job_id}/ack',
                           {'generation': ctx.generation, 'status': job.status, 'execution_stopped': True, 'terminal_persisted': True})
        self.manager.saved(ctx.job_id)
        return True

    async def request(self, ctx, method, path, body=None):
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.request(method, self.runtime_url + f'/internal/sessions/{ctx.session_id}' + path,
                                            headers=service_headers(ctx.user_id), json=body)
        if response.status_code >= 400:
            raise HTTPException(response.status_code, response.json().get('detail', {}),
                                headers={'Retry-After': response.headers.get('Retry-After', '')})
        return response.json()

    async def register(self, ctx, tool_name, args, *, parent_id=None):
        op_id = ctx.operation_id
        digest = hashlib.sha256(json.dumps({'tool': tool_name, 'args': args}, sort_keys=True,
                                          default=str, separators=(',', ':')).encode()).hexdigest()
        body = {'run_id': ctx.run_id, 'generation': ctx.generation, 'operation_id': op_id, 'fingerprint': digest,
                'parent_id': parent_id, 'executor_instance': self.instance_id, 'tool_name': tool_name}
        delay = 1
        while not self.closed:
            try:
                row = await self.request(ctx, 'POST', '/jobs/register', body)
                return row, replace(ctx, job_id=row['job_id'], operation_id=op_id)
            except (httpx.RequestError, HTTPException) as exc:
                if isinstance(exc, HTTPException) and exc.status_code not in {408, 429} and exc.status_code < 500:
                    raise
                retry_after = (getattr(exc, 'headers', None) or {}).get('Retry-After', '')
                try:
                    wait = max(delay, min(300, float(retry_after)))
                except ValueError:
                    wait = delay
                logger.warning('job_registration_confirmation_pending run_id=%s operation_id=%s', ctx.run_id, op_id)
                await asyncio.sleep(wait)
                delay = min(30, delay * 2)
        raise asyncio.CancelledError()

    async def launch(self, ctx, tool_name, arguments, fn, args=(), kwargs=None, *, child=False):
        """Own registration through enqueue, including a lost registration reply.

        Observers may disappear at every await. This task retains the exact
        operation key and completes the handoff; before_start then checks the
        durable fence. No orphan queued record is inferred to have exited.
        """
        if self.closed:
            raise HTTPException(503, {'code': 'EXECUTOR_SHUTTING_DOWN'})
        if child:
            ctx = replace(ctx, operation_id=str(uuid.uuid4()))
        key = (ctx.user_id, ctx.session_id, ctx.run_id, ctx.generation, ctx.operation_id)
        digest = hashlib.sha256(json.dumps({'tool': tool_name, 'args': arguments}, sort_keys=True,
                                          default=str, separators=(',', ':')).encode()).hexdigest()
        existing = self.launches.get(key)
        if existing and existing[0] != digest:
            raise HTTPException(409, {'code': 'JOB_IDEMPOTENCY_CONFLICT'})
        if existing is None:
            async def enqueue():
                # A child key is allocated above, once, before any async work.
                row, context = await self.register(ctx, tool_name, arguments, parent_id=ctx.job_id if child else None)
                if row['state']['executor_instance'] != self.instance_id:
                    raise HTTPException(409, {'code': 'EXECUTION_OWNER_UNAVAILABLE'})
                if not row.get('claimed') and context.job_id not in self.manager._jobs:
                    restored = self.load_job(context.job_id)
                    if restored:
                        self.manager._jobs[context.job_id] = restored
                        return context.job_id
                    if row['state']['execution_stopped']:
                        raise HTTPException(409, {'code': 'JOB_RESULT_UNAVAILABLE'})
                identity = self.identity(context)
                if child:
                    identity['parent_job_id'] = ctx.job_id
                return await self.manager.submit(ctx.session_id, tool_name, fn, *args, **(kwargs or {}),
                           _job_id=context.job_id, _registered=True, _identity=identity, _context=context)
            task = asyncio.create_task(enqueue())
            self.launches[key] = (digest, task)
            def finished(done):
                if not done.cancelled():
                    done.exception()
                for old_key, (_, old_task) in list(self.launches.items()):
                    if len(self.launches) <= 256:
                        break
                    if old_task.done():
                        self.launches.pop(old_key, None)
            task.add_done_callback(finished)
        else:
            task = existing[1]
        return await asyncio.shield(task)

    async def execute(self, ctx, tool_name, arguments, invoke):
        jid = await self.launch(ctx, tool_name, arguments, invoke)
        # Disconnecting an MCP/HTTP observer never kills independently owned work.
        if tool_name in {'load_graph_from_upload', 'load_graph_from_text'}:
            return {'success': True, 'async': True, 'job_id': jid, 'status': self.manager.status(jid).status}
        await self.manager.wait(jid)
        job = self.manager.status(jid)
        if job.status in {'cancelled', 'cancelling'}:
            raise HTTPException(409, {'code': 'RUN_CANCELLED', 'job_id': jid})
        if job.status == 'failed':
            return {'success': False, 'error': job.message, 'job_id': jid}
        return job.result

    @staticmethod
    def identity(ctx):
        return {'user_id': ctx.user_id, 'run_id': ctx.run_id, 'execution_generation': ctx.generation,
                'operation_id': ctx.operation_id}

    async def before_start(self, ctx):
        state = await self.request(ctx, 'GET', f'/jobs/{ctx.job_id}/control')
        if state.get('cancel_requested'):
            self.manager.cancel(ctx.job_id)
            raise asyncio.CancelledError()

    async def worker_started(self, ctx, identity):
        try:
            await self.request(ctx, 'POST', f'/jobs/{ctx.job_id}/executor', {'generation': ctx.generation,
                               'executor_instance': self.instance_id, **identity})
        except HTTPException as exc:
            if exc.status_code == 409 and isinstance(exc.detail, dict) and exc.detail.get('code') == 'RUN_CANCELLED':
                # The durable fence can arrive before the local polling signal.
                # Preserve cancellation semantics while WorkerProcess owns reap.
                self.manager.cancel(ctx.job_id)
                raise asyncio.CancelledError() from exc
            raise

    async def request_job_stop(self, uid, sid, jid, cancel_id):
        job = self.manager.owned(jid, sid, uid)
        if not job:
            raise HTTPException(404, {'code': 'JOB_NOT_FOUND'})
        if not job.run_id:
            return self.cancel_job(uid, sid, jid)
        # Also used by the MCP cancel_job tool. Intent survives that tool's
        # connection, and admission of every descendant is durably fenced.
        ctx = JobContext(uid, sid, job.run_id, job.execution_generation, job.operation_id, jid)
        return await self.request(ctx, 'POST', f'/jobs/{jid}/cancel', {'cancel_request_id': cancel_id})

    def retry_cleanup(self, path, jid):
        async def cleanup():
            import shutil
            for delay in (1, 2, 4, 8, 15, 30):
                await asyncio.sleep(delay)
                try:
                    await asyncio.to_thread(shutil.rmtree, path)
                    return
                except FileNotFoundError:
                    return
                except OSError:
                    logger.warning('worker_staging_cleanup_pending job_id=%s', jid)
        task = asyncio.create_task(cleanup())
        self.cleanups.add(task)
        task.add_done_callback(self.cleanups.discard)

    def watch(self, ctx):
        if ctx.job_id not in self.monitors:
            self.monitors[ctx.job_id] = asyncio.create_task(self._watch(ctx))

    async def _watch(self, ctx):
        delay = 1
        try:
            while not self.closed:
                job = self.manager.status(ctx.job_id)
                if job is None:
                    return
                try:
                    if job.status in {'done', 'failed', 'cancelled'}:
                        await self.request(ctx, 'POST', f'/jobs/{ctx.job_id}/ack',
                                           {'generation': ctx.generation, 'status': job.status, 'execution_stopped': True, 'terminal_persisted': False})
                        await run_sync(self.save_job, job)
                        await self.request(ctx, 'POST', f'/jobs/{ctx.job_id}/ack',
                                           {'generation': ctx.generation, 'status': job.status, 'execution_stopped': True, 'terminal_persisted': True})
                        self.manager.saved(ctx.job_id)
                        return
                    control = await self.request(ctx, 'GET', f'/jobs/{ctx.job_id}/control')
                    if control.get('cancel_requested'):
                        self.manager.cancel(ctx.job_id)
                    delay = 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning('job_control_pending job_id=%s', ctx.job_id)
                    delay = min(30, delay * 2)
                await asyncio.sleep(delay)
        finally:
            self.monitors.pop(ctx.job_id, None)

    def cancel_job(self, uid, sid, jid):
        target = self.manager.owned(jid, sid, uid)
        if target is None:
            raise HTTPException(404, {'code': 'JOB_NOT_FOUND'})
        targets = {jid}
        while True:
            before = len(targets)
            targets.update(j.job_id for j in self.manager.list_jobs(sid) if j.user_id == uid and j.parent_job_id in targets)
            if len(targets) == before:
                break
        for job_id in targets:
            self.manager.cancel(job_id)
        return {'job_id': jid, 'execution_stopped': all(self.manager.status(j).status in {'done', 'failed', 'cancelled'} for j in targets)}

    async def close(self):
        if self.scanner:
            self.scanner.cancel()
            await asyncio.gather(self.scanner, return_exceptions=True)
        await asyncio.gather(*list(self.recoveries.values()), return_exceptions=True)
        # Complete registration handoffs while the control service is reachable.
        # Shutdown does not grant an unknown execution permission to restart.
        pending = [task for _, task in self.launches.values() if not task.done()]
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=5)
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*unfinished, return_exceptions=True)
        self.manager.cancel_all()
        await asyncio.gather(*(self.manager.wait(jid) for jid in list(self.manager._tasks)))
        # Try to save actual terminal evidence before stopping the monitor.
        for job in list(self.manager._jobs.values()):
            if not job.run_id or job.status not in {'done', 'failed', 'cancelled'}:
                continue
            ctx = JobContext(job.user_id, job.session_id, job.run_id, job.execution_generation, job.operation_id, job.job_id)
            try:
                await run_sync(self.save_job, job)
                await self.request(ctx, 'POST', f'/jobs/{job.job_id}/ack', {'generation': ctx.generation, 'status': job.status, 'execution_stopped': True})
            except Exception:
                logger.warning('job_shutdown_confirmation_pending job_id=%s', job.job_id)
        self.closed = True
        for task in list(self.monitors.values()):
            task.cancel()
        await asyncio.gather(*list(self.monitors.values()), return_exceptions=True)
        for task in self.cleanups:
            task.cancel()
        await asyncio.gather(*self.cleanups, return_exceptions=True)
        self.lease.close()

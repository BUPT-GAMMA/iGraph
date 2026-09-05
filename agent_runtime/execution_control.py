"""Durable stop coordinator. Requests record intent; executors provide evidence."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from uuid import UUID
from pathlib import Path
from execution_lifecycle import ProcessLease, owned_sync

import httpx
from agno.os.middleware.user_scope import session_matches_component
from agno.run.base import RunStatus
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from execution_protocol import Target, CancelRequest, JobCancelRequest, service_headers, require_service

from agent_runtime.cancellation_compat import cancel_saved_pause
from agent_runtime.execution_scope import RunCancellationManager, RunScope
from agent_runtime.execution_store import ControlError, ExecutionStore, TERMINAL
from agent_runtime.native_persistence import NativePersistence

logger = logging.getLogger(__name__)


class RegisterRequest(BaseModel):
    start_request_id: UUID
    fingerprint: str
    preparation_owner: UUID | None = None


class PreparationAck(BaseModel):
    generation: int
    failed: bool = False


class JobRegistration(BaseModel):
    run_id: UUID
    generation: int
    operation_id: UUID
    fingerprint: str
    parent_id: str | None = None
    executor_instance: str
    tool_name: str | None = None


class JobAck(BaseModel):
    generation: int
    status: str
    execution_stopped: bool
    terminal_persisted: bool = True


class WorkerIdentity(BaseModel):
    generation: int
    executor_instance: str
    pid: int
    started_at: float
    process_group: int


class ExecutionController:
    def __init__(self, db, store: ExecutionStore, manager: RunCancellationManager, *, pending_directory=None):
        self.db, self.store, self.manager = db, store, manager
        self.tasks: dict[str, asyncio.Task] = {}
        self.closed = False
        self.scanner: asyncio.Task | None = None
        self.known_runs: dict[str, dict] = {}
        self.job_deliveries: dict[str, asyncio.Task] = {}
        root = Path(os.getenv('IGRAPH_DATA_ROOT', 'data')).resolve() / 'agent_runtime' / 'pending-writes'
        if store.engine.dialect.name == 'sqlite' and store.engine.url.database not in {None, ':memory:'}:
            root = Path(store.engine.url.database).resolve().parent / 'pending-writes'
        self.persistence = NativePersistence(Path(pending_directory) if pending_directory else root)
        self.lease = ProcessLease(self.persistence.root.parent / 'executors', store.instance_id)

    async def deliver_job_cancel(self, uid, sid, jid, cancel_id):
        base = os.getenv('GRAPH_ENGINE_API_URL', 'http://graph-engine-api:21111').rstrip('/')
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.post(base + f'/internal/sessions/{sid}/jobs/{jid}/cancel',
                                        headers=service_headers(uid), json={'cancel_request_id': cancel_id})
            response.raise_for_status()

    def wake_job(self, uid, sid, jid, cancel_id):
        if jid in self.job_deliveries or self.closed:
            return
        async def coordinate():
            delay = 1
            try:
                while not self.closed:
                    try:
                        await self.deliver_job_cancel(uid, sid, jid, cancel_id)
                        row = await self.call(self.store.cancel_job, uid, sid, jid, cancel_id)
                        if row:
                            state = await self.call(self.store.job_summary, row)
                            if state['execution_stopped'] and state['terminal_persisted']:
                                return
                    except ControlError as exc:
                        if exc.status in {401, 403, 404, 422}:
                            return
                        logger.warning('job_cancel_confirmation_pending job_id=%s', jid)
                    except Exception:
                        logger.warning('job_cancel_confirmation_pending job_id=%s', jid)
                    await asyncio.sleep(delay)
                    delay = min(30, delay * 2)
            finally:
                self.job_deliveries.pop(jid, None)
        self.job_deliveries[jid] = asyncio.create_task(coordinate())

    async def call(self, method, *args, **kwargs):
        return await owned_sync(method, *args, **kwargs)

    async def owned_session(self, uid, sid):
        if not uid:
            raise HTTPException(401, {"code": "MISSING_USER_IDENTITY"})
        session = await self.call(self.db.get_session, session_id=sid, user_id=uid)
        if session is None or session.user_id != uid or not session_matches_component(session, "agents", "igraph-agent"):
            raise HTTPException(404, {"code": "SESSION_NOT_FOUND"})
        return session

    async def begin(self, uid, sid, rid, *, continuing=False):
        await self.owned_session(uid, sid)
        row = await self.call(self.store.claim_agent, uid, sid, rid, continuing=continuing)
        self.known_runs[row['run_id']] = row
        scope = RunScope(row["run_id"], uid, sid, row["generation"])
        # Mark starting before entering native code, including before registration.
        task = asyncio.current_task()
        if task:
            scope.executors.add(task)
        self.manager.scopes[row["run_id"]] = scope
        self.wake(row["run_id"])
        return scope

    def wake(self, rid):
        task = self.tasks.get(rid)
        if not self.closed and (task is None or task.done()):
            created = self.tasks[rid] = asyncio.create_task(self._coordinate(rid))
            def forget(done):
                if self.tasks.get(rid) is done:
                    self.tasks.pop(rid, None)
            created.add_done_callback(forget)

    async def _remote_cancel(self, row, component):
        uid, sid, rid = row["user_id"], row["session_id"], row["run_id"]
        base = os.getenv("BACKEND_CONTROL_URL", "http://backend:21109") if component == "preparation" else os.getenv("GRAPH_ENGINE_API_URL", "http://graph-engine-api:21111")
        path = f"/internal/sessions/{sid}/runs/{rid}/cancel"
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.post(base.rstrip('/') + path, headers=service_headers(uid),
                                         json={"cancel_request_id": row["state"]["cancel_request_id"], "generation": row["generation"],
                                               'start_request_id': row['start_request_id'], 'preparation_owner': row['state'].get('preparation_owner')})
            response.raise_for_status()
            return response.json()

    async def _preparation_status(self, row):
        base = os.getenv('BACKEND_CONTROL_URL', 'http://backend:21109').rstrip('/')
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(base + f"/internal/sessions/{row['session_id']}/runs/{row['run_id']}/preparation",
                                        headers=service_headers(row['user_id']), params={'start_request_id': row['start_request_id'],
                                        'preparation_owner': row['state'].get('preparation_owner') or ''})
            response.raise_for_status()
            return response.json()

    async def reconcile(self, row):
        uid, sid, rid, gen = row["user_id"], row["session_id"], row["run_id"], row["generation"]
        cancelling = row["state"]["cancel_status"] != "none"
        scope = self.manager.scopes.get(rid)
        if scope and scope.generation != gen:
            return await self.call(self.store.coordinator_record, rid)
        if not cancelling and (not row['state']['preparation_stopped'] or row['state']['run_status'] == 'preparing'):
            try:
                evidence = await self._preparation_status(row)
                if evidence.get('execution_stopped') and evidence.get('generation') == gen:
                    row = await self.call(self.store.preparation_exited, uid, sid, rid, gen, failed=bool(evidence.get('failed')))
            except Exception:
                logger.warning('preparation_confirmation_pending run_id=%s', rid)
        if cancelling:
            # Signal every branch before waiting on any remote acknowledgement.
            if scope:
                scope.cancel()
            await self.manager.acancel_run(rid)
            signals = []
            if not row["state"]["preparation_stopped"]:
                signals.append(asyncio.create_task(self._remote_cancel(row, "preparation")))
            # Dispatch by exact Run identity without waiting for a DB job listing.
            signals.append(asyncio.create_task(self._remote_cancel(row, "jobs")))
            # No tasks are launched while a short DB transaction is held.
            if signals:
                results = await asyncio.gather(*signals, return_exceptions=True)
                for result in results:
                    if isinstance(result, dict) and result.get('execution_stopped') and result.get('generation') == gen:
                        row = await self.call(self.store.acknowledge, uid, sid, rid, gen, preparation_stopped=True)
                if any(isinstance(result, BaseException) for result in results):
                    logger.warning("cancel_delivery_pending run_id=%s", rid)

        # Only an actual local executor's exit or a saved stable phase proves
        # the Agent stopped. A missing executor after restart is insufficient.
        session = await self.owned_session(uid, sid)
        agent_exited = bool(scope and scope.execution_stopped)
        lost_executor = scope is None and row['state']['agent_claimed'] and await self.call(
            ProcessLease.inspect, self.lease.root, row['state'].get('executor_instance')) == 'exited'
        agent_exited = agent_exited or lost_executor
        pending = await self.call(self.persistence.load, row)
        if pending and (agent_exited or (scope is None and pending['execution_stopped'])):
            # Executor exit and native save have independent evidence. Retain
            # the spool through both the read-back and the ledger acknowledgement.
            row = await self.call(self.store.acknowledge, uid, sid, rid, gen,
                                  agent_stopped=True, native_run_created=True, terminal_persisted=False)
            try:
                pending = await self.call(self.persistence.finalize_checkpoint, row, pending, cancelled=cancelling)
                session = await self.call(self.persistence.persist, self.db, row, pending, session)
                agent_exited = True
            except Exception:
                row = await self.call(self.store.acknowledge, uid, sid, rid, gen, last_error='NATIVE_TERMINAL_SAVE_PENDING')
                logger.warning('native_terminal_save_pending run_id=%s', rid)
                return row
        run = session.get_run(rid)
        if lost_executor and pending is None and run is not None and str(getattr(run.status, 'value', run.status)).lower() not in TERMINAL | {'paused'}:
            # A released, previously registered kernel lease proves local
            # execution exited. Finalize the saved native payload without Factory.
            from agent_runtime.cancellation_compat import finalize_abandoned_run
            await finalize_abandoned_run(self.db, session, run, cancelled=cancelling)
        if run is None and agent_exited and scope is not None and not scope.native_run_created:
            row = await self.call(self.store.acknowledge, uid, sid, rid, gen, agent_stopped=True,
                                  terminal_persisted=True, native_run_created=False, run_status='cancelled' if cancelling else 'failed')
        elif run is None and agent_exited:
            # Exit evidence never proves an absent native payload was saved.
            # Keep unknown creation conservative, including after process death.
            row = await self.call(self.store.acknowledge, uid, sid, rid, gen, agent_stopped=True,
                                  terminal_persisted=False, native_run_created=True,
                                  run_status='cancelled' if cancelling else 'failed',
                                  last_error='EXECUTOR_EXITED_BEFORE_SAVE' if lost_executor else 'NATIVE_TERMINAL_SAVE_PENDING')
        stable_pause = run is not None and str(getattr(run.status, 'value', run.status)).lower() == 'paused' and (not scope or agent_exited)
        if cancelling and stable_pause:
            await cancel_saved_pause(self.db, session, run)
            agent_exited = True
        status = str(getattr(run.status, 'value', run.status)).lower() if run else ''
        if run and (agent_exited or (scope is None and status in TERMINAL)):
            if status in TERMINAL or status == "paused":
                row = await self.call(self.store.acknowledge, uid, sid, rid, gen,
                                      agent_stopped=True, terminal_persisted=True, native_run_created=True, run_status=status, last_error=None)
                if pending:
                    await self.call(self.persistence.acknowledged, row)
        if cancelling and not row["state"]["agent_claimed"] and row["state"]["preparation_stopped"]:
            row = await self.call(self.store.acknowledge, uid, sid, rid, gen,
                                  agent_stopped=True, terminal_persisted=True, run_status="cancelled")
        if cancelling:
            previous = row['state']['cancel_status']
            row = await self.call(self.store.confirm_cancel, uid, sid, rid, gen)
            if previous != 'confirmed' and row['state']['cancel_status'] == 'confirmed':
                logger.info('run_cancel_confirmed run_id=%s elapsed_ms=%s', rid,
                            int((time.time() - row['state'].get('cancel_requested_at', time.time())) * 1000))
        return row

    async def _coordinate(self, rid):
        delay = 1
        try:
            while not self.closed:
                row = await self.call(self.store.coordinator_record, rid)
                if not row or (not row['admission_blocked'] and row['state']['cancel_status'] not in {'requested', 'stopping'}):
                    if row and row['state']['run_status'] in TERMINAL and row['state']['terminal_persisted']:
                        scope = self.manager.scopes.get(rid)
                        if scope is None or (scope.generation == row['generation'] and scope.execution_stopped):
                            self.known_runs.pop(rid, None)
                            self.manager.scopes.pop(rid, None)
                    return
                try:
                    row = await self.reconcile(row)
                    if row["state"]["cancel_status"] == "confirmed" or (row["state"]["cancel_status"] == "none" and row["state"]["agent_stopped"] and row["state"]["run_status"] != "preparing"):
                        scope = self.manager.scopes.get(rid)
                        if scope and scope.generation == row['generation'] and scope.execution_stopped:
                            self.manager.scopes.pop(rid, None)
                        if row['state']['terminal_persisted'] and row['state']['run_status'] in TERMINAL:
                            self.known_runs.pop(rid, None)
                        return
                    delay = 1
                except Exception:
                    logger.warning("execution_confirmation_pending run_id=%s", rid)
                    delay = min(30, delay * 2)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("execution_control_unavailable run_id=%s", rid)

    async def restore(self):
        if self.scanner is None:
            self.scanner = asyncio.create_task(self._scan())

    async def _scan(self):
        delay = 1
        while not self.closed:
            try:
                for row in await self.call(self.store.pending):
                    self.wake(row['run_id'])
                for row in await self.call(self.store.pending_job_cancellations):
                    self.wake_job(row['user_id'], row['session_id'], row['job_id'], row['state']['cancel_request_id'])
                delay = 5
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('execution_control_reconcile_unavailable')
                delay = min(30, delay * 2)
            await asyncio.sleep(delay)

    async def close(self):
        self.closed = True
        executors = set()
        for scope in self.manager.scopes.values():
            scope.cancel()
            executors.update(scope.executors | scope.leaves)
        executors.discard(asyncio.current_task())
        if self.scanner:
            self.scanner.cancel()
            await asyncio.gather(self.scanner, return_exceptions=True)
        tasks = [*self.tasks.values(), *self.job_deliveries.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*executors, return_exceptions=True)
        self.lease.close()


def create_control_router(controller: ExecutionController):
    router = APIRouter()

    async def identity(request, sid):
        require_service(request)
        uid = str(getattr(request.state, "user_id", "") or request.headers.get("X-User-ID", ""))
        await controller.owned_session(uid, sid)
        return uid

    @router.get('/internal/capabilities')
    async def capabilities(request: Request):
        require_service(request)
        from importlib.metadata import version
        ready = version('agno') == '2.9.0'
        # Readiness probes never call back into Runtime or BFF from MCP.
        try:
            base = os.environ['GRAPH_ENGINE_MCP_URL'].rstrip('/').removesuffix('/mcp')
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                response = await client.get(base + '/internal/capabilities', headers=service_headers(''))
                response.raise_for_status()
                value = response.json()
                ready = ready and value.get('run_cancel_v1') and value.get('job_cancel_v1')
        except Exception:
            ready = False
        return {'run_cancel_v1': bool(ready), 'job_cancel_v1': bool(ready)}

    @router.post('/internal/sessions/{sid}/jobs/register')
    async def register_job(sid: str, req: JobRegistration, request: Request):
        uid = await identity(request, sid)
        row, claimed = await controller.call(controller.store.register_job, uid, sid, str(req.run_id), req.generation,
                        str(req.operation_id), req.fingerprint, parent_id=req.parent_id, executor_instance=req.executor_instance,
                        tool_name=req.tool_name)
        return {**row, 'claimed': claimed}

    @router.get('/internal/execution/jobs/pending')
    async def pending_jobs(request: Request):
        # Graph additionally proves ownership through local executor leases.
        require_service(request)
        return await controller.call(controller.store.pending_execution_jobs)

    @router.post('/internal/sessions/{sid}/prepare-delete')
    async def prepare_delete(sid: str, request: Request):
        uid = await identity(request, sid)
        session = await controller.owned_session(uid, sid)
        if any(str(getattr(run.status, 'value', run.status)).lower() not in TERMINAL for run in session.runs or []):
            raise ControlError('SESSION_EXECUTION_ACTIVE')
        await controller.call(controller.store.seal_session, uid, sid)
        return {'sealed': True}

    @router.get('/internal/sessions/{sid}/jobs/{jid}/control')
    async def job_control(sid: str, jid: str, request: Request):
        uid = await identity(request, sid)
        row = await controller.call(controller.store.get, uid, sid, job_id=jid)
        if not row:
            raise ControlError('JOB_NOT_FOUND', 404)
        parent = await controller.call(controller.store.get, uid, sid, run_id=row['run_id'])
        return {**row, 'cancel_requested': row['state']['cancel_status'] != 'none' or parent['state']['cancel_status'] != 'none'}

    @router.post('/internal/sessions/{sid}/jobs/{jid}/ack')
    async def job_ack(sid: str, jid: str, req: JobAck, request: Request):
        uid = await identity(request, sid)
        row = await controller.call(controller.store.acknowledge_job, uid, sid, jid, req.generation,
                                   status=req.status, execution_stopped=req.execution_stopped,
                                   terminal_persisted=req.execution_stopped and req.terminal_persisted)
        if not row:
            raise ControlError('JOB_NOT_FOUND', 404)
        controller.wake(row['run_id'])
        return {'revision': row['revision']}

    @router.post('/internal/sessions/{sid}/jobs/{jid}/executor')
    async def worker_started(sid: str, jid: str, req: WorkerIdentity, request: Request):
        uid = await identity(request, sid)
        row = await controller.call(controller.store.worker_started, uid, sid, jid, req.generation, req.executor_instance,
                                    req.model_dump(exclude={'generation', 'executor_instance'}))
        return {'revision': row['revision']}

    @router.post('/internal/sessions/{sid}/jobs/{jid}/cancel')
    async def stop_job(sid: str, jid: str, req: JobCancelRequest, request: Request):
        require_service(request)
        uid = str(getattr(request.state, 'user_id', '') or request.headers.get('X-User-ID', ''))
        try:
            await controller.owned_session(uid, sid)
            row = await controller.call(controller.store.cancel_job, uid, sid, jid, str(req.cancel_request_id))
        except (ControlError, HTTPException):
            raise
        except Exception:
            # Graph Engine independently verifies this exact live job owner.
            # An unavailable ledger cannot turn a best-effort stop into an ack.
            controller.wake_job(uid, sid, jid, str(req.cancel_request_id))
            raise HTTPException(503, {'code': 'CANCEL_NOT_CONFIRMED'})
        # Graph Engine also reconciles durable job intent, so browser disconnect
        # or a failed immediate delivery cannot drop an accepted stop.
        controller.wake_job(uid, sid, jid, str(req.cancel_request_id))
        row = await controller.call(controller.store.get, uid, sid, job_id=jid)
        summary = await controller.call(controller.store.job_summary, row)
        return JSONResponse(summary, status_code=200 if summary['execution_stopped'] else 202)

    @router.get('/internal/sessions/{sid}/jobs/{jid}/summary')
    async def job_summary(sid: str, jid: str, request: Request):
        uid = await identity(request, sid)
        row = await controller.call(controller.store.get, uid, sid, job_id=jid)
        if row is None:
            raise ControlError('JOB_NOT_FOUND', 404)
        return await controller.call(controller.store.job_summary, row)

    @router.post('/internal/sessions/{sid}/runs/register')
    async def register(sid: str, req: RegisterRequest, request: Request):
        uid = await identity(request, sid)
        session = await controller.owned_session(uid, sid)
        # Legacy/native runs also gate admission during a rolling upgrade.
        existing = await controller.call(controller.store.get, uid, sid, start_request_id=str(req.start_request_id))
        if existing is None and any(str(getattr(run.status, 'value', run.status)).lower() not in TERMINAL for run in session.runs or []):
            raise ControlError("RUN_ACTIVE")
        row, claimed = await controller.call(controller.store.register, uid, sid, str(req.start_request_id), req.fingerprint,
                                            preparation_owner=str(req.preparation_owner) if req.preparation_owner else None)
        controller.known_runs[row['run_id']] = row
        controller.wake(row['run_id'])
        return {**await controller.call(controller.store.summary, row), "preparation_claimed": claimed}

    @router.post('/internal/sessions/{sid}/runs/{rid}/preparation-finished')
    async def preparation_finished(sid: str, rid: str, req: PreparationAck, request: Request):
        uid = await identity(request, sid)
        row = await controller.call(controller.store.preparation_exited, uid, sid, rid, req.generation, failed=req.failed)
        if row is None:
            raise ControlError("RUN_NOT_FOUND", 404)
        controller.wake(rid)
        return await controller.call(controller.store.summary, row)

    @router.post('/internal/sessions/{sid}/runs/cancel')
    async def cancel(sid: str, req: CancelRequest, request: Request):
        require_service(request)
        uid = str(getattr(request.state, 'user_id', '') or request.headers.get('X-User-ID', ''))
        try:
            await controller.owned_session(uid, sid)
            row = await controller.call(controller.store.request_cancel, uid, sid, str(req.cancel_request_id), **req.target.lookup())
        except (ControlError, HTTPException):
            raise
        except Exception:
            # Validated local ownership permits best-effort stopping during a DB
            # outage; never manufacture a successful durable acknowledgement.
            for known in controller.known_runs.values():
                if (known['user_id'], known['session_id']) != (uid, sid):
                    continue
                if not all(known.get(k) == v for k, v in req.target.lookup().items()):
                    continue
                scope = controller.manager.scopes.get(known['run_id'])
                if scope:
                    scope.cancel()
                intent = {**known, 'state': {**known['state'], 'cancel_request_id': str(req.cancel_request_id)}}
                # Local cached registration is prior ownership evidence. Signal
                # known branches independently while reporting unconfirmed.
                for component in ('preparation', 'jobs'):
                    delivery = asyncio.create_task(controller._remote_cancel(intent, component))
                    delivery.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            raise HTTPException(503, {"code": "CANCEL_NOT_CONFIRMED"})
        scope = controller.manager.scopes.get(row['run_id'])
        if scope:
            scope.cancel()
        controller.wake(row['run_id'])
        logger.info('run_cancel_accepted run_id=%s revision=%s', row['run_id'], row['revision'])
        summary = await controller.call(controller.store.summary, row)
        return JSONResponse(summary, status_code=200 if summary['execution_stopped'] else 202)

    @router.get('/internal/sessions/{sid}/execution')
    async def session_execution(sid: str, request: Request):
        uid = await identity(request, sid)
        row = await controller.call(controller.store.active, uid, sid)
        if row is None:
            return None
        controller.wake(row['run_id'])
        return await controller.call(controller.store.summary, row)

    @router.get('/internal/sessions/{sid}/runs/control')
    async def control(sid: str, request: Request, run_id: UUID | None = None, start_request_id: UUID | None = None):
        uid = await identity(request, sid)
        if (run_id is None) == (start_request_id is None):
            raise ControlError("INVALID_CONTROL_TARGET", 422)
        row = await controller.call(controller.store.get, uid, sid, **Target(run_id=run_id, start_request_id=start_request_id).lookup())
        if row is None:
            if run_id:
                raise ControlError("RUN_NOT_FOUND", 404)
            return {"registration_status": "not_seen", "start_request_id": str(start_request_id), "run_id": None,
                    "can_send": False, "execution_stopped": False, "terminal_persisted": False, "revision": 0}
        controller.wake(row['run_id'])
        return await controller.call(controller.store.summary, row)

    return router

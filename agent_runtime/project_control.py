"""Durable project deletion composed from the existing session/run controllers."""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
from agno.db.base import SessionType

from execution_protocol import service_headers
from agent_runtime.execution_store import ControlError, TERMINAL

logger = logging.getLogger(__name__)


class ProjectDeletionMixin:
    async def project_remote(self, row, component, method, path, body=None):
        base = os.getenv('BACKEND_CONTROL_URL', 'http://backend:21109') if component == 'backend' else os.getenv('GRAPH_ENGINE_API_URL', 'http://graph-engine-api:21111')
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.request(method, base.rstrip('/') + path,
                headers=service_headers(row['user_id']), json=body)
            response.raise_for_status()
            return response.json()

    async def request_project_deletion(self, uid, pid, preparation_owner):
        row = await self.call(self.store.begin_project_deletion, uid, pid, preparation_owner)
        self.wake_project_deletion(row)
        return row

    def wake_project_deletion(self, row):
        key = row['control_id']
        if self.closed or row['state']['delete_status'] == 'deleted':
            return
        if key in self.project_deletions and not self.project_deletions[key].done():
            return
        task = self.project_deletions[key] = asyncio.create_task(self._delete_project(row))
        def forget(done):
            if self.project_deletions.get(key) is done:
                self.project_deletions.pop(key, None)
        task.add_done_callback(forget)

    async def _enumerate_project(self, uid, pid):
        # Complete pagination, stable ordering. The project fence already blocks
        # new membership; persisted reservations cover concurrent native creation.
        page = 1
        while True:
            rows = await self.call(self.db.get_sessions, session_type=SessionType.AGENT,
                                  user_id=uid, limit=100, page=page, sort_by='session_id', sort_order='asc')
            for session in rows:
                if session.user_id == uid and (session.session_data or {}).get('session_state', {}).get('project_id') == pid:
                    await self.call(self.store.register_project_session, uid, pid, session.session_id, historical=True)
            if len(rows) < 100:
                return
            page += 1

    async def _request_project_session_delete(self, uid, pid, sid):
        # Missing native rows are valid only for a previously reserved member.
        session = await self.call(self.db.get_session, session_id=sid, user_id=uid)
        active = [run.run_id for run in (session.runs or []) if str(getattr(run.status, 'value', run.status)).lower() not in TERMINAL] if session else []
        deletion, targets = await self.call(self.store.begin_session_deletion, uid, sid, active)
        for target in targets:
            scope = self.manager.scopes.get(target['run_id'])
            if scope:
                scope.cancel()
            self.wake(target['run_id'])
        return deletion

    async def _delete_project(self, row):
        uid, pid = row['user_id'], row['state']['project_id']
        delay = .25
        while not self.closed:
            try:
                # Send preparation cancellation concurrently with enumeration and
                # run cancellation. A stalled branch never delays the other signals.
                preparation = asyncio.create_task(self.project_remote(row, 'backend', 'POST',
                    f'/internal/projects/{pid}/cancel-preparations',
                    {'preparation_owner': row['state'].get('preparation_owner')}))
                try:
                    if not row['state']['enumerated']:
                        await self._enumerate_project(uid, pid)
                        row = await self.call(self.store.update_project_deletion, uid, pid, enumerated=True)
                    members = await self.call(self.store.project_members, uid, pid)
                    results = await asyncio.gather(*(self._request_project_session_delete(uid, pid, item['session_id'])
                        for item in members), return_exceptions=True)
                    stopped = await preparation
                    if not stopped.get('execution_stopped'):
                        raise ControlError('PROJECT_PREPARATION_ACTIVE')
                    for result in results:
                        if isinstance(result, BaseException):
                            raise result
                        self.wake_session_deletion(result)
                    tasks = [self.session_deletions[result['control_id']] for result in results
                             if result['control_id'] in self.session_deletions]
                    if tasks:
                        await asyncio.gather(*(asyncio.shield(task) for task in tasks))
                    for item in members:
                        deletion = await self.call(self.store.session_deletion, uid, item['session_id'])
                        if not deletion or deletion['state'].get('delete_status') != 'deleted':
                            raise ControlError('PROJECT_SESSION_DELETE_PENDING')
                    row = await self.call(self.store.update_project_deletion, uid, pid, sessions_deleted=True)
                    await self.project_remote(row, 'graph', 'DELETE', f'/internal/projects/{pid}/artifacts',
                        {'session_ids': [item['session_id'] for item in members]})
                    await self.project_remote(row, 'backend', 'DELETE', f'/internal/projects/{pid}/resources')
                    await self.call(self.store.update_project_deletion, uid, pid,
                                    delete_status='deleted', resources_deleted=True)
                    return
                finally:
                    if not preparation.done():
                        preparation.cancel()
                    await asyncio.gather(preparation, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('project_deletion_pending project_id=%s', pid)
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)

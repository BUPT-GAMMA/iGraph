"""Resumable General migration. Source files and graph versions never move."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from fastapi import HTTPException
from execution_lifecycle import atomic_json
from execution_protocol import controls_enabled, service_headers


class GeneralMigration:
    def __init__(self, projects, datasets, runtime, agent_id, graph=None):
        self.projects, self.datasets, self.runtime, self.agent_id = projects, datasets, runtime, agent_id
        self.graph = graph
        self.lock = asyncio.Lock()

    async def sessions(self, uid):
        rows, seen, page = [], set(), 1
        while True:
            payload = await self.runtime.request('GET', '/sessions', headers=service_headers(uid),
                params={'type': 'agent', 'component_id': self.agent_id, 'user_id': uid,
                        'limit': 100, 'page': page, 'sort_by': 'session_id', 'sort_order': 'asc'})
            batch = payload.get('data', []) if isinstance(payload, dict) else payload
            fresh = [row for row in batch if row.get('session_id') not in seen]
            if not fresh:
                break
            rows.extend(fresh)
            seen.update(row['session_id'] for row in fresh)
            if len(batch) < 100:
                break
            page += 1
        return rows

    async def run(self, uid, *, apply=True):
        async with self.lock:
            pid = self.projects.default_project_id(uid)
            project = self.projects.get(pid, uid)
            root = self.projects.path.parent / 'migrations' / 'general-v1' / hashlib.sha256(uid.encode()).hexdigest()
            manifest_path = root / 'manifest.json'
            saved = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
            if not project or not self.projects.is_legacy_default(project):
                return saved if saved.get('status') == 'completed' else {}
            rows = [row for row in await self.sessions(uid)
                    if (row.get('session_state') or {}).get('project_id') == pid]
            session_ids = sorted(set(saved.get('session_ids', [])) | {row['session_id'] for row in rows})
            report = {'legacy_default_project_id': pid, 'user_id': uid, 'conversations': len(rows),
                      'datasets': saved.get('datasets', len(self.datasets.list(pid, uid))),
                      'session_ids': session_ids, 'status': 'pending'}
            report['conversations'] = len(session_ids)
            if not apply:
                return report
            # Persist backups once, before any migration writes. A retry keeps the
            # original backups and applies only sessions still carrying General.
            root.mkdir(parents=True, exist_ok=True)
            for name, source in [('projects.json', self.projects.path), ('datasets.json', self.datasets.metadata_path)]:
                target = root / name
                if source.exists() and not target.exists():
                    atomic_json(target, json.loads(source.read_text()))
            for row in rows:
                sid = row['session_id']
                target = root / ('session-' + hashlib.sha256(sid.encode()).hexdigest() + '.json')
                if not target.exists():
                    # The native detail preserves messages and state for recovery.
                    detail = await self.runtime.request('GET', '/sessions/' + sid, headers=service_headers(uid),
                                                        params={'type': 'agent', 'user_id': uid})
                    atomic_json(target, detail)
            atomic_json(manifest_path, report)
            for row in rows:
                sid = row['session_id']
                runs = await self.runtime.request('GET', '/sessions/' + sid + '/runs',
                                                  headers=service_headers(uid), params={'user_id': uid})
                runs = runs.get('data', []) if isinstance(runs, dict) else runs
                if any(str(run.get('status', '')).lower() not in {'completed', 'cancelled', 'failed', 'error'} for run in runs):
                    raise HTTPException(409, {'code': 'GENERAL_MIGRATION_ACTIVE', 'message': '请先结束旧 General 中的任务，再完成升级'})
                if self.graph and controls_enabled():
                    status = await self.graph.request('GET', f'/internal/sessions/{sid}/resource-status',
                                                      headers=service_headers(uid))
                    if not status.get('execution_stopped'):
                        raise HTTPException(409, {'code': 'GENERAL_MIGRATION_ACTIVE', 'message': '请先结束旧 General 中的后台作业，再完成升级'})
            self.datasets.migrate_to_personal(pid, uid)
            for row in rows:
                detail = await self.runtime.request('GET', '/sessions/' + row['session_id'],
                    headers=service_headers(uid), params={'type': 'agent', 'user_id': uid})
                state = dict(detail.get('session_state') or {})
                if state.get('project_id') != pid:
                    continue
                state['project_id'] = None
                await self.runtime.request('PATCH', '/sessions/' + row['session_id'], headers=service_headers(uid),
                    params={'type': 'agent', 'user_id': uid}, json_body={'session_state': state})
            if self.graph:
                for offset in range(0, len(session_ids), 100):
                    await self.graph.request('POST', '/internal/artifacts/ownership', headers=service_headers(uid),
                        json_body={'sessions': [{'session_id': sid, 'project_id': None}
                            for sid in session_ids[offset:offset + 100]]})
            report['status'] = 'completed'
            atomic_json(manifest_path, report)
            # Metadata only; paths containing the legacy ID remain valid.
            self.projects.delete(pid, uid)
            return report

"""Shared provider connections, seeded once from deployment configuration.

Lock order is session -> user models -> shared catalogue. Shared mutations never
acquire another user's lock. Run admission holds the shared lock until its frozen
model version and run record commit, including across runtime replicas.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from urllib.parse import urlsplit

from sqlalchemy import Column, Integer, JSON, String, Table, select, text

from agent_runtime.execution_store import ControlError, metadata
from agent_runtime.model_factory import _endpoint
from agent_runtime.model_registry import SUPPORTED_MODELS, provider_for_model
from agent_runtime.provider_catalog import PROVIDER_PRESETS

shared_providers = Table('igraph_shared_model_providers', metadata,
    Column('provider_id', String(256), primary_key=True),
    Column('revision', Integer, nullable=False), Column('data', JSON, nullable=False))
provider_migrations = Table('igraph_provider_migrations', metadata,
    Column('migration_id', String(80), primary_key=True))


def connection_identity(base, model_id):
    host = urlsplit(base).hostname or ''
    if host in {'api.zhizengzeng.com', 'api.smarttrot.com', 'flag.smarttrot.com', 'api.zzz-api.top', 'key.zhizengzeng.com'}:
        return '智增增', 'zhizengzeng'
    for preset in PROVIDER_PRESETS:
        if base and preset['base_url'] and host == urlsplit(preset['base_url']).hostname:
            return preset['name'], preset['id']
    brand = provider_for_model(model_id)
    return host or {'openai': 'OpenAI', 'anthropic': 'Anthropic', 'deepseek': 'DeepSeek',
                    'doubao': '豆包', 'qwen': '阿里云百炼'}.get(brand, brand), 'custom'


class SharedProviderCatalogMixin:
    def _shared_lock(self, conn):
        if self.store.engine.dialect.name == 'postgresql':
            key = int.from_bytes(hashlib.sha256(b'igraph/shared-model-providers/v1').digest()[:8], 'big', signed=True)
            conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})

    def _is_admin(self, conn, uid):
        return bool(self._preference(conn, uid).get('is_admin'))

    def _assert_provider_editable(self, conn, uid, row):
        if row['data'].get('scope') == 'shared' and not self._is_admin(conn, uid):
            raise ControlError('MODEL_PROVIDER_READ_ONLY', 403)

    def _seed_shared(self, conn):
        self._shared_lock(conn)
        if conn.execute(select(provider_migrations).where(provider_migrations.c.migration_id == 'deployment-v1')).first():
            return
        groups = {}
        for mid in sorted(SUPPORTED_MODELS):
            key, base = _endpoint(mid, self.config, allow_missing=True)
            base = base.rstrip('/')
            group = (base, key, '' if base else provider_for_model(mid))
            if group not in groups:
                name, preset = connection_identity(base, mid)
                groups[group] = dict(provider_id=f'shared-{uuid.uuid4()}', revision=1,
                    data=dict(name=name, preset_id=preset, scope='shared', base_url=base,
                              api_key=key, enabled=True, models={}))
            groups[group]['data']['models'][mid] = dict(model_id=mid, display_name=mid,
                tools=True, vision=False, context_window=int(os.getenv('IGRAPH_MODEL_CONTEXT_WINDOW', '131072')),
                max_output_tokens=4096, restricted=True)
        for row in groups.values():
            conn.execute(shared_providers.insert().values(**row))
        conn.execute(provider_migrations.insert().values(migration_id='deployment-v1'))

    def _shared_rows(self, conn):
        return [dict(row) for row in conn.execute(select(shared_providers)).mappings().all()
                if not row['data'].get('deleted')]

    def _sync_shared(self, conn, uid):
        self._shared_lock(conn)
        pref = self._preference(conn, uid)
        if not pref:
            return
        for provider in self._shared_rows(conn):
            for cid, model in provider['data']['models'].items():
                existing = self._row(conn, uid, cid)
                if model.get('restricted') and cid not in pref.get('allowed', []) and not pref.get('is_admin') and not existing:
                    continue
                old = existing['data'] if existing else {}
                # Keep each user's visibility switch and the original configuration
                # ID used by session selections and historical run snapshots.
                data = dict(model, source='deployment', enabled=old.get('enabled', True),
                            **self._provider_snapshot(provider))
                if not existing or old != data:
                    self._write(conn, uid, cid, data, existing)

    def _save_shared_model(self, conn, uid, provider, cid, stored):
        self._assert_provider_editable(conn, uid, provider)
        data = provider['data']
        catalog = dict(data['models'])
        if any(key != cid and model['model_id'] == stored['model_id'] for key, model in catalog.items()):
            raise ControlError('MODEL_DUPLICATE', 409)
        cid = cid or f'shared-model-{uuid.uuid4()}'
        fields = ('model_id', 'display_name', 'tools', 'vision', 'context_window', 'max_output_tokens')
        catalog[cid] = {**{key: stored[key] for key in fields}, 'restricted': catalog.get(cid, {}).get('restricted', False)}
        self._update_shared_models(conn, provider, catalog)
        self._sync_shared(conn, uid)
        return self.public(conn, uid, self._row(conn, uid, cid))

    def _update_shared_models(self, conn, provider, catalog):
        # Connection revisions guard credentials; model edits have their own
        # immutable revisions, so multi-batch discovery imports remain valid.
        conn.execute(shared_providers.update().where(shared_providers.c.provider_id == provider['provider_id'])
                     .values(data={**provider['data'], 'models': catalog}))

    def _remove_shared_models(self, conn, provider, ids, *, in_use_code='MODEL_IN_USE'):
        table = self.model_table
        rows = conn.execute(select(table).where(table.c.config_id.in_(ids),
            table.c.data['source'].as_string() == 'deployment')).mappings().all()
        grouped = {}
        for row in rows:
            if not row['data'].get('deleted'):
                grouped.setdefault(row['user_id'], []).append(row)
        # Any active member causes this entire transaction to roll back.
        for uid, group in grouped.items():
            self._remove_models(conn, uid, group, in_use_code=in_use_code)
        self._update_shared_models(conn, provider, {cid: model for cid, model in provider['data']['models'].items() if cid not in ids})

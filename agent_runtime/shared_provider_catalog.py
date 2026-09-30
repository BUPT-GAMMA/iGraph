"""One-time migration from deployment connections to user-owned providers.

The old shared tables remain readable migration inputs. Browser requests only
read or mutate rows in the requesting user's personal provider catalogue.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from urllib.parse import urlsplit

from sqlalchemy import Column, Integer, JSON, String, Table, select, text

from agent_runtime.execution_store import metadata
from agent_runtime.model_factory import _endpoint
from agent_runtime.model_registry import SUPPORTED_MODELS, provider_for_model
from agent_runtime.provider_catalog import PROVIDER_PRESETS, providers

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


class DeploymentProviderCatalogMixin:
    def _deployment_lock(self, conn):
        # Keep the old lock key while reading legacy rows during an upgrade.
        if self.store.engine.dialect.name == 'postgresql':
            key = int.from_bytes(hashlib.sha256(b'igraph/shared-model-providers/v1').digest()[:8], 'big', signed=True)
            conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})

    def _deployment_templates(self, conn):
        legacy = [dict(row['data']) for row in conn.execute(select(shared_providers)).mappings()
                  if not row['data'].get('deleted')]
        if legacy:
            templates = legacy
        else:
            groups = {}
            for mid in sorted(SUPPORTED_MODELS):
                key, base = _endpoint(mid, self.config, allow_missing=True)
                base = base.rstrip('/') or 'https://api.zhizengzeng.com/v1'
                group = (base, key)
                if group not in groups:
                    name, preset = connection_identity(base, mid)
                    groups[group] = dict(name=name, preset_id=preset, base_url=base,
                                         api_key=key, enabled=True, models={})
                groups[group]['models'][mid] = dict(model_id=mid, display_name=mid,
                    tools=True, vision=False, context_window=int(os.getenv('IGRAPH_MODEL_CONTEXT_WINDOW', '131072')),
                    max_output_tokens=4096)
            templates = list(groups.values())
        if not any(p.get('preset_id') == 'zhizengzeng' for p in templates):
            templates.append(dict(name='智增增', preset_id='zhizengzeng',
                base_url='https://api.zhizengzeng.com/v1', api_key='', enabled=True, models={}))
        return templates

    def _ensure_personal_providers(self, conn, uid):
        pref = self._preference(conn, uid)
        if not pref or pref.get('personal_providers_v1'):
            return
        self._deployment_lock(conn)
        for template in self._deployment_templates(conn):
            data = {key: value for key, value in template.items()
                    if key not in {'scope', 'models', 'deleted'}}
            provider = dict(user_id=uid, provider_id=f'provider-{uuid.uuid4()}', revision=1, data=data)
            conn.execute(providers.insert().values(**provider))
            for cid, model in template.get('models', {}).items():
                existing = self._row(conn, uid, cid)
                old = existing['data'] if existing else {}
                # Retain deletions and personal models; do not revive tombstones.
                if old.get('deleted') or (existing and old.get('source') != 'deployment'):
                    continue
                model_data = {key: value for key, value in model.items() if key != 'restricted'}
                model_data.update(source='custom', enabled=old.get('enabled', True),
                                  **self._provider_snapshot(provider))
                # IDs and selections stay stable; frozen runs keep their old versions.
                self._write(conn, uid, cid, model_data, existing)
        for row in conn.execute(select(self.model_table).where(self.model_table.c.user_id == uid)).mappings():
            if row['data'].get('source') == 'deployment' and not row['data'].get('deleted'):
                data = {**row['data'], 'source': 'custom'}
                data.pop('restricted', None)
                self._write(conn, uid, row['config_id'], self._legacy_provider(conn, uid, data), row)
        pref.pop('is_admin', None)
        pref.pop('allowed', None)
        self._set_preference(conn, uid, {**pref, 'personal_providers_v1': True})

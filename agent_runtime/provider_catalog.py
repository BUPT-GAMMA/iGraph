"""Provider connections share the model catalog transaction and revision history."""
from __future__ import annotations

import re
import uuid
from urllib.parse import urlsplit
from sqlalchemy import Column, Integer, JSON, String, Table, select
from agent_runtime.execution_store import ControlError, metadata
from model_protocols import PROTOCOLS, protocol_for

providers = Table('igraph_model_providers', metadata,
    Column('user_id', String(256), primary_key=True),
    Column('provider_id', String(256), primary_key=True),
    Column('revision', Integer, nullable=False), Column('data', JSON, nullable=False))

# Presets describe connections, not a rapidly ageing hard-coded model catalogue.
PROVIDER_PRESETS = [
    dict(id='zhizengzeng', name='智增增', base_url='https://api.zhizengzeng.com/v1', icon='zhizengzeng'),
    dict(id='openai', name='OpenAI', base_url='https://api.openai.com/v1', icon='openai'),
    dict(id='openai-responses', name='OpenAI Responses', base_url='https://api.openai.com/v1', icon='openai', protocol='openai-responses'),
    dict(id='google', name='Google Gemini', base_url='https://generativelanguage.googleapis.com/v1beta', icon='google', protocol='gemini'),
    dict(id='anthropic', name='Anthropic', base_url='https://api.anthropic.com/v1', icon='anthropic', protocol='anthropic'),
    dict(id='deepseek', name='DeepSeek', base_url='https://api.deepseek.com/v1', icon='deepseek'),
    dict(id='qwen', name='阿里云百炼', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1', icon='qwen'),
    dict(id='gemini', name='Gemini (OpenAI)', base_url='https://generativelanguage.googleapis.com/v1beta/openai', icon='google'),
    dict(id='ollama', name='Ollama', base_url='http://host.docker.internal:11434/v1', icon='ollama'),
    dict(id='custom', name='自定义提供商', base_url='', icon='custom'),
]
PROVIDER_PRESETS = [dict(protocol='openai-compatible', **p) if 'protocol' not in p else p for p in PROVIDER_PRESETS]


def validate_base_url(value):
    base = str(value or '').strip().rstrip('/')
    try:
        url = urlsplit(base)
        if (url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password
                or url.query or url.fragment or any(char.isspace() or ord(char) < 32 for char in base)):
            raise ValueError()
        _ = url.port
    except ValueError:
        raise ControlError('MODEL_INVALID_CONFIG', 422) from None
    if len(base) > 2048:
        raise ControlError('MODEL_INVALID_CONFIG', 422)
    return base


class ProviderCatalogMixin:
    def _provider_row(self, conn, uid, pid):
        row = conn.execute(select(providers).where(providers.c.user_id == uid, providers.c.provider_id == pid)).mappings().first()
        return dict(row) if row else None

    def _require_provider(self, conn, uid, pid, revision=None):
        row = self._provider_row(conn, uid, pid)
        if not row:
            raise ControlError('MODEL_PROVIDER_REMOVED', 404)
        if revision is not None and row['revision'] != revision:
            raise ControlError('MODEL_PROVIDER_CHANGED')
        return row

    def _provider_models(self, conn, uid, pid):
        table = self.model_table
        return [row for row in conn.execute(select(table).where(table.c.user_id == uid,
                    table.c.data['provider_id'].as_string() == pid)).mappings().all()
                if not row['data'].get('deleted')]

    def _public_provider(self, conn, uid, row):
        data = row['data']
        return dict(provider_id=row['provider_id'], revision=row['revision'], name=data['name'],
                    preset_id=data['preset_id'], protocol=protocol_for(data), base_url=data['base_url'],
                    enabled=data['enabled'], has_api_key=bool(data['api_key']),
                    model_count=len(self._provider_models(conn, uid, row['provider_id'])),
                    scope='personal', editable=True)

    def _provider_snapshot(self, row):
        data = row['data']
        return dict(provider_id=row['provider_id'], provider_name=data['name'], provider_enabled=data['enabled'],
                    requires_api_key=data['preset_id'] not in {'custom', 'ollama'},
                    provider=data['preset_id'] if data['preset_id'] != 'custom' else 'openai-compatible',
                    protocol=protocol_for(data), base_url=data['base_url'], api_key=data['api_key'])

    def _legacy_provider(self, conn, uid, data):
        """Group old connections once, without changing their IDs or selections."""
        rows = conn.execute(select(providers).where(providers.c.user_id == uid)).mappings().all()
        row = next((r for r in rows if r['data']['base_url'] == data['base_url']
                    and r['data']['api_key'] == data.get('api_key', '') and r['data']['enabled']
                    and protocol_for(r['data']) == 'openai-compatible'), None)
        if not row:
            try:
                name = urlsplit(data['base_url']).hostname or 'Custom provider'
            except ValueError:
                name = 'Custom provider'
            row = dict(user_id=uid, provider_id=f'provider-{uuid.uuid4()}', revision=1,
                data=dict(name=name, preset_id='custom', base_url=data['base_url'], api_key=data.get('api_key', ''), enabled=True))
            conn.execute(providers.insert().values(**row))
        return {**data, **self._provider_snapshot(row)}

    def _migrate_providers(self, conn, uid):
        table = self.model_table
        for row in conn.execute(select(table).where(table.c.user_id == uid)).mappings().all():
            data = row['data']
            if data.get('source') == 'custom' and not data.get('deleted') and not data.get('provider_id'):
                self._write(conn, uid, row['config_id'], self._legacy_provider(conn, uid, data), row)

    def list_providers(self, uid):
        with self.store.transaction(uid, '__models__') as conn:
            self._ensure_personal_providers(conn, uid)
            self._migrate_providers(conn, uid)
            rows = conn.execute(select(providers).where(providers.c.user_id == uid)).mappings().all()
            return dict(providers=[self._public_provider(conn, uid, r) for r in rows],
                        presets=PROVIDER_PRESETS)

    def save_provider(self, uid, data, pid=None, expected_revision=None):
        with self.store.transaction(uid, '__models__') as conn:
            self._ensure_personal_providers(conn, uid)
            existing = self._require_provider(conn, uid, pid) if pid else None
            if existing and existing['revision'] != expected_revision:
                raise ControlError('MODEL_PROVIDER_CHANGED')
            old = existing['data'] if existing else {}
            merged = {**old, **data}
            name = str(merged.get('name') or '').strip()
            preset = str(merged.get('preset_id') or 'custom')
            if not name or len(name) > 120 or preset not in {p['id'] for p in PROVIDER_PRESETS}:
                raise ControlError('MODEL_PROVIDER_INVALID', 422)
            protocol = data.get('protocol') or (protocol_for(old) if existing else next(p['protocol'] for p in PROVIDER_PRESETS if p['id'] == preset))
            if protocol not in PROTOCOLS:
                raise ControlError('MODEL_PROTOCOL_UNSUPPORTED', 422)
            secret = '' if data.get('clear_api_key') else str(data.get('api_key') or old.get('api_key', '')).strip()
            if len(secret) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in secret):
                raise ControlError('MODEL_PROVIDER_INVALID', 422)
            base = str(merged.get('base_url') or '').strip()
            stored = dict(name=name, preset_id=preset, protocol=protocol, base_url=validate_base_url(base) if base else '',
                          api_key=secret, enabled=bool(merged.get('enabled', True)))
            row = dict(user_id=uid, provider_id=pid or f'provider-{uuid.uuid4()}',
                       revision=existing['revision'] + 1 if existing else 1, data=stored)
            if existing:
                conn.execute(providers.update().where(providers.c.user_id == uid, providers.c.provider_id == pid).values(**row))
                # Copy into versioned model rows atomically. Active runs retain their
                # old credentials and endpoint through the immutable model version.
                for model in self._provider_models(conn, uid, pid):
                    self._write(conn, uid, model['config_id'], {**model['data'], **self._provider_snapshot(row)}, model)
            else:
                conn.execute(providers.insert().values(**row))
            return self._public_provider(conn, uid, row)

    def import_provider_models(self, uid, pid, revision, items):
        if not 1 <= len(items) <= 200:
            raise ControlError('MODEL_IMPORT_LIMIT', 422)
        with self.store.transaction(uid, '__models__') as conn:
            self._ensure_personal_providers(conn, uid)
            provider = self._require_provider(conn, uid, pid, revision)
            if not provider['data']['base_url']:
                raise ControlError('MODEL_NOT_CONFIGURED', 422)
            known = {row['data']['model_id'] for row in self._provider_models(conn, uid, pid)}
            added = 0
            for item in items:
                mid = str(item.get('id') or '').strip()
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}', mid):
                    raise ControlError('MODEL_INVALID_CONFIG', 422)
                if mid in known:
                    continue
                data = dict(model_id=mid, display_name=str(item.get('name') or mid).strip()[:120],
                    source='custom', enabled=True, tools=True, vision=False,
                    context_window=131072, max_output_tokens=4096, **self._provider_snapshot(provider))
                self._write(conn, uid, f'custom-{uuid.uuid4()}', data)
                known.add(mid)
                added += 1
            return dict(success=True, added_count=added, skipped_count=len(items) - added)

    def provider_credentials(self, uid, pid, revision):
        with self.store.transaction(uid, '__models__') as conn:
            self._ensure_personal_providers(conn, uid)
            row = self._require_provider(conn, uid, pid, revision)
            return dict(provider_id=pid, revision=row['revision'], api_key=row['data']['api_key'])

    def provider_configuration(self, uid, pid, revision):
        with self.store.transaction(uid, '__models__') as conn:
            self._ensure_personal_providers(conn, uid)
            row = self._require_provider(conn, uid, pid, revision)
            data = dict(row['data'])
            if not data['base_url']:
                raise ControlError('MODEL_NOT_CONFIGURED', 422)
            return data

    def delete_provider(self, uid, pid, revision):
        with self.store.transaction(uid, '__models__') as conn:
            self._ensure_personal_providers(conn, uid)
            row = self._require_provider(conn, uid, pid, revision)
            self._remove_models(conn, uid, self._provider_models(conn, uid, pid),
                                in_use_code='MODEL_PROVIDER_IN_USE')
            conn.execute(providers.delete().where(providers.c.user_id == uid, providers.c.provider_id == pid))

"""Durable model configurations. Lists omit secrets; provider owners can read credentials through a scoped API.

All mutations and run admission share the user's model lock. Session selection
additionally uses the execution ledger's session lock (session, then model).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from urllib.parse import urlsplit

from sqlalchemy import Column, Integer, JSON, String, Table, select, text

from agent_runtime.execution_store import ControlError, controls, metadata
from agent_runtime.provider_catalog import ProviderCatalogMixin, providers, validate_base_url
from agent_runtime.shared_provider_catalog import SharedProviderCatalogMixin

models = Table('igraph_model_configs', metadata,
    Column('user_id', String(256), primary_key=True),
    Column('config_id', String(256), primary_key=True),
    Column('revision', Integer, nullable=False), Column('data', JSON, nullable=False))
versions = Table('igraph_model_versions', metadata,
    Column('user_id', String(256), primary_key=True),
    Column('config_id', String(256), primary_key=True),
    Column('revision', Integer, primary_key=True), Column('data', JSON, nullable=False))
selections = Table('igraph_model_selections', metadata,
    Column('user_id', String(256), primary_key=True),
    Column('session_id', String(256), primary_key=True),
    Column('config_id', String(256), nullable=False), Column('revision', Integer, nullable=False))
preferences = Table('igraph_model_preferences', metadata,
    Column('user_id', String(256), primary_key=True), Column('data', JSON, nullable=False))


class ModelCatalog(SharedProviderCatalogMixin, ProviderCatalogMixin):
    model_table = models
    def __init__(self, store, config):
        self.store, self.config = store, config

    def lock(self, conn, uid):
        if self.store.engine.dialect.name == 'postgresql':
            key = int.from_bytes(hashlib.sha256(json.dumps([uid, '__models__']).encode()).digest()[:8], 'big', signed=True)
            conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
        self._sync_shared(conn, uid)

    def _row(self, conn, uid, cid):
        row = conn.execute(select(models).where(models.c.user_id == uid, models.c.config_id == cid)).mappings().first()
        return dict(row) if row else None

    def _write(self, conn, uid, cid, data, existing=None):
        revision = existing['revision'] + 1 if existing else 1
        row = dict(user_id=uid, config_id=cid, revision=revision, data=data)
        if existing:
            conn.execute(models.update().where(models.c.user_id == uid, models.c.config_id == cid).values(**row))
        else:
            conn.execute(models.insert().values(**row))
        conn.execute(versions.insert().values(**row))
        return row

    def _preference(self, conn, uid):
        return conn.execute(select(preferences.c.data).where(preferences.c.user_id == uid)).scalar_one_or_none() or {}

    def _set_preference(self, conn, uid, data):
        exists = conn.execute(select(preferences.c.user_id).where(preferences.c.user_id == uid)).first()
        if exists:
            conn.execute(preferences.update().where(preferences.c.user_id == uid).values(data=data))
        else:
            conn.execute(preferences.insert().values(user_id=uid, data=data))

    def bootstrap(self, uid, allowed, legacy=(), is_admin=False):
        """Idempotent migration; tombstones prevent deleted legacy rows returning."""
        with self.store.transaction(uid, '__models__') as conn:
            pref = self._preference(conn, uid)
            pref.update(allowed=list(allowed), is_admin=is_admin)
            pref.setdefault('default_model', next(iter(allowed), ''))
            self._set_preference(conn, uid, pref)
            self._seed_shared(conn)
            self._sync_shared(conn, uid)
            for item in legacy:
                cid = str(item.get('model_id') or '')
                if not cid or self._row(conn, uid, cid):
                    continue
                data = {key: item.get(key, '') for key in ('model_id', 'display_name', 'base_url', 'api_key')}
                data.update(source='custom', provider='openai-compatible', enabled=True, tools=True,
                            vision=False, context_window=131072, max_output_tokens=4096)
                self._write(conn, uid, cid, self._legacy_provider(conn, uid, data))
            self._migrate_providers(conn, uid)
        return self.list(uid)

    def _reason(self, conn, uid, row):
        if not row or row['data'].get('deleted'):
            return 'MODEL_REMOVED'
        data = row['data']
        if (data.get('source') == 'deployment' and data.get('restricted', True)
                and row['config_id'] not in self._preference(conn, uid).get('allowed', []) and not self._is_admin(conn, uid)):
            return 'MODEL_NOT_ALLOWED'
        if not data.get('provider_enabled', True):
            return 'MODEL_PROVIDER_DISABLED'
        if not data.get('enabled', True):
            return 'MODEL_DISABLED'
        if not data.get('base_url') or (data.get('requires_api_key') and not data.get('api_key')):
            return 'MODEL_NOT_CONFIGURED'
        if not data.get('tools', True):
            return 'MODEL_TOOLS_REQUIRED'
        return data.get('unavailable_reason')

    def public(self, conn, uid, row):
        data = row['data']
        return dict(model_id=row['config_id'], config_id=row['config_id'], revision=row['revision'],
            upstream_model_id=data['model_id'], display_name=data.get('display_name') or data['model_id'],
            provider=data.get('provider', 'openai-compatible'), provider_id=data.get('provider_id'),
            provider_name=data.get('provider_name'), base_url=data.get('base_url', ''),
            has_api_key=bool(data.get('api_key')), source=data['source'], enabled=data.get('enabled', True),
            tools=data.get('tools', True), vision=data.get('vision', False),
            context_window=data.get('context_window', 131072), max_output_tokens=data.get('max_output_tokens', 4096),
            unavailable_reason=self._reason(conn, uid, row))

    def list(self, uid):
        with self.store.transaction(uid, '__models__') as conn:
            self._sync_shared(conn, uid)
            rows = conn.execute(select(models).where(models.c.user_id == uid)).mappings().all()
            return {'models': [self.public(conn, uid, row) for row in rows if not row['data'].get('deleted')],
                    'default_model': self._preference(conn, uid).get('default_model', '')}

    def save(self, uid, data, cid=None, expected_revision=None):
        with self.store.transaction(uid, '__models__') as conn:
            self._sync_shared(conn, uid)
            existing = self._row(conn, uid, cid) if cid else None
            if cid and (not existing or existing['data'].get('deleted')):
                raise ControlError('MODEL_REMOVED', 404)
            if existing and expected_revision != existing['revision']:
                raise ControlError('MODEL_CONFIG_CHANGED')
            old = existing['data'] if existing else {}
            if old.get('source') == 'deployment' and not data.get('provider_id'):
                raise ControlError('MODEL_PROVIDER_READ_ONLY', 403)
            merged = {**old, **data}
            provider_row = None
            if data.get('provider_id'):
                if data.get('provider_revision') is None:
                    raise ControlError('MODEL_PROVIDER_CHANGED')
                provider_row = self._require_provider(conn, uid, data['provider_id'], data['provider_revision'])
                self._assert_provider_editable(conn, uid, provider_row)
                if old.get('provider_id') and old['provider_id'] != data['provider_id']:
                    raise ControlError('MODEL_PROVIDER_INVALID', 422)
                merged.update(self._provider_snapshot(provider_row))
            mid = str(merged.get('upstream_model_id') or merged.get('model_id') or '').strip()
            base = validate_base_url(merged.get('base_url'))
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}', mid):
                raise ControlError('MODEL_INVALID_CONFIG', 422)
            window = int(merged.get('context_window', 131072))
            output = int(merged.get('max_output_tokens', 4096))
            if not 2048 <= window <= 2_000_000 or not 128 <= output < window:
                raise ControlError('MODEL_INVALID_LIMITS', 422)
            # Blank preserves a saved credential; explicit clear supports local servers.
            secret = '' if data.get('clear_api_key') else (data.get('api_key') or old.get('api_key', ''))
            stored = dict(model_id=mid, display_name=str(merged.get('display_name') or mid).strip()[:120],
                provider=str(merged.get('provider') or 'openai-compatible')[:80], base_url=base, api_key=secret,
                source='custom', enabled=old.get('enabled', True), tools=bool(merged.get('tools', True)),
                vision=bool(merged.get('vision', False)), context_window=window, max_output_tokens=output)
            if provider_row and provider_row['data'].get('scope') == 'shared':
                return self._save_shared_model(conn, uid, provider_row, cid, stored)
            if provider_row:
                stored.update(self._provider_snapshot(provider_row))
                if any(r['config_id'] != cid and r['data']['model_id'] == mid
                       for r in self._provider_models(conn, uid, data['provider_id'])):
                    raise ControlError('MODEL_DUPLICATE', 409)
            else:
                stored = self._legacy_provider(conn, uid, stored)
            row = self._write(conn, uid, cid or f'custom-{uuid.uuid4()}', stored, existing)
            return self.public(conn, uid, row)

    def set_enabled(self, uid, cid, enabled, expected_revision):
        with self.store.transaction(uid, '__models__') as conn:
            self._sync_shared(conn, uid)
            row = self._row(conn, uid, cid)
            if not row or row['data'].get('deleted'):
                raise ControlError('MODEL_REMOVED', 404)
            if row['revision'] != expected_revision:
                raise ControlError('MODEL_CONFIG_CHANGED')
            row = self._write(conn, uid, cid, {**row['data'], 'enabled': enabled}, row)
            return self.public(conn, uid, row)

    def set_default(self, uid, cid):
        with self.store.transaction(uid, '__models__') as conn:
            self._sync_shared(conn, uid)
            reason = self._reason(conn, uid, self._row(conn, uid, cid))
            if reason:
                raise ControlError(reason, 422)
            self._set_preference(conn, uid, {**self._preference(conn, uid), 'default_model': cid})
        return self.list(uid)

    def delete(self, uid, cid, expected_revision):
        with self.store.transaction(uid, '__models__') as conn:
            self._sync_shared(conn, uid)
            row = self._row(conn, uid, cid)
            if not row or row['data'].get('deleted'):
                raise ControlError('MODEL_REMOVED', 404)
            if row['revision'] != expected_revision:
                raise ControlError('MODEL_CONFIG_CHANGED')
            if row['data']['source'] == 'deployment':
                provider = self._require_provider(conn, uid, row['data']['provider_id'])
                self._assert_provider_editable(conn, uid, provider)
                self._remove_shared_models(conn, provider, [cid])
            else:
                self._remove_models(conn, uid, [row])

    def _remove_models(self, conn, uid, rows, *, in_use_code='MODEL_IN_USE'):
        if not rows:
            return
        ids = [row['config_id'] for row in rows]
        active = conn.execute(select(controls.c.session_id).where(controls.c.user_id == uid,
            controls.c.kind == 'run', controls.c.admission_blocked.is_(True),
            controls.c.state['model_snapshot']['config_id'].as_string().in_(ids))).all()
        if active:
            exc = ControlError(in_use_code)
            exc.sessions = sorted({item[0] for item in active})
            raise exc
        for row in rows:
            cid = row['config_id']
            # Keep a non-secret tombstone for old selections and repeat migration.
            clean = {**row['data'], 'api_key': '', 'base_url': '', 'enabled': False, 'deleted': True}
            self._write(conn, uid, cid, clean, row)
            conn.execute(versions.delete().where(versions.c.user_id == uid, versions.c.config_id == cid))

    def _selection(self, conn, uid, sid, legacy_model=None):
        row = conn.execute(select(selections).where(selections.c.user_id == uid, selections.c.session_id == sid)).mappings().first()
        if row:
            return dict(row)
        row = dict(user_id=uid, session_id=sid, config_id=legacy_model or self._preference(conn, uid).get('default_model', ''), revision=0)
        conn.execute(selections.insert().values(**row))
        return row

    def purge_user(self, uid):
        with self.store.transaction(uid, '__models__') as conn:
            self._sync_shared(conn, uid)
            if conn.execute(select(controls.c.control_id).where(controls.c.user_id == uid,
                    controls.c.kind == 'run', controls.c.admission_blocked.is_(True))).first():
                raise ControlError('MODEL_IN_USE')
            for table in (versions, models, selections, preferences, providers):
                conn.execute(table.delete().where(table.c.user_id == uid))

    def selection(self, uid, sid, legacy_model=None):
        with self.store.transaction(uid, sid) as conn:
            self.lock(conn, uid)
            choice = self._selection(conn, uid, sid, legacy_model)
            row = self._row(conn, uid, choice['config_id'])
            return dict(model=choice['config_id'], model_revision=choice['revision'],
                model_unavailable_reason=self._reason(conn, uid, row),
                model_details=self.public(conn, uid, row) if row else None)

    def select(self, uid, sid, cid, expected_revision, legacy_model=None):
        with self.store.transaction(uid, sid) as conn:
            self.store._assert_session_open(conn, uid, sid)
            self.lock(conn, uid)
            if self.store._active(conn, uid, sid):
                raise ControlError('RUN_ACTIVE')
            choice = self._selection(conn, uid, sid, legacy_model)
            if choice['revision'] != expected_revision:
                raise ControlError('MODEL_SELECTION_CHANGED')
            reason = self._reason(conn, uid, self._row(conn, uid, cid))
            if reason:
                raise ControlError(reason, 422)
            revision = choice['revision'] + (choice['config_id'] != cid)
            conn.execute(selections.update().where(selections.c.user_id == uid, selections.c.session_id == sid)
                         .values(config_id=cid, revision=revision))
        return self.selection(uid, sid)

    def freeze(self, conn, uid, sid, legacy_model=None, expected_revision=None):
        """Called inside run registration, before releasing the session lock."""
        self.lock(conn, uid)
        choice = self._selection(conn, uid, sid, legacy_model)
        if expected_revision is not None and expected_revision != choice['revision']:
            raise ControlError('MODEL_SELECTION_CHANGED')
        row = self._row(conn, uid, choice['config_id'])
        reason = self._reason(conn, uid, row)
        if reason:
            raise ControlError(reason, 422)
        data = row['data']
        return dict(config_id=row['config_id'], revision=row['revision'], selection_revision=choice['revision'],
            model_id=data['model_id'], display_name=data.get('display_name') or data['model_id'], provider=data['provider'])

    def resolve(self, uid, snapshot):
        """Private lookup. Snapshot never includes keys or endpoint credentials."""
        with self.store.transaction(uid, '__models__') as conn:
            self._sync_shared(conn, uid)
            data = conn.execute(select(versions.c.data).where(versions.c.user_id == uid,
                versions.c.config_id == snapshot['config_id'], versions.c.revision == snapshot['revision'])).scalar_one_or_none()
            if not data:
                raise ControlError('MODEL_REMOVED', 422)
            return dict(data)

    def test_configuration(self, uid, cid, revision):
        with self.store.transaction(uid, '__models__') as conn:
            self._sync_shared(conn, uid)
            row = self._row(conn, uid, cid)
            if not row or row['data'].get('deleted'):
                raise ControlError('MODEL_REMOVED', 404)
            if revision != row['revision']:
                raise ControlError('MODEL_CONFIG_CHANGED')
            if self._reason(conn, uid, row) == 'MODEL_NOT_ALLOWED':
                raise ControlError('MODEL_NOT_ALLOWED', 403)
            return dict(row['data'])

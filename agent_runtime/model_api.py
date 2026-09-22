"""Service-authenticated model configuration and selection routes."""
import asyncio
import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from execution_protocol import require_service
from agent_runtime.execution_store import ControlError, TERMINAL


class BootstrapModels(BaseModel):
    allowed: list[str] = Field(default_factory=list)
    legacy: list[dict] = Field(default_factory=list)


class ModelWrite(BaseModel):
    config_id: str | None = None
    revision: int | None = None
    upstream_model_id: str = Field(min_length=1, max_length=256)
    display_name: str = Field(default='', max_length=120)
    base_url: str = Field(default='', max_length=2048)
    provider_id: str | None = None
    provider_revision: int | None = None
    api_key: str = Field(default='', max_length=8192)
    clear_api_key: bool = False
    provider: str = Field(default='openai-compatible', max_length=80)
    tools: bool = True
    vision: bool = False
    context_window: int = Field(default=131072, ge=2048, le=2_000_000)
    max_output_tokens: int = Field(default=4096, ge=128)


class ProviderWrite(BaseModel):
    provider_id: str | None = None
    revision: int | None = None
    name: str = Field(min_length=1, max_length=120)
    preset_id: str = 'custom'
    protocol: str = 'openai-compatible'
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str = ''
    clear_api_key: bool = False
    enabled: bool = True


class ProviderTarget(BaseModel):
    provider_id: str
    revision: int


class DiscoveredModel(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(default='', max_length=120)


class ProviderImport(ProviderTarget):
    models: list[DiscoveredModel] = Field(min_length=1, max_length=200)


class ModelState(BaseModel):
    config_id: str
    revision: int
    enabled: bool


class ModelDefault(BaseModel):
    config_id: str


class ModelTest(ModelDefault):
    revision: int


class ModelSelection(BaseModel):
    model: str
    model_revision: int = Field(ge=0)


def create_model_router(catalog, db):
    router = APIRouter()

    def identity(request):
        require_service(request)
        uid = request.headers.get('X-User-ID', '')
        if not uid:
            raise ControlError('MISSING_USER_IDENTITY', 401)
        return uid

    async def session(uid, sid):
        from agno.os.middleware.user_scope import session_matches_component
        record = await asyncio.to_thread(db.get_session, session_id=sid, user_id=uid)
        if not record or record.user_id != uid or not session_matches_component(record, 'agents', 'igraph-agent'):
            raise ControlError('SESSION_NOT_FOUND', 404)
        return record

    @router.get('/internal/model-providers')
    async def provider_listing(request: Request):
        return await asyncio.to_thread(catalog.list_providers, identity(request))

    @router.post('/internal/model-providers')
    async def provider_save(req: ProviderWrite, request: Request):
        row = await asyncio.to_thread(catalog.save_provider, identity(request),
            req.model_dump(exclude={'provider_id', 'revision'}), req.provider_id, req.revision)
        return {'success': True, 'provider': row}

    @router.post('/internal/model-providers/discover')
    async def provider_discover(req: ProviderTarget, request: Request):
        from agent_runtime.provider_probe import discover_models
        uid = identity(request)
        data = await asyncio.to_thread(catalog.provider_configuration, uid, req.provider_id, req.revision)
        result = await discover_models(data)
        # Do not return stale discovery results after another page edits the key.
        await asyncio.to_thread(catalog.provider_configuration, uid, req.provider_id, req.revision)
        return {**result, 'provider_id': req.provider_id, 'revision': req.revision}

    @router.post('/internal/model-providers/import')
    async def provider_import(req: ProviderImport, request: Request):
        return await asyncio.to_thread(catalog.import_provider_models, identity(request),
            req.provider_id, req.revision, [m.model_dump() for m in req.models])

    @router.delete('/internal/model-providers/{provider_id}')
    async def provider_delete(provider_id: str, revision: int, request: Request):
        await asyncio.to_thread(catalog.delete_provider, identity(request), provider_id, revision)
        return {'success': True}

    @router.post('/internal/model-catalog/bootstrap')
    async def bootstrap(req: BootstrapModels, request: Request):
        return await asyncio.to_thread(catalog.bootstrap, identity(request), req.allowed, req.legacy)

    @router.get('/internal/model-catalog')
    async def listing(request: Request):
        return await asyncio.to_thread(catalog.list, identity(request))

    @router.post('/internal/model-catalog')
    async def save(req: ModelWrite, request: Request):
        model = await asyncio.to_thread(catalog.save, identity(request), req.model_dump(exclude={'config_id', 'revision'}), req.config_id, req.revision)
        return {'success': True, 'model': model}

    @router.patch('/internal/model-catalog/state')
    async def state(req: ModelState, request: Request):
        model = await asyncio.to_thread(catalog.set_enabled, identity(request), req.config_id, req.enabled, req.revision)
        return {'success': True, 'model': model}

    @router.put('/internal/model-catalog/default')
    async def default(req: ModelDefault, request: Request):
        return await asyncio.to_thread(catalog.set_default, identity(request), req.config_id)

    @router.post('/internal/model-catalog/test')
    async def test(req: ModelTest, request: Request):
        data = await asyncio.to_thread(catalog.test_configuration, identity(request), req.config_id, req.revision)
        headers = {'Authorization': 'Bearer ' + data['api_key']} if data.get('api_key') else {}
        body = {'model': data['model_id'], 'messages': [{'role': 'user', 'content': 'Reply OK.'}],
                'max_tokens': 16, 'stream': False}
        if data.get('tools'):
            body['tools'] = [{'type': 'function', 'function': {'name': 'connection_test',
                'description': 'A harmless connectivity check; never executed.',
                'parameters': {'type': 'object', 'properties': {}}}}]
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False) as client:
                response = await client.post(data['base_url'].rstrip('/') + '/chat/completions', headers=headers, json=body)
            if response.status_code in {401, 403}:
                raise ControlError('MODEL_AUTH_FAILED', 422)
            if response.status_code == 429:
                raise ControlError('MODEL_RATE_LIMITED', 422)
            if response.status_code >= 300:
                raise ControlError('MODEL_TEST_FAILED', 422)
            payload = response.json()
            if not isinstance(payload, dict) or not payload.get('choices'):
                raise ControlError('MODEL_INVALID_RESPONSE', 422)
        except (httpx.HTTPError, ValueError):
            raise ControlError('MODEL_CONNECTION_FAILED', 422) from None
        return {'success': True, 'config_id': req.config_id, 'revision': req.revision}

    @router.delete('/internal/model-catalog/user/all')
    async def purge_user(request: Request):
        await asyncio.to_thread(catalog.purge_user, identity(request))
        return {'success': True}

    @router.delete('/internal/model-catalog/{config_id:path}')
    async def delete(config_id: str, revision: int, request: Request):
        await asyncio.to_thread(catalog.delete, identity(request), config_id, revision)
        return {'success': True}

    @router.get('/internal/sessions/{sid}/model-selection')
    async def selection(sid: str, request: Request):
        uid = identity(request)
        record = await session(uid, sid)
        return await asyncio.to_thread(catalog.selection, uid, sid, (record.session_data or {}).get('session_state', {}).get('model'))

    @router.get('/internal/sessions/{sid}/model-provenance')
    async def provenance(sid: str, request: Request):
        record = await session(identity(request), sid)
        # Agno's public RunSchema omits metadata. Expose only these non-secret
        # audit fields so refresh/replay retains the actual model per answer.
        return {'runs': {run.run_id: {key: (run.metadata or {}).get(key)
                for key in ('model_snapshot', 'history_compacted')}
                for run in record.runs or []}}

    @router.get('/internal/sessions/{sid}/runs/{rid}/model-config')
    async def run_model(sid: str, rid: str, request: Request):
        uid = identity(request)
        await session(uid, sid)
        run = await asyncio.to_thread(catalog.store.get, uid, sid, run_id=rid)
        if not run or not run['admission_blocked']:
            raise ControlError('MODEL_RUN_NOT_ACTIVE', 409)
        snapshot = run['state'].get('model_snapshot')
        if not snapshot:
            raise ControlError('MODEL_SNAPSHOT_MISSING', 409)
        # Credentials only cross the authenticated server-to-server boundary.
        return await asyncio.to_thread(catalog.resolve, uid, snapshot)

    @router.get('/internal/sessions/{sid}/model-config')
    async def session_ingestion_model(sid: str, request: Request):
        uid = identity(request)
        record = await session(uid, sid)
        def resolve_selection():
            with catalog.store.transaction(uid, sid) as conn:
                snapshot = catalog.freeze(conn, uid, sid, (record.session_data or {}).get('session_state', {}).get('model'))
            return catalog.resolve(uid, snapshot)
        return await asyncio.to_thread(resolve_selection)

    @router.patch('/internal/sessions/{sid}/model-selection')
    async def choose(sid: str, req: ModelSelection, request: Request):
        uid = identity(request)
        record = await session(uid, sid)
        if any(str(getattr(run.status, 'value', run.status)).lower() not in TERMINAL for run in record.runs or []):
            raise ControlError('RUN_ACTIVE')
        return await asyncio.to_thread(catalog.select, uid, sid, req.model, req.model_revision,
            (record.session_data or {}).get('session_state', {}).get('model'))

    return router

"""Internal connection API. Authenticate before parsing credential payloads."""
import json

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from execution_lifecycle import owned_sync
from execution_protocol import require_service
from agent_runtime.execution_store import ControlError
from .contracts import ConnectionWrite


class SelectionWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    connection_id: str | None


class RemoteGraphSessions:
    """Session access when the optional execution controller is disabled."""
    def __init__(self, db):
        self.db = db

    async def call(self, method, *args, **kwargs):
        return await owned_sync(method, *args, **kwargs)

    async def owned_session(self, uid, sid):
        from agno.os.middleware.user_scope import session_matches_component
        record = await self.call(self.db.get_session, session_id=sid, user_id=uid)
        if not record or record.user_id != uid or not session_matches_component(record, 'agents', 'igraph-agent'):
            raise ControlError('SESSION_NOT_FOUND', 404)
        return record


def create_remote_graph_router(catalog, controller, executor) -> APIRouter:
    router = APIRouter()

    def identity(request):
        require_service(request)
        uid = request.headers.get('X-User-ID', '').strip()
        if not uid:
            raise ControlError('MISSING_USER_IDENTITY', 401)
        return uid

    async def parse(request, model):
        try:
            return model.model_validate(await request.json())
        except (ValidationError, json.JSONDecodeError, UnicodeError):
            # FastAPI's default validation error can echo the original password.
            raise ControlError('REMOTE_GRAPH_INVALID_CONFIG', 422) from None

    async def test(uid, body):
        if executor is None:
            raise ControlError('REMOTE_GRAPH_UNREACHABLE', 503)
        return await executor.test_connection(uid, body)

    @router.get('/internal/remote-graphs')
    async def listing(request: Request):
        items = await controller.call(catalog.list, identity(request))
        return {'connections': [item.model_dump() for item in items]}

    @router.post('/internal/remote-graphs')
    async def save(request: Request):
        uid = identity(request)
        body = await parse(request, ConnectionWrite)
        await test(uid, body)
        # The catalog checks revision again after network I/O.
        item = await controller.call(catalog.save, uid, body)
        return {'connection': item.model_dump()}

    @router.post('/internal/remote-graphs/test')
    async def test_form(request: Request):
        uid = identity(request)
        return await test(uid, await parse(request, ConnectionWrite))

    @router.delete('/internal/remote-graphs/{connection_id}')
    async def delete(connection_id: str, revision: int, request: Request):
        await controller.call(catalog.delete, identity(request), connection_id, revision)
        return {'success': True}

    @router.get('/internal/sessions/{sid}/remote-graph')
    async def selection(sid: str, request: Request):
        uid = identity(request)
        await controller.owned_session(uid, sid)
        return await controller.call(catalog.selection, uid, sid)

    @router.put('/internal/sessions/{sid}/remote-graph')
    async def select(sid: str, request: Request):
        uid = identity(request)
        await controller.owned_session(uid, sid)
        body = await parse(request, SelectionWrite)
        binding = await controller.call(catalog.select, uid, sid, body.connection_id)
        return {'remote_graph': binding.model_dump() if binding else None}

    return router

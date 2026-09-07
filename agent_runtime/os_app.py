"""Standalone AgentOS composition root for iGraph."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import asyncio
from agno.os import AgentOS
from pydantic import BaseModel, Field

from agent_runtime.agno_factory import create_db, create_igraph_factory
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.custom_models import register_custom_model, remove_custom_model
from agent_runtime.skill_api import create_skill_router
from agent_runtime.recovery import create_recovery_router
from agent_runtime.execution_control import ExecutionController, create_control_router
from agent_runtime.execution_store import ExecutionStore, ControlError
from agent_runtime.execution_store import TERMINAL
from agent_runtime.execution_scope import RunCancellationManager
from agno.run.cancel import set_cancellation_manager
from execution_protocol import controls_enabled, require_service


class RuntimeCustomModelReq(BaseModel):
    user_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    provider: str = "openai-compatible"


def create_app(config: AgentRuntimeConfig | None = None) -> FastAPI:
    settings = config or AgentRuntimeConfig.from_env()
    db = create_db(settings)
    controller = None
    if controls_enabled():
        manager = RunCancellationManager()
        set_cancellation_manager(manager)
        controller = ExecutionController(db, ExecutionStore(db.db_engine), manager)

    @asynccontextmanager
    async def lifespan(_app):
        if controller:
            await asyncio.to_thread(controller.store.migrate)
            await controller.restore()
        try:
            yield
        finally:
            if controller:
                await controller.close()

    base_app = FastAPI(title="iGraph Agent Runtime", version="3.0.0", lifespan=lifespan)
    if controller:
        base_app.include_router(create_control_router(controller))

    @base_app.exception_handler(ControlError)
    async def control_error(_request, exc):
        return JSONResponse({"detail": {"code": exc.code}}, status_code=exc.status,
                            headers={"Retry-After": "2"} if exc.status == 429 else None)
    base_app.include_router(create_skill_router(settings))
    base_app.include_router(create_recovery_router(db))

    @base_app.get("/internal/health")
    def internal_health():
        return {
            "status": "ok",
            "module": "agent-runtime",
            "runtime": "AgentOS",
            "agent_id": "igraph-agent",
        }

    @base_app.put("/internal/models/custom")
    def configure_custom_model(req: RuntimeCustomModelReq):
        register_custom_model(req.user_id, req.model_dump())
        return {"success": True, "model_id": req.model_id}

    @base_app.delete("/internal/models/custom/{user_id}/{model_id:path}")
    def forget_custom_model(user_id: str, model_id: str):
        remove_custom_model(user_id, model_id)
        return {"success": True, "model_id": model_id}

    agent_os = AgentOS(
        id="igraph-agent-os",
        name="iGraph Agent Runtime",
        description="AgentOS runtime for iGraph sessions, memory, learning and skills",
        agents=[create_igraph_factory(settings, db, controller)],
        db=db,
        base_app=base_app,
        on_route_conflict="preserve_agentos",
        authorization=settings.authorization,
        tracing=settings.tracing,
        telemetry=settings.telemetry,
    )
    app = agent_os.get_app()
    app.state.execution_controller = controller
    if controller:
        @app.middleware('http')
        async def protect_native_execution(request: Request, call_next):
            creating = None
            path = request.url.path.removeprefix(request.scope.get('root_path', '')).strip('/').split('/')
            if request.method == 'POST' and path == ['sessions']:
                uid = str(getattr(request.state, 'user_id', '') or request.headers.get('X-User-ID', ''))
                try:
                    body = await request.json()
                except ValueError:
                    body = None  # Keep native request validation for malformed JSON.
                if uid and isinstance(body, dict) and isinstance(body.get('session_id'), str):
                    pid = (body.get('session_state') or {}).get('project_id')
                    try:
                        require_service(request)
                        if body.get('user_id') and body['user_id'] != uid:
                            raise ControlError('EXECUTION_OWNER_MISMATCH', 403)
                        await controller.call(controller.store.register_project_session, uid, pid, body['session_id'])
                    except ControlError as exc:
                        return JSONResponse({'detail': {'code': exc.code}}, status_code=exc.status)
                    except HTTPException as exc:
                        return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
                    deletion = await controller.call(controller.store.session_deletion, uid, body['session_id'])
                    if deletion:
                        return JSONResponse({'detail': {'code': 'SESSION_DELETING'}}, status_code=409)
                    try:
                        await controller.call(controller.store.native_creation, uid, body['session_id'], started=True)
                        creating = (uid, body['session_id'])
                    except ControlError as exc:
                        return JSONResponse({'detail': {'code': exc.code}}, status_code=exc.status)
            if request.method == 'GET' and len(path) >= 2 and path[0] == 'sessions':
                uid = str(getattr(request.state, 'user_id', '') or request.headers.get('X-User-ID', ''))
                if uid:
                    deletion = await controller.call(controller.store.session_deletion, uid, path[1])
                    if deletion and deletion['state'].get('delete_status'):
                        return JSONResponse({'detail': {'code': 'SESSION_NOT_FOUND'}}, status_code=404)
            if request.method == 'DELETE' and path[0] == 'sessions':
                try:
                    require_service(request)
                    if len(path) != 2:
                        raise ControlError('USE_SCOPED_SESSION_DELETE')
                    uid = str(getattr(request.state, 'user_id', '') or request.headers.get('X-User-ID', ''))
                    session = await controller.owned_session(uid, path[1])
                    if any(str(getattr(run.status, 'value', run.status)).lower() not in TERMINAL for run in session.runs or []):
                        raise ControlError('SESSION_EXECUTION_ACTIVE')
                    await controller.call(controller.store.seal_session, uid, path[1])
                except ControlError as exc:
                    return JSONResponse({'detail': {'code': exc.code}}, status_code=exc.status)
                except HTTPException as exc:
                    return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
            if request.method == 'POST' and len(path) >= 3 and path[0] == 'agents' and path[2] == 'runs':
                try:
                    require_service(request)
                    if path[-1] == 'cancel':
                        raise ControlError('USE_DURABLE_CANCEL_ENDPOINT')
                    await request.body()
                    form = await request.form()
                    uid = str(getattr(request.state, 'user_id', '') or request.headers.get('X-User-ID', ''))
                    if form.get('user_id') and str(form['user_id']) != uid:
                        raise HTTPException(403, {'code': 'EXECUTION_OWNER_MISMATCH'})
                    sid = str(form.get('session_id') or '')
                    await controller.owned_session(uid, sid)
                    rid = str(form.get('run_id') or '') if len(path) == 3 else path[3]
                    row = await controller.call(controller.store.get, uid, sid, run_id=rid)
                    if row is None:
                        raise ControlError('RUN_NOT_REGISTERED', 404)
                    if row['state']['cancel_status'] != 'none':
                        raise ControlError('RUN_CANCELLED')
                except ControlError as exc:
                    return JSONResponse({'detail': {'code': exc.code}}, status_code=exc.status)
                except HTTPException as exc:
                    return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
            try:
                return await call_next(request)
            finally:
                if creating:
                    await asyncio.shield(controller.call(controller.store.native_creation, *creating, started=False))
    return app


app = create_app()

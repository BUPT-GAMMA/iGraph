"""Agno 2.9.0 compatibility boundary. Never resolve a business AgentFactory.

Only original, in-memory indices belong to the delta protocol. In particular,
the native generator's DB fallback renumbers events, so it is disabled here.
Changes to these private imports must pass test_recovery_runtime.py.
"""
from __future__ import annotations

import json
import logging
from contextlib import aclosing

from agno.agent import Agent
from agno.db.base import AsyncBaseDb
from agno.os.managers import event_buffer
from agno.os.middleware.user_scope import session_matches_component, run_matches_component
from agno.os.routers.agents.router import _resume_stream_generator
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)


async def owned_run(db, session_id: str, run_id: str, user_id: str, agent_id: str):
    """Explicit ownership checks also apply with AgentOS isolation disabled."""
    if not user_id:
        raise HTTPException(401, "missing user identity")
    if db is None:
        raise HTTPException(404, "Run not found")
    if isinstance(db, AsyncBaseDb):
        session = await db.get_session(session_id=session_id, user_id=user_id)
    else:
        session = await run_in_threadpool(db.get_session, session_id=session_id, user_id=user_id)
    if (session is None or session.user_id != user_id
            or not session_matches_component(session, "agents", agent_id)):
        raise HTTPException(404, "Run not found")
    run = session.get_run(run_id=run_id)
    if run is None or (run.user_id is not None and run.user_id != user_id) or not run_matches_component(run, "agents", agent_id):
        raise HTTPException(404, "Run not found")
    return run


def replay_unavailable(reason: str):
    logger.info("stream_recovery_required reason=%s", reason)
    return HTTPException(409, {"code": "REPLAY_UNAVAILABLE", "reason": reason,
                               "message": "需要同步已保存的运行结果"})


def validate_cursor(run_id: str, cursor: int):
    if cursor < -1:
        raise replay_unavailable("invalid_cursor")
    if event_buffer.get_run_status(run_id) is None:
        raise replay_unavailable("buffer_missing")
    last = event_buffer.get_last_index(run_id)
    first = last - event_buffer.get_event_count(run_id) + 1
    if cursor > last:
        raise replay_unavailable("invalid_cursor")
    if cursor < first - 1:
        raise replay_unavailable("window_trimmed")


def recovery_frame(run_id: str, reason: str):
    return 'data: ' + json.dumps({"event": "stream_recovery_required", "run_id": run_id,
                                  "code": "REPLAY_UNAVAILABLE", "reason": reason}) + '\n\n'


async def replay(db, agent_id: str, run_id: str, cursor: int, user_id: str):
    reader = Agent(id=agent_id, db=db, telemetry=False)
    # session_id=None deliberately disables native database event replay.
    generator = _resume_stream_generator(reader, run_id, cursor, None, user_id=user_id)
    try:
        async with aclosing(generator):
            async for frame in generator:
                payloads = [json.loads(line[5:]) for line in frame.splitlines() if line.startswith('data:')]
                for payload in payloads:
                    index = payload.get('event_index')
                    if index is not None:
                        if not isinstance(index, int) or index != cursor + 1:
                            yield recovery_frame(run_id, "window_trimmed")
                            return
                        cursor = index
                    elif payload.get('event') == 'error':
                        yield recovery_frame(run_id, "buffer_missing")
                        return
                yield frame
            if event_buffer.get_run_status(run_id) is None:
                yield recovery_frame(run_id, "buffer_missing")
    except Exception:
        logger.warning("stream_recovery_transport_error", exc_info=False)
        yield recovery_frame(run_id, "transport_error")


def create_recovery_router(db, agent_id: str = "igraph-agent") -> APIRouter:
    router = APIRouter()

    @router.get('/internal/sessions/{session_id}/runs/{run_id}/recovery-status')
    async def recovery_status(session_id: str, run_id: str, request: Request):
        user_id = str(getattr(request.state, 'user_id', '') or request.headers.get('X-User-ID', '')).strip()
        run = await owned_run(db, session_id, run_id, user_id, agent_id)
        status = event_buffer.get_run_status(run_id)
        phase = status or run.status
        return {'run_id': run_id, 'status': str(getattr(phase, 'value', phase)).lower()}

    @router.post('/internal/sessions/{session_id}/runs/{run_id}/resume')
    async def resume(session_id: str, run_id: str, request: Request, last_event_index: int = Form(-1)):
        # JWT identity takes precedence over the backend-forwarded header.
        user_id = str(getattr(request.state, 'user_id', '') or request.headers.get('X-User-ID', '')).strip()
        await owned_run(db, session_id, run_id, user_id, agent_id)
        validate_cursor(run_id, last_event_index)
        return StreamingResponse(replay(db, agent_id, run_id, last_event_index, user_id),
                                 media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    return router

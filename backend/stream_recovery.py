"""Observation-only stream lifetime and heartbeat helpers."""
from __future__ import annotations

import asyncio
import logging
import anyio
from contextlib import suppress
import json
from fastapi import HTTPException

logger = logging.getLogger(__name__)


class StartedNativeStream:
    def __init__(self, lines, iterator):
        self.lines, self.iterator = lines, iterator

    async def aiter_lines(self):
        for line in self.lines:
            yield line
        async for line in self.iterator:
            yield line


async def wait_for_native_start(response, *, run_id: str):
    """Keep the owned handoff alive until Agno starts its detached producer.

    HTTP headers arrive before the streaming generator claims the Run. Closing
    at that point cancels startup, leaving a registered Run preparing forever.
    Preserve the first event for the observer instead of consuming it twice.
    """
    iterator = response.aiter_lines()
    lines, data = [], []
    try:
        async with asyncio.timeout(60):
            async for line in iterator:
                lines.append(line)
                if line.startswith('data:'):
                    data.append(line[5:].lstrip())
                elif not line and data:
                    event = json.loads('\n'.join(data))
                    if event.get('event') == 'RunStarted' and event.get('run_id') == run_id:
                        return StartedNativeStream(lines, iterator)
                    break
    except (TimeoutError, ValueError):
        pass
    raise HTTPException(502, {'code': 'NATIVE_RUN_START_FAILED', 'message': '模型任务未能启动，请重试'})


async def close_observation(stack):
    with anyio.CancelScope(shield=True):
        try:
            await stack.aclose()
        except Exception:
            logger.warning('stream_cleanup_error', exc_info=False)
            raise


async def with_heartbeats(source, interval: float = 30):
    """Keep one pending read across heartbeats; only cancel it on disconnect."""
    iterator = source.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            ready, _ = await asyncio.wait({pending}, timeout=interval)
            if not ready:
                yield ': heartbeat\n\n'
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield item
    finally:
        with anyio.CancelScope(shield=True):
            if pending is not None:
                pending.cancel()
                with suppress(asyncio.CancelledError, StopAsyncIteration):
                    await pending
            if hasattr(iterator, 'aclose'):
                await iterator.aclose()


async def observation_events(response, *, protocol_version, session_id, run_id=''):
    from backend.event_mapper import translate_sse
    import json

    try:
        async for chunk in with_heartbeats(translate_sse(response.aiter_lines(), protocol_version=protocol_version,
                                                        session_id=session_id, run_id=run_id)):
            yield chunk
    except Exception:
        logger.warning('stream_transport_error', exc_info=False)
        event = {'type': 'stream_recovery_required', 'reason': 'transport_error', 'run_id': run_id}
        payload = {'protocol_version': 2, 'session_id': session_id, 'run_id': run_id,
                   'event_index': None, 'events': [event]} if protocol_version == 2 else event
        yield 'data: ' + json.dumps(payload) + '\n\n'

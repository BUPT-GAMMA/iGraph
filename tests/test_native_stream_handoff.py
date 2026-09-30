import asyncio
import json

import pytest
from fastapi import HTTPException

from backend.stream_recovery import wait_for_native_start


class Response:
    def __init__(self, frames):
        self.frames = frames

    async def aiter_lines(self):
        for line in self.frames:
            yield line


def test_handoff_preserves_first_event_and_remaining_stream():
    async def scenario():
        lines = [': heartbeat', '', 'event: RunStarted',
                 'data: ' + json.dumps({'event': 'RunStarted', 'run_id': 'r'}), '',
                 'data: ' + json.dumps({'event': 'RunContent', 'content': '回答'}), '']
        stream = await wait_for_native_start(Response(lines), run_id='r')
        assert [line async for line in stream.aiter_lines()] == lines
    asyncio.run(scenario())


@pytest.mark.parametrize('frames', [[], ['data: {"event":"RunError","content":"startup failed"}', ''],
    ['data: {"event":"RunStarted","run_id":"different"}', '']])
def test_handoff_rejects_missing_or_failed_native_start(frames):
    async def scenario():
        with pytest.raises(HTTPException) as error:
            await wait_for_native_start(Response(frames), run_id='r')
        assert error.value.status_code == 502
        assert error.value.detail['code'] == 'NATIVE_RUN_START_FAILED'
    asyncio.run(scenario())

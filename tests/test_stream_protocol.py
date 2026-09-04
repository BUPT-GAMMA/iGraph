from __future__ import annotations

import asyncio
import json

from backend.app import _active_run_summary, _messages_from_runs
from backend.event_mapper import map_agentos_event, map_product_events, translate_sse


async def _lines(*values: str):
    for value in values:
        yield value


async def _collect(*values: str, protocol_version: int = 2):
    return [
        chunk
        async for chunk in translate_sse(
            _lines(*values),
            protocol_version=protocol_version,
            session_id="session-a",
        )
    ]


def _decode(chunk: str) -> dict:
    assert chunk.startswith("data: ") and chunk.endswith("\n\n")
    return json.loads(chunk[6:-2])


def test_v2_emits_one_atomic_frame_per_agentos_event() -> None:
    source = {
        "event": "ReasoningStep",
        "session_id": "session-a",
        "run_id": "run-123",
        "event_index": 42,
        "reasoning_step": {"title": "Inspect graph", "result": "done"},
    }
    chunks = asyncio.run(_collect(f"data: {json.dumps(source)}"))

    assert len(chunks) == 1
    envelope = _decode(chunks[0])
    assert envelope["protocol_version"] == 2
    assert envelope["session_id"] == "session-a"
    assert envelope["run_id"] == "run-123"
    assert envelope["event_index"] == 42
    assert [event["type"] for event in envelope["events"]] == ["thinking", "plan_analyzing", "plan_step"]


def test_v1_keeps_legacy_single_event_frames() -> None:
    source = {
        "event": "ReasoningStep",
        "run_id": "run-123",
        "event_index": 1,
        "content": "step",
    }
    chunks = asyncio.run(_collect(f"data: {json.dumps(source)}", protocol_version=1))
    assert [_decode(chunk)["type"] for chunk in chunks] == ["thinking", "plan_analyzing", "plan_step"]


def test_message_events_have_stable_message_id() -> None:
    token = map_agentos_event({"event": "RunContent", "run_id": "run-7", "content": "x"})
    done = map_agentos_event({"event": "RunCompleted", "run_id": "run-7", "content": "x"})
    assert token["message_id"] == "assistant:run-7"
    assert done["message_id"] == "assistant:run-7"


def test_done_preserves_the_difference_between_missing_and_empty_text():
    assert 'content' not in map_agentos_event({'event': 'RunCompleted', 'run_id': 'r'})
    assert map_agentos_event({'event': 'RunCompleted', 'content': ''})['content'] == ''


def test_empty_completed_snapshot_retains_stable_assistant_message():
    messages = _messages_from_runs([{'run_id': 'r', 'status': 'completed', 'content': ''}])
    assert messages[-1]['id'] == 'assistant:r'
    assert messages[-1]['content'] == ''


def test_graph_delta_and_snapshot_share_edges_and_stable_identity():
    tool = {'tool_name': 'visualize_subgraph', 'tool_call_id': 'graph-call',
            'result': {'nodes': [{'id': 'a'}], 'links': []}}
    events = map_product_events({'event': 'ToolCallCompleted', 'run_id': 'r', 'tool': tool})
    assert events[0]['data']['edges'] == []
    snapshot = _messages_from_runs([{'run_id': 'r', 'status': 'completed', 'tools': [tool]}])
    assert snapshot[0]['graphs'][0]['id'] == 'run-r-graph-call'
    assert snapshot[0]['graphs'][0]['edges'] == events[0]['data']['edges']


def test_preserves_heartbeats_and_connection_stable_reasoning_ids():
    assert asyncio.run(_collect(': heartbeat')) == [': heartbeat\n\n']
    first = _decode(asyncio.run(_collect('data: {"event":"ReasoningStep","event_index":20,"run_id":"r"}'))[0])
    second = _decode(asyncio.run(_collect('data: {"event":"ReasoningStep","event_index":21,"run_id":"r"}'))[0])
    assert first['events'][-1]['step']['step_id'] == 20
    assert second['events'][-1]['step']['step_id'] == 21


def test_active_run_summary_uses_latest_active_run_and_max_cursor() -> None:
    runs = [
        {"run_id": "done", "status": "COMPLETED", "created_at": 1},
        {
            "run_id": "active",
            "status": "PAUSED",
            "created_at": 2,
            "last_event_index": 8,
            "events": [{"event_index": 10}, {"event_index": "12"}],
        },
    ]
    assert _active_run_summary(runs) == {
        "run_id": "active",
        "status": "paused",
        "last_event_index": 12,
    }

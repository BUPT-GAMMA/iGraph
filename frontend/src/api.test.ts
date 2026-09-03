import { describe, expect, it, vi } from 'vitest';
import { ApiError, consumeSseResponse, parseGraphDataResponse } from './api';

function responseFromChunks(chunks: string[]) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

describe('SSE parser', () => {
  it('keeps structured run and feedback conflict details', () => {
    const error = ApiError.fromResponse(409, {
      detail: {
        code: 'RUN_AWAITING_FEEDBACK',
        message: 'wait',
        active_run: { run_id: 'run-1', status: 'paused', last_event_index: 4 },
        pending_feedback: { run_id: 'run-1', request_id: 'run-1', call_id: 'call-1' },
      },
    });
    expect(error.code).toBe('RUN_AWAITING_FEEDBACK');
    expect(error.activeRun?.run_id).toBe('run-1');
    expect(error.pendingFeedback?.call_id).toBe('call-1');
  });

  it('parses a v2 envelope split at arbitrary byte boundaries', async () => {
    const onBatch = vi.fn();
    const frame = 'data: {"protocol_version":2,"session_id":"A","run_id":"run-1","event_index":42,"events":[{"type":"token","message_id":"assistant:run-1","content":"好"}]}\n\n';
    await consumeSseResponse(responseFromChunks([frame.slice(0, 9), frame.slice(9, 37), frame.slice(37, 81), frame.slice(81)]), 'A', { onBatch });

    expect(onBatch).toHaveBeenCalledTimes(1);
    expect(onBatch.mock.calls[0][0]).toEqual(expect.objectContaining({ run_id: 'run-1', event_index: 42 }));
    expect(onBatch.mock.calls[0][0].events[0]).toEqual(expect.objectContaining({ content: '好' }));
  });

  it('does not dispatch an unterminated half frame', async () => {
    const onBatch = vi.fn();
    await consumeSseResponse(responseFromChunks(['data: {"protocol_version":2,"session_id":"A","run_id":"run-1","event_index":42']), 'A', { onBatch });
    expect(onBatch).not.toHaveBeenCalled();
  });

  it('wraps legacy single events without applying transport events as content', async () => {
    const onBatch = vi.fn();
    await consumeSseResponse(responseFromChunks([
      'data: {"type":"stream_status","status":"catch_up","run_id":"run-1","event_index":1}\n\n',
      'data: {"type":"token","content":"x","run_id":"run-1","event_index":2}\n\n',
    ]), 'A', { onBatch });

    expect(onBatch).toHaveBeenCalledTimes(2);
    expect(onBatch.mock.calls[0][0].protocol_version).toBe(1);
    expect(onBatch.mock.calls[0][0].events[0].type).toBe('stream_status');
    expect(onBatch.mock.calls[1][0].events[0].type).toBe('token');
  });
});

describe('graph response validation', () => {
  it('accepts only complete graph structures', () => {
    expect(parseGraphDataResponse({ success: true, nodes: [], edges: [] })).toEqual({ success: true, nodes: [], edges: [] });
  });

  it.each([
    { success: false, error: 'not loaded' },
    { success: true, edges: [] },
    { success: true, nodes: [] },
  ])('rejects malformed graph payload %#', payload => {
    expect(() => parseGraphDataResponse(payload)).toThrow(ApiError);
    try { parseGraphDataResponse(payload); } catch (cause) {
      expect((cause as ApiError).code).toBe('INVALID_GRAPH_RESPONSE');
    }
  });
});

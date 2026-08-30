import { describe, expect, it, vi } from 'vitest';
import { consumeSseResponse } from './api';

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

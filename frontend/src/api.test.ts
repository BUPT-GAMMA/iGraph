import { afterEach, describe, expect, it, vi } from 'vitest';
import { AUTH_ACCOUNT_CHANGED_EVENT, AUTH_EXPIRED_EVENT, ApiError, authenticateUser, bindRequestAccount, createSession, streamChat, uploadFile, uploadProjectDataset, consumeSseResponse, parseGraphDataResponse, USER_KEY, validateUser } from './api';

let unbindAccount = () => {};

afterEach(() => {
  unbindAccount();
  localStorage.clear();
  vi.unstubAllGlobals();
});

function responseFromChunks(chunks: string[]) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

describe('authentication headers', () => {
  it.each([
    ['session creation', () => createSession({ initialMessagePreview: 'private draft' })],
    ['chat', () => streamChat('A', 'private draft', [], {}, new AbortController().signal)],
    ['session upload', () => uploadFile('A', new File(['private'], 'private.csv'))],
    ['dataset upload', () => uploadProjectDataset(null, new File(['private'], 'private.csv'))],
  ])('blocks %s before storage synchronization can run', async (_name, send) => {
    unbindAccount = bindRequestAccount('account-a');
    localStorage.setItem(USER_KEY, 'account-b');
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);

    await expect(send()).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('blocks requests when another tab clears the account', async () => {
    unbindAccount = bindRequestAccount('account-a');
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    await expect(createSession()).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('uses a session cookie and pins the expected workspace account', async () => {
    localStorage.setItem(USER_KEY, 'account-b');
    unbindAccount = bindRequestAccount('account-b');
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ session_id: 'B' })));
    vi.stubGlobal('fetch', fetcher);
    await createSession({ initialMessagePreview: 'B draft' });
    const headers = new Headers(fetcher.mock.calls[0][1].headers);
    expect(headers.get('X-User-ID')).toBeNull();
    expect(headers.get('X-iGraph-Account')).toBe('account-b');
    expect(fetcher.mock.calls[0][1].credentials).toBe('include');
  });

  it('validates the session cookie without accepting a caller supplied user ID', async () => {
    localStorage.setItem(USER_KEY, 'cached-user');
    unbindAccount = bindRequestAccount('rendered-user');
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ valid: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetcher);

    await validateUser('next-user');

    expect(fetcher).toHaveBeenCalledOnce();
    const headers = new Headers(fetcher.mock.calls[0][1]?.headers);
    expect(headers.get('X-User-ID')).toBeNull();
    expect(headers.get('X-iGraph-Account')).toBeNull();
    expect(fetcher.mock.calls[0][1].credentials).toBe('include');
  });

  it('expires the workspace when cookie validation returns unauthorized', async () => {
    localStorage.setItem(USER_KEY, 'alice');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })));
    const expired = vi.fn(); window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    try {
      await expect(validateUser('alice')).rejects.toMatchObject({ status: 401 });
      expect(expired).toHaveBeenCalledOnce();
      expect(localStorage.getItem(USER_KEY)).toBe('');
    } finally { window.removeEventListener(AUTH_EXPIRED_EVENT, expired); }
  });

  it('does not globally expire a workspace for an incorrect login password', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })));
    const expired = vi.fn(); window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    try {
      await expect(authenticateUser('login', 'alice', 'wrong-password')).rejects.toMatchObject({ status: 401 });
      expect(expired).not.toHaveBeenCalled();
    } finally { window.removeEventListener(AUTH_EXPIRED_EVENT, expired); }
  });

  it('requests authoritative identity synchronization for cookie account races', async () => {
    localStorage.setItem(USER_KEY, 'alice'); unbindAccount = bindRequestAccount('alice');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code: 'ACCOUNT_CHANGED', message: 'Account changed' } }), { status: 409 })));
    const changed = vi.fn(); window.addEventListener(AUTH_ACCOUNT_CHANGED_EVENT, changed);
    try {
      await expect(createSession()).rejects.toMatchObject({ code: 'ACCOUNT_CHANGED' });
      expect(changed).toHaveBeenCalledOnce();
    } finally { window.removeEventListener(AUTH_ACCOUNT_CHANGED_EVENT, changed); }
  });
});

describe('SSE parser', () => {
  it('handles UTF-8 byte splits, CRLF across chunks, comments and multiline data', async () => {
    const bytes = new TextEncoder().encode(': heartbeat\r\ndata: {"type":"token",\r\ndata: "content":"中文🙂"}\r\n\r\n');
    const onBatch = vi.fn(), onActivity = vi.fn();
    const body = new ReadableStream({ start(controller) {
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
      controller.close();
    } });
    await consumeSseResponse(new Response(body), 'A', { onBatch, onActivity });
    expect(onBatch).toHaveBeenCalledTimes(1);
    expect(onBatch.mock.calls[0][0].events[0].content).toBe('中文🙂');
    expect(onActivity).toHaveBeenCalledTimes(bytes.length);
  });
  it('propagates JSON and business-dispatch errors instead of dropping a complete frame', async () => {
    await expect(consumeSseResponse(responseFromChunks(['data: invalid\n\n']), 'A', {})).rejects.toThrow();
    await expect(consumeSseResponse(responseFromChunks(['data: {"type":"token","content":"x"}\n\n']), 'A', {
      onBatch: () => { throw new Error('cannot apply'); },
    })).rejects.toThrow('cannot apply');
  });
  it('rejects partially valid V2 batches as a whole', async () => {
    const onBatch = vi.fn();
    await expect(consumeSseResponse(responseFromChunks(['data: {"protocol_version":2,"event_index":0,"events":[{"type":"token"},null]}\n\n']), 'A', { onBatch })).rejects.toThrow();
    expect(onBatch).not.toHaveBeenCalled();
  });
  it('formats FastAPI validation details instead of showing a generic 422', () => {
    const error = ApiError.fromResponse(422, {
      detail: [{ loc: ['body', 'name'], msg: 'String should have at most 80 characters' }],
    });

    expect(error.message).toBe('name: String should have at most 80 characters');
  });

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

  it('ignores SSE heartbeat comments while preserving the next event', async () => {
    const onBatch = vi.fn();
    await consumeSseResponse(responseFromChunks([
      ': keep-alive\n\n',
      'data: {"protocol_version":2,"session_id":"A","run_id":"run-1","event_index":3,"events":[{"type":"done"}]}\n\n',
    ]), 'A', { onBatch });

    expect(onBatch).toHaveBeenCalledTimes(1);
    expect(onBatch.mock.calls[0][0].events[0].type).toBe('done');
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

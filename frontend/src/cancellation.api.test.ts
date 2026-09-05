import { afterEach, expect, it, vi } from 'vitest';
import { activateSession, ApiError, cancelJob, cancelRun, getCapabilities, getJobControl,
  getRunControl, getRunSnapshot, resumeRun, setUserId, streamChat, streamFeedback } from './api';

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });

it('preserves stop identities, request cancellation and authoritative control on the HTTP boundary', async () => {
  const signal = new AbortController().signal;
  const pending = { run_id: 'r', start_request_id: 'start', registration_status: 'registered',
    can_send: false, execution_stopped: false, terminal_persisted: false, revision: 4, cancel_status: 'stopping' };
  const fetcher = vi.fn().mockImplementation(async () => new Response(JSON.stringify(pending), { status: 202 }));
  vi.stubGlobal('fetch', fetcher);
  setUserId('alice');
  for (const target of [{ run_id: 'r' }, { start_request_id: 'start' }]) {
    expect(await cancelRun('s/1', target, 'stable-stop', signal)).toEqual(pending);
    const [path, request] = fetcher.mock.calls[fetcher.mock.calls.length - 1];
    expect(path).toBe('/api/sessions/s%2F1/runs/cancel');
    expect(request.signal).toBe(signal);
    expect(request.headers.get('X-User-ID')).toBe('alice');
    expect(JSON.parse(request.body)).toEqual({ target, cancel_request_id: 'stable-stop' });
    expect(await getRunControl('s/1', target, signal)).toEqual(pending);
    expect(fetcher.mock.calls[fetcher.mock.calls.length - 1][1].method).toBeUndefined();
  }
  await cancelJob('s/1', 'j/1', 'stable-job-stop', signal);
  expect(fetcher.mock.calls[fetcher.mock.calls.length - 1][0]).toBe('/api/sessions/s%2F1/jobs/j%2F1/cancel');
  expect(JSON.parse(fetcher.mock.calls[fetcher.mock.calls.length - 1][1].body)).toEqual({ cancel_request_id: 'stable-job-stop' });
  await getJobControl('s/1', 'j/1', signal);
  expect(fetcher.mock.calls[fetcher.mock.calls.length - 1][0]).toBe('/api/sessions/s%2F1/jobs/j%2F1/control');
  await getRunSnapshot('s/1', 'r/1', signal);
  expect(fetcher.mock.calls[fetcher.mock.calls.length - 1][0]).toBe('/api/sessions/s%2F1/runs/r%2F1/snapshot');
  fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ control: pending, active_run: null })));
  expect((await activateSession('s/1', signal)).control).toEqual(pending);
  fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ run_cancel_v1: false, job_cancel_v1: false })));
  expect((await getCapabilities(signal)).run_cancel_v1).toBe(false);
  fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: 'CONTROL_RATE_LIMITED' } }), { status: 429, headers: { 'Retry-After': '3' } }));
  await expect(cancelRun('s', { run_id: 'r' }, 'stable-stop', signal)).rejects.toMatchObject({ status: 429, code: 'CONTROL_RATE_LIMITED', retryAfterMs: 3000 });
  fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: 'RUN_CANCELLED' } }), { status: 409 }));
  await expect(cancelRun('s', { run_id: 'r' }, 'stable-stop', signal)).rejects.toBeInstanceOf(ApiError);
});

it('keeps launch, approval and reconnection wire identities separate', async () => {
  const fetcher = vi.fn().mockImplementation(async () => new Response('data: {"type":"token","content":"A"}\n\n', { headers: { 'Content-Type': 'text/event-stream' } }));
  vi.stubGlobal('fetch', fetcher);
  const controller = new AbortController(), onBatch = vi.fn();
  await streamChat('s', 'question', [], { onBatch }, controller.signal, 'stable-start');
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toMatchObject({ message: 'question', start_request_id: 'stable-start', background: true });
  await streamFeedback('s', { run_id: 'r', request_id: 'r', call_id: 'tool' }, 'approve', 'stable-decision', { onBatch }, controller.signal);
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toMatchObject({ run_id: 'r', tool_call_id: 'tool', decision_id: 'stable-decision' });
  await resumeRun('s', 'r', 12, { onBatch }, controller.signal);
  expect(fetcher.mock.calls[2][0]).toContain('/runs/r/resume?last_event_index=12');
  expect(fetcher.mock.calls[2][1].body).toBeUndefined();
  expect(onBatch).toHaveBeenCalledTimes(3);
  expect(fetcher.mock.calls.every(([, request]) => request.signal === controller.signal)).toBe(true);
});

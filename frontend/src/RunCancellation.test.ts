import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, cancelJob, cancelRun, getJobControl, getRunControl, getRunSnapshot, type RunControl } from './api';
import { RunCancellation } from './RunCancellation';
import { RunConnectionManager } from './RunConnectionManager';
import { conversationReducer, createSessionView, initialConversationState, selectMessages, selectRunGate } from './sessionState';
import type { ConversationAction, ConversationState } from './sessionState';

vi.mock('./api', async load => ({ ...await load<typeof import('./api')>(),
  cancelRun: vi.fn(), getRunControl: vi.fn(), getRunSnapshot: vi.fn(), cancelJob: vi.fn(), getJobControl: vi.fn() }));
const managers: RunCancellation[] = [];
const gen = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const rid = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const control = (patch: Partial<RunControl> = {}): RunControl => ({ run_id: rid, start_request_id: gen,
  registration_status: 'registered', run_status: 'cancelled', cancel_status: 'confirmed', execution_stopped: true,
  terminal_persisted: true, pending_components: [], revision: 3, active_run: null, can_send: true, ...patch });

function setup() {
  let identity = 'alice';
  let state: ConversationState = { ...initialConversationState, viewsBySessionId: { s: { ...createSessionView('s', 'm'), hydration: 'ready' as const } } };
  const dispatch = (action: ConversationAction) => { state = conversationReducer(state, action); };
  dispatch({ type: 'START_RUN', sessionId: 's', generation: gen,
    userMessage: { id: `user:${gen}`, role: 'user', content: 'question' },
    assistantMessage: { id: `assistant:${gen}`, role: 'assistant', content: 'A' } });
  const manager = new RunConnectionManager(1);
  const settled = vi.fn();
  const cancellation = new RunCancellation({ manager, identity: () => identity,
    view: sid => state.viewsBySessionId[sid], dispatch, settled });
  managers.push(cancellation);
  return { manager, cancellation, settled, dispatch, view: () => state.viewsBySessionId.s, identity: (uid: string) => { identity = uid; } };
}

beforeEach(() => { vi.useFakeTimers(); vi.resetAllMocks(); localStorage.clear(); vi.spyOn(Math, 'random').mockReturnValue(.5); });
afterEach(() => { managers.splice(0).forEach(manager => manager.reset()); vi.useRealTimers(); vi.restoreAllMocks(); });

describe('durable cancellation scheduler', () => {
  it('rejects an older control snapshot and keeps cancelled terminal through native done', () => {
    const s = setup();
    s.dispatch({ type: 'SET_RUN_ID', sessionId: 's', generation: gen, runId: rid });
    s.dispatch({ type: 'APPLY_CONTROL', sessionId: 's', generation: gen, control: control({ revision: 8 }) });
    const snapshot = { session_id: 's', run_id: rid, status: 'completed', snapshot_complete: true,
      active_run: null, pending_feedback: null, resume_cursor: null, messages: [{ id: `assistant:${rid}`, run_id: rid, role: 'assistant' as const, content: 'late complete' }] };
    s.dispatch({ type: 'APPLY_SNAPSHOT', sessionId: 's', generation: gen, snapshot: { ...snapshot, control: control({ revision: 7, run_status: 'completed' }) } });
    expect(selectMessages(s.view()).some(message => message.content === 'late complete')).toBe(false);
    s.dispatch({ type: 'APPLY_SNAPSHOT', sessionId: 's', generation: gen, snapshot: { ...snapshot, control: control({ revision: 9, run_status: 'completed' }) } });
    expect(s.view().activeRun?.status).toBe('cancelled');
    expect(selectMessages(s.view()).some(message => message.content === 'late complete')).toBe(true);
  });
  it.each(['snapshot', 'hydrate'])('uses remote control from %s without creating a local stop intent', async source => {
    const s = setup();
    const pending = control({ execution_stopped: false, terminal_persisted: false,
      can_send: false, cancel_status: 'stopping', pending_components: ['job:slow'] });
    s.dispatch({ type: 'SET_RUN_ID', sessionId: 's', generation: gen, runId: rid });
    if (source === 'snapshot') s.dispatch({ type: 'APPLY_SNAPSHOT', sessionId: 's', generation: gen,
      snapshot: { session_id: 's', run_id: rid, status: 'cancelled', messages: [],
        snapshot_complete: true, active_run: null, pending_feedback: null, resume_cursor: null, control: pending } });
    else {
      s.dispatch({ type: 'HYDRATE_START', sessionId: 's', requestVersion: 1 });
      s.dispatch({ type: 'HYDRATE_SUCCESS', sessionId: 's', requestVersion: 1, model: 'm', messages: [], activeRun: null, control: pending });
    }
    expect(selectRunGate(s.view()).canStartRun).toBe(false);
    expect(s.view().activeRun?.cancellation).toBe('stopping');
    vi.mocked(getRunControl).mockResolvedValueOnce(pending).mockResolvedValue(control({ revision: 4, native_run_created: false }));
    s.cancellation.observe('s'); s.cancellation.observe('s');
    await vi.advanceTimersByTimeAsync(0);
    expect(getRunControl).toHaveBeenCalledTimes(1);
    expect(cancelRun).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    await vi.advanceTimersByTimeAsync(1000);
    expect(selectRunGate(s.view()).canStartRun).toBe(true);
    expect(cancelRun).not.toHaveBeenCalled();
  });
  it('requires an explicit retry after a definitive permission error', async () => {
    const s = setup();
    vi.mocked(cancelRun).mockRejectedValue(new ApiError(403, 'forbidden'));
    s.cancellation.request('s'); await vi.advanceTimersByTimeAsync(0);
    s.cancellation.wake(); await vi.advanceTimersByTimeAsync(60000);
    expect(cancelRun).toHaveBeenCalledTimes(1);
    expect(getRunControl).not.toHaveBeenCalled();
    s.cancellation.request('s'); await vi.advanceTimersByTimeAsync(0);
    expect(cancelRun).toHaveBeenCalledTimes(2);
  });
  it('preempts a low priority query and reuses one ID across click, timeout and retry', async () => {
    const s = setup();
    const low = s.manager.query('s', 'snapshot', async () => new Promise(() => {})).catch(error => error);
    vi.mocked(cancelRun).mockImplementationOnce(async () => new Promise(() => {})).mockResolvedValue(control({ native_run_created: false }));
    vi.mocked(getRunControl).mockResolvedValue(control({ cancel_status: 'none', execution_stopped: false, terminal_persisted: false }));
    s.cancellation.request('s'); s.cancellation.request('s');
    expect((await low as DOMException).name).toBe('AbortError');
    expect(cancelRun).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(5000);
    expect(s.view().activeRun?.cancellation).toBe('unconfirmed');
    await vi.advanceTimersByTimeAsync(1000);
    expect(cancelRun).toHaveBeenCalledTimes(2);
    expect(vi.mocked(cancelRun).mock.calls[0][2]).toBe(vi.mocked(cancelRun).mock.calls[1][2]);
    expect(s.view().draft).toBe('question');
    expect(selectMessages(s.view())).toEqual([]);
    expect(selectRunGate(s.view()).canStartRun).toBe(true);
    await vi.advanceTimersByTimeAsync(1); // Deliver jsdom's queued storage event.
    expect(vi.getTimerCount()).toBe(0);
  });

  it('waits for execution and persistence, then unlocks before a failed snapshot', async () => {
    const s = setup();
    vi.mocked(cancelRun).mockResolvedValue(control({ execution_stopped: false, terminal_persisted: false, can_send: false }));
    vi.mocked(getRunControl).mockResolvedValueOnce(control({ terminal_persisted: false, can_send: false, revision: 4 }))
      .mockResolvedValue(control({ revision: 5 }));
    vi.mocked(getRunSnapshot).mockRejectedValue(new Error('offline'));
    s.cancellation.request('s'); await vi.advanceTimersByTimeAsync(0);
    expect(selectRunGate(s.view()).canStartRun).toBe(false);
    await vi.advanceTimersByTimeAsync(1000);
    expect(s.view().activeRun?.cancellation).toBe('saving');
    await vi.advanceTimersByTimeAsync(2000);
    expect(selectRunGate(s.view()).canStartRun).toBe(true);
    expect(getRunSnapshot).toHaveBeenCalledTimes(1);
    s.cancellation.reset(); await vi.advanceTimersByTimeAsync(1); expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps the cancel backoff independent from heartbeat and business progress', async () => {
    const s = setup();
    s.manager.acquire({ sessionId: 's', generation: gen, foreground: true, retry: vi.fn(), onDetached: vi.fn() });
    vi.mocked(cancelRun).mockRejectedValue(new Error('offline'));
    vi.mocked(getRunControl).mockRejectedValue(new Error('offline'));
    s.cancellation.request('s'); await vi.advanceTimersByTimeAsync(0);
    for (const delay of [1000, 2000, 4000, 8000, 15000, 30000]) {
      const before = vi.mocked(getRunControl).mock.calls.length;
      s.manager.receivedEvent('s', gen);
      await vi.advanceTimersByTimeAsync(delay - 1);
      expect(getRunControl).toHaveBeenCalledTimes(before);
      await vi.advanceTimersByTimeAsync(1);
      expect(getRunControl).toHaveBeenCalledTimes(before + 1);
      expect(vi.getTimerCount()).toBe(1);
    }
  });

  it('ignores old completion after another execution generation replaces it', async () => {
    const s = setup();
    let resolve!: (value: RunControl) => void;
    vi.mocked(cancelRun).mockImplementation(() => new Promise(done => { resolve = done; }));
    s.cancellation.request('s'); await vi.advanceTimersByTimeAsync(0);
    s.dispatch({ type: 'ADOPT_RUN', sessionId: 's', generation: 'new', activeRun: { run_id: 'new-run', status: 'running', last_event_index: -1 } });
    resolve(control()); await vi.advanceTimersByTimeAsync(0);
    expect(s.view().activeRun?.runId).toBe('new-run');
    expect(s.settled).not.toHaveBeenCalled();
  });

  it('queries a restored intent before retrying the same cancellation', async () => {
    const first = setup();
    vi.mocked(cancelRun).mockRejectedValue(new Error('lost'));
    first.cancellation.request('s'); await vi.advanceTimersByTimeAsync(0);
    const firstId = vi.mocked(cancelRun).mock.calls[0][2];
    first.cancellation.reset();
    const next = setup();
    vi.mocked(getRunControl).mockResolvedValue(control({ native_run_created: false }));
    next.cancellation.restore('s'); await vi.advanceTimersByTimeAsync(0);
    expect(getRunControl).toHaveBeenCalledTimes(1);
    expect(cancelRun).toHaveBeenCalledTimes(1);
    expect(next.view().activeRun?.cancelRequestId).toBe(firstId);
  });

  it('honors Retry-After and stops automatic retries on definite permission errors', async () => {
    const s = setup();
    const limited = new ApiError(429, 'rate limited'); limited.retryAfterMs = 7000;
    vi.mocked(cancelRun).mockRejectedValue(limited);
    vi.mocked(getRunControl).mockRejectedValue(new ApiError(403, 'forbidden'));
    s.cancellation.request('s'); await vi.advanceTimersByTimeAsync(6999);
    expect(getRunControl).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(getRunControl).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(100000);
    expect(getRunControl).toHaveBeenCalledTimes(1);
  });

  it('stopping a historical job leaves the current Run and send gate intact', async () => {
    const s = setup();
    vi.mocked(cancelJob).mockResolvedValue({ job_id: 'old-job', cancel_status: 'confirmed', execution_stopped: true, terminal_persisted: true });
    const before = s.view().activeRun;
    const notify = vi.fn();
    s.cancellation.requestJob('s', 'old-job', notify); await vi.advanceTimersByTimeAsync(0);
    expect(s.view().activeRun).toBe(before);
    expect(notify).toHaveBeenLastCalledWith('confirmed');
    expect(cancelRun).not.toHaveBeenCalled();
    s.cancellation.forget('s'); expect(vi.getTimerCount()).toBe(0);
  });

  it('an old restored launch-key intent cannot steal the new Run stop button', async () => {
    const s = setup();
    localStorage.setItem('igraph-cancel:v1:alice:s', JSON.stringify({ identity: 'alice', sessionId: 's', generation: 'old',
      cancelRequestId: 'old-stop', target: { start_request_id: 'old-start' } }));
    let finish!: (control: RunControl) => void;
    vi.mocked(getRunControl).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    vi.mocked(cancelRun).mockResolvedValue(control({ execution_stopped: false, terminal_persisted: false, can_send: false }));
    s.cancellation.restore('s'); await vi.advanceTimersByTimeAsync(0);
    expect(s.view().activeRun?.cancellation).toBeUndefined();
    s.cancellation.request('s'); await vi.advanceTimersByTimeAsync(0);
    expect(cancelRun).toHaveBeenCalledWith('s', { start_request_id: gen }, expect.any(String), expect.any(AbortSignal));
    finish(control({ run_id: 'old-run', start_request_id: 'old-start' })); await vi.advanceTimersByTimeAsync(0);
    expect(s.view().activeRun?.generation).toBe(gen);
    expect(s.settled).not.toHaveBeenCalled();
    expect(selectRunGate(s.view()).canStartRun).toBe(false);
  });

  it('restores job cancellation with a read first and keeps its stable request ID', async () => {
    const s = setup();
    vi.mocked(cancelJob).mockRejectedValue(new Error('lost'));
    s.cancellation.requestJob('s', 'job', vi.fn()); await vi.advanceTimersByTimeAsync(0);
    const id = vi.mocked(cancelJob).mock.calls[0][2];
    s.cancellation.reset();
    const next = setup();
    vi.mocked(getJobControl).mockResolvedValue({ job_id: 'job', cancel_status: 'none', execution_stopped: false, terminal_persisted: false });
    vi.mocked(cancelJob).mockResolvedValue({ job_id: 'job', cancel_status: 'confirmed', execution_stopped: true, terminal_persisted: true });
    next.cancellation.restore('s'); await vi.advanceTimersByTimeAsync(0);
    expect(getJobControl).toHaveBeenCalledTimes(1);
    expect(vi.mocked(cancelJob).mock.calls[1][2]).toBe(id);
    expect(localStorage.getItem('igraph-job-cancel:v1:alice:s')).toBeNull();
  });
});

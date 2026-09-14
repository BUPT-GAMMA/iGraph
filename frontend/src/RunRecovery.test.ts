import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RunConnectionManager } from './RunConnectionManager';
import { RunRecovery } from './RunRecovery';
import { conversationReducer, initialConversationState, selectMessages, selectRunGate } from './sessionState';
import type { ConversationAction } from './sessionState';
import { activateSession, ApiError, getRunControl, getRunSnapshot, resumeRun, streamChat, streamFeedback } from './api';
import type { RunSnapshot, StreamHandlers } from './api';

vi.mock('./api', async () => ({ ...await vi.importActual('./api'),
  activateSession: vi.fn(), getRunControl: vi.fn(), getRunSnapshot: vi.fn(), resumeRun: vi.fn(), streamChat: vi.fn(), streamFeedback: vi.fn() }));

const managers: RunConnectionManager[] = [];
function harness(cold = false, bind = true, controlEnabled = false) {
  let state = conversationReducer(initialConversationState, { type: 'ENSURE_SESSION', sessionId: 'A' });
  const dispatch = (action: ConversationAction) => { state = conversationReducer(state, action); };
  dispatch({ type: 'START_RUN', sessionId: 'A', generation: 'g', userMessage: { id: 'user:g', role: 'user', content: 'question' },
    assistantMessage: { id: 'assistant:g', role: 'assistant', content: '' } });
  if (bind) dispatch({ type: 'SET_RUN_ID', sessionId: 'A', generation: 'g', runId: 'r' });
  if (!cold) dispatch({ type: 'APPLY_BATCH', sessionId: 'A', generation: 'g', batch: { protocol_version: 2, session_id: 'A', run_id: 'r', event_index: 0, events: [{ type: 'token', content: 'A', message_id: 'assistant:r' }] } });
  let paused = false, identity = 'alice';
  const manager = new RunConnectionManager(); managers.push(manager);
  const settled = vi.fn(), synchronize = vi.fn();
  const recovery = new RunRecovery({ controlEnabled, manager, view: () => state.viewsBySessionId.A, identity: () => identity, paused: () => paused,
    foreground: () => true, dispatch, accepted: vi.fn(), settled, synchronize });
  const open = () => recovery.open('A', 'g', { run_id: 'r', status: 'running', last_event_index: 500 });
  return { manager, recovery, dispatch, open, settled, synchronize, view: () => state.viewsBySessionId.A,
    pause: () => { paused = true; manager.abort('A'); }, identity: (value: string) => { identity = value; } };
}
const snapshot = (complete = true, status = 'completed'): RunSnapshot => ({ session_id: 'A', run_id: 'r', status,
  messages: [{ id: 'assistant:r', role: 'assistant', content: 'ABC' }],
  active_run: complete ? null : { run_id: 'r', status: 'running', last_event_index: 100 }, pending_feedback: null,
  snapshot_complete: complete, resume_cursor: null });
function emit(handlers: StreamHandlers, index: number, content: string, type = 'token') {
  handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'r', event_index: index,
    events: [{ type, content, message_id: 'assistant:r' }] });
}
async function flush() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
beforeEach(() => {
  vi.useFakeTimers(); vi.spyOn(Math, 'random').mockReturnValue(.5);
  vi.mocked(resumeRun).mockReset(); vi.mocked(getRunSnapshot).mockReset(); vi.mocked(activateSession).mockReset();
  vi.mocked(streamChat).mockReset(); vi.mocked(streamFeedback).mockReset();
  vi.mocked(getRunControl).mockReset().mockResolvedValue({ run_id: 'r', start_request_id: 'g', registration_status: 'registered',
    run_status: 'completed', cancel_status: 'none', execution_stopped: true, terminal_persisted: true, can_send: true, active_run: null, revision: 3 });
  vi.mocked(getRunSnapshot).mockResolvedValue(snapshot(false));
});
afterEach(() => { managers.splice(0).forEach(manager => manager.abortAll()); vi.useRealTimers(); vi.restoreAllMocks(); });

describe('single recovery coordinator', () => {
  it('preserves a preparation conflict message while restoring the draft after confirmed failure', async () => {
    const h = harness(true, false, true);
    const error = ApiError.fromResponse(409, { detail: { code: 'JOB_IDEMPOTENCY_CONFLICT' } });
    vi.mocked(streamChat).mockRejectedValue(error);
    vi.mocked(getRunControl).mockResolvedValue({ run_id: 'r', start_request_id: 'g', registration_status: 'registered',
      run_status: 'failed', cancel_status: 'none', native_run_created: false, execution_stopped: true,
      terminal_persisted: true, can_send: true, revision: 3 });
    const connection = h.manager.acquire({ sessionId: 'A', generation: 'g', foreground: true, retry: vi.fn(), onDetached: vi.fn() })!;
    await h.recovery.consume(connection, 'start', { message: 'question', attachments: [] });
    await vi.advanceTimersByTimeAsync(1000);
    expect(h.view().draft).toBe('question');
    expect(h.view().error).toBe(error.message);
    expect(h.view().error).toContain('数据加载操作编号冲突');
    expect(selectRunGate(h.view()).canStartRun).toBe(true);
    expect(streamChat).toHaveBeenCalledOnce();
    expect(resumeRun).not.toHaveBeenCalled();
  });

  it('checks cancellation before reopening a terminal run, including snapshot-only recovery', async () => {
    const h = harness(true, true, true);
    h.dispatch({ type: 'SET_RECEPTION', sessionId: 'A', generation: 'g', reception: 'snapshot_sync', snapshotOnly: true });
    h.dispatch({ type: 'REQUEST_CANCEL', sessionId: 'A', generation: 'g', cancelRequestId: 'c' });
    vi.mocked(getRunControl).mockResolvedValueOnce({ run_id: 'r', start_request_id: 'g', registration_status: 'registered', run_status: 'cancelled',
      cancel_status: 'requested', native_run_created: false, execution_stopped: false, terminal_persisted: false, can_send: false, revision: 2 })
      .mockResolvedValue({ run_id: 'r', start_request_id: 'g', registration_status: 'registered', run_status: 'cancelled', cancel_status: 'confirmed',
        native_run_created: false, execution_stopped: true, terminal_persisted: true, can_send: true, revision: 3 });
    h.open(); await flush();
    expect(selectRunGate(h.view()).canStartRun).toBe(false);
    expect(resumeRun).not.toHaveBeenCalled();
    expect(getRunSnapshot).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(250);
    expect(selectRunGate(h.view()).canStartRun).toBe(true);
    expect(h.view().activeRun?.reception).toBe('idle');
    expect(h.view().activeRun?.synchronized).toBe(true);
    expect(selectMessages(h.view()).map(message => message.content)).toEqual(['question', '已停止本轮对话']);
    expect(h.synchronize).not.toHaveBeenCalled();
    expect(h.settled).toHaveBeenCalledOnce();
    h.dispatch({ type: 'SET_RECEPTION', sessionId: 'A', generation: 'g', reception: 'connecting', synchronized: false, error: 'Run not found' });
    h.open(); await vi.advanceTimersByTimeAsync(30000);
    expect(h.view().error).toBeUndefined();
    expect(h.view().activeRun?.reception).toBe('idle');
    expect(h.manager.all()).toHaveLength(0);
    expect(getRunControl).toHaveBeenCalledTimes(2);
    expect(resumeRun).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
  it('resolves a missing native replay through control instead of leaving a fatal loading state', async () => {
    const h = harness(true, true, true);
    vi.mocked(resumeRun).mockRejectedValue(new ApiError(404, 'Run not found'));
    vi.mocked(getRunControl).mockResolvedValue({ run_id: 'r', start_request_id: 'g', registration_status: 'registered', run_status: 'cancelled',
      cancel_status: 'confirmed', native_run_created: false, execution_stopped: true, terminal_persisted: true, can_send: true, revision: 3 });
    h.open(); await flush();
    expect(resumeRun).toHaveBeenCalledOnce();
    expect(getRunControl).toHaveBeenCalledOnce();
    expect(getRunSnapshot).not.toHaveBeenCalled();
    expect(h.synchronize).not.toHaveBeenCalled();
    expect(h.view().error).toBeUndefined();
    expect(selectRunGate(h.view()).canStartRun).toBe(true);
    expect(h.view().activeRun?.replay).toBeUndefined();
    expect(h.manager.all()).toHaveLength(0);
  });
  it('keeps a missing-run error when neither control nor native history contains the run', async () => {
    const h = harness(true, true, true);
    vi.mocked(resumeRun).mockRejectedValue(new ApiError(404, 'Run not found'));
    vi.mocked(getRunControl).mockRejectedValue(ApiError.fromResponse(404, { detail: { code: 'RUN_NOT_FOUND' } }));
    vi.mocked(getRunSnapshot).mockRejectedValue(new ApiError(404, 'Run not found'));
    h.open(); await vi.advanceTimersByTimeAsync(0);
    expect(h.view().error).toBe('Run not found');
    expect(selectRunGate(h.view()).canStartRun).toBe(false);
    expect(h.manager.all()).toHaveLength(0);
  });
  it('does not reopen the observer while cancellation owns the session', () => {
    const h = harness(true, true, true);
    h.manager.prioritizeCancellation('A');
    h.open();
    expect(resumeRun).not.toHaveBeenCalled();
    expect(h.manager.all()).toHaveLength(0);
  });

  it('reopens send on authoritative completion while the stream and content synchronization are still pending', async () => {
    const h = harness(false, true, true); let handlers!: StreamHandlers;
    vi.mocked(resumeRun).mockImplementation(async (_s, _r, _c, value) => { handlers = value; await new Promise(() => {}); });
    h.open();
    emit(handlers, 1, 'ABC', 'done');
    await flush();
    expect(selectRunGate(h.view()).canStartRun).toBe(true);
    expect(h.manager.all()).toHaveLength(0);
    expect(getRunSnapshot).not.toHaveBeenCalled();
    expect(h.synchronize).toHaveBeenCalledWith('A', 'r');
    expect(h.settled).toHaveBeenCalledOnce();
  });
  it('keeps send blocked until native persistence is confirmed and rechecks promptly', async () => {
    const h = harness(false, true, true); let handlers!: StreamHandlers;
    vi.mocked(getRunControl).mockResolvedValueOnce({ run_id: 'r', start_request_id: 'g', registration_status: 'registered',
      run_status: 'completed', cancel_status: 'none', execution_stopped: true, terminal_persisted: false, can_send: false, revision: 2 });
    vi.mocked(resumeRun).mockImplementation(async (_s, _r, _c, value) => { handlers = value; await new Promise(() => {}); });
    h.open(); emit(handlers, 1, 'ABC', 'done'); await flush();
    expect(selectRunGate(h.view()).canStartRun).toBe(false);
    await vi.advanceTimersByTimeAsync(250);
    expect(selectRunGate(h.view()).canStartRun).toBe(true);
    expect(getRunControl).toHaveBeenCalledTimes(2);
    expect(h.synchronize).toHaveBeenCalledOnce();
  });
  it('deduplicates terminal notification and stream closure verification', async () => {
    const h = harness(false, true, true); let finish!: (value: any) => void;
    vi.mocked(getRunControl).mockImplementation(async () => new Promise(resolve => { finish = resolve; }));
    vi.mocked(resumeRun).mockImplementation(async (_s, _r, _c, handlers) => { emit(handlers, 1, 'ABC', 'done'); });
    h.open(); await flush();
    expect(getRunControl).toHaveBeenCalledOnce();
    finish({ run_id: 'r', registration_status: 'registered', run_status: 'completed', cancel_status: 'none',
      execution_stopped: true, terminal_persisted: true, can_send: true, revision: 3 });
    await flush();
    expect(h.synchronize).toHaveBeenCalledOnce();
    expect(h.settled).toHaveBeenCalledOnce();
  });
  it('adopts a newer active run without unlocking or waiting for the older content', async () => {
    const h = harness(false, true, true);
    vi.mocked(getRunControl).mockResolvedValue({ run_id: 'r', start_request_id: 'g', registration_status: 'registered', run_status: 'completed', cancel_status: 'none',
      execution_stopped: true, terminal_persisted: true, can_send: false, revision: 3,
      active_run: { run_id: 'new', status: 'running', last_event_index: 0 } });
    vi.mocked(resumeRun).mockImplementation(async (_s, _r, _c, handlers) => { emit(handlers, 1, 'ABC', 'done'); });
    h.open(); await flush();
    expect(h.view().activeRun?.runId).toBe('new');
    expect(selectRunGate(h.view()).canStartRun).toBe(false);
    expect(h.synchronize).toHaveBeenCalledWith('A', 'r');
    expect(h.settled).not.toHaveBeenCalled();
  });
  it('recovers completed runs created before control registration through their snapshot', async () => {
    const h = harness(false, true, true);
    vi.mocked(getRunControl).mockResolvedValue({ run_id: null, start_request_id: null, registration_status: 'not_seen', cancel_status: 'none',
      execution_stopped: false, terminal_persisted: false, can_send: false, revision: 0 });
    vi.mocked(getRunSnapshot).mockResolvedValue(snapshot());
    vi.mocked(resumeRun).mockImplementation(async (_s, _r, _c, handlers) => { emit(handlers, 1, 'ABC', 'done'); });
    h.open(); await h.recovery.check(h.manager.get('A')!, true);
    expect(selectRunGate(h.view()).canStartRun).toBe(true);
    expect(getRunSnapshot).toHaveBeenCalledOnce();
  });
  it('recovers a lost first response from the newly persisted run without resending', async () => {
    const h = harness(true, false);
    const connection = h.manager.acquire({ sessionId: 'A', generation: 'g', foreground: true, retry: vi.fn(), onDetached: vi.fn() })!;
    vi.mocked(streamChat).mockRejectedValue(new Error('lost response'));
    vi.mocked(activateSession).mockResolvedValue({ active_run: null, messages: [
      { id: 'user:r', role: 'user', content: 'question' }, { id: 'assistant:r', role: 'assistant', content: 'ABC' },
    ] });
    vi.mocked(getRunSnapshot).mockResolvedValue(snapshot());
    await h.recovery.consume(connection, 'start', { message: 'question', attachments: [] });
    await vi.advanceTimersByTimeAsync(3000);
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(resumeRun).not.toHaveBeenCalled();
    expect(selectMessages(h.view()).slice(-1)[0].content).toBe('ABC');
    expect(selectRunGate(h.view()).canStartRun).toBe(true);
  });
  it('restores the unsent draft when another client already owns an active run', async () => {
    const h = harness(true, false);
    const connection = h.manager.acquire({ sessionId: 'A', generation: 'g', foreground: true, retry: vi.fn(), onDetached: vi.fn() })!;
    const pending = { run_id: 'r', request_id: 'r', call_id: 'call' };
    vi.mocked(streamChat).mockRejectedValue(new ApiError(409, 'active', null, { run_id: 'r', status: 'paused', last_event_index: 100 }, pending));
    vi.mocked(getRunSnapshot).mockResolvedValue({ ...snapshot(), status: 'paused', active_run: { run_id: 'r', status: 'paused', last_event_index: 100 }, pending_feedback: pending });
    await h.recovery.consume(connection, 'start', { message: 'unsent question', attachments: [] });
    await vi.advanceTimersByTimeAsync(1000);
    expect(h.view().draft).toBe('unsent question');
    expect(streamChat).toHaveBeenCalledTimes(1);
    expect(selectRunGate(h.view()).canSubmitFeedback).toBe(true);
  });
  it('allows a manual approval retry after a proven upstream rejection, without auto-submission', async () => {
    const h = harness();
    const pending = { run_id: 'r', request_id: 'r', call_id: 'call', tool_name: 'train' };
    const paused = { ...snapshot(), status: 'paused', active_run: { run_id: 'r', status: 'paused', last_event_index: 100 }, pending_feedback: pending };
    h.dispatch({ type: 'APPLY_SNAPSHOT', sessionId: 'A', generation: 'g', snapshot: paused });
    const connection = h.manager.acquire({ sessionId: 'A', runId: 'r', generation: 'g', foreground: true, retry: vi.fn(), onDetached: vi.fn() })!;
    h.dispatch({ type: 'BEGIN_FEEDBACK_SUBMISSION', sessionId: 'A', generation: 'g', runId: 'r', callId: 'call', decisionId: 'd', decision: 'approve' });
    const rejected = new ApiError(503, 'upstream unavailable'); rejected.feedbackAccepted = false;
    vi.mocked(streamFeedback).mockRejectedValue(rejected);
    vi.mocked(getRunSnapshot).mockResolvedValue(paused);
    await h.recovery.consume(connection, 'feedback', undefined, pending, 'approve', 'd');
    await vi.advanceTimersByTimeAsync(1000);
    expect(selectRunGate(h.view()).canSubmitFeedback).toBe(true);
    expect(streamFeedback).toHaveBeenCalledTimes(1);
  });
  it('keeps approval in snapshot-only mode and never resubmits a decision after disconnection', async () => {
    const h = harness();
    const pending = { run_id: 'r', request_id: 'r', call_id: 'call', tool_name: 'train' };
    h.dispatch({ type: 'APPLY_SNAPSHOT', sessionId: 'A', generation: 'g', snapshot: { ...snapshot(), status: 'paused',
      active_run: { run_id: 'r', status: 'paused', last_event_index: 100 }, pending_feedback: pending } });
    expect(h.view().activeRun?.snapshotOnly).toBe(true);
    expect(selectRunGate(h.view()).canSubmitFeedback).toBe(true);
    const connection = h.manager.acquire({ sessionId: 'A', runId: 'r', generation: 'g', foreground: true, retry: vi.fn(), onDetached: vi.fn() })!;
    h.dispatch({ type: 'BEGIN_FEEDBACK_SUBMISSION', sessionId: 'A', generation: 'g', runId: 'r', callId: 'call', decisionId: 'decision', decision: 'approve' });
    vi.mocked(streamFeedback).mockRejectedValue(new Error('lost response'));
    vi.mocked(getRunSnapshot).mockResolvedValueOnce(snapshot(false)).mockResolvedValue(snapshot());
    await h.recovery.consume(connection, 'feedback', undefined, pending, 'approve', 'decision');
    await vi.advanceTimersByTimeAsync(3000);
    expect(streamFeedback).toHaveBeenCalledTimes(1);
    expect(resumeRun).not.toHaveBeenCalled();
    expect(selectMessages(h.view()).slice(-1)[0].content).toBe('ABC');
    expect(selectRunGate(h.view()).canStartRun).toBe(true);
  });
  it('does not expose historical approval while catching up to a continuation', async () => {
    const h = harness(); let handlers!: StreamHandlers;
    vi.mocked(resumeRun).mockImplementation(async (_s, _r, _c, value) => { handlers = value; await new Promise(() => {}); });
    h.open();
    handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'r', event_index: 1, events: [{ type: 'await_user_feedback', request: { run_id: 'r', call_id: 'call' } }] });
    expect(selectRunGate(h.view()).canSubmitFeedback).toBe(false);
    handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'r', event_index: 2, events: [{ type: 'run_continued' }] });
    handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'r', events: [{ type: 'stream_status', status: 'subscribed' }] });
    expect(h.view().activeRun?.status).toBe('streaming');
    expect(selectRunGate(h.view()).canSubmitFeedback).toBe(false);
    expect(h.view().activeRun?.synchronized).toBe(true);
  });
  it('ignores an old connection callback after a restart', async () => {
    const h = harness(); let handlers!: StreamHandlers;
    vi.mocked(resumeRun).mockImplementation(async (_s, _r, _c, value) => { handlers = value; await new Promise(() => {}); });
    h.open(); h.manager.restart('A', 'g'); emit(handlers, 1, 'BAD');
    expect(h.view().activeRun?.lastAppliedEventIndex).toBe(0);
  });
  it('ignores a late snapshot after a new generation takes over', async () => {
    const h = harness(); let resolve!: (snapshot: RunSnapshot) => void;
    vi.mocked(resumeRun).mockRejectedValue(new ApiError(409, 'gap', {}, null, null, 'REPLAY_UNAVAILABLE'));
    vi.mocked(getRunSnapshot).mockImplementation(() => new Promise(done => { resolve = done; }));
    h.open(); await flush();
    h.dispatch({ type: 'ADOPT_RUN', sessionId: 'A', generation: 'new-generation', activeRun: { run_id: 'new-run', status: 'running', last_event_index: -1 } });
    resolve(snapshot()); await flush();
    expect(h.view().activeRun?.runId).toBe('new-run');
    expect(selectRunGate(h.view()).canStartRun).toBe(false);
  });
  it('replays from the applied cursor and repairs ABC through duplicate delivery', async () => {
    const h = harness();
    vi.mocked(resumeRun).mockRejectedValueOnce(new Error('offline')).mockImplementationOnce(async (_s, _r, cursor, handlers) => {
      expect(cursor).toBe(0);
      emit(handlers, 0, 'A'); emit(handlers, 1, 'B'); emit(handlers, 1, 'B'); emit(handlers, 2, 'C'); emit(handlers, 3, 'ABC', 'done');
    });
    vi.mocked(getRunSnapshot).mockResolvedValue(snapshot());
    h.open(); await flush();
    expect(resumeRun).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(resumeRun).toHaveBeenCalledTimes(2);
    expect(selectMessages(h.view()).slice(-1)[0].content).toBe('ABC');
    expect(selectRunGate(h.view()).canStartRun).toBe(true);
    expect(h.manager.all()).toHaveLength(0);
  });
  it('backs off 1,2,4,8,15,30 seconds across successful running status probes', async () => {
    const h = harness();
    vi.mocked(resumeRun).mockRejectedValue(new Error('offline'));
    h.open(); await flush();
    for (const [index, delay] of [1000, 2000, 4000, 8000, 15000, 30000].entries()) {
      await vi.advanceTimersByTimeAsync(delay - 1);
      expect(resumeRun).toHaveBeenCalledTimes(index + 1);
      await vi.advanceTimersByTimeAsync(1);
      expect(resumeRun).toHaveBeenCalledTimes(index + 2);
    }
  });
  it('honors Retry-After and stops for denied access', async () => {
    const h = harness();
    vi.mocked(resumeRun).mockRejectedValueOnce(ApiError.fromResponse(429, {}, '10')).mockRejectedValueOnce(new ApiError(403, 'denied'));
    h.open(); await flush();
    await vi.advanceTimersByTimeAsync(9999); expect(resumeRun).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1); expect(resumeRun).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(100000); expect(resumeRun).toHaveBeenCalledTimes(2);
    expect(h.view().error).toBe('denied');
  });
  it('retains partial output until a complete snapshot exists after a replay gap', async () => {
    const h = harness();
    vi.mocked(resumeRun).mockImplementation(async (_s, _r, _c, handlers) => emit(handlers, 3, 'C'));
    vi.mocked(getRunSnapshot).mockResolvedValueOnce(snapshot(false)).mockResolvedValue(snapshot());
    h.open(); await flush();
    expect(selectMessages(h.view()).slice(-1)[0].content).toBe('A');
    expect(h.view().activeRun?.lastAppliedEventIndex).toBe(0);
    expect(selectRunGate(h.view()).canStartRun).toBe(false);
    await vi.advanceTimersByTimeAsync(1000);
    expect(selectMessages(h.view()).slice(-1)[0].content).toBe('ABC');
    expect(resumeRun).toHaveBeenCalledTimes(1);
  });
  it('stages cold replay and publishes only when caught up', async () => {
    const h = harness(true);
    h.dispatch({ type: 'APPEND_MESSAGES', sessionId: 'A', messages: [{ id: 'assistant:r', role: 'assistant', content: 'AB' }] });
    let handlers!: StreamHandlers;
    vi.mocked(resumeRun).mockImplementation(async (_s, _r, cursor, streamHandlers) => {
      expect(cursor).toBe(-1); handlers = streamHandlers; await new Promise(() => {});
    });
    h.open(); emit(handlers, 0, 'A');
    expect(selectMessages(h.view()).slice(-1)[0].content).toBe('AB');
    emit(handlers, 1, 'B'); emit(handlers, 2, 'C');
    handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'r', event_index: null, events: [{ type: 'stream_status', status: 'subscribed' }] });
    expect(selectMessages(h.view()).slice(-1)[0].content).toBe('ABC');
    expect(h.view().activeRun?.lastAppliedEventIndex).toBe(2);
  });
  it('ignores late callbacks and snapshots after pause or identity change', async () => {
    const h = harness();
    let handlers!: StreamHandlers;
    vi.mocked(resumeRun).mockImplementation(async (_s, _r, _c, value) => { handlers = value; await new Promise(() => {}); });
    h.open(); h.pause(); emit(handlers, 1, 'BAD');
    expect(selectMessages(h.view()).slice(-1)[0].content).toBe('A');
    h.recovery.wake(); await vi.advanceTimersByTimeAsync(90000);
    expect(getRunSnapshot).not.toHaveBeenCalled();
    const second = harness(); second.open(); second.identity('bob'); emit(handlers, 1, 'BAD');
    expect(selectMessages(second.view()).slice(-1)[0].content).toBe('A');
  });
  it('detects a lost terminal notification despite live heartbeats and deduplicates probes', async () => {
    const h = harness(); let handlers!: StreamHandlers;
    vi.mocked(resumeRun).mockImplementation(async (_s, _r, _c, value) => { handlers = value; await new Promise(() => {}); });
    vi.mocked(getRunSnapshot).mockResolvedValue(snapshot());
    h.open(); await vi.advanceTimersByTimeAsync(29000); handlers.onActivity?.();
    await vi.advanceTimersByTimeAsync(1000);
    expect(getRunSnapshot).toHaveBeenCalledTimes(1);
    expect(selectRunGate(h.view()).canStartRun).toBe(true);
    expect(h.manager.all()).toHaveLength(0);
  });
  it('times out a stalled stream at 90 seconds', async () => {
    const h = harness(); vi.mocked(resumeRun).mockImplementation(async () => { await new Promise(() => {}); });
    h.open(); await vi.advanceTimersByTimeAsync(90000);
    expect(vi.mocked(resumeRun).mock.calls[0][4].aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(1000);
    expect(resumeRun).toHaveBeenCalledTimes(2);
    expect(getRunSnapshot).toHaveBeenCalledTimes(2);
  });
  it('makes one control request and times it out after 15 seconds', async () => {
    const h = harness();
    const query = vi.fn(async () => { await new Promise(() => {}); return 1; });
    const first = h.manager.query('A', 'g', query);
    expect(h.manager.query('A', 'g', query)).toBe(first);
    const assertion = expect(first).rejects.toMatchObject({ name: 'TimeoutError' });
    await vi.advanceTimersByTimeAsync(15000);
    await assertion;
    expect(query).toHaveBeenCalledTimes(1);
  });
  it('uses one timer for both a stream watchdog and a control deadline, then clears everything', async () => {
    const h = harness();
    vi.mocked(resumeRun).mockImplementation(async () => { await new Promise(() => {}); });
    h.open();
    const pending = h.manager.query('A', 'g', async () => new Promise(() => {})).catch(() => undefined);
    expect(vi.getTimerCount()).toBe(1);
    h.manager.abortAll(); await pending;
    expect(vi.getTimerCount()).toBe(0);
    expect(h.manager.all()).toHaveLength(0);
  });
});

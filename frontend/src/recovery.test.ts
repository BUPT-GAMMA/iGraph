import { describe, expect, it, vi, afterEach } from 'vitest';
import { conversationReducer as reduce, initialConversationState, selectMessages, selectRunGate } from './sessionState';
import { RunConnectionManager } from './RunConnectionManager';

function started() {
  let state = reduce(initialConversationState, { type: 'ENSURE_SESSION', sessionId: 'A' });
  state = reduce(state, { type: 'START_RUN', sessionId: 'A', generation: 'g',
    userMessage: { id: 'user:g', role: 'user', content: 'question' },
    assistantMessage: { id: 'assistant:g', role: 'assistant', content: '' } });
  return reduce(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'g', batch: {
    protocol_version: 2, session_id: 'A', run_id: 'run', event_index: 0,
    events: [{ type: 'token', content: 'A', message_id: 'assistant:run' }],
  } });
}

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });
describe('recovery regression baseline', () => {
  it.each([{ type: 'token', content: {} }, { type: 'done', content: null }, { type: 'plan_step' },
    { type: 'analysis_report', data: {} }, { type: 'graph_visualize', data: {} }])('rejects an unapplicable business event atomically: %s', malformed => {
    const state = reduce(started(), { type: 'APPLY_BATCH', sessionId: 'A', generation: 'g', batch: {
      protocol_version: 2, session_id: 'A', run_id: 'run', event_index: 1,
      events: [{ type: 'token', content: 'must not apply' }, malformed],
    } });
    expect(state.viewsBySessionId.A.activeRun?.lastAppliedEventIndex).toBe(0);
    expect(state.viewsBySessionId.A.activeRun?.recoveryRequired).toBe(true);
    expect(selectMessages(state.viewsBySessionId.A).slice(-1)[0].content).toBe('A');
  });
  it('reactivates reception when a previously paused run continued on another client', () => {
    let state = reduce(started(), { type: 'SET_RECEPTION', sessionId: 'A', generation: 'g', reception: 'idle', synchronized: true });
    state = reduce(state, { type: 'HYDRATE_START', sessionId: 'A', requestVersion: 1 });
    state = reduce(state, { type: 'HYDRATE_SUCCESS', sessionId: 'A', requestVersion: 1, model: 'm', preserveMessages: true,
      activeRun: { run_id: 'run', status: 'running', last_event_index: 500 }, messages: [] });
    expect(state.viewsBySessionId.A.activeRun?.reception).toBe('connecting');
    expect(state.viewsBySessionId.A.activeRun?.lastAppliedEventIndex).toBe(0);
    expect(state.viewsBySessionId.A.activeRun?.synchronized).toBe(false);
  });
  it.each(['', 'ABC', undefined])('handles done content %s without duplicating the assistant message', content => {
    const state = reduce(started(), { type: 'APPLY_BATCH', sessionId: 'A', generation: 'g', batch: {
      protocol_version: 2, session_id: 'A', run_id: 'run', event_index: 1,
      events: [{ type: 'done', ...(content === undefined ? {} : { content }), message_id: 'assistant:run' }],
    } });
    const messages = selectMessages(state.viewsBySessionId.A).filter(message => message.role === 'assistant');
    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe(content === undefined ? 'A' : content);
  });
  it('stops confirmation at a gap and ignores all later deltas', () => {
    let state = started();
    for (const index of [3, 4, 1]) state = reduce(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'g', batch: {
      protocol_version: 2, session_id: 'A', run_id: 'run', event_index: index, events: [{ type: 'token', content: 'BAD' }],
    } });
    expect(state.viewsBySessionId.A.activeRun?.lastAppliedEventIndex).toBe(0);
    expect(selectMessages(state.viewsBySessionId.A).slice(-1)[0].content).toBe('A');
    expect(selectRunGate(state.viewsBySessionId.A).canStartRun).toBe(false);
  });
  it('does not advance a cursor on heartbeats or subscription notifications', () => {
    const state = reduce(started(), { type: 'APPLY_BATCH', sessionId: 'A', generation: 'g', batch: {
      protocol_version: 2, session_id: 'A', run_id: 'run', event_index: 500, events: [{ type: 'stream_status', status: 'subscribed' }],
    } });
    expect(state.viewsBySessionId.A.activeRun?.lastAppliedEventIndex).toBe(0);
  });
  it('keeps reasoning and graph identities across replayed plan starts', () => {
    let state = started();
    for (const index of [1, 2]) state = reduce(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'g', batch: {
      protocol_version: 2, session_id: 'A', run_id: 'run', event_index: index, events: [
        { type: 'plan_analyzing' }, { type: 'plan_step', step: { step_id: index, goal: `step ${index}` } },
        { type: 'graph_visualize', call_id: 'stable', data: { success: true, nodes: [], edges: [] } },
      ],
    } });
    expect(state.viewsBySessionId.A.activeRun?.plan?.steps).toHaveLength(2);
    expect(selectMessages(state.viewsBySessionId.A).slice(-1)[0].graphs).toHaveLength(1);
  });
  it('uses the complete done text to repair partial output', () => {
    const state = reduce(started(), { type: 'APPLY_BATCH', sessionId: 'A', generation: 'g', batch: {
      protocol_version: 2, session_id: 'A', run_id: 'run', event_index: 1,
      events: [{ type: 'done', content: 'ABC', message_id: 'assistant:run' }],
    } });
    expect(selectMessages(state.viewsBySessionId.A).slice(-1)[0]?.content).toBe('ABC');
    expect(selectMessages(state.viewsBySessionId.A).filter(message => message.role === 'assistant')).toHaveLength(1);
  });
  it('does not confirm unseen events when binding a run id', () => {
    const state = reduce(started(), { type: 'SET_RUN_ID', sessionId: 'A', generation: 'g', runId: 'run', lastAppliedEventIndex: 100 });
    expect(state.viewsBySessionId.A.activeRun?.lastAppliedEventIndex).toBe(0);
  });
  it('restores a completed paused view and unlocks sending', () => {
    let state = reduce(started(), { type: 'SET_RECEPTION', sessionId: 'A', generation: 'g', reception: 'user_paused', synchronized: false });
    state = reduce(state, { type: 'HYDRATE_START', sessionId: 'A', requestVersion: 1 });
    state = reduce(state, { type: 'HYDRATE_SUCCESS', sessionId: 'A', requestVersion: 1, model: 'm', activeRun: null,
      messages: [{ id: 'assistant:run', role: 'assistant', content: 'ABC' }] });
    expect(selectMessages(state.viewsBySessionId.A).slice(-1)[0]?.content).toBe('ABC');
    expect(selectRunGate(state.viewsBySessionId.A).canStartRun).toBe(true);
  });
  it('keeps at most one scheduled retry', () => {
    vi.useFakeTimers();
    vi.spyOn(Math, 'random').mockReturnValue(.5);
    const manager = new RunConnectionManager();
    const retry = vi.fn();
    manager.acquire({ sessionId: 'A', generation: 'g', foreground: true, retry, onDetached: vi.fn() });
    manager.scheduleReconnect('A', 'g', retry);
    manager.scheduleReconnect('A', 'g', retry);
    vi.advanceTimersByTime(3000);
    expect(retry).toHaveBeenCalledTimes(1);
    manager.abortAll();
  });
});

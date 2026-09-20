import { afterEach, expect, it, vi } from 'vitest';
import { activityKey, forgetSessionActivity, readSessionActivities, saveSessionActivity } from './sessionActivity';
import { conversationReducer, createSessionView, initialConversationState } from './sessionState';

afterEach(() => { localStorage.clear(); sessionStorage.clear(); vi.restoreAllMocks(); });

it('persists only run/read identifiers, never the response, uploads or graph', () => {
  const view = createSessionView('A');
  view.draft = 'private draft';
  view.messagesById = { x: { role: 'assistant', content: 'private answer' } };
  view.activeRun = { generation: 'g', runId: 'r', status: 'streaming', lastAppliedEventIndex: 40, toolsById: {}, toolOrder: [], plan: null, feedback: null };
  saveSessionActivity('user', view);
  expect(readSessionActivities('user')).toEqual([{ sessionId: 'A', runId: 'r', status: 'streaming' }]);
  expect(localStorage.getItem(activityKey('user', 'A'))).not.toContain('private');
  expect(readSessionActivities('other-user')).toEqual([]);
  forgetSessionActivity('user', 'A');
  expect(readSessionActivities('user')).toEqual([]);
});

it('restores unread completion and retains the read marker through another reload', () => {
  let state = conversationReducer(initialConversationState, { type: 'RESTORE_SESSION_ACTIVITY', activity: { sessionId: 'A', completedRunId: 'r' } });
  expect(state.viewsBySessionId.A.unreadCompletion).toBe(true);
  state = conversationReducer(state, { type: 'SET_ACTIVE_SESSION', sessionId: 'A' });
  saveSessionActivity('user', state.viewsBySessionId.A);
  const saved = readSessionActivities('user')[0];
  expect(saved.completedRunId).toBe(saved.readRunId);
  const restored = conversationReducer(initialConversationState, { type: 'RESTORE_SESSION_ACTIVITY', activity: saved });
  expect(restored.viewsBySessionId.A.unreadCompletion).toBe(false);
});

it('does not mark a previously read completion unread when the same run is replayed', () => {
  let state = conversationReducer(initialConversationState, { type: 'RESTORE_SESSION_ACTIVITY', activity: {
    sessionId: 'A', runId: 'r', status: 'streaming', completedRunId: 'r', readRunId: 'r',
  } });
  state = conversationReducer(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'restore:r', batch: {
    protocol_version: 2, session_id: 'A', run_id: 'r', event_index: 0, events: [{ type: 'done' }],
  } });
  expect(state.viewsBySessionId.A.lastCompletedRunId).toBe('r');
  expect(state.viewsBySessionId.A.unreadCompletion).toBe(false);
});

it('never replaces an already-live run with stale persisted activity', () => {
  const initial = { ...initialConversationState, viewsBySessionId: { A: { ...createSessionView('A'), hydration: 'ready' as const } } };
  expect(conversationReducer(initial, { type: 'RESTORE_SESSION_ACTIVITY', activity: { sessionId: 'A', runId: 'old', status: 'streaming' } })).toBe(initial);
});

it('isolates corrupt records and tolerates unavailable storage', () => {
  localStorage.setItem(activityKey('user', 'bad'), '{');
  localStorage.setItem(activityKey('user', 'good'), JSON.stringify({ sessionId: 'good', completedRunId: 'r' }));
  expect(readSessionActivities('user')).toHaveLength(1);
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota'); });
  expect(() => saveSessionActivity('user', { ...createSessionView('A'), lastCompletedRunId: 'r' })).not.toThrow();
});

it('recovers legacy background identities without replacing a newer read marker', () => {
  sessionStorage.setItem('igraph-active-run:v2:user:A', JSON.stringify({ runId: 'old', lastAppliedEventIndex: 30 }));
  expect(readSessionActivities('user')).toEqual([{ sessionId: 'A', runId: 'old', status: 'starting', startRequestId: undefined }]);
  localStorage.setItem(activityKey('user', 'A'), JSON.stringify({ sessionId: 'A', completedRunId: 'new', readRunId: 'new' }));
  expect(readSessionActivities('user')).toEqual([{ sessionId: 'A', completedRunId: 'new', readRunId: 'new' }]);
  expect(readSessionActivities('other')).toEqual([]);
});

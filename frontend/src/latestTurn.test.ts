import { expect, it } from 'vitest';
import { conversationReducer, createSessionView, selectMessages, type ConversationState } from './sessionState';
import type { Message } from './types';

const history: Message[] = [
  { id: 'u1', run_id: 'r1', role: 'user', content: 'earlier' },
  { id: 'a1', run_id: 'r1', role: 'assistant', content: 'keep', run_status: 'completed' },
  { id: 'u2', run_id: 'r2', role: 'user', content: 'old' },
  { id: 'a2', run_id: 'r2', role: 'assistant', content: 'remove', run_status: 'cancelled' },
];
function initial(): ConversationState {
  return { activeSessionId: 's', activeDraftKey: null, draftsByScope: {}, viewsBySessionId: { s: {
    ...createSessionView('s'), hydration: 'ready', draft: 'preserve draft',
    messagesById: Object.fromEntries(history.map(message => [message.id!, message])), messageOrder: history.map(message => message.id!),
  } } };
}
const action = { type: 'START_RUN' as const, sessionId: 's', generation: 'g', replaceRunId: 'r2',
  userMessage: { id: 'user:g', role: 'user' as const, content: 'new' },
  assistantMessage: { id: 'assistant:g', role: 'assistant' as const, content: '', streaming: true } };

it('replaces only the tail, preserves the composer, and ignores a late old-run snapshot', () => {
  const state = conversationReducer(initial(), action);
  expect(selectMessages(state.viewsBySessionId.s).map(message => message.content)).toEqual(['earlier', 'keep', 'new', '']);
  expect(state.viewsBySessionId.s.draft).toBe('preserve draft');
  const merged = conversationReducer(state, { type: 'MERGE_RUN_SNAPSHOT', sessionId: 's', snapshot: {
    session_id: 's', run_id: 'r2', status: 'cancelled', snapshot_complete: true, messages: history,
    active_run: null, pending_feedback: null, resume_cursor: null,
  } });
  expect(merged).toBe(state);
});
it('restores the original tail when replacement is rejected and rejects stale edits locally', () => {
  const original = initial();
  expect(conversationReducer(original, { ...action, replaceRunId: 'r1' })).toBe(original);
  const started = conversationReducer(original, action);
  const restored = conversationReducer(started, { type: 'ROLLBACK_RUN_START', sessionId: 's', generation: 'g', restoreDraft: 'new' });
  expect(selectMessages(restored.viewsBySessionId.s)).toEqual(history);
  expect(restored.viewsBySessionId.s.draft).toBe('preserve draft');
});

it('restores a pre-launch cancelled edit exactly once without filling an empty composer', () => {
  const original = initial();
  original.viewsBySessionId.s.draft = '';
  let state = conversationReducer(original, action);
  state = conversationReducer(state, { type: 'SET_RUN_ID', sessionId: 's', generation: 'g', runId: 'new-run' });
  const control = { run_id: 'new-run', start_request_id: 'g', registration_status: 'registered' as const,
    run_status: 'cancelled', cancel_status: 'confirmed' as const, execution_stopped: true,
    terminal_persisted: true, native_run_created: false, can_send: true, revision: 2, active_run: null };
  for (let index = 0; index < 2; index++) {
    state = conversationReducer(state, { type: 'APPLY_CONTROL', sessionId: 's', generation: 'g', control });
    expect(selectMessages(state.viewsBySessionId.s)).toEqual(history);
    expect(state.viewsBySessionId.s.draft).toBe('');
  }
});

it('does not duplicate the old turn when a recovery snapshot arrives before cancellation confirmation', () => {
  let state = conversationReducer(initial(), action);
  state = conversationReducer(state, { type: 'SET_RUN_ID', sessionId: 's', generation: 'g', runId: 'new-run' });
  state = conversationReducer(state, { type: 'APPLY_SNAPSHOT', sessionId: 's', generation: 'g', snapshot: {
    session_id: 's', run_id: 'new-run', status: 'cancelled', snapshot_complete: true, messages: history,
    active_run: null, pending_feedback: null, resume_cursor: null,
    control: { run_id: 'new-run', start_request_id: 'g', registration_status: 'registered',
      run_status: 'cancelled', cancel_status: 'confirmed', execution_stopped: true,
      terminal_persisted: true, native_run_created: false, can_send: true, revision: 2, active_run: null },
  } });
  expect(selectMessages(state.viewsBySessionId.s)).toEqual(history);
});

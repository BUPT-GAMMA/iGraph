import { describe, expect, it } from 'vitest';
import { conversationReducer, initialConversationState } from './sessionState';

describe('graph exploration composer isolation', () => {
  it.each(['', 'unfinished question'])('keeps the original draft on a failed graph query: %s', draft => {
    let state = conversationReducer(initialConversationState, { type: 'ENSURE_SESSION', sessionId: 'A' });
    state = conversationReducer(state, { type: 'PATCH_VIEW', sessionId: 'A', patch: { draft } });
    state = conversationReducer(state, { type: 'START_RUN', sessionId: 'A', generation: 'query', preserveComposer: true,
      userMessage: { id: 'user:query', role: 'user', content: 'generated graph question' }, assistantMessage: { id: 'assistant:query', role: 'assistant', content: '' } });
    expect(state.viewsBySessionId.A.draft).toBe(draft);
    state = conversationReducer(state, { type: 'ROLLBACK_RUN_START', sessionId: 'A', generation: 'query', restoreDraft: 'generated graph question' });
    expect(state.viewsBySessionId.A.draft).toBe(draft);
    expect(state.viewsBySessionId.A.activeRun).toBeNull();
  });
});

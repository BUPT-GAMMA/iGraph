import { describe, expect, it } from 'vitest';
import { conversationReducer, initialConversationState, selectMessages, selectRunTools } from './sessionState';
import type { ConversationState } from './sessionState';
import type { StreamBatch, StreamEvent } from './types';

function session(state: ConversationState, sessionId: string) {
  return conversationReducer(state, { type: 'ENSURE_SESSION', sessionId, accessedAt: 1 });
}

function start(state: ConversationState, sessionId: string, generation: string) {
  return conversationReducer(state, {
    type: 'START_RUN', sessionId, generation,
    userMessage: { id: `user:${generation}`, role: 'user', content: 'question' },
    assistantMessage: { id: `assistant:${generation}`, role: 'assistant', content: '', streaming: true },
  });
}

function batch(sessionId: string, runId: string, eventIndex: number, events: StreamEvent[]): StreamBatch {
  return { protocol_version: 2, session_id: sessionId, run_id: runId, event_index: eventIndex, events };
}

function lastMessage(state: ConversationState, sessionId: string) {
  const messages = selectMessages(state.viewsBySessionId[sessionId]);
  return messages[messages.length - 1];
}

describe('conversationReducer session/run isolation', () => {
  it('only applies A tokens to session A', () => {
    let state = session(session(initialConversationState, 'A'), 'B');
    state = start(start(state, 'A', 'gen-a'), 'B', 'gen-b');
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen-a',
      batch: batch('A', 'run-a', 1, [{ type: 'token', message_id: 'assistant:run-a', content: 'A only' }]),
    });

    expect(lastMessage(state, 'A')?.content).toBe('A only');
    expect(lastMessage(state, 'B')?.content).toBe('');
  });

  it('ignores events from an older generation in the same session', () => {
    let state = session(initialConversationState, 'A');
    state = start(state, 'A', 'old');
    state = start(state, 'A', 'new');
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'old',
      batch: batch('A', 'old-run', 1, [{ type: 'token', content: 'stale' }]),
    });

    expect(lastMessage(state, 'A')?.content).toBe('');
  });

  it('deduplicates an entire v2 event_index batch', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'gen');
    const value = batch('A', 'run', 7, [{ type: 'token', message_id: 'assistant:run', content: 'hello' }]);
    state = conversationReducer(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen', batch: value });
    state = conversationReducer(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen', batch: value });

    expect(lastMessage(state, 'A')?.content).toBe('hello');
    expect(state.viewsBySessionId.A.activeRun?.lastEventIndex).toBe(7);
  });

  it('updates messages and tools by stable ids inside one atomic batch', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'gen');
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen',
      batch: batch('A', 'run', 2, [
        { type: 'token', message_id: 'assistant:run', content: 'answer' },
        { type: 'tool_call', tool: 'train', call_id: 'call-1', args: { epochs: 2 } },
        { type: 'plan_step', step: { step_id: 1, goal: 'train', status: 'done' } },
        { type: 'tool_result', tool: 'train', call_id: 'call-1', result: { success: true }, done: true },
      ]),
    });

    expect(state.viewsBySessionId.A.messageOrder).toContain('assistant:run');
    expect(selectMessages(state.viewsBySessionId.A).find(message => message.id === 'assistant:run')?.content).toBe('answer');
    expect(selectRunTools(state.viewsBySessionId.A)).toEqual([expect.objectContaining({ call_id: 'call-1', done: true })]);
    expect(state.viewsBySessionId.A.activeRun?.plan?.steps?.[0]?.goal).toBe('train');
  });

  it('does not reactivate a terminal run and ignores events after deletion', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'gen');
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen',
      batch: batch('A', 'run', 3, [{ type: 'done' }]),
    });
    state = conversationReducer(state, { type: 'SET_RUN_STATUS', sessionId: 'A', generation: 'gen', status: 'reconnecting' });
    expect(state.viewsBySessionId.A.activeRun?.status).toBe('completed');

    state = conversationReducer(state, { type: 'REMOVE_SESSION', sessionId: 'A' });
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen',
      batch: batch('A', 'run', 4, [{ type: 'token', content: 'late' }]),
    });
    expect(state.viewsBySessionId.A).toBeUndefined();
  });

  it('rejects stale graph responses by requestVersion', () => {
    let state = session(initialConversationState, 'A');
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_START', sessionId: 'A', requestVersion: 1 });
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_START', sessionId: 'A', requestVersion: 2 });
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_SUCCESS', sessionId: 'A', requestVersion: 1, graph: { nodes: [{ id: 'old' }], edges: [] } });
    expect(state.viewsBySessionId.A.graph).toBeNull();
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_SUCCESS', sessionId: 'A', requestVersion: 2, graph: { nodes: [{ id: 'new' }], edges: [] } });
    expect(state.viewsBySessionId.A.graph?.nodes[0]).toEqual({ id: 'new' });
  });

  it('rejects stale hydration responses and protects active/draft/running LRU entries', () => {
    let state = session(initialConversationState, 'A');
    state = conversationReducer(state, { type: 'HYDRATE_START', sessionId: 'A', requestVersion: 2 });
    state = conversationReducer(state, {
      type: 'HYDRATE_SUCCESS', sessionId: 'A', requestVersion: 1, messages: [{ role: 'assistant', content: 'stale' }], model: 'old',
    });
    expect(state.viewsBySessionId.A.hydration).toBe('loading');

    for (let index = 0; index < 11; index += 1) state = session(state, `S${index}`);
    state = conversationReducer(state, { type: 'PATCH_VIEW', sessionId: 'S0', patch: { draft: 'keep me' } });
    state = start(state, 'S1', 'running');
    state = conversationReducer(state, { type: 'SET_ACTIVE_SESSION', sessionId: 'S2', accessedAt: 99 });
    state = session(state, 'overflow');

    expect(Object.keys(state.viewsBySessionId)).toHaveLength(12);
    expect(state.viewsBySessionId.S0).toBeDefined();
    expect(state.viewsBySessionId.S1).toBeDefined();
    expect(state.viewsBySessionId.S2).toBeDefined();
  });
});

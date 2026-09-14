import { describe, expect, it } from 'vitest';
import { conversationReducer, initialConversationState, selectBottomInteraction, selectMessages, selectRunGate, selectRunTools } from './sessionState';
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
  it('shows a friendly notice for a cancelled run with no answer', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'g');
    state = conversationReducer(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'g',
      batch: batch('A', 'r', 0, [{ type: 'error', cancelled: true, error: 'Run r was cancelled' }]) });
    expect(lastMessage(state, 'A').content).toBe('已停止本轮对话');
    expect(state.viewsBySessionId.A.error).toBeUndefined();
    state = conversationReducer(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'g',
      batch: batch('A', 'r', 1, [{ type: 'done', content: 'Run r was cancelled' }]) });
    expect(lastMessage(state, 'A').content).toBe('已停止本轮对话');
  });
  it.each([false, true])('keeps an immediately stopped first message (run identity already known: %s)', known => {
    let state = start(session(initialConversationState, 'A'), 'A', 'g');
    if (known) state = conversationReducer(state, { type: 'SET_RUN_ID', sessionId: 'A', generation: 'g', runId: 'r' });
    state = conversationReducer(state, { type: 'REQUEST_CANCEL', sessionId: 'A', generation: 'g', cancelRequestId: 'c' });
    const control = { run_id: 'r', start_request_id: 'g', registration_status: 'registered' as const,
      run_status: 'cancelled', cancel_status: 'confirmed' as const, execution_stopped: true,
      terminal_persisted: true, native_run_created: false, can_send: true, revision: 2, active_run: null };
    for (let index = 0; index < 2; index++) {
      state = conversationReducer(state, { type: 'APPLY_CONTROL', sessionId: 'A', generation: 'g', control });
      expect(selectMessages(state.viewsBySessionId.A).map(message => message.content)).toEqual(['question', '已停止本轮对话']);
      expect(state.viewsBySessionId.A.draft).toBe('');
      expect(lastMessage(state, 'A').streaming).toBe(false);
      expect(selectRunGate(state.viewsBySessionId.A).canStartRun).toBe(true);
    }
    state = start(state, 'A', 'next');
    expect(selectMessages(state.viewsBySessionId.A).map(message => message.content)).toEqual(['question', '已停止本轮对话', 'question', '']);
  });
  it('still rolls back a send that fails before native execution', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'g');
    state = conversationReducer(state, { type: 'APPLY_CONTROL', sessionId: 'A', generation: 'g', control: {
      run_id: 'r', start_request_id: 'g', registration_status: 'registered', run_status: 'failed', cancel_status: 'none',
      execution_stopped: true, terminal_persisted: true, native_run_created: false, can_send: true, revision: 2, active_run: null,
    } });
    expect(selectMessages(state.viewsBySessionId.A)).toEqual([]);
    expect(state.viewsBySessionId.A.draft).toBe('question');
  });
  it('normalizes saved cancellation notices without changing user text or real errors', () => {
    const state = session(initialConversationState, 'A');
    const notice = 'Run c865e4a4-be91-4af2-b4d0-90fcf777b35f was cancelled';
    const view = { ...state.viewsBySessionId.A, messageOrder: ['u', 'a', 'e'], messagesById: {
      u: { id: 'u', role: 'user' as const, content: notice },
      a: { id: 'a', role: 'assistant' as const, content: notice },
      e: { id: 'e', role: 'assistant' as const, content: 'Connection failed' },
    } };
    expect(selectMessages(view).map(message => message.content)).toEqual([notice, '已停止本轮对话', 'Connection failed']);
  });
  it('does not unlock a cancelling preparation when its old upload fails', () => {
    let state = session(initialConversationState, 'A');
    state = conversationReducer(state, { type: 'BEGIN_RUN_PREPARATION', sessionId: 'A', generation: 'g' });
    state = conversationReducer(state, { type: 'REQUEST_CANCEL', sessionId: 'A', generation: 'g', cancelRequestId: 'c' });
    state = conversationReducer(state, { type: 'PREPARE_RUN_FAILED', sessionId: 'A', generation: 'g', error: 'late error' });
    state = conversationReducer(state, { type: 'ROLLBACK_RUN_START', sessionId: 'A', generation: 'g' });
    expect(state.viewsBySessionId.A.activeRun?.cancellation).toBe('requested');
    expect(selectRunGate(state.viewsBySessionId.A).canStartRun).toBe(false);
  });
  it('keeps cancellation terminal when native completion fills partial output', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'g');
    for (const [index, event] of [
      { type: 'token', content: 'A' },
      { type: 'error', cancelled: true, error: '用户终止了当前任务' },
      { type: 'done', content: 'AB' },
    ].entries()) {
      state = conversationReducer(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'g', batch: batch('A', 'r', index, [event]) });
    }
    expect(state.viewsBySessionId.A.activeRun?.status).toBe('cancelled');
    expect(lastMessage(state, 'A').content).toBe('AB');
    expect(state.viewsBySessionId.A.error).toBeUndefined();
  });
  it('keeps one local draft per project without creating a session view', () => {
    const draftA = { draftId: 'draft-a', projectId: 'project-a', phase: 'editing' as const, text: 'hello', model: 'model-a', activeDatasetId: null, attachments: [] };
    let state = conversationReducer(initialConversationState, { type: 'OPEN_DRAFT', draft: draftA });
    state = conversationReducer(state, { type: 'OPEN_DRAFT', draft: { ...draftA, draftId: 'replacement', text: '' } });

    expect(state.draftsByScope['project-a']).toEqual(draftA);
    expect(state.activeDraftKey).toBe('project-a');
    expect(state.activeSessionId).toBeNull();
    expect(state.viewsBySessionId).toEqual({});
  });

  it('isolates drafts from different projects and switches without losing either one', () => {
    let state = conversationReducer(initialConversationState, { type: 'OPEN_DRAFT', draft: { draftId: 'A', projectId: 'project-a', phase: 'editing', text: 'A text', model: 'm', activeDatasetId: null, attachments: [] } });
    state = conversationReducer(state, { type: 'OPEN_DRAFT', draft: { draftId: 'B', projectId: 'project-b', phase: 'editing', text: 'B text', model: 'm', activeDatasetId: null, attachments: [] } });

    expect(state.activeDraftKey).toBe('project-b');
    expect(state.draftsByScope['project-a'].text).toBe('A text');
    expect(state.draftsByScope['project-b'].text).toBe('B text');
  });

  it('materializes a draft atomically into one preparing session', () => {
    let state = conversationReducer(initialConversationState, {
      type: 'OPEN_DRAFT',
      draft: {
        draftId: 'session-new', projectId: 'project-a', phase: 'editing', text: 'first turn', model: 'm', activeDatasetId: 'dataset-a',
        attachments: [
          { localId: 'uploaded', filename: 'ready.txt', fileToken: 'token-1', status: 'uploaded' },
          { localId: 'pending', filename: 'local.txt', status: 'staged' },
        ],
      },
    });
    state = conversationReducer(state, { type: 'MATERIALIZE_DRAFT', projectId: 'project-a', draftId: 'session-new', sessionId: 'session-new', generation: 'gen', model: 'm', activeDatasetId: 'dataset-a' });

    expect(state.draftsByScope['project-a']).toBeUndefined();
    expect(state.activeDraftKey).toBeNull();
    expect(state.activeSessionId).toBe('session-new');
    expect(state.viewsBySessionId['session-new']).toEqual(expect.objectContaining({
      draft: 'first turn',
      attachments: [{ file_token: 'token-1', filename: 'ready.txt' }],
      pendingAttachments: [expect.objectContaining({ localId: 'pending' })],
      activeRun: expect.objectContaining({ generation: 'gen', status: 'preparing' }),
    }));
    const unchanged = start(state, 'session-new', 'duplicate');
    expect(unchanged).toBe(state);
  });

  it('only applies A tokens to session A', () => {
    let state = session(session(initialConversationState, 'A'), 'B');
    state = start(start(state, 'A', 'gen-a'), 'B', 'gen-b');
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen-a',
      batch: batch('A', 'run-a', 0, [{ type: 'token', message_id: 'assistant:run-a', content: 'A only' }]),
    });

    expect(lastMessage(state, 'A')?.content).toBe('A only');
    expect(lastMessage(state, 'B')?.content).toBe('');
  });

  it('adopts an authoritative run id without discarding live tools or messages', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'gen');
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen',
      batch: batch('A', '', 0, [{ type: 'tool_call', tool: 'degree_analysis', call_id: 'call-1' }]),
    });
    const messageOrder = state.viewsBySessionId.A.messageOrder;

    state = conversationReducer(state, {
      type: 'SET_RUN_ID', sessionId: 'A', generation: 'gen', runId: 'run-authoritative', lastAppliedEventIndex: 4,
    });

    expect(state.viewsBySessionId.A.activeRun?.runId).toBe('run-authoritative');
    expect(state.viewsBySessionId.A.activeRun?.lastAppliedEventIndex).toBe(0);
    expect(state.viewsBySessionId.A.activeRun?.toolOrder).toEqual(['call-1']);
    expect(state.viewsBySessionId.A.messageOrder).toEqual(messageOrder);
  });

  it('ignores events from an older generation in the same session', () => {
    let state = session(initialConversationState, 'A');
    state = start(state, 'A', 'old');
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'old',
      batch: batch('A', 'old-run', 0, [{ type: 'done' }]),
    });
    state = start(state, 'A', 'new');
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'old',
      batch: batch('A', 'old-run', 0, [{ type: 'token', content: 'stale' }]),
    });

    expect(lastMessage(state, 'A')?.content).toBe('');
  });

  it('blocks START_RUN in every active state without touching draft, attachments, or messages', () => {
    const statuses = ['awaiting_feedback', 'submitting_feedback', 'streaming', 'starting'] as const;
    for (const status of statuses) {
      let state = start(session(initialConversationState, 'A'), 'A', 'gen');
      state = conversationReducer(state, { type: 'SET_RUN_STATUS', sessionId: 'A', generation: 'gen', status });
      state = conversationReducer(state, {
        type: 'PATCH_VIEW', sessionId: 'A',
        patch: { draft: 'next question', attachments: [{ file_token: 'file-1', filename: 'next.txt' }] },
      });
      const before = state.viewsBySessionId.A;
      state = start(state, 'A', 'replacement');

      expect(state.viewsBySessionId.A).toBe(before);
      expect(selectRunGate(state.viewsBySessionId.A).canStartRun).toBe(false);
      expect(state.viewsBySessionId.A.draft).toBe('next question');
      expect(state.viewsBySessionId.A.attachments).toHaveLength(1);
      expect(selectMessages(state.viewsBySessionId.A)).toHaveLength(2);
    }
  });

  it('submits feedback only for the current run/call and clears it after acceptance', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'gen');
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen',
      batch: batch('A', 'run', 0, [{
        type: 'await_user_feedback',
        request: { run_id: 'run', request_id: 'run', call_id: 'call-1', tool_name: 'train', proposed_args: { epochs: 2 } },
      }]),
    });
    const waiting = state;
    state = conversationReducer(state, {
      type: 'BEGIN_FEEDBACK_SUBMISSION', sessionId: 'A', generation: 'gen', runId: 'run', callId: 'stale-call', decisionId: 'bad', decision: 'approve',
    });
    expect(state).toBe(waiting);

    state = conversationReducer(state, {
      type: 'BEGIN_FEEDBACK_SUBMISSION', sessionId: 'A', generation: 'gen', runId: 'run', callId: 'call-1', decisionId: 'decision-1', decision: 'approve',
    });
    expect(state.viewsBySessionId.A.activeRun?.status).toBe('submitting_feedback');
    expect(state.viewsBySessionId.A.activeRun?.feedback?.call_id).toBe('call-1');

    const submitting = state;
    state = conversationReducer(state, {
      type: 'BEGIN_FEEDBACK_SUBMISSION', sessionId: 'A', generation: 'gen', runId: 'run', callId: 'call-1', decisionId: 'decision-2', decision: 'reject',
    });
    expect(state).toBe(submitting);

    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen',
      batch: { protocol_version: 2, session_id: 'A', run_id: 'run', events: [{ type: 'feedback_accepted', decision_id: 'decision-1' }] },
    });
    expect(state.viewsBySessionId.A.activeRun?.status).toBe('streaming');
    expect(state.viewsBySessionId.A.activeRun?.feedback).toBeNull();
    expect(state.viewsBySessionId.A.activeRun?.feedbackSubmission).toBeUndefined();
  });

  it('derives a safe bottom interaction from authoritative Run state', () => {
    let state = session(initialConversationState, 'A');
    expect(selectBottomInteraction(state.viewsBySessionId.A)).toEqual({ kind: 'composer' });
    const hydrating = conversationReducer(state, { type: 'HYDRATE_START', sessionId: 'A', requestVersion: 1 });
    expect(selectBottomInteraction(hydrating.viewsBySessionId.A)).toEqual({ kind: 'syncing_feedback', reason: 'session_hydration' });

    state = start(state, 'A', 'gen');
    state = conversationReducer(state, { type: 'SET_RUN_STATUS', sessionId: 'A', generation: 'gen', status: 'awaiting_feedback' });
    expect(selectBottomInteraction(state.viewsBySessionId.A)).toEqual({ kind: 'syncing_feedback', reason: 'missing_feedback' });

    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen',
      batch: batch('A', 'run', 0, [{
        type: 'await_user_feedback',
        request: { run_id: 'run', call_id: 'call-1', tool_name: 'train' },
      }]),
    });
    expect(selectBottomInteraction(state.viewsBySessionId.A)).toEqual(expect.objectContaining({ kind: 'approval', phase: 'awaiting' }));

    state = conversationReducer(state, {
      type: 'BEGIN_FEEDBACK_SUBMISSION', sessionId: 'A', generation: 'gen', runId: 'run', callId: 'call-1', decisionId: 'decision-1', decision: 'approve',
    });
    expect(selectBottomInteraction(state.viewsBySessionId.A)).toEqual(expect.objectContaining({ kind: 'approval', phase: 'submitting' }));
  });

  it('restores a blocked optimistic send without overwriting a newer draft', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'gen');
    state = conversationReducer(state, { type: 'PATCH_VIEW', sessionId: 'A', patch: { draft: 'newer draft' } });
    state = conversationReducer(state, {
      type: 'ROLLBACK_RUN_START', sessionId: 'A', generation: 'gen', restoreDraft: 'original',
      restoreAttachments: [{ file_token: 'original-file', filename: 'graph.txt' }],
    });
    expect(state.viewsBySessionId.A.draft).toBe('newer draft');
    expect(state.viewsBySessionId.A.attachments).toEqual([{ file_token: 'original-file', filename: 'graph.txt' }]);
    expect(selectMessages(state.viewsBySessionId.A)).toEqual([]);
  });

  it('deduplicates an entire v2 event_index batch', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'gen');
    const value = batch('A', 'run', 0, [{ type: 'token', message_id: 'assistant:run', content: 'hello' }]);
    state = conversationReducer(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen', batch: value });
    state = conversationReducer(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen', batch: value });

    expect(lastMessage(state, 'A')?.content).toBe('hello');
    expect(state.viewsBySessionId.A.activeRun?.lastAppliedEventIndex).toBe(0);
  });

  it('updates messages and tools by stable ids inside one atomic batch', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'gen');
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen',
      batch: batch('A', 'run', 0, [
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
      batch: batch('A', 'run', 0, [{ type: 'done' }]),
    });
    state = conversationReducer(state, { type: 'SET_RUN_STATUS', sessionId: 'A', generation: 'gen', status: 'streaming' });
    expect(state.viewsBySessionId.A.activeRun?.status).toBe('completed');

    state = conversationReducer(state, { type: 'REMOVE_SESSION', sessionId: 'A' });
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen',
      batch: batch('A', 'run', 0, [{ type: 'token', content: 'late' }]),
    });
    expect(state.viewsBySessionId.A).toBeUndefined();
  });

  it('rejects stale graph responses by requestVersion', () => {
    let state = session(initialConversationState, 'A');
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_START', sessionId: 'A', requestVersion: 1 });
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_START', sessionId: 'A', requestVersion: 2 });
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_SUCCESS', sessionId: 'A', requestVersion: 1, graph: { status: 'ready', data: { nodes: [{ id: 'old' }], edges: [] } } });
    expect(state.viewsBySessionId.A.graph.status).toBe('loading');
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_SUCCESS', sessionId: 'A', requestVersion: 2, graph: { status: 'ready', data: { nodes: [{ id: 'new' }], edges: [] } } });
    expect(state.viewsBySessionId.A.graph.status === 'ready' ? state.viewsBySessionId.A.graph.data.nodes[0] : null).toEqual({ id: 'new' });
  });

  it('keeps the current graph visible during a background refresh', () => {
    let state = session(initialConversationState, 'A');
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_START', sessionId: 'A', requestVersion: 1 });
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_SUCCESS', sessionId: 'A', requestVersion: 1, graph: { status: 'ready', data: { nodes: [{ id: 'current' }], edges: [] } } });
    state = conversationReducer(state, { type: 'PATCH_VIEW', sessionId: 'A', patch: { selectedNode: { id: 'current' } } });

    state = conversationReducer(state, { type: 'GRAPH_REQUEST_START', sessionId: 'A', requestVersion: 2, preserveCurrent: true });

    expect(state.viewsBySessionId.A.graph).toEqual({ status: 'ready', data: { nodes: [{ id: 'current' }], edges: [] } });
    expect(state.viewsBySessionId.A.selectedNode).toEqual({ id: 'current' });
    expect(state.viewsBySessionId.A.graphRequestVersion).toBe(2);
  });

  it('switches atomically to unsupported and rejects an older graph response', () => {
    let state = session(initialConversationState, 'A');
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_START', sessionId: 'A', requestVersion: 1 });
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_SUCCESS', sessionId: 'A', requestVersion: 1, graph: { status: 'ready', data: { nodes: [{ id: 'cora' }], edges: [] } } });
    state = conversationReducer(state, { type: 'PATCH_VIEW', sessionId: 'A', patch: { selectedNode: { id: 'cora' }, activeDatasetId: 'cora' } });
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_START', sessionId: 'A', requestVersion: 2 });
    state = conversationReducer(state, {
      type: 'GRAPH_REQUEST_SUCCESS', sessionId: 'A', requestVersion: 2,
      graph: { status: 'unsupported', data: null, code: 'NON_SINGLE_GRAPH', summary: { num_graphs: 41127 } },
    });
    state = conversationReducer(state, { type: 'GRAPH_REQUEST_SUCCESS', sessionId: 'A', requestVersion: 1, graph: { status: 'ready', data: { nodes: [{ id: 'stale' }], edges: [] } } });

    state = start(state, 'A', 'collection-run');
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'collection-run',
      batch: batch('A', 'collection-run-id', 3, [{ type: 'graph_visualize', data: { success: true, nodes: [{ id: 'sample' }], edges: [] } }]),
    });

    expect(state.viewsBySessionId.A.graph).toEqual(expect.objectContaining({ status: 'unsupported', summary: { num_graphs: 41127 } }));
    expect(state.viewsBySessionId.A.selectedNode).toBeNull();
  });

  it('keeps visualization metadata when the full graph refresh completes', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'gen');
    state = conversationReducer(state, {
      type: 'APPLY_BATCH', sessionId: 'A', generation: 'gen',
      batch: batch('A', 'run', 0, [{
        type: 'graph_visualize',
        data: {
          nodes: [{ id: 'a' }, { id: 'b' }],
          edges: [{ src: 'a', dst: 'b' }],
          community: { num_communities: 2, node_community: { a: 0, b: 1 } },
          highlights: { paths: [{ node_sequence: ['a', 'b'], color: '#f59e0b' }] },
        },
      }]),
    });

    expect(state.viewsBySessionId.A.graph?.data?.community?.node_community).toEqual({ a: 0, b: 1 });
    expect(state.viewsBySessionId.A.graph?.data?.highlights).toEqual({ paths: [{ node_sequence: ['a', 'b'], color: '#f59e0b' }] });

    state = conversationReducer(state, { type: 'GRAPH_REQUEST_START', sessionId: 'A', requestVersion: 1 });
    state = conversationReducer(state, {
      type: 'GRAPH_REQUEST_SUCCESS', sessionId: 'A', requestVersion: 1,
      graph: {
        status: 'ready', data: {
          nodes: [{ id: 'a' }, { id: 'b' }, { id: 'c' }],
          edges: [{ src: 'a', dst: 'b' }],
          community: { num_communities: 2, node_community: { a: 1, b: 0, c: 1 } },
          highlights: null,
        },
      },
    });

    expect(state.viewsBySessionId.A.graph?.data?.nodes).toHaveLength(3);
    expect(state.viewsBySessionId.A.graph?.data?.communities?.node_community).toEqual({ a: 1, b: 0, c: 1 });
    expect(state.viewsBySessionId.A.graph?.data?.highlights).toEqual({ paths: [{ node_sequence: ['a', 'b'], color: '#f59e0b' }] });
  });

  it('restores the latest graph artifact when hydrating a session', () => {
    let state = session(initialConversationState, 'A');
    state = conversationReducer(state, { type: 'HYDRATE_START', sessionId: 'A', requestVersion: 1 });
    state = conversationReducer(state, {
      type: 'HYDRATE_SUCCESS', sessionId: 'A', requestVersion: 1, model: 'qwen3.6-plus',
      messages: [{
        role: 'assistant', content: 'result',
        graphs: [{
          nodes: [{ id: 'a' }, { id: 'b' }],
          links: [{ source: 'a', target: 'b' }],
          communities: { num_communities: 2, node_community: { a: 0, b: 1 } },
          highlights: [{ type: 'node', nodes: ['a'], color: '#22c55e' }],
        }],
      }],
    });

    expect(state.viewsBySessionId.A.graph?.data?.edges?.[0]).toEqual(expect.objectContaining({ src: 'a', dst: 'b' }));
    expect(state.viewsBySessionId.A.graph?.data?.community?.node_community).toEqual({ a: 0, b: 1 });
    expect(state.viewsBySessionId.A.graph?.data?.highlights).toEqual([{ type: 'node', nodes: ['a'], color: '#22c55e' }]);
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

describe('background job progress', () => {
  it('keeps same-name calls separate and replays epoch frames exactly once', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'g');
    const apply = (index: number, events: StreamEvent[]) => {
      state = conversationReducer(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'g', batch: batch('A', 'r', index, events) });
    };
    apply(0, [
      { type: 'tool_call', tool: 'train', call_id: 'first', args: {} },
      { type: 'tool_call', tool: 'train', call_id: 'second', args: {} },
    ]);
    const progress: StreamEvent = { type: 'tool_progress', tool: 'train', call_id: 'first',
      progress: { kind: 'train_epoch', epoch: 1, val_acc: 0.5, job_id: 'job-first', progress_seq: 1, status: 'running' } };
    apply(1, [progress]);
    apply(1, [progress]);
    apply(2, [{ type: 'tool_progress', tool: 'train', call_id: 'second',
      progress: { kind: 'train_epoch', epoch: 1, val_acc: 0.9, job_id: 'job-second', progress_seq: 1, status: 'running' } }]);
    const tools = state.viewsBySessionId.A.activeRun!.toolsById;
    expect(tools.first.history).toHaveLength(1);
    expect(tools.first.history?.[0].val_acc).toBe(0.5);
    expect(tools.second.history?.[0].val_acc).toBe(0.9);
    expect(tools.first.done).toBe(false);
  });

  it('waits for the job terminal event after early stopping and retains the curve', () => {
    let state = start(session(initialConversationState, 'A'), 'A', 'g');
    const events: StreamEvent[][] = [
      [{ type: 'tool_call', tool: 'train', call_id: 'train', args: {} }],
      [{ type: 'tool_progress', tool: 'train', call_id: 'train', progress: { kind: 'train_epoch', epoch: 1, val_acc: .7 } }],
      [{ type: 'tool_progress', tool: 'train', call_id: 'train', progress: { kind: 'train_early_stop', status: 'running' } }],
      [{ type: 'tool_progress', tool: 'train', call_id: 'train', progress: { kind: 'train_done', status: 'done', progress: 1 } },
       { type: 'tool_result', tool: 'train', call_id: 'train', result: { success: true, model_id: 'm' }, done: true }],
    ];
    for (const [index, value] of events.entries()) {
      state = conversationReducer(state, { type: 'APPLY_BATCH', sessionId: 'A', generation: 'g', batch: batch('A', 'r', index, value) });
      if (index === 2) expect(state.viewsBySessionId.A.activeRun!.toolsById.train.done).toBe(false);
    }
    const tool = state.viewsBySessionId.A.activeRun!.toolsById.train;
    expect(tool.done).toBe(true);
    expect(tool.history).toHaveLength(1);
    expect(tool.result).toEqual({ success: true, model_id: 'm' });
  });
});

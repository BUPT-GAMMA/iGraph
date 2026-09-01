import { isGraphData } from './types';
import type { ActiveRun, AttachmentRef, DatasetView, DraftAttachment, FeedbackRequest, GraphViewState, Message, NewConversationDraft, Plan, StreamBatch, StreamEvent, ToolCall } from './types';

export type RunStatus =
  | 'idle'
  | 'preparing'
  | 'starting'
  | 'streaming'
  | 'awaiting_feedback'
  | 'submitting_feedback'
  | 'reconnecting'
  | 'detached'
  | 'completed'
  | 'failed'
  | 'cancelled';

export type SessionRunState = {
  runId?: string;
  generation: string;
  status: RunStatus;
  lastEventIndex: number;
  assistantMessageId?: string;
  toolsById: Record<string, ToolCall>;
  toolOrder: string[];
  plan: Plan | null;
  feedback: FeedbackRequest | null;
  feedbackSubmission?: {
    decisionId: string;
    callId: string;
    decision: 'approve' | 'reject';
  };
  error?: string;
};

export type SessionViewState = {
  sessionId: string;
  hydration: 'unloaded' | 'loading' | 'ready' | 'failed';
  hydrationRequestVersion: number;
  messagesById: Record<string, Message>;
  messageOrder: string[];
  graph: GraphViewState;
  graphRequestVersion: number;
  selectedNode: Record<string, unknown> | null;
  draft: string;
  attachments: AttachmentRef[];
  pendingAttachments: DraftAttachment[];
  uploading: boolean;
  model: string;
  activeDatasetId?: string | null;
  error?: string;
  activeRun: SessionRunState | null;
  lastAccessedAt: number;
};

export type ConversationState = {
  activeSessionId: string | null;
  activeDraftProjectId: string | null;
  viewsBySessionId: Record<string, SessionViewState>;
  draftsByProjectId: Record<string, NewConversationDraft>;
};

export type ConversationAction =
  | { type: 'ENSURE_SESSION'; sessionId: string; model?: string; accessedAt?: number }
  | { type: 'SET_ACTIVE_SESSION'; sessionId: string | null; model?: string; accessedAt?: number }
  | { type: 'OPEN_DRAFT'; draft: NewConversationDraft }
  | { type: 'PATCH_DRAFT'; projectId: string; draftId: string; patch: Partial<Pick<NewConversationDraft, 'phase' | 'text' | 'model' | 'activeDatasetId' | 'attachments' | 'error'>> }
  | { type: 'REMOVE_DRAFT'; projectId: string; draftId?: string }
  | { type: 'MATERIALIZE_DRAFT'; projectId: string; draftId: string; sessionId: string; generation: string; model: string; activeDatasetId?: string | null; datasetView?: DatasetView | null; accessedAt?: number }
  | { type: 'HYDRATE_START'; sessionId: string; requestVersion: number }
  | {
      type: 'HYDRATE_SUCCESS'; sessionId: string; requestVersion: number; messages: Message[];
      model: string; activeDatasetId?: string | null; feedback?: FeedbackRequest | null;
      activeRun?: ActiveRun | null; generation?: string; preserveMessages?: boolean;
      datasetView?: DatasetView | null; hasGraph?: boolean;
    }
  | { type: 'HYDRATE_FAILED'; sessionId: string; requestVersion: number; error: string }
  | { type: 'PATCH_VIEW'; sessionId: string; patch: Partial<Pick<SessionViewState, 'draft' | 'attachments' | 'pendingAttachments' | 'uploading' | 'model' | 'activeDatasetId' | 'error' | 'selectedNode'>> }
  | { type: 'APPEND_MESSAGES'; sessionId: string; messages: Message[] }
  | { type: 'BEGIN_RUN_PREPARATION'; sessionId: string; generation: string }
  | { type: 'START_PREPARED_RUN'; sessionId: string; generation: string; userMessage: Message; assistantMessage: Message }
  | { type: 'PREPARE_RUN_FAILED'; sessionId: string; generation: string; error: string }
  | { type: 'SET_PENDING_ATTACHMENT'; sessionId: string; localId: string; status: DraftAttachment['status']; error?: string }
  | { type: 'RESOLVE_PENDING_ATTACHMENT'; sessionId: string; localId: string; attachment: AttachmentRef }
  | { type: 'REMOVE_PENDING_ATTACHMENT'; sessionId: string; localId: string }
  | { type: 'START_RUN'; sessionId: string; generation: string; userMessage: Message; assistantMessage: Message }
  | { type: 'ROLLBACK_RUN_START'; sessionId: string; generation: string; restoreDraft?: string; restoreAttachments?: AttachmentRef[] }
  | { type: 'ADOPT_RUN'; sessionId: string; generation: string; activeRun: ActiveRun; feedback?: FeedbackRequest | null }
  | { type: 'BEGIN_FEEDBACK_SUBMISSION'; sessionId: string; generation: string; runId: string; callId: string; decisionId: string; decision: 'approve' | 'reject' }
  | { type: 'CLEAR_ACTIVE_RUN'; sessionId: string; generation: string }
  | { type: 'APPLY_BATCH'; sessionId: string; generation: string; batch: StreamBatch }
  | { type: 'SET_RUN_STATUS'; sessionId: string; generation: string; status: RunStatus; error?: string }
  | { type: 'GRAPH_REQUEST_START'; sessionId: string; requestVersion: number }
  | { type: 'GRAPH_REQUEST_SUCCESS'; sessionId: string; requestVersion: number; graph: GraphViewState }
  | { type: 'REMOVE_SESSION'; sessionId: string };

export const SESSION_VIEW_CACHE_LIMIT = 12;
const PROTECTED_RUN_STATUSES = new Set<RunStatus>(['preparing', 'starting', 'streaming', 'awaiting_feedback', 'submitting_feedback', 'reconnecting', 'detached']);
const TERMINAL_RUN_STATUSES = new Set<RunStatus>(['completed', 'failed', 'cancelled']);

export type RunGateReason = 'preparing' | 'run_active' | 'awaiting_feedback' | 'submitting_feedback' | 'reconnecting' | 'detached' | null;

export type BottomInteraction =
  | { kind: 'composer' }
  | { kind: 'approval'; feedback: FeedbackRequest; phase: 'awaiting' | 'submitting' }
  | { kind: 'syncing_feedback'; reason: 'session_hydration' | 'missing_feedback' };

export function selectRunGate(view?: SessionViewState | null) {
  const status = view?.activeRun?.status;
  const reason: RunGateReason = status === 'preparing'
    ? 'preparing'
    : status === 'awaiting_feedback'
    ? 'awaiting_feedback'
    : status === 'submitting_feedback'
      ? 'submitting_feedback'
      : status === 'reconnecting'
        ? 'reconnecting'
        : status === 'detached'
          ? 'detached'
          : status === 'starting' || status === 'streaming'
            ? 'run_active'
            : null;
  return {
    reason,
    canStartRun: reason === null,
    canSubmitFeedback: status === 'awaiting_feedback' && Boolean(view?.activeRun?.feedback),
    blocksSessionMutation: reason !== null,
  };
}

export function selectBottomInteraction(view?: SessionViewState | null): BottomInteraction {
  const run = view?.activeRun;
  if (run?.status === 'awaiting_feedback' || run?.status === 'submitting_feedback') {
    if (!run.feedback) return { kind: 'syncing_feedback', reason: 'missing_feedback' };
    return {
      kind: 'approval',
      feedback: run.feedback,
      phase: run.status === 'submitting_feedback' ? 'submitting' : 'awaiting',
    };
  }
  if (view?.hydration === 'loading') return { kind: 'syncing_feedback', reason: 'session_hydration' };
  return { kind: 'composer' };
}

export const initialConversationState: ConversationState = { activeSessionId: null, activeDraftProjectId: null, viewsBySessionId: {}, draftsByProjectId: {} };

export function createSessionView(sessionId: string, model = 'qwen3.6-plus', accessedAt = 0): SessionViewState {
  return {
    sessionId,
    hydration: 'unloaded',
    hydrationRequestVersion: 0,
    messagesById: {},
    messageOrder: [],
    graph: { status: 'empty', data: null },
    graphRequestVersion: 0,
    selectedNode: null,
    draft: '',
    attachments: [],
    pendingAttachments: [],
    uploading: false,
    model,
    activeDatasetId: null,
    activeRun: null,
    lastAccessedAt: accessedAt,
  };
}

function stableMessageId(sessionId: string, message: Message, index: number) {
  return String(message.id || `${sessionId}:history:${index}`);
}

function indexMessages(sessionId: string, messages: Message[]) {
  const messagesById: Record<string, Message> = {};
  const messageOrder: string[] = [];
  messages.forEach((message, index) => {
    const id = stableMessageId(sessionId, message, index);
    messagesById[id] = { ...message, id };
    messageOrder.push(id);
  });
  return { messagesById, messageOrder };
}

export function selectMessages(view?: SessionViewState | null): Message[] {
  if (!view) return [];
  return view.messageOrder.map(id => view.messagesById[id]).filter(Boolean);
}

export function selectRunTools(view?: SessionViewState | null): ToolCall[] {
  const run = view?.activeRun;
  if (!run) return [];
  return run.toolOrder.map(id => run.toolsById[id]).filter(Boolean);
}

function appendMessages(view: SessionViewState, messages: Message[]) {
  const messagesById = { ...view.messagesById };
  const messageOrder = [...view.messageOrder];
  for (const message of messages) {
    let id = String(message.id || `${view.sessionId}:local:${messageOrder.length}`);
    while (messagesById[id]) id = `${id}:next`;
    messagesById[id] = { ...message, id };
    messageOrder.push(id);
  }
  return { ...view, messagesById, messageOrder };
}

function decodedToolResult(value: unknown) {
  if (typeof value !== 'string') return value;
  try { return JSON.parse(value); } catch { return value; }
}

function appendTrainingProgress(history: Array<Record<string, unknown>> = [], progress: Record<string, unknown>) {
  if (progress.kind !== 'train_epoch' || !Number.isFinite(Number(progress.epoch))) return history;
  const epoch = Number(progress.epoch);
  return [...history.filter(item => Number(item.epoch) !== epoch), progress]
    .sort((left, right) => Number(left.epoch) - Number(right.epoch));
}

function mergeTrainingHistory(history: Array<Record<string, unknown>> = [], updates: unknown) {
  if (!Array.isArray(updates)) return history;
  return updates.reduce<Array<Record<string, unknown>>>((current, update) => (
    update && typeof update === 'object' && !Array.isArray(update)
      ? appendTrainingProgress(current, update as Record<string, unknown>)
      : current
  ), history);
}

function normalizeFeedbackRequest(event: StreamEvent): FeedbackRequest | null {
  const raw = event.request || event.payload?.request || event.payload || {};
  if (!raw || typeof raw !== 'object') return null;
  const request = raw as Record<string, any>;
  const proposedArgs = request.proposed_args ?? request.tool_args ?? request.arguments ?? {};
  const runId = String(request.run_id ?? request.runId ?? request.request_id ?? request.requestId ?? event.run_id ?? '');
  return {
    run_id: runId,
    request_id: String(request.request_id ?? request.requestId ?? runId),
    call_id: String(request.call_id ?? request.callId ?? request.tool_call_id ?? request.toolCallId ?? ''),
    tool_name: String(request.tool_name ?? request.toolName ?? request.proposed_tool ?? request.name ?? ''),
    proposed_args: proposedArgs && typeof proposedArgs === 'object' && !Array.isArray(proposedArgs) ? proposedArgs : {},
    description: String(request.description ?? request.message ?? request.user_message ?? '该工具调用需要确认'),
  };
}

function renameAssistantMessage(view: SessionViewState, run: SessionRunState, nextId: string) {
  const previousId = run.assistantMessageId;
  if (!previousId || previousId === nextId || !view.messagesById[previousId] || view.messagesById[nextId]) {
    return { view, run: { ...run, assistantMessageId: nextId } };
  }
  const messagesById = { ...view.messagesById, [nextId]: { ...view.messagesById[previousId], id: nextId } };
  delete messagesById[previousId];
  return {
    view: { ...view, messagesById, messageOrder: view.messageOrder.map(id => id === previousId ? nextId : id) },
    run: { ...run, assistantMessageId: nextId },
  };
}

function applyStreamEvent(view: SessionViewState, run: SessionRunState, event: StreamEvent): { view: SessionViewState; run: SessionRunState } {
  const eventType = String(event.type || '');
  const eventName = String(event.type || event.event || '').toLowerCase();
  if (eventType === 'feedback_submit_error') {
    if (run.feedbackSubmission && event.decision_id && run.feedbackSubmission.decisionId !== event.decision_id) return { view, run };
    const error = String(event.error || '确认提交失败，正在核对 Run 状态');
    return { view: { ...view, error }, run: { ...run, error } };
  }
  if (eventType === 'feedback_accepted') {
    if (run.feedbackSubmission && event.decision_id && run.feedbackSubmission.decisionId !== event.decision_id) return { view, run };
    return {
      view,
      run: { ...run, status: 'streaming', feedback: null, feedbackSubmission: undefined, error: undefined },
    };
  }
  if (eventType === 'stream_status') {
    return { view, run: { ...run, status: run.status === 'reconnecting' ? 'streaming' : run.status } };
  }

  if (run.status === 'submitting_feedback') {
    run = { ...run, status: 'streaming', feedback: null, feedbackSubmission: undefined, error: undefined };
  }

  if (eventType === 'token') {
    const requestedId = String(event.message_id || run.assistantMessageId || `assistant:${run.runId || run.generation}`);
    const renamed = renameAssistantMessage(view, run, requestedId);
    view = renamed.view;
    run = renamed.run;
    const current = view.messagesById[requestedId] || { id: requestedId, role: 'assistant' as const, content: '' };
    view = {
      ...view,
      messagesById: {
        ...view.messagesById,
        [requestedId]: { ...current, content: current.content + String(event.content || ''), streaming: true },
      },
      messageOrder: view.messageOrder.includes(requestedId) ? view.messageOrder : [...view.messageOrder, requestedId],
    };
    run = { ...run, status: 'streaming' };
  }

  if (eventType === 'tool_call') {
    const callId = String(event.call_id || `tool:${event.tool || event.name || 'unknown'}:${run.toolOrder.length}`);
    const exists = Boolean(run.toolsById[callId]);
    run = {
      ...run,
      status: 'streaming',
      toolsById: {
        ...run.toolsById,
        [callId]: {
          ...(run.toolsById[callId] || {}),
          tool: event.tool || event.name,
          call_id: callId,
          args: event.args ?? event.arguments ?? {},
          done: false,
        },
      },
      toolOrder: exists ? run.toolOrder : [...run.toolOrder, callId],
    };
  }

  if (eventType === 'tool_result' || eventType === 'tool_progress') {
    const exactId = String(event.call_id || '');
    const fallbackId = [...run.toolOrder].reverse().find(id => run.toolsById[id]?.tool === String(event.tool || ''));
    const callId = exactId && run.toolsById[exactId] ? exactId : fallbackId;
    if (callId) {
      const tool = run.toolsById[callId];
      if (eventType === 'tool_result') {
        const result = decodedToolResult(event.result);
        const resultHistory = result && typeof result === 'object' && Array.isArray((result as any).history) ? (result as any).history : null;
        run = {
          ...run,
          toolsById: {
            ...run.toolsById,
            [callId]: { ...tool, result, history: mergeTrainingHistory(tool.history, resultHistory), done: event.done !== false },
          },
        };
      } else {
        const progress = event.progress && typeof event.progress === 'object' ? event.progress as Record<string, unknown> : {};
        const terminal = progress.kind === 'train_done' || progress.kind === 'train_early_stop';
        run = {
          ...run,
          toolsById: {
            ...run.toolsById,
            [callId]: { ...tool, progress, history: appendTrainingProgress(tool.history, progress), done: Boolean(tool.done || terminal) },
          },
        };
      }
    }
  }

  if (eventType === 'plan_analyzing') run = { ...run, plan: { completed: false, steps: [] } };
  if (eventType === 'plan_ready') run = { ...run, plan: { completed: false, steps: event.steps || [] } };
  if (eventType === 'plan_step') {
    const currentSteps = run.plan?.steps || [];
    run = {
      ...run,
      plan: {
        ...(run.plan || {}),
        completed: false,
        steps: [...currentSteps.filter(step => step.step_id !== event.step?.step_id), event.step],
      },
    };
  }
  if (eventType === 'plan_complete') run = { ...run, plan: { ...(run.plan || {}), completed: true } };

  if (eventType === 'graph_visualize' && view.graph.status !== 'unsupported' && isGraphData(event.data)) {
    view = {
      ...view,
      graph: { status: 'ready', data: {
        success: true,
        nodes: event.data.nodes,
        edges: event.data.edges.map((link: any) => ({
          ...link,
          source: link.source ?? link.src,
          target: link.target ?? link.dst,
          src: link.src ?? link.source,
          dst: link.dst ?? link.target,
        })),
        directed: event.data.directed,
        num_nodes: event.data.num_nodes,
        num_edges: event.data.num_edges,
        total_nodes: event.data.total_nodes,
        total_edges: event.data.total_edges,
        truncated: event.data.truncated,
        truncated_to: event.data.truncated_to,
      } },
    };
  }

  if (eventType === 'await_user_feedback' || eventName.includes('runpaused') || eventName.includes('confirmation_required')) {
    run = { ...run, feedback: normalizeFeedbackRequest(event), status: 'awaiting_feedback' };
    view = {
      ...view,
      messagesById: Object.fromEntries(Object.entries(view.messagesById).map(([id, message]) => [id, { ...message, streaming: false }])),
    };
  }

  if (eventType === 'done') {
    const messageId = String(event.message_id || run.assistantMessageId || '');
    const current = messageId ? view.messagesById[messageId] : undefined;
    const content = current?.content || String(event.content || '');
    view = {
      ...view,
      messagesById: Object.fromEntries(Object.entries(view.messagesById).map(([id, message]) => [
        id,
        id === messageId ? { ...message, content, streaming: false } : { ...message, streaming: false },
      ])),
    };
    run = { ...run, status: 'completed', feedback: null };
  }

  if (eventType === 'error') {
    const error = String(event.error || '分析失败');
    const messageId = run.assistantMessageId || '';
    view = {
      ...view,
      error,
      messagesById: Object.fromEntries(Object.entries(view.messagesById).map(([id, message]) => [
        id,
        id === messageId && !message.content ? { ...message, content: error, streaming: false } : { ...message, streaming: false },
      ])),
    };
    run = { ...run, status: eventName.includes('cancel') ? 'cancelled' : 'failed', feedback: null, error };
  }

  return { view, run };
}

function applyBatch(view: SessionViewState, generation: string, batch: StreamBatch): SessionViewState {
  if (batch.session_id && batch.session_id !== view.sessionId) return view;
  let run = view.activeRun;
  if (!run || run.generation !== generation) return view;
  if (run.runId && batch.run_id && run.runId !== batch.run_id) return view;
  if (TERMINAL_RUN_STATUSES.has(run.status) && batch.events.every(event => event.type !== 'done' && event.type !== 'error')) return view;
  const eventIndex = batch.event_index == null ? null : Number(batch.event_index);
  if (batch.protocol_version === 2 && eventIndex != null && Number.isFinite(eventIndex) && eventIndex <= run.lastEventIndex) return view;
  run = { ...run, runId: batch.run_id || run.runId };
  for (const event of batch.events) {
    const applied = applyStreamEvent(view, run, event);
    view = applied.view;
    run = applied.run;
  }
  if (batch.protocol_version === 2 && eventIndex != null && Number.isFinite(eventIndex)) {
    run = { ...run, lastEventIndex: eventIndex };
  }
  return { ...view, activeRun: run };
}

function evictViews(state: ConversationState): ConversationState {
  const ids = Object.keys(state.viewsBySessionId);
  if (ids.length <= SESSION_VIEW_CACHE_LIMIT) return state;
  const removable = ids
    .filter(id => {
      const view = state.viewsBySessionId[id];
      return id !== state.activeSessionId
        && !view.draft
        && view.attachments.length === 0
        && view.pendingAttachments.length === 0
        && !view.uploading
        && !PROTECTED_RUN_STATUSES.has(view.activeRun?.status || 'idle');
    })
    .sort((left, right) => state.viewsBySessionId[left].lastAccessedAt - state.viewsBySessionId[right].lastAccessedAt);
  const viewsBySessionId = { ...state.viewsBySessionId };
  while (Object.keys(viewsBySessionId).length > SESSION_VIEW_CACHE_LIMIT && removable.length) {
    delete viewsBySessionId[removable.shift()!];
  }
  return { ...state, viewsBySessionId };
}

function graphStateAfterHydration(
  view: SessionViewState,
  datasetView: DatasetView | null | undefined,
  activeDatasetId: string | null | undefined,
  hasGraph: boolean | undefined,
): GraphViewState {
  if (datasetView?.kind === 'graph_collection') {
    return { status: 'unsupported', data: null, code: 'NON_SINGLE_GRAPH', summary: datasetView.summary || {} };
  }
  const datasetChanged = view.activeDatasetId !== activeDatasetId;
  if (hasGraph) {
    if (!datasetChanged && view.graph.status === 'ready') return view.graph;
    return { status: 'loading', data: null };
  }
  return { status: 'empty', data: null };
}

export function conversationReducer(state: ConversationState, action: ConversationAction): ConversationState {
  if (action.type === 'OPEN_DRAFT') {
    const current = state.draftsByProjectId[action.draft.projectId];
    const draft = current || action.draft;
    return {
      ...state,
      activeSessionId: null,
      activeDraftProjectId: draft.projectId,
      draftsByProjectId: current ? state.draftsByProjectId : { ...state.draftsByProjectId, [draft.projectId]: draft },
    };
  }
  if (action.type === 'PATCH_DRAFT') {
    const current = state.draftsByProjectId[action.projectId];
    if (!current || current.draftId !== action.draftId) return state;
    return {
      ...state,
      draftsByProjectId: {
        ...state.draftsByProjectId,
        [action.projectId]: { ...current, ...action.patch },
      },
    };
  }
  if (action.type === 'REMOVE_DRAFT') {
    const current = state.draftsByProjectId[action.projectId];
    if (!current || (action.draftId && current.draftId !== action.draftId)) return state;
    const draftsByProjectId = { ...state.draftsByProjectId };
    delete draftsByProjectId[action.projectId];
    return {
      ...state,
      activeDraftProjectId: state.activeDraftProjectId === action.projectId ? null : state.activeDraftProjectId,
      draftsByProjectId,
    };
  }
  if (action.type === 'MATERIALIZE_DRAFT') {
    const draft = state.draftsByProjectId[action.projectId];
    if (!draft || draft.draftId !== action.draftId) return state;
    const uploaded = draft.attachments
      .filter(item => Boolean(item.fileToken))
      .map(item => ({ file_token: item.fileToken!, filename: item.filename }));
    const pendingAttachments = draft.attachments.filter(item => !item.fileToken);
    let view = createSessionView(action.sessionId, action.model, action.accessedAt);
    view = {
      ...view,
      hydration: 'ready',
      draft: draft.text,
      attachments: uploaded,
      pendingAttachments,
      activeDatasetId: action.activeDatasetId,
      graph: graphStateAfterHydration(view, action.datasetView, action.activeDatasetId, false),
      activeRun: {
        generation: action.generation,
        status: 'preparing',
        lastEventIndex: -1,
        toolsById: {}, toolOrder: [], plan: null, feedback: null,
      },
    };
    const draftsByProjectId = { ...state.draftsByProjectId };
    delete draftsByProjectId[action.projectId];
    const wasActive = state.activeDraftProjectId === action.projectId;
    return evictViews({
      ...state,
      activeSessionId: wasActive ? action.sessionId : state.activeSessionId,
      activeDraftProjectId: wasActive ? null : state.activeDraftProjectId,
      draftsByProjectId,
      viewsBySessionId: { ...state.viewsBySessionId, [action.sessionId]: view },
    });
  }
  if (action.type === 'ENSURE_SESSION' || action.type === 'SET_ACTIVE_SESSION') {
    const sessionId = action.sessionId;
    if (sessionId === null) return { ...state, activeSessionId: null, activeDraftProjectId: null };
    const current = state.viewsBySessionId[sessionId];
    const view = current
      ? { ...current, model: action.model || current.model, lastAccessedAt: action.accessedAt ?? current.lastAccessedAt }
      : createSessionView(sessionId, action.model, action.accessedAt);
    return evictViews({
      ...state,
      activeSessionId: action.type === 'SET_ACTIVE_SESSION' ? sessionId : state.activeSessionId,
      activeDraftProjectId: action.type === 'SET_ACTIVE_SESSION' ? null : state.activeDraftProjectId,
      viewsBySessionId: { ...state.viewsBySessionId, [sessionId]: view },
    });
  }

  const current = state.viewsBySessionId[action.sessionId];
  if (!current) return state;
  let view = current;

  switch (action.type) {
    case 'HYDRATE_START':
      if (action.requestVersion < view.hydrationRequestVersion) return state;
      view = { ...view, hydration: 'loading', hydrationRequestVersion: action.requestVersion, error: undefined };
      break;
    case 'HYDRATE_SUCCESS': {
      if (action.requestVersion !== view.hydrationRequestVersion) return state;
      const indexed = action.preserveMessages && view.messageOrder.length
        ? { messagesById: view.messagesById, messageOrder: view.messageOrder }
        : indexMessages(view.sessionId, action.messages);
      let activeRun = view.activeRun;
      if (action.activeRun) {
        const status = action.activeRun.status.toLowerCase() === 'paused' ? 'awaiting_feedback' : 'reconnecting';
        if (!activeRun || activeRun.runId !== action.activeRun.run_id) {
          activeRun = {
            runId: action.activeRun.run_id,
            generation: action.generation || `resume:${action.activeRun.run_id}`,
            status,
            lastEventIndex: Number(action.activeRun.last_event_index ?? -1),
            assistantMessageId: `assistant:${action.activeRun.run_id}`,
            toolsById: {}, toolOrder: [], plan: null,
            feedback: action.feedback || null,
          };
        } else {
          activeRun = {
            ...activeRun,
            status,
            lastEventIndex: Math.max(activeRun.lastEventIndex, Number(action.activeRun.last_event_index ?? -1)),
            feedback: status === 'awaiting_feedback' ? action.feedback || activeRun.feedback : null,
            feedbackSubmission: undefined,
          };
        }
      } else if (!activeRun || !PROTECTED_RUN_STATUSES.has(activeRun.status)) activeRun = null;
      const graph = graphStateAfterHydration(view, action.datasetView, action.activeDatasetId, action.hasGraph);
      view = {
        ...view,
        ...indexed,
        hydration: 'ready',
        model: action.model,
        activeDatasetId: action.activeDatasetId,
        graph,
        selectedNode: graph.status === 'ready' ? view.selectedNode : null,
        activeRun,
        error: undefined,
      };
      break;
    }
    case 'HYDRATE_FAILED':
      if (action.requestVersion !== view.hydrationRequestVersion) return state;
      view = { ...view, hydration: 'failed', error: action.error };
      break;
    case 'PATCH_VIEW':
      view = { ...view, ...action.patch };
      break;
    case 'APPEND_MESSAGES':
      view = appendMessages(view, action.messages);
      break;
    case 'BEGIN_RUN_PREPARATION':
      if (PROTECTED_RUN_STATUSES.has(view.activeRun?.status || 'idle')) return state;
      view = {
        ...view,
        error: undefined,
        activeRun: {
          generation: action.generation,
          status: 'preparing',
          lastEventIndex: -1,
          toolsById: {}, toolOrder: [], plan: null, feedback: null,
        },
      };
      break;
    case 'START_PREPARED_RUN': {
      const preparingRun = view.activeRun;
      if (!preparingRun || preparingRun.generation !== action.generation || preparingRun.status !== 'preparing') return state;
      view = appendMessages(view, [action.userMessage, action.assistantMessage]);
      view = {
        ...view,
        draft: '',
        error: undefined,
        activeRun: {
          ...preparingRun,
          status: 'starting',
          assistantMessageId: action.assistantMessage.id,
        },
      };
      break;
    }
    case 'PREPARE_RUN_FAILED':
      if (!view.activeRun || view.activeRun.generation !== action.generation || view.activeRun.status !== 'preparing') return state;
      view = { ...view, activeRun: null, error: action.error };
      break;
    case 'SET_PENDING_ATTACHMENT':
      view = {
        ...view,
        pendingAttachments: view.pendingAttachments.map(item => item.localId === action.localId
          ? { ...item, status: action.status, error: action.error }
          : item),
      };
      break;
    case 'RESOLVE_PENDING_ATTACHMENT':
      view = {
        ...view,
        attachments: view.attachments.some(item => item.file_token === action.attachment.file_token)
          ? view.attachments
          : [...view.attachments, action.attachment],
        pendingAttachments: view.pendingAttachments.filter(item => item.localId !== action.localId),
      };
      break;
    case 'REMOVE_PENDING_ATTACHMENT':
      if (PROTECTED_RUN_STATUSES.has(view.activeRun?.status || 'idle')) return state;
      view = { ...view, pendingAttachments: view.pendingAttachments.filter(item => item.localId !== action.localId) };
      break;
    case 'START_RUN': {
      if (PROTECTED_RUN_STATUSES.has(view.activeRun?.status || 'idle')) return state;
      view = appendMessages(view, [action.userMessage, action.assistantMessage]);
      view = {
        ...view,
        draft: '',
        error: undefined,
        activeRun: {
          generation: action.generation,
          status: 'starting',
          lastEventIndex: -1,
          assistantMessageId: action.assistantMessage.id,
          toolsById: {}, toolOrder: [], plan: null, feedback: null,
        },
      };
      break;
    }
    case 'ROLLBACK_RUN_START': {
      if (!view.activeRun || view.activeRun.generation !== action.generation) return state;
      const removeIds = new Set([`user:${action.generation}`, `assistant:${action.generation}`]);
      const messagesById = { ...view.messagesById };
      for (const id of removeIds) delete messagesById[id];
      view = {
        ...view,
        messagesById,
        messageOrder: view.messageOrder.filter(id => !removeIds.has(id)),
        draft: view.draft.trim() ? view.draft : action.restoreDraft || view.draft,
        attachments: [
          ...view.attachments,
          ...(action.restoreAttachments || []).filter(item => !view.attachments.some(current => current.file_token === item.file_token)),
        ],
        activeRun: null,
      };
      break;
    }
    case 'ADOPT_RUN':
      view = {
        ...view,
        activeRun: {
          runId: action.activeRun.run_id,
          generation: action.generation,
          status: action.activeRun.status.toLowerCase() === 'paused' ? 'awaiting_feedback' : 'reconnecting',
          lastEventIndex: Number(action.activeRun.last_event_index ?? -1),
          assistantMessageId: `assistant:${action.activeRun.run_id}`,
          toolsById: {}, toolOrder: [], plan: null, feedback: action.feedback || null,
        },
      };
      break;
    case 'BEGIN_FEEDBACK_SUBMISSION': {
      const run = view.activeRun;
      if (!run || run.generation !== action.generation || run.runId !== action.runId || run.status !== 'awaiting_feedback') return state;
      const callId = String(run.feedback?.call_id || '');
      if (!callId || callId !== action.callId) return state;
      view = {
        ...view,
        error: undefined,
        activeRun: {
          ...run,
          status: 'submitting_feedback',
          feedbackSubmission: { decisionId: action.decisionId, callId: action.callId, decision: action.decision },
          error: undefined,
        },
      };
      break;
    }
    case 'CLEAR_ACTIVE_RUN':
      if (!view.activeRun || view.activeRun.generation !== action.generation) return state;
      view = { ...view, activeRun: null };
      break;
    case 'APPLY_BATCH':
      view = applyBatch(view, action.generation, action.batch);
      break;
    case 'SET_RUN_STATUS':
      if (!view.activeRun || view.activeRun.generation !== action.generation) return state;
      if (TERMINAL_RUN_STATUSES.has(view.activeRun.status) && !TERMINAL_RUN_STATUSES.has(action.status)) return state;
      if (view.activeRun.status === 'awaiting_feedback' && !['awaiting_feedback', 'failed', 'cancelled'].includes(action.status)) return state;
      view = {
        ...view,
        error: action.error || view.error,
        activeRun: {
          ...view.activeRun,
          status: action.status,
          feedback: action.status === 'streaming' ? null : view.activeRun.feedback,
          error: action.error || view.activeRun.error,
        },
      };
      break;
    case 'GRAPH_REQUEST_START':
      if (action.requestVersion < view.graphRequestVersion) return state;
      view = { ...view, graphRequestVersion: action.requestVersion, graph: { status: 'loading', data: null }, selectedNode: null };
      break;
    case 'GRAPH_REQUEST_SUCCESS':
      if (action.requestVersion !== view.graphRequestVersion) return state;
      view = { ...view, graph: action.graph };
      break;
    case 'REMOVE_SESSION': {
      const viewsBySessionId = { ...state.viewsBySessionId };
      delete viewsBySessionId[action.sessionId];
      return { ...state, activeSessionId: state.activeSessionId === action.sessionId ? null : state.activeSessionId, viewsBySessionId };
    }
  }
  return { ...state, viewsBySessionId: { ...state.viewsBySessionId, [action.sessionId]: view } };
}

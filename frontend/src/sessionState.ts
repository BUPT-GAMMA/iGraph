import type { RemoteBinding } from './types';
import { editableLatestTurn } from './latestTurn';
import { isGraphData } from './types';
import type { ActiveRun, AttachmentRef, DatasetView, DraftAttachment, FeedbackRequest, GraphData, GraphSnapshot, GraphViewState, Message, NewConversationDraft, Plan, ProcessItem, StreamBatch, StreamEvent, ToolCall } from './types';
import type { RunSnapshot, RunControl } from './api';
import { modelErrorMessage } from './api';
import { tr } from './i18n';
import { pendingActivity, type SavedSessionActivity } from './sessionActivity';

export const PERSONAL_DRAFT_KEY = 'personal';
export function draftScopeKey(projectId: string | null | undefined) { return projectId || PERSONAL_DRAFT_KEY; }

export type ReceptionStatus = 'connecting' | 'receiving' | 'retrying' | 'user_paused' | 'capacity_suspended' | 'snapshot_sync' | 'idle';

export type RunStatus =
  | 'idle'
  | 'preparing'
  | 'starting'
  | 'streaming'
  | 'awaiting_feedback'
  | 'submitting_feedback'
  | 'completed'
  | 'failed'
  | 'cancelled';

export type SessionRunState = {
  runId?: string;
  startRequestId?: string;
  cancelRequestId?: string;
  cancellation?: 'requested' | 'stopping' | 'unconfirmed' | 'saving' | 'confirmed';
  controlRevision?: number;
  serverCanSend?: boolean;
  nativeRunCreated?: boolean;
  generation: string;
  status: RunStatus;
  lastAppliedEventIndex: number;
  reception?: ReceptionStatus;
  synchronized?: boolean;
  snapshotOnly?: boolean;
  recoveryRequired?: boolean;
  replay?: Pick<SessionViewState, 'messagesById' | 'messageOrder' | 'graph'>;
  assistantMessageId?: string;
  submittedInput?: string;
  preserveComposer?: boolean;
  editBackup?: { runId: string; messages: Message[] };
  replaceRunId?: string;
  toolsById: Record<string, ToolCall>;
  toolOrder: string[];
  processItems?: ProcessItem[];
  pendingProcessText?: { content: string; sequence: number };
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
  replacedRunIds?: string[];
  hydration: 'unloaded' | 'loading' | 'ready' | 'failed';
  hydrationRequestVersion: number;
  messagesById: Record<string, Message>;
  messageOrder: string[];
  graph: GraphViewState;
  graphSnapshot?: GraphData | null;
  graphRequestVersion: number;
  selectedNode: Record<string, unknown> | null;
  draft: string;
  attachments: AttachmentRef[];
  pendingAttachments: DraftAttachment[];
  uploading: boolean;
  model: string;
  modelRevision?: number;
  modelUnavailableReason?: string | null;
  activeDatasetId?: string | null;
  remoteGraph?: RemoteBinding | null;
  remoteGraphUnavailableReason?: string | null;
  error?: string;
  activeRun: SessionRunState | null;
  unreadCompletion: boolean;
  lastCompletedRunId?: string;
  lastReadRunId?: string;
  lastAccessedAt: number;
};

export type ConversationState = {
  activeSessionId: string | null;
  activeDraftKey: string | null;
  viewsBySessionId: Record<string, SessionViewState>;
  draftsByScope: Record<string, NewConversationDraft>;
};

export type ConversationAction =
  | { type: 'RESET' }
  | { type: 'RESTORE_SESSION_ACTIVITY'; activity: SavedSessionActivity }
  | { type: 'ENSURE_SESSION'; sessionId: string; model?: string; accessedAt?: number }
  | { type: 'SET_ACTIVE_SESSION'; sessionId: string | null; model?: string; accessedAt?: number }
  | { type: 'OPEN_DRAFT'; draft: NewConversationDraft }
  | { type: 'PATCH_DRAFT'; projectId: string | null; draftId: string; patch: Partial<Pick<NewConversationDraft, 'phase' | 'text' | 'model' | 'activeDatasetId' | 'attachments' | 'error'>> }
  | { type: 'REMOVE_DRAFT'; projectId: string | null; draftId?: string }
  | { type: 'MATERIALIZE_DRAFT'; prepareRun?: boolean; projectId: string | null; draftId: string; sessionId: string; generation: string; model: string; activeDatasetId?: string | null; datasetView?: DatasetView | null; accessedAt?: number }
  | { type: 'HYDRATE_START'; sessionId: string; requestVersion: number }
  | {
      type: 'HYDRATE_SUCCESS'; sessionId: string; requestVersion: number; messages: Message[];
      model: string; modelRevision?: number; modelUnavailableReason?: string | null; remoteGraph?: RemoteBinding | null; remoteGraphUnavailableReason?: string | null; activeDatasetId?: string | null; feedback?: FeedbackRequest | null;
      activeRun?: ActiveRun | null; generation?: string; preserveMessages?: boolean;
      preserveComposer?: boolean;
      control?: RunControl | null;
      datasetView?: DatasetView | null; hasGraph?: boolean;
    }
  | { type: 'HYDRATE_FAILED'; sessionId: string; requestVersion: number; error: string }
  | { type: 'PATCH_VIEW'; sessionId: string; patch: Partial<Pick<SessionViewState, 'draft' | 'attachments' | 'pendingAttachments' | 'uploading' | 'model' | 'modelRevision' | 'modelUnavailableReason' | 'activeDatasetId' | 'remoteGraph' | 'remoteGraphUnavailableReason' | 'error' | 'selectedNode'>> }
  | { type: 'APPEND_MESSAGES'; sessionId: string; messages: Message[] }
  | { type: 'BEGIN_RUN_PREPARATION'; sessionId: string; generation: string }
  | { type: 'START_PREPARED_RUN'; sessionId: string; generation: string; userMessage: Message; assistantMessage: Message }
  | { type: 'PREPARE_RUN_FAILED'; sessionId: string; generation: string; error: string }
  | { type: 'SET_PENDING_ATTACHMENT'; sessionId: string; localId: string; status: DraftAttachment['status']; error?: string; progress?: number }
  | { type: 'RESOLVE_PENDING_ATTACHMENT'; sessionId: string; localId: string; attachment: AttachmentRef }
  | { type: 'REMOVE_PENDING_ATTACHMENT'; sessionId: string; localId: string }
  | { type: 'START_RUN'; sessionId: string; generation: string; userMessage: Message; assistantMessage: Message; replaceRunId?: string; preserveComposer?: boolean }
  | { type: 'ROLLBACK_RUN_START'; sessionId: string; generation: string; restoreDraft?: string; restoreAttachments?: AttachmentRef[] }
  | { type: 'ADOPT_RUN'; sessionId: string; generation: string; activeRun: ActiveRun; feedback?: FeedbackRequest | null }
  | { type: 'SET_RUN_ID'; sessionId: string; generation: string; runId: string; lastAppliedEventIndex?: number }
  | { type: 'REQUEST_CANCEL'; sessionId: string; generation: string; cancelRequestId: string; startRequestId?: string; runId?: string }
  | { type: 'CANCEL_UNCONFIRMED'; sessionId: string; generation: string }
  | { type: 'APPLY_CONTROL'; sessionId: string; generation: string; control: RunControl }
  | { type: 'MERGE_RUN_SNAPSHOT'; sessionId: string; snapshot: RunSnapshot }
  | { type: 'BEGIN_FEEDBACK_SUBMISSION'; sessionId: string; generation: string; runId: string; callId: string; decisionId: string; decision: 'approve' | 'reject' }
  | { type: 'FEEDBACK_NOT_ACCEPTED'; sessionId: string; generation: string; decisionId: string }
  | { type: 'CLEAR_ACTIVE_RUN'; sessionId: string; generation: string }
  | { type: 'APPLY_BATCH'; sessionId: string; generation: string; batch: StreamBatch }
  | { type: 'SET_RECEPTION'; sessionId: string; generation: string; reception: ReceptionStatus; synchronized?: boolean; snapshotOnly?: boolean; error?: string }
  | { type: 'BEGIN_REPLAY'; sessionId: string; generation: string }
  | { type: 'REPLAY_CAUGHT_UP'; sessionId: string; generation: string }
  | { type: 'APPLY_SNAPSHOT'; sessionId: string; generation: string; snapshot: RunSnapshot }
  | { type: 'SET_RUN_STATUS'; sessionId: string; generation: string; status: RunStatus; error?: string }
  | { type: 'GRAPH_REQUEST_START'; sessionId: string; requestVersion: number; preserveCurrent?: boolean }
  | { type: 'GRAPH_REQUEST_SUCCESS'; sessionId: string; requestVersion: number; graph: GraphViewState }
  | { type: 'REMOVE_SESSION'; sessionId: string };

export const SESSION_VIEW_CACHE_LIMIT = 12;
const PROTECTED_RUN_STATUSES = new Set<RunStatus>(['preparing', 'starting', 'streaming', 'awaiting_feedback', 'submitting_feedback']);
const TERMINAL_RUN_STATUSES = new Set<RunStatus>(['completed', 'failed', 'cancelled']);

export type RunGateReason = 'preparing' | 'run_active' | 'awaiting_feedback' | 'submitting_feedback' | 'reconnecting' | 'detached' | null;

export type BottomInteraction =
  | { kind: 'composer' }
  | { kind: 'approval'; feedback: FeedbackRequest; phase: 'awaiting' | 'submitting' }
  | { kind: 'syncing_feedback'; reason: 'session_hydration' | 'missing_feedback' };

export function selectRunGate(view?: SessionViewState | null) {
  const status = view?.activeRun?.status;
  const cancelPending = Boolean(view?.activeRun?.cancellation && view.activeRun.serverCanSend !== true);
  const unverified = cancelPending || view?.activeRun?.serverCanSend === false || (view?.activeRun?.serverCanSend !== true && (view?.activeRun?.synchronized === false || view?.hydration === 'loading' || view?.hydration === 'failed'));
  const reason: RunGateReason = unverified ? 'reconnecting' : status === 'preparing'
    ? 'preparing'
    : status === 'awaiting_feedback'
    ? 'awaiting_feedback'
    : status === 'submitting_feedback'
      ? 'submitting_feedback'
      : status === 'starting' || status === 'streaming'
            ? 'run_active'
            : null;
  return {
    reason,
    canStartRun: reason === null,
    canSubmitFeedback: !unverified && status === 'awaiting_feedback' && Boolean(view?.activeRun?.feedback),
    blocksSessionMutation: reason !== null,
  };
}

export function selectBottomInteraction(view?: SessionViewState | null): BottomInteraction {
  const run = view?.activeRun;
  if (run?.cancellation) return { kind: 'composer' };
  if (run?.synchronized === false && (run.status === 'awaiting_feedback' || run.status === 'submitting_feedback')) return { kind: 'syncing_feedback', reason: 'session_hydration' };
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

export const initialConversationState: ConversationState = { activeSessionId: null, activeDraftKey: null, viewsBySessionId: {}, draftsByScope: {} };

export function createSessionView(sessionId: string, model = 'qwen3.6-plus', accessedAt = 0): SessionViewState {
  return {
    sessionId,
    hydration: 'unloaded',
    hydrationRequestVersion: 0,
    messagesById: {},
    messageOrder: [],
    graph: { status: 'empty', data: null },
    graphSnapshot: null,
    graphRequestVersion: 0,
    selectedNode: null,
    draft: '',
    attachments: [],
    pendingAttachments: [],
    uploading: false,
    model,
    activeDatasetId: null,
    activeRun: null,
    unreadCompletion: false,
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

function restoreEditedTurn(view: SessionViewState, backup: NonNullable<SessionRunState['editBackup']>): SessionViewState {
  const restored = indexMessages(view.sessionId, backup.messages);
  return { ...view, messagesById: { ...view.messagesById, ...restored.messagesById },
    messageOrder: [...view.messageOrder.filter(id => !restored.messagesById[id]), ...restored.messageOrder],
    replacedRunIds: view.replacedRunIds?.filter(id => id !== backup.runId) };
}

function normalizeGraph(graph: GraphSnapshot): GraphData | null {
  if (!graph || !Array.isArray(graph.nodes)) return null;
  const edges = (graph.edges || graph.links || []).map(link => ({
    ...link,
    source: link.source ?? link.src,
    target: link.target ?? link.dst,
    src: link.src ?? link.source,
    dst: link.dst ?? link.target,
  }));
  const community = graph.community ?? graph.communities;
  return {
    ...graph,
    nodes: graph.nodes,
    edges,
    community,
    communities: community,
  };
}

function latestMessageGraph(messages: Message[]): GraphData | null {
  for (let messageIndex = messages.length - 1; messageIndex >= 0; messageIndex -= 1) {
    const graphs = messages[messageIndex].graphs || [];
    for (let graphIndex = graphs.length - 1; graphIndex >= 0; graphIndex -= 1) {
      const graph = normalizeGraph(graphs[graphIndex]);
      if (graph) return graph;
    }
  }
  return null;
}

function mergeRefreshedGraph(current: GraphData | null, next: GraphData): GraphData {
  const community = next.community ?? next.communities ?? current?.community ?? current?.communities;
  return {
    ...next,
    community,
    communities: community,
    // The full-graph endpoint does not know which analysis result is highlighted.
    highlights: next.highlights ?? current?.highlights,
  };
}

export function selectMessages(view?: SessionViewState | null): Message[] {
  if (!view) return [];
  return view.messageOrder.map(id => view.messagesById[id]).filter(Boolean).map(message => {
    if (message.role !== 'assistant') return message;
    const run = view.activeRun;
    const cancelled = message.run_status === 'cancelled' || (run?.status === 'cancelled' && (message.id === run.assistantMessageId
      || Boolean(run.runId && message.run_id === run.runId)));
    // Native cancellation text also occurs in saved history, without an active run.
    const nativeCancellation = /^Run [a-z0-9_-]+ was cancelled\.?$/i.test(message.content.trim());
    if (nativeCancellation || (cancelled && !message.content.trim())) {
      return { ...message, content: tr('This conversation turn was stopped', '已停止本轮对话'), streaming: false };
    }
    return message;
  });
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
    description: String(request.description ?? request.message ?? request.user_message ?? tr('This tool call requires confirmation', '该工具调用需要确认')),
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

function bindRunMessages(view: SessionViewState, run: SessionRunState): SessionViewState {
  if (!run.runId) return view;
  const ids = new Set([`user:${run.generation}`, `assistant:${run.generation}`, run.assistantMessageId]);
  return { ...view, messagesById: Object.fromEntries(Object.entries(view.messagesById).map(([id, message]) =>
    [id, ids.has(id) ? { ...message, run_id: run.runId, ...(message.role === 'assistant' ? { run_status: run.status } : {}) } : message])) };
}

function applyStreamEvent(view: SessionViewState, run: SessionRunState, event: StreamEvent): { view: SessionViewState; run: SessionRunState } {
  const eventType = String(event.type || '');
  const eventName = String(event.type || event.event || '').toLowerCase();
  if (eventType === 'feedback_submit_error') {
    if (run.feedbackSubmission && event.decision_id && run.feedbackSubmission.decisionId !== event.decision_id) return { view, run };
    const error = String(event.error || tr('Confirmation submission failed; checking the run status', '确认提交失败，正在核对 Run 状态'));
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
    return { view, run };
  }
  if (eventType === 'run_continued') {
    return { view, run: { ...run, status: 'streaming', feedback: null, feedbackSubmission: undefined } };
  }

  if (run.status === 'submitting_feedback') {
    run = { ...run, status: 'streaming', feedback: null, feedbackSubmission: undefined, error: undefined };
  }

  if (eventType === 'token' || eventType === 'message_snapshot') {
    const requestedId = String(event.message_id || run.assistantMessageId || `assistant:${run.runId || run.generation}`);
    const renamed = renameAssistantMessage(view, run, requestedId);
    view = renamed.view;
    run = renamed.run;
    const current = view.messagesById[requestedId] || { id: requestedId, role: 'assistant' as const, content: '' };
    const content = eventType === 'message_snapshot'
      ? String(event.content || '')
      : current.content + String(event.content || '');
    view = {
      ...view,
      messagesById: {
        ...view.messagesById,
        [requestedId]: { ...current, content, streaming: true },
      },
      messageOrder: view.messageOrder.includes(requestedId) ? view.messageOrder : [...view.messageOrder, requestedId],
    };
    run = { ...run, status: 'streaming' };
  }

  if (eventType === 'token' && event.content) {
    const pending = run.pendingProcessText;
    run = { ...run, pendingProcessText: { content: (pending?.content || '') + String(event.content || ''),
      sequence: pending?.sequence ?? Number(event.event_index ?? (run.processItems || []).length) } };
  }
  if (eventType === 'thinking' && event.content) {
    const items = run.processItems || [];
    const last = items[items.length - 1];
    const content = String(event.content);
    run = { ...run, processItems: last?.kind === 'reasoning'
      ? [...items.slice(0, -1), { ...last, content: (last.content || '') + content }]
      : [...items, { id: 'reasoning:' + String(event.event_index ?? items.length), kind: 'reasoning', sequence: Number(event.event_index ?? items.length), content }] };
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
        const kind = String(progress.kind || '').toLowerCase();
        const status = String(progress.status || '').toLowerCase();
        const terminal = [
          'train_done', 'text2graph_done',
          'job_done', 'job_failed', 'job_cancelled',
          'large_graph_prepare_done', 'large_graph_compute_done',
        ].includes(kind) || ['done', 'failed', 'cancelled'].includes(status);
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

  if (eventType === 'plan_analyzing' && !run.plan) run = { ...run, plan: { completed: false, steps: [] } };
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
    const community = event.data.community ?? event.data.communities;
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
        community,
        communities: community,
        highlights: event.data.highlights,
      },
      },
    };
  }

  if (eventType === 'analysis_report' || (eventType === 'graph_visualize' && isGraphData(event.data))) {
    const messageId = `assistant:${run.runId || run.generation}`;
    const renamed = renameAssistantMessage(view, run, messageId);
    view = renamed.view; run = renamed.run;
    const message = view.messagesById[messageId] || { id: messageId, role: 'assistant' as const, content: '' };
    const id = String(event.data?.report?.report_id || event.data?.report?.id || event.data?.id || `run-${run.runId}-${event.call_id || event.event_index}`);
    const updated = eventType === 'graph_visualize'
      ? { ...message, graphs: [...(message.graphs || []).filter(graph => graph.id !== id), { ...event.data, id }] }
      : { ...message, reports: [...(message.reports || []).filter(report => String(report.report_id || report.id) !== id), { ...event.data?.report, id }] };
    view = { ...view, messagesById: { ...view.messagesById, [messageId]: updated },
      messageOrder: view.messageOrder.includes(messageId) ? view.messageOrder : [...view.messageOrder, messageId] };
  }

  if (eventType === 'await_user_feedback' || eventName.includes('runpaused') || eventName.includes('confirmation_required')) {
    run = { ...run, feedback: normalizeFeedbackRequest(event), status: 'awaiting_feedback' };
    view = {
      ...view,
      messagesById: Object.fromEntries(Object.entries(view.messagesById).map(([id, message]) => [id, { ...message, streaming: false }])),
    };
  }

  if (eventType === 'done') {
    const messageId = String(event.message_id || run.assistantMessageId || `assistant:${run.runId || run.generation}`);
    const renamed = renameAssistantMessage(view, run, messageId);
    view = renamed.view;
    run = renamed.run;
    const current = messageId ? view.messagesById[messageId] : undefined;
    const content = typeof event.content === 'string' ? event.content : current?.content || '';
    view = { ...view,
      messagesById: { ...view.messagesById, [messageId]: { ...current, id: messageId, role: 'assistant', content, streaming: false,
        model_snapshot: event.model_snapshot ?? current?.model_snapshot,
        history_compacted: event.history_compacted ?? current?.history_compacted,
        tools: run.toolOrder.map(id => run.toolsById[id]), plan: run.plan } },
      messageOrder: view.messageOrder.includes(messageId) ? view.messageOrder : [...view.messageOrder, messageId] };
    run = { ...run, status: run.status === 'cancelled' ? 'cancelled' : 'completed', feedback: null };
  }

  if (eventType === 'error') {
    const rawError = String(event.error || tr('Analysis failed', '分析失败'));
    const modelCode = rawError.match(/MODEL_[A-Z_]+/)?.[0];
    const error = modelCode ? modelErrorMessage(modelCode) : rawError;
    const cancelled = Boolean(event.cancelled || eventName.includes('cancel') || run.status === 'cancelled');
    const messageId = run.assistantMessageId || '';
    view = {
      ...view,
      error: cancelled ? undefined : error,
      messagesById: Object.fromEntries(Object.entries(view.messagesById).map(([id, message]) => [
        id,
        !cancelled && id === messageId && !message.content ? { ...message, content: error, streaming: false } : { ...message, streaming: false },
      ])),
    };
    run = { ...run, status: cancelled ? 'cancelled' : 'failed', feedback: null, error: cancelled ? undefined : error };
  }

  let processItems = run.processItems || [];
  if (eventType === 'tool_call') {
    const callId = String(event.call_id || run.toolOrder[run.toolOrder.length - 1] || '');
    if (!processItems.some(item => item.id === 'tool:' + callId)) {
      if (run.pendingProcessText?.content) processItems = [...processItems, {
        id: 'text:' + run.pendingProcessText.sequence, kind: 'text', ...run.pendingProcessText }];
      processItems = [...processItems, { id: 'tool:' + callId, kind: 'tool',
        sequence: Number(event.event_index ?? processItems.length), tool: run.toolsById[callId] }];
      run = { ...run, pendingProcessText: undefined };
    }
  }
  processItems = processItems.map(item => {
    if (item.kind !== 'tool') return item;
    const tool = run.toolsById[item.tool?.call_id || ''] || item.tool || {};
    const result = tool.result as Record<string, unknown> | undefined;
    const terminal = ['completed', 'failed', 'cancelled'].includes(run.status);
    const failed = result?.success === false || result?.status === 'failed';
    const cancelled = result?.status === 'cancelled' || (terminal && !tool.done && run.status === 'cancelled');
    const status: ToolCall['status'] = cancelled ? 'cancelled' : failed ? 'failed' : tool.done ? 'completed' : terminal ? 'failed' : 'running';
    return { ...item, tool: { ...tool, status, done: Boolean(tool.done || terminal) } };
  });
  run = { ...run, processItems };
  const processMessageId = run.assistantMessageId;
  if (processMessageId && view.messagesById[processMessageId] && processItems.length) {
    view = { ...view, messagesById: { ...view.messagesById,
      [processMessageId]: { ...view.messagesById[processMessageId], processItems } } };
  }
  return { view, run };
}

function applicableEvent(event: StreamEvent) {
  if (!event || typeof event.type !== 'string') return false;
  if (event.type === 'token') return typeof event.content === 'string';
  if (event.type === 'done') return !('content' in event) || typeof event.content === 'string';
  if (event.type === 'plan_step') return event.step && typeof event.step === 'object' && event.step.step_id != null;
  if (event.type === 'plan_ready') return Array.isArray(event.steps) && event.steps.every((step: any) => step && step.step_id != null);
  if (event.type === 'analysis_report') return event.data?.report && typeof event.data.report === 'object' && !Array.isArray(event.data.report);
  if (event.type === 'graph_visualize') return isGraphData(event.data);
  return true; // Unrecognized extension events remain forward compatible.
}

export function applyBatch(view: SessionViewState, generation: string, batch: StreamBatch): SessionViewState {
  if (batch.session_id && batch.session_id !== view.sessionId) return view;
  let run = view.activeRun;
  if (!run || run.generation !== generation) return view;
  if (run.runId && batch.run_id && run.runId !== batch.run_id) return view;
  if (TERMINAL_RUN_STATUSES.has(run.status) && batch.events.every(event => event.type !== 'done' && event.type !== 'error')) return view;
  const eventIndex = batch.event_index == null ? null : Number(batch.event_index);
  if (batch.protocol_version === 2 && eventIndex != null && Number.isFinite(eventIndex) && eventIndex <= run.lastAppliedEventIndex) return view;
  const control = batch.events.every(event => ['stream_status', 'stream_recovery_required', 'feedback_accepted', 'feedback_submit_error'].includes(event.type || ''));
  if (run.recoveryRequired || run.snapshotOnly) return view;
  if (batch.events.some(event => event.type === 'stream_recovery_required') || (!control && batch.protocol_version === 2 && (
    !Number.isSafeInteger(eventIndex) || eventIndex !== run.lastAppliedEventIndex + 1
    || batch.events.some(event => !applicableEvent(event))
  ))) return { ...view, activeRun: { ...run, recoveryRequired: true, synchronized: false, reception: 'snapshot_sync' } };
  const visible = view;
  if (run.replay) view = { ...view, ...run.replay };
  run = { ...run, runId: batch.run_id || run.runId };
  view = bindRunMessages(view, run);
  const wasCancelled = run.status === 'cancelled';
  try {
    for (const event of batch.events) {
      const applied = applyStreamEvent(view, run, event);
      view = applied.view;
      run = applied.run;
    }
  } catch {
    return { ...visible, activeRun: { ...visible.activeRun!, recoveryRequired: true, synchronized: false, reception: 'snapshot_sync' } };
  }
  if (!control && batch.protocol_version === 2 && eventIndex != null && Number.isFinite(eventIndex)) {
    run = { ...run, lastAppliedEventIndex: eventIndex };
  }
  if (wasCancelled) run = { ...run, status: 'cancelled', feedback: null };
  view = bindRunMessages(view, run);
  if (run.replay) return { ...visible, activeRun: { ...run, replay: { messagesById: view.messagesById, messageOrder: view.messageOrder, graph: view.graph } } };
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
        && !view.unreadCompletion
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
  if (datasetView?.kind === 'partitioned_graph') {
    return {
      status: 'unsupported',
      data: null,
      code: 'PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE',
      summary: datasetView.summary || {},
    };
  }
  const datasetChanged = view.activeDatasetId !== activeDatasetId;
  if (hasGraph) {
    if (!datasetChanged && view.graph.status === 'ready') return view.graph;
    return { status: 'loading', data: null };
  }
  return { status: 'empty', data: null };
}

export function conversationReducer(state: ConversationState, action: ConversationAction): ConversationState {
  if (action.type === 'RESET') return initialConversationState;
  if (action.type === 'OPEN_DRAFT') {
    const current = state.draftsByScope[draftScopeKey(action.draft.projectId)];
    const draft = current || action.draft;
    return {
      ...state,
      activeSessionId: null,
      activeDraftKey: draftScopeKey(draft.projectId),
      draftsByScope: current ? state.draftsByScope : { ...state.draftsByScope, [draftScopeKey(draft.projectId)]: draft },
    };
  }
  if (action.type === 'PATCH_DRAFT') {
    const current = state.draftsByScope[draftScopeKey(action.projectId)];
    if (!current || current.draftId !== action.draftId) return state;
    return {
      ...state,
      draftsByScope: {
        ...state.draftsByScope,
        [draftScopeKey(action.projectId)]: { ...current, ...action.patch },
      },
    };
  }
  if (action.type === 'REMOVE_DRAFT') {
    const current = state.draftsByScope[draftScopeKey(action.projectId)];
    if (!current || (action.draftId && current.draftId !== action.draftId)) return state;
    const draftsByScope = { ...state.draftsByScope };
    delete draftsByScope[draftScopeKey(action.projectId)];
    return {
      ...state,
      activeDraftKey: state.activeDraftKey === draftScopeKey(action.projectId) ? null : state.activeDraftKey,
      draftsByScope,
    };
  }
  if (action.type === 'MATERIALIZE_DRAFT') {
    const draft = state.draftsByScope[draftScopeKey(action.projectId)];
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
      activeRun: action.prepareRun === false ? null : {
        generation: action.generation,
        startRequestId: action.generation,
        status: 'preparing',
        lastAppliedEventIndex: -1,
        toolsById: {}, toolOrder: [], plan: null, feedback: null,
      },
    };
    const draftsByScope = { ...state.draftsByScope };
    delete draftsByScope[draftScopeKey(action.projectId)];
    const wasActive = state.activeDraftKey === draftScopeKey(action.projectId);
    return evictViews({
      ...state,
      activeSessionId: wasActive ? action.sessionId : state.activeSessionId,
      activeDraftKey: wasActive ? null : state.activeDraftKey,
      draftsByScope,
      viewsBySessionId: { ...state.viewsBySessionId, [action.sessionId]: view },
    });
  }
  if (action.type === 'RESTORE_SESSION_ACTIVITY') {
    const saved = action.activity;
    const current = state.viewsBySessionId[saved.sessionId];
    // Never overwrite a live/hydrated run with an older browser record.
    if (current?.activeRun || current?.hydration === 'ready') return state;
    const active = state.activeSessionId === saved.sessionId;
    const view = { ...(current || createSessionView(saved.sessionId)),
      lastCompletedRunId: saved.completedRunId,
      lastReadRunId: active ? saved.completedRunId : saved.readRunId,
      unreadCompletion: !active && Boolean(saved.completedRunId && saved.completedRunId !== saved.readRunId),
    };
    if (pendingActivity(saved)) {
      view.activeRun = { runId: saved.runId, startRequestId: saved.startRequestId,
        generation: `restore:${saved.runId || saved.startRequestId}`,
        status: saved.status === 'awaiting_feedback' ? 'awaiting_feedback' : 'starting',
        lastAppliedEventIndex: -1, reception: 'connecting', synchronized: false,
        toolsById: {}, toolOrder: [], plan: null, feedback: null };
    }
    return { ...state, viewsBySessionId: { ...state.viewsBySessionId, [saved.sessionId]: view } };
  }
  if (action.type === 'ENSURE_SESSION' || action.type === 'SET_ACTIVE_SESSION') {
    const sessionId = action.sessionId;
    if (sessionId === null) return { ...state, activeSessionId: null, activeDraftKey: null };
    const current = state.viewsBySessionId[sessionId];
    const view = current
      ? { ...current, model: action.model || current.model,
        unreadCompletion: action.type === 'SET_ACTIVE_SESSION' ? false : current.unreadCompletion,
        lastReadRunId: action.type === 'SET_ACTIVE_SESSION' ? current.lastCompletedRunId : current.lastReadRunId,
        lastAccessedAt: action.accessedAt ?? current.lastAccessedAt }
      : createSessionView(sessionId, action.model, action.accessedAt);
    return evictViews({
      ...state,
      activeSessionId: action.type === 'SET_ACTIVE_SESSION' ? sessionId : state.activeSessionId,
      activeDraftKey: action.type === 'SET_ACTIVE_SESSION' ? null : state.activeDraftKey,
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
      if (view.activeRun?.cancellation && view.activeRun.serverCanSend !== true) return state;
      const indexed = action.preserveMessages && view.messageOrder.length
        ? { messagesById: view.messagesById, messageOrder: view.messageOrder }
        : indexMessages(view.sessionId, action.messages);
      let activeRun = view.activeRun;
      if (action.activeRun) {
        const status = action.activeRun.status.toLowerCase() === 'paused' ? 'awaiting_feedback' : 'streaming';
        if (!activeRun || activeRun.runId !== action.activeRun.run_id) {
          activeRun = {
            runId: action.activeRun.run_id,
            generation: action.generation || `resume:${action.activeRun.run_id}`,
            status,
            lastAppliedEventIndex: -1,
            reception: 'connecting', synchronized: false,
            assistantMessageId: `assistant:${action.activeRun.run_id}`,
            toolsById: {}, toolOrder: [], plan: null,
            feedback: action.feedback || null,
          };
        } else {
          activeRun = {
            ...activeRun,
            status,
            reception: 'connecting', synchronized: false,
            lastAppliedEventIndex: activeRun.lastAppliedEventIndex,
            feedback: status === 'awaiting_feedback' ? action.feedback || activeRun.feedback : null,
            feedbackSubmission: undefined,
          };
        }
      } else if (action.control?.run_id && !action.control.can_send) {
        activeRun = { generation: action.generation || `resume:${action.control.run_id}`,
          runId: action.control.run_id, startRequestId: action.control.start_request_id || undefined,
          status: 'starting', lastAppliedEventIndex: -1, reception: 'idle', synchronized: true,
          toolsById: {}, toolOrder: [], plan: null, feedback: null };
      } else activeRun = null;
      if (activeRun && action.preserveComposer) activeRun = { ...activeRun, preserveComposer: true };
      const graph = graphStateAfterHydration(view, action.datasetView, action.activeDatasetId, action.hasGraph);
      const latestGraph = action.preserveMessages || graph.status === 'unsupported'
        ? null
        : latestMessageGraph(action.messages);
      const hydratedGraph: GraphViewState = latestGraph ? { status: 'ready', data: latestGraph } : graph;
      view = {
        ...view,
        ...indexed,
        graph: hydratedGraph,
        hydration: 'ready',
        model: action.model,
        modelRevision: action.modelRevision ?? 0,
        modelUnavailableReason: action.modelUnavailableReason,
        activeDatasetId: action.activeDatasetId,
        remoteGraph: action.remoteGraph ?? null,
        remoteGraphUnavailableReason: action.remoteGraphUnavailableReason,
        selectedNode: hydratedGraph.status === 'ready' ? view.selectedNode : null,
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
        unreadCompletion: false,
        activeRun: {
          generation: action.generation,
          startRequestId: action.generation,
          status: 'preparing',
          lastAppliedEventIndex: -1,
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
          submittedInput: action.userMessage.content,
          assistantMessageId: action.assistantMessage.id,
        },
      };
      break;
    }
    case 'PREPARE_RUN_FAILED':
      if (!view.activeRun || view.activeRun.generation !== action.generation || view.activeRun.status !== 'preparing') return state;
      if (view.activeRun.cancellation) return state;
      view = { ...view, activeRun: null, error: action.error };
      break;
    case 'SET_PENDING_ATTACHMENT':
      view = {
        ...view,
        pendingAttachments: view.pendingAttachments.map(item => item.localId === action.localId
          ? { ...item, status: action.status, error: action.error, progress: action.progress }
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
      let editBackup: SessionRunState['editBackup'];
      if (action.replaceRunId) {
        const latest = editableLatestTurn(selectMessages(view));
        if (latest?.assistant.run_id !== action.replaceRunId || !selectRunGate(view).canStartRun) return state;
        const position = view.messageOrder.indexOf(latest.user.id!);
        if (position < 0) return state;
        const removedIds = view.messageOrder.slice(position);
        editBackup = { runId: action.replaceRunId, messages: removedIds.map(id => view.messagesById[id]) };
        view = { ...view, messageOrder: view.messageOrder.slice(0, position),
          messagesById: Object.fromEntries(Object.entries(view.messagesById).filter(([id]) => !removedIds.includes(id))),
          replacedRunIds: [...(view.replacedRunIds || []), action.replaceRunId] };
      }
      view = appendMessages(view, [action.userMessage, action.assistantMessage]);
      view = {
        ...view,
        draft: editBackup || action.preserveComposer ? view.draft : '',
        error: undefined,
        unreadCompletion: false,
        activeRun: {
          generation: action.generation,
          startRequestId: action.generation,
          status: 'starting',
          submittedInput: action.userMessage.content,
          preserveComposer: action.preserveComposer,
          editBackup,
          replaceRunId: action.replaceRunId,
          lastAppliedEventIndex: -1,
          assistantMessageId: action.assistantMessage.id,
          toolsById: {}, toolOrder: [], plan: null, feedback: null,
        },
      };
      break;
    }
    case 'ROLLBACK_RUN_START': {
      if (!view.activeRun || view.activeRun.generation !== action.generation) return state;
      if (view.activeRun.cancellation) return state;
      const backup = view.activeRun.editBackup;
      const removeIds = new Set([`user:${action.generation}`, `assistant:${action.generation}`]);
      const messagesById = { ...view.messagesById };
      for (const id of removeIds) delete messagesById[id];
      view = {
        ...view,
        messagesById,
        messageOrder: view.messageOrder.filter(id => !removeIds.has(id)),
        draft: backup || view.activeRun.preserveComposer ? view.draft : view.draft.trim() ? view.draft : action.restoreDraft || view.draft,
        attachments: [
          ...view.attachments,
          ...(backup ? [] : action.restoreAttachments || []).filter(item => !view.attachments.some(current => current.file_token === item.file_token)),
        ],
        activeRun: null,
      };
      if (backup) view = restoreEditedTurn(view, backup);
      break;
    }
    case 'ADOPT_RUN':
      view = {
        ...view,
        activeRun: {
          runId: action.activeRun.run_id,
          generation: action.generation,
          status: action.activeRun.status.toLowerCase() === 'paused' ? 'awaiting_feedback' : 'streaming',
          lastAppliedEventIndex: -1,
          reception: 'connecting', synchronized: false,
          assistantMessageId: `assistant:${action.activeRun.run_id}`,
          toolsById: {}, toolOrder: [], plan: null, feedback: action.feedback || null,
        },
      };
      break;
    case 'SET_RUN_ID':
      if (!view.activeRun || view.activeRun.generation !== action.generation) return state;
      if (view.activeRun.runId && view.activeRun.runId !== action.runId) return state;
      view = {
        ...view,
        activeRun: {
          ...view.activeRun,
          runId: action.runId,
        },
      };
      view = bindRunMessages(view, view.activeRun!);
      break;
    case 'REQUEST_CANCEL': {
      let run = view.activeRun;
      if (run && run.generation !== action.generation) return state;
      run ||= { generation: action.generation, runId: action.runId, startRequestId: action.startRequestId,
        status: 'preparing', lastAppliedEventIndex: -1, toolsById: {}, toolOrder: [], plan: null, feedback: null };
      view = { ...view, activeRun: { ...run, cancelRequestId: action.cancelRequestId, cancellation: 'requested', serverCanSend: false } };
      break;
    }
    case 'CANCEL_UNCONFIRMED':
      if (view.activeRun?.generation !== action.generation) return state;
      view = { ...view, activeRun: { ...view.activeRun, cancellation: 'unconfirmed' } };
      break;
    case 'APPLY_CONTROL': {
      const run = view.activeRun;
      const control = action.control;
      if (!run || run.generation !== action.generation || (run.runId && control.run_id && run.runId !== control.run_id)) return state;
      if (control.revision < (run.controlRevision || 0)) return state;
      if (control.registration_status === 'not_seen') return state;
      const safe = control.execution_stopped && control.terminal_persisted;
      const status = control.run_status === 'cancelled' ? 'cancelled' : control.run_status === 'completed' ? 'completed'
        : ['failed', 'error'].includes(control.run_status || '') ? 'failed' : run.status;
      // A stopped ordinary send is still a visible conversation turn. Only failed
      // admission or an edit that never replaced native history should roll back.
      if (safe && control.native_run_created === false && (status !== 'cancelled' || run.replaceRunId)) {
        const remove = new Set([`user:${run.generation}`, `assistant:${run.generation}`, ...view.messageOrder.filter(id => view.messagesById[id]?.run_id === run.runId)]);
        view = { ...view, messagesById: Object.fromEntries(Object.entries(view.messagesById).filter(([id]) => !remove.has(id))),
          messageOrder: view.messageOrder.filter(id => !remove.has(id)), draft: run.replaceRunId ? view.draft : view.draft || run.submittedInput || '',
          pendingAttachments: view.pendingAttachments.map(item => item.status === 'uploading'
            ? { ...item, status: item.file ? 'staged' : 'needs_reselect' } : item) };
      }
      if (safe && control.native_run_created === false && run.editBackup) {
        view = restoreEditedTurn(view, run.editBackup);
      }
      view = { ...view, error: safe && status === 'cancelled' ? undefined : view.error,
        activeRun: { ...run, runId: control.run_id || run.runId, controlRevision: control.revision,
        nativeRunCreated: control.native_run_created ?? run.nativeRunCreated,
        synchronized: safe && control.native_run_created === false ? true : run.synchronized,
        replay: safe ? undefined : run.replay,
        error: safe && status === 'cancelled' ? undefined : run.error,
        editBackup: safe && control.native_run_created === false ? undefined : run.editBackup,
        serverCanSend: safe && control.can_send, status: run.status === 'cancelled' ? 'cancelled' : status,
        reception: safe ? 'idle' : run.reception,
        cancellation: run.cancellation || control.cancel_status !== 'none' ? safe ? 'confirmed' : control.execution_stopped ? 'saving' : 'stopping' : undefined,
        feedback: control.cancel_status !== 'none' ? null : run.feedback } };
      view = bindRunMessages(view, view.activeRun!);
      break;
    }
    case 'MERGE_RUN_SNAPSHOT': {
      const snapshot = action.snapshot;
      if (!snapshot.snapshot_complete || snapshot.session_id !== view.sessionId || view.replacedRunIds?.includes(snapshot.run_id)) return state;
      const own = snapshot.messages.filter(message => message.run_id === snapshot.run_id
        || message.id === `assistant:${snapshot.run_id}` || message.id === `user:${snapshot.run_id}`);
      const indexed = indexMessages(view.sessionId, own);
      const replacedIds = new Set(view.messageOrder.filter(id => view.messagesById[id]?.run_id === snapshot.run_id));
      const position = view.messageOrder.findIndex(id => replacedIds.has(id));
      const order = view.messageOrder.filter(id => !replacedIds.has(id));
      order.splice(position < 0 ? order.length : position, 0, ...indexed.messageOrder.filter(id => !order.includes(id)));
      // A late old-run snapshot only updates its messages. Draft, current graph
      // version, newer messages and active execution remain untouched.
      view = { ...view, messagesById: { ...Object.fromEntries(Object.entries(view.messagesById).filter(([id]) => !replacedIds.has(id))), ...indexed.messagesById },
        messageOrder: order };
      break;
    }
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
    case 'FEEDBACK_NOT_ACCEPTED':
      if (!view.activeRun || view.activeRun.generation !== action.generation || view.activeRun.feedbackSubmission?.decisionId !== action.decisionId) return state;
      view = { ...view, activeRun: { ...view.activeRun, feedbackSubmission: undefined, synchronized: false } };
      break;
    case 'APPLY_BATCH':
      view = applyBatch(view, action.generation, action.batch);
      break;
    case 'SET_RECEPTION':
      if (!view.activeRun || view.activeRun.generation !== action.generation) return state;
      // A late observer callback cannot restart an already confirmed empty run.
      if (view.activeRun.nativeRunCreated === false && view.activeRun.cancellation === 'confirmed') return state;
      view = { ...view, error: action.error ?? view.error, activeRun: { ...view.activeRun,
        reception: action.reception, synchronized: action.synchronized ?? view.activeRun.synchronized,
        snapshotOnly: action.snapshotOnly ?? view.activeRun.snapshotOnly } };
      break;
    case 'BEGIN_REPLAY': {
      const run = view.activeRun;
      if (!run || run.generation !== action.generation || run.snapshotOnly) return state;
      const ids = new Set([run.assistantMessageId, `assistant:${run.runId}`]);
      view = { ...view, activeRun: { ...run, lastAppliedEventIndex: -1, recoveryRequired: false, synchronized: false,
        toolsById: {}, toolOrder: [], plan: null, feedback: null, processItems: [], pendingProcessText: undefined,
        replay: { messagesById: Object.fromEntries(Object.entries(view.messagesById).filter(([id]) => !ids.has(id))),
          messageOrder: view.messageOrder.filter(id => !ids.has(id)), graph: view.graph } } };
      break;
    }
    case 'REPLAY_CAUGHT_UP': {
      const run = view.activeRun;
      if (!run || run.generation !== action.generation || run.recoveryRequired || run.snapshotOnly) return state;
      view = { ...view, ...run.replay, error: undefined,
        activeRun: { ...run, replay: undefined, synchronized: true, reception: TERMINAL_RUN_STATUSES.has(run.status) || run.status === 'awaiting_feedback' ? 'idle' : 'receiving' } };
      break;
    }
    case 'APPLY_SNAPSHOT': {
      const run = view.activeRun;
      const snapshot = action.snapshot;
      if (!run || run.generation !== action.generation || run.runId !== snapshot.run_id || snapshot.session_id !== view.sessionId || !snapshot.snapshot_complete) return state;
      if (snapshot.control && snapshot.control.revision < (run.controlRevision || 0)) return state;
      const active = snapshot.active_run;
      const sameRun = active?.run_id === run.runId;
      const status = run.status === 'cancelled' && (!active || sameRun) ? 'cancelled' : active ? (active.status === 'paused' ? 'awaiting_feedback' : 'streaming')
        : snapshot.status === 'completed' ? 'completed' : snapshot.status === 'cancelled' ? 'cancelled' : 'failed';
      const indexed = indexMessages(view.sessionId, snapshot.messages);
      const assistant = indexed.messagesById[`assistant:${active?.run_id || run.runId}`];
      const tools = assistant?.tools || [];
      view = { ...view, ...indexed, error: undefined, hydration: 'ready',
        graph: latestMessageGraph(snapshot.messages) ? { status: 'ready', data: latestMessageGraph(snapshot.messages)! } : view.graph,
        activeRun: { ...run, editBackup: snapshot.messages.some(message => message.run_id === run.runId) ? undefined : run.editBackup, runId: active?.run_id || run.runId,
          ...(active && !sameRun ? { serverCanSend: false, cancellation: undefined, cancelRequestId: undefined, controlRevision: 0, startRequestId: undefined } : {}),
          generation: active && !sameRun ? `resume:${active.run_id}` : run.generation,
          status: (!active || sameRun) && run.status === 'cancelled' ? 'cancelled' : status, reception: active && !sameRun ? 'connecting' : 'idle', synchronized: !active || sameRun,
          snapshotOnly: sameRun && snapshot.resume_cursor == null,
          recoveryRequired: false, replay: undefined,
          lastAppliedEventIndex: sameRun && snapshot.resume_cursor != null ? snapshot.resume_cursor : -1,
          assistantMessageId: `assistant:${active?.run_id || run.runId}`,
          toolsById: Object.fromEntries(tools.map((tool, index) => [tool.call_id || `snapshot:${index}`, tool])),
          toolOrder: tools.map((tool, index) => tool.call_id || `snapshot:${index}`), plan: assistant?.plan || null,
          feedback: active?.status === 'paused' ? snapshot.pending_feedback : null, feedbackSubmission: undefined,
        } };
      break;
    }
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
      view = action.preserveCurrent && view.graph.status === 'ready'
        ? { ...view, graphRequestVersion: action.requestVersion }
        : { ...view, graphRequestVersion: action.requestVersion, graphSnapshot: view.graph.status === 'ready' ? view.graph.data : null, graph: { status: 'loading', data: null }, selectedNode: null };
      break;
    case 'GRAPH_REQUEST_SUCCESS':
      if (action.requestVersion !== view.graphRequestVersion) return state;
      view = {
        ...view,
        graph: action.graph.status === 'ready'
          ? {
              status: 'ready',
              data: mergeRefreshedGraph(view.graph.status === 'ready' ? view.graph.data : view.graphSnapshot || null, action.graph.data),
            }
          : action.graph,
        graphSnapshot: null,
      };
      break;
    case 'REMOVE_SESSION': {
      const viewsBySessionId = { ...state.viewsBySessionId };
      delete viewsBySessionId[action.sessionId];
      return { ...state, activeSessionId: state.activeSessionId === action.sessionId ? null : state.activeSessionId, viewsBySessionId };
    }
  }
  const completedNow = current.activeRun?.status !== 'completed' && view.activeRun?.status === 'completed';
  if (completedNow) {
    const completedId = view.activeRun!.runId || view.activeRun!.generation;
    const active = state.activeSessionId === action.sessionId;
    view = { ...view, lastCompletedRunId: completedId,
      lastReadRunId: active ? completedId : view.lastReadRunId,
      unreadCompletion: !active && view.lastReadRunId !== completedId };
  }
  const updated = { ...state, viewsBySessionId: { ...state.viewsBySessionId, [action.sessionId]: view } };
  const control = action.type === 'APPLY_SNAPSHOT' ? action.snapshot.control : action.type === 'HYDRATE_SUCCESS' ? action.control : null;
  if (control && view.activeRun) return conversationReducer(updated, { type: 'APPLY_CONTROL',
    sessionId: action.sessionId, generation: view.activeRun.generation, control });
  return updated;
}

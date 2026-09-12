import { isGraphData } from './types';
import { createParser } from 'eventsource-parser';
import type { ActiveRun, AttachmentRef, ConstructionBuildOptions, ConstructionGraph, ConstructionOperation, ConstructionResult, ConstructionState, Dataset, DatasetView, FeedbackRequest, GraphData, Message, Project, ProjectTrainingRun, Session, StreamBatch, StreamEvent } from './types';

const API_BASE = import.meta.env.VITE_API_BASE || '';
const SESSION_STATE_V2 = import.meta.env.VITE_SESSION_STATE_V2 !== 'false';
export const USER_KEY = 'igraph_user_id';

export type CustomModel = {
  model_id: string;
  display_name: string;
  provider?: string;
  base_url: string;
  has_api_key?: boolean;
  created_at?: number;
  updated_at?: number;
};

export function getUserId() { return localStorage.getItem(USER_KEY) || ''; }
export function setUserId(value: string) { localStorage.setItem(USER_KEY, value); }

let requestAccount: { userId: string } | undefined;

/** Bind requests to the account rendered by the mounted workspace. */
export function bindRequestAccount(userId: string) {
  const account = { userId };
  requestAccount = account;
  return () => { if (requestAccount === account) requestAccount = undefined; };
}

export async function apiFetch(path: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers);
  if (!headers.has('X-User-ID')) {
    const storedUserId = getUserId();
    const userId = requestAccount?.userId ?? storedUserId;
    // A storage event can arrive after a click. Never send the old workspace's
    // content with an identity read from another tab; keep the header pinned.
    if (userId !== storedUserId) {
      throw new ApiError(409, '账户已在其他标签页切换，请等待同步后重试', null, null, null, 'ACCOUNT_CHANGED');
    }
    if (userId) headers.set('X-User-ID', userId);
  }
  return fetch(`${API_BASE}${path}`, { ...options, headers });
}

async function json<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await apiFetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw ApiError.fromResponse(response.status, data, response.headers.get('Retry-After'));
  return data as T;
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  code?: string;
  activeRun?: ActiveRun | null;
  pendingFeedback?: FeedbackRequest | null;
  retryAfterMs?: number;
  feedbackAccepted?: boolean;

  constructor(status: number, message: string, detail?: unknown, activeRun?: ActiveRun | null, pendingFeedback?: FeedbackRequest | null, code?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.code = code;
    this.activeRun = activeRun;
    this.pendingFeedback = pendingFeedback;
  }

  static fromResponse(status: number, data: any, retryAfter?: string | null) {
    const detail = data?.detail ?? data?.error;
    const body = detail && typeof detail === 'object' ? detail : data;
    const validationMessage = Array.isArray(detail)
      ? detail.map(item => {
          if (!item || typeof item !== 'object') return String(item);
          const location = Array.isArray(item.loc) ? item.loc.filter((part: unknown) => part !== 'body').join('.') : '';
          return `${location ? `${location}: ` : ''}${String(item.msg || item.message || '参数无效')}`;
        }).filter(Boolean).join('；')
      : '';
    const message = typeof detail === 'string'
      ? detail
      : validationMessage || String(body?.message || body?.error || `请求失败 (${status})`);
    const error = new ApiError(status, message, detail, body?.active_run || null, body?.pending_feedback || null, body?.code);
    if (retryAfter) {
      const seconds = Number(retryAfter);
      const delay = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(retryAfter) - Date.now();
      if (Number.isFinite(delay)) error.retryAfterMs = Math.max(0, delay);
    }
    return error;
  }
}

export async function validateUser(userId: string) {
  const data = await json<{ valid?: boolean; available_models?: string[]; default_model?: string }>('/api/auth/validate', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-User-ID': userId },
  });
  return data;
}

export async function listCustomModels() {
  return json<{ success?: boolean; models?: CustomModel[] }>('/api/models/custom');
}

export async function saveCustomModel(input: { model_id: string; display_name: string; base_url: string; api_key: string }) {
  return json<{ success?: boolean; model: CustomModel }>('/api/models/custom', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  });
}

export async function deleteCustomModel(modelId: string) {
  return json<{ success?: boolean; model_id: string }>(`/api/models/custom/${encodeURIComponent(modelId)}`, { method: 'DELETE' });
}

export async function listSessions() { return json<{ success?: boolean; sessions?: Session[] }>('/api/sessions'); }
export async function createSession(options: {
  clientSessionId?: string;
  model?: string;
  projectId?: string | null;
  activeDatasetId?: string | null;
  initialMessagePreview?: string;
  signal?: AbortSignal;
} = {}) {
  return json<{ success?: boolean; session_id: string; reused?: boolean; project_id?: string | null; active_dataset_id?: string | null; dataset_view?: DatasetView | null; model?: string; title?: string }>('/api/sessions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: options.signal,
    body: JSON.stringify({
      client_session_id: options.clientSessionId,
      model: options.model,
      project_id: options.projectId,
      active_dataset_id: options.activeDatasetId,
      initial_message_preview: options.initialMessagePreview,
      reasoning_mode: 'direct',
      planning_mode: 'auto',
    }),
  });
}
export async function activateSession(id: string, signal?: AbortSignal) {
  return json<{
    control?: RunControl | null;
    messages?: Message[];
    active_run?: ActiveRun | null;
    pending_feedback?: FeedbackRequest | null;
    project_id?: string | null;
    active_dataset_id?: string | null;
    has_graph?: boolean;
    dataset_view?: DatasetView | null;
    model?: string;
    default_model?: string;
    available_models?: string[];
  }>(`/api/sessions/${encodeURIComponent(id)}/activate`, { method: 'POST', signal });
}
export async function updateSessionModel(id: string, model: string) {
  return json<{ success?: boolean; session_id: string; model: string; available_models?: string[]; default_model?: string }>(`/api/sessions/${encodeURIComponent(id)}/model`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model }),
  });
}
export async function deleteSession(id: string) {
  // 202 acknowledges durable stop-and-delete; server cleanup survives disconnect.
  return json<{ success: boolean; delete_status?: 'requested' | 'deleted' }>(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export async function getMessages(id: string, signal?: AbortSignal) { return json<{ messages?: Message[] }>(`/api/sessions/${encodeURIComponent(id)}/messages`, { signal }); }
export async function listProjects() { return json<{ success?: boolean; projects?: Project[]; legacy_default_project_id?: string }>('/api/projects'); }
export async function createProject(name: string, description = '') {
  return json<{ success?: boolean; project: Project }>('/api/projects', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, description }),
  });
}
export async function updateProject(id: string, patch: { name?: string; description?: string }) {
  return json<{ success?: boolean; project: Project }>(`/api/projects/${encodeURIComponent(id)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
  });
}
export async function deleteProject(id: string) {
  return json<{ success?: boolean; project_id: string; delete_status: 'requested' | 'deleted' }>(`/api/projects/${encodeURIComponent(id)}`, {
    method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ conversation_action: 'delete' }),
  });
}

export async function listProjectConversations(id: string) {
  return json<{ success?: boolean; conversations?: Session[] }>(`/api/projects/${encodeURIComponent(id)}/conversations`);
}
export async function listProjectTrainingRuns(id: string, signal?: AbortSignal) {
  return json<{ success?: boolean; project_id: string; training_runs?: ProjectTrainingRun[]; has_running?: boolean }>(`/api/projects/${encodeURIComponent(id)}/training-runs`, { signal });
}
function datasetScopeUrl(projectId: string | null) {
  return projectId ? `/api/projects/${encodeURIComponent(projectId)}/datasets` : '/api/personal-datasets';
}
export async function listProjectDatasets(projectId: string | null, signal?: AbortSignal) {
  return json<{ success?: boolean; project_id: string | null; default_dataset_id?: string | null; datasets?: Dataset[] }>(datasetScopeUrl(projectId), { signal });
}
export async function uploadProjectDataset(projectId: string | null, file: File, sessionId?: string) {
  const body = new FormData();
  body.append('file', file);
  if (sessionId) body.append('session_id', sessionId);
  return json<{ success?: boolean; dataset: Dataset; default_dataset_id?: string | null; activated_session_id?: string | null; dataset_view?: DatasetView | null }>(datasetScopeUrl(projectId), { method: 'POST', body });
}
export async function renameProjectDataset(projectId: string | null, datasetId: string, name: string) {
  return json<{ success?: boolean; dataset: Dataset }>(`${datasetScopeUrl(projectId)}/${encodeURIComponent(datasetId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
  });
}
export async function setDefaultProjectDataset(projectId: string, datasetId: string) {
  return json<{ success?: boolean; default_dataset_id: string }>(`/api/projects/${encodeURIComponent(projectId)}/datasets/${encodeURIComponent(datasetId)}/set-default`, { method: 'POST' });
}
export async function deleteProjectDataset(projectId: string | null, datasetId: string) {
  return json<{ success?: boolean; default_dataset_id?: string | null; cleared_conversations?: number }>(`${datasetScopeUrl(projectId)}/${encodeURIComponent(datasetId)}`, { method: 'DELETE' });
}
export async function setSessionDataset(sessionId: string, datasetId: string | null) {
  return json<{ success?: boolean; session_id: string; project_id: string; active_dataset_id?: string | null; dataset_view?: DatasetView | null }>(`/api/sessions/${encodeURIComponent(sessionId)}/dataset`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ active_dataset_id: datasetId }),
  });
}
export async function uploadFile(sessionId: string, file: File, signal?: AbortSignal) {
  const body = new FormData(); body.append('file', file);
  return json<{ success: boolean; filename: string; file_token: string; dataset?: Dataset; construction_required?: boolean }>(`/api/sessions/${encodeURIComponent(sessionId)}/upload`, { method: 'POST', body, signal });
}

function constructionUrl(dataset: Pick<Dataset, 'project_id' | 'dataset_id'>) {
  return `${datasetScopeUrl(dataset.project_id)}/${encodeURIComponent(dataset.dataset_id)}/construction`;
}
export function getConstructionState(dataset: Dataset, signal?: AbortSignal) {
  return json<ConstructionState>(constructionUrl(dataset), { signal });
}
export function buildDatasetGraph(dataset: Dataset, options: ConstructionBuildOptions = {}) {
  return json<ConstructionState>(`${constructionUrl(dataset)}/build`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ options }),
  });
}
export function getConstructionGraph(dataset: Dataset, signal?: AbortSignal) {
  return json<ConstructionGraph>(`${constructionUrl(dataset)}/graph`, { signal });
}
export function previewConstruction(dataset: Dataset, baseRevision: number, operations: ConstructionOperation[]) {
  return json<ConstructionResult>(`${constructionUrl(dataset)}/preview`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ base_revision: baseRevision, operations }),
  });
}
export function commitConstruction(dataset: Dataset, baseRevision: number, operations: ConstructionOperation[]) {
  return json<ConstructionResult>(`${constructionUrl(dataset)}/commit`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ base_revision: baseRevision, operations }),
  });
}
export async function visualizeGraph(sessionId: string, options: Record<string, unknown> = {}, signal?: AbortSignal): Promise<GraphData> {
  const value = await json<unknown>(`/api/sessions/${encodeURIComponent(sessionId)}/graph/visualize`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ include_community: true, ...options }),
  });
  return parseGraphDataResponse(value);
}

export function parseGraphDataResponse(value: unknown): GraphData {
  if (isGraphData(value)) return value;
  const payload = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const message = payload.success === false
    ? String(payload.error || '图谱可视化失败')
    : '服务返回的图结构不完整';
  throw new ApiError(502, message, value, null, null, 'INVALID_GRAPH_RESPONSE');
}

export type StreamHandlers = {
  onOpen?: () => void;
  onActivity?: () => void;
  onEvent?: (event: StreamEvent) => void;
  onBatch?: (batch: StreamBatch) => void;
};

export class StreamProtocolError extends Error {}

function deliverStreamPayload(payload: unknown, sessionId: string, handlers: StreamHandlers) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new StreamProtocolError('无效的流事件');
  const value = payload as Record<string, any>;
  if (value.protocol_version === 2 && Array.isArray(value.events)) {
    if (!value.events.every((event: unknown) => event && typeof event === 'object' && typeof (event as StreamEvent).type === 'string')
      || (value.event_index != null && (!Number.isSafeInteger(value.event_index) || value.event_index < 0))) throw new StreamProtocolError('无效的 V2 事件批次');
    const batch: StreamBatch = {
      protocol_version: 2,
      session_id: String(value.session_id || sessionId),
      run_id: String(value.run_id || ''),
      event_index: value.event_index != null && Number.isFinite(Number(value.event_index)) ? Number(value.event_index) : null,
      events: value.events,
    };
    if (handlers.onBatch) handlers.onBatch(batch);
    else for (const event of batch.events) handlers.onEvent?.(event);
    return;
  }
  if (value.protocol_version === 2 || typeof value.type !== 'string') throw new StreamProtocolError('无效的流事件');
  const event = value as StreamEvent;
  if (handlers.onBatch) {
    handlers.onBatch({
      protocol_version: 1,
      session_id: String(event.session_id || sessionId),
      run_id: String(event.run_id || ''),
      event_index: event.event_index != null && Number.isFinite(Number(event.event_index)) ? Number(event.event_index) : null,
      events: [event],
    });
  } else handlers.onEvent?.(event);
}

export async function consumeSseResponse(response: Response, sessionId: string, handlers: StreamHandlers) {
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = ApiError.fromResponse(response.status, data, response.headers.get('Retry-After'));
    if (response.headers.get('X-iGraph-Feedback-Accepted') === 'false') error.feedbackAccepted = false;
    throw error;
  }
  if (!response.body) throw new Error('服务器没有返回流式响应');
  handlers.onOpen?.();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = createParser({
    onEvent: ({ data }) => {
      let payload: unknown;
      try { payload = JSON.parse(data); } catch { throw new StreamProtocolError('流事件 JSON 无效'); }
      deliverStreamPayload(payload, sessionId, handlers);
    },
    onError: error => { if (error.type === 'max-buffer-size-exceeded') throw new StreamProtocolError('流事件超过缓冲限制'); },
    maxBufferSize: 16 * 1024 * 1024,
  });
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      handlers.onActivity?.();
      parser.feed(decoder.decode(value, { stream: true }));
    }
    parser.feed(decoder.decode());
  } finally {
    // EOF is not a frame delimiter. Replay any incomplete final event.
    parser.reset();
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export type RunSnapshot = {
  session_id: string; run_id: string; status: string; messages: Message[];
  active_run: ActiveRun | null; pending_feedback: FeedbackRequest | null;
  snapshot_complete: boolean; resume_cursor: number | null;
  control?: RunControl | null;
};

export type RunControl = {
  run_id: string | null; start_request_id: string | null; registration_status: 'not_seen' | 'registered';
  run_status?: string; cancel_status?: 'none' | 'requested' | 'stopping' | 'confirmed';
  execution_stopped: boolean; terminal_persisted: boolean; pending_components?: string[];
  revision: number; active_run?: ActiveRun | null; can_send: boolean;
  native_run_created?: boolean;
};
export type RunTarget = { run_id: string; start_request_id?: never } | { start_request_id: string; run_id?: never };
export function getCapabilities(signal?: AbortSignal) {
  return json<{ run_cancel_v1: boolean; job_cancel_v1: boolean }>('/api/capabilities', { signal });
}
export function cancelRun(sessionId: string, target: RunTarget, cancelRequestId: string, signal?: AbortSignal) {
  return json<RunControl>(`/api/sessions/${encodeURIComponent(sessionId)}/runs/cancel`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target, cancel_request_id: cancelRequestId }),
  });
}
export function getRunControl(sessionId: string, target: RunTarget, signal?: AbortSignal) {
  const query = new URLSearchParams(Object.entries(target).filter((entry): entry is [string, string] => typeof entry[1] === 'string'));
  return json<RunControl>(`/api/sessions/${encodeURIComponent(sessionId)}/runs/control?${query}`, { signal });
}
export type JobControl = { job_id: string; execution_stopped: boolean; terminal_persisted: boolean; cancel_status: string };
export function getJobControl(sessionId: string, jobId: string, signal?: AbortSignal) {
  return json<JobControl>(`/api/sessions/${encodeURIComponent(sessionId)}/jobs/${encodeURIComponent(jobId)}/control`, { signal });
}
export function cancelJob(sessionId: string, jobId: string, cancelRequestId: string, signal?: AbortSignal) {
  return json<JobControl>(
    `/api/sessions/${encodeURIComponent(sessionId)}/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cancel_request_id: cancelRequestId }),
    });
}

export function getRunSnapshot(sessionId: string, runId: string, signal?: AbortSignal) {
  return json<RunSnapshot>(`/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/snapshot`, { signal });
}

export async function streamChat(sessionId: string, message: string, attachments: AttachmentRef[], handlers: StreamHandlers, signal: AbortSignal, startRequestId?: string) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (SESSION_STATE_V2) headers['X-iGraph-Stream-Version'] = '2';
  const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/chat/stream`, {
    method: 'POST', signal, headers,
    body: JSON.stringify({ message, start_request_id: startRequestId, attachments: attachments.length ? attachments : undefined, reasoning_mode: 'direct', planning_mode: 'auto', stream: true, background: true }),
  });
  await consumeSseResponse(response, sessionId, handlers);
}

export async function streamFeedback(sessionId: string, feedback: FeedbackRequest, decision: 'approve' | 'reject', decisionId: string, handlers: StreamHandlers, signal: AbortSignal) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (SESSION_STATE_V2) headers['X-iGraph-Stream-Version'] = '2';
  const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/feedback/stream`, {
    method: 'POST', signal, headers,
    body: JSON.stringify({
      run_id: feedback.run_id || feedback.request_id || '',
      request_id: feedback.request_id || '',
      decision_id: decisionId,
      decision,
      patched_args: feedback.proposed_args || {},
      comment: '',
      tool_call_id: feedback.call_id || '',
      tool_name: feedback.tool_name || '',
    }),
  });
  await consumeSseResponse(response, sessionId, handlers);
}

export async function resumeRun(sessionId: string, runId: string, lastEventIndex: number, handlers: StreamHandlers, signal: AbortSignal) {
  const query = new URLSearchParams({ last_event_index: String(lastEventIndex) });
  const headers: Record<string, string> = {};
  if (SESSION_STATE_V2) headers['X-iGraph-Stream-Version'] = '2';
  const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/resume?${query}`, {
    method: 'POST',
    signal,
    headers,
  });
  await consumeSseResponse(response, sessionId, handlers);
}

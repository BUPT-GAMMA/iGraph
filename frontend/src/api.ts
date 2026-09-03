import { isGraphData } from './types';
import type { ActiveRun, AttachmentRef, Dataset, DatasetView, FeedbackRequest, GraphData, Message, Project, ProjectTrainingRun, Session, StreamBatch, StreamEvent } from './types';

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

export async function apiFetch(path: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers);
  const userId = getUserId();
  if (userId) headers.set('X-User-ID', userId);
  return fetch(`${API_BASE}${path}`, { ...options, headers });
}

async function json<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await apiFetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw ApiError.fromResponse(response.status, data);
  return data as T;
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  code?: string;
  activeRun?: ActiveRun | null;
  pendingFeedback?: FeedbackRequest | null;

  constructor(status: number, message: string, detail?: unknown, activeRun?: ActiveRun | null, pendingFeedback?: FeedbackRequest | null, code?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.code = code;
    this.activeRun = activeRun;
    this.pendingFeedback = pendingFeedback;
  }

  static fromResponse(status: number, data: any) {
    const detail = data?.detail ?? data?.error;
    const body = detail && typeof detail === 'object' ? detail : data;
    const message = typeof detail === 'string'
      ? detail
      : String(body?.message || body?.error || `请求失败 (${status})`);
    return new ApiError(status, message, detail, body?.active_run || null, body?.pending_feedback || null, body?.code);
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
  projectId?: string;
  activeDatasetId?: string | null;
  initialMessagePreview?: string;
  signal?: AbortSignal;
} = {}) {
  return json<{ success?: boolean; session_id: string; reused?: boolean; project_id?: string; active_dataset_id?: string | null; dataset_view?: DatasetView | null; model?: string; title?: string }>('/api/sessions', {
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
    messages?: Message[];
    active_run?: ActiveRun | null;
    pending_feedback?: FeedbackRequest | null;
    project_id?: string;
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
export async function deleteSession(id: string) { return json<any>(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' }); }
export async function moveSession(id: string, projectId: string) {
  return json<any>(`/api/sessions/${encodeURIComponent(id)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: projectId }),
  });
}
export async function getMessages(id: string, signal?: AbortSignal) { return json<{ messages?: Message[] }>(`/api/sessions/${encodeURIComponent(id)}/messages`, { signal }); }
export async function listProjects() { return json<{ success?: boolean; projects?: Project[] }>('/api/projects'); }
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
export async function deleteProject(id: string, action: 'move_to_general' | 'delete' = 'move_to_general') {
  return json<{ success?: boolean; affected_conversations: number; general_project_id: string }>(`/api/projects/${encodeURIComponent(id)}`, {
    method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ conversation_action: action }),
  });
}
export async function listProjectConversations(id: string) {
  return json<{ success?: boolean; conversations?: Session[] }>(`/api/projects/${encodeURIComponent(id)}/conversations`);
}
export async function listProjectTrainingRuns(id: string, signal?: AbortSignal) {
  return json<{ success?: boolean; project_id: string; training_runs?: ProjectTrainingRun[]; has_running?: boolean }>(`/api/projects/${encodeURIComponent(id)}/training-runs`, { signal });
}
export async function listProjectDatasets(projectId: string, signal?: AbortSignal) {
  return json<{ success?: boolean; project_id: string; default_dataset_id?: string | null; datasets?: Dataset[] }>(`/api/projects/${encodeURIComponent(projectId)}/datasets`, { signal });
}
export async function uploadProjectDataset(projectId: string, file: File, sessionId?: string) {
  const body = new FormData();
  body.append('file', file);
  if (sessionId) body.append('session_id', sessionId);
  return json<{ success?: boolean; dataset: Dataset; default_dataset_id?: string | null; activated_session_id?: string | null; dataset_view?: DatasetView | null }>(`/api/projects/${encodeURIComponent(projectId)}/datasets`, { method: 'POST', body });
}
export async function renameProjectDataset(projectId: string, datasetId: string, name: string) {
  return json<{ success?: boolean; dataset: Dataset }>(`/api/projects/${encodeURIComponent(projectId)}/datasets/${encodeURIComponent(datasetId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
  });
}
export async function setDefaultProjectDataset(projectId: string, datasetId: string) {
  return json<{ success?: boolean; default_dataset_id: string }>(`/api/projects/${encodeURIComponent(projectId)}/datasets/${encodeURIComponent(datasetId)}/set-default`, { method: 'POST' });
}
export async function deleteProjectDataset(projectId: string, datasetId: string) {
  return json<{ success?: boolean; default_dataset_id?: string | null; cleared_conversations?: number }>(`/api/projects/${encodeURIComponent(projectId)}/datasets/${encodeURIComponent(datasetId)}`, { method: 'DELETE' });
}
export async function setSessionDataset(sessionId: string, datasetId: string | null) {
  return json<{ success?: boolean; session_id: string; project_id: string; active_dataset_id?: string | null; dataset_view?: DatasetView | null }>(`/api/sessions/${encodeURIComponent(sessionId)}/dataset`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ active_dataset_id: datasetId }),
  });
}
export async function uploadFile(sessionId: string, file: File, signal?: AbortSignal) {
  const body = new FormData(); body.append('file', file);
  return json<{ success: boolean; filename: string; file_token: string }>(`/api/sessions/${encodeURIComponent(sessionId)}/upload`, { method: 'POST', body, signal });
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
  onEvent?: (event: StreamEvent) => void;
  onBatch?: (batch: StreamBatch) => void;
};

function deliverStreamPayload(payload: unknown, sessionId: string, handlers: StreamHandlers) {
  if (!payload || typeof payload !== 'object') return;
  const value = payload as Record<string, any>;
  if (value.protocol_version === 2 && Array.isArray(value.events)) {
    const batch: StreamBatch = {
      protocol_version: 2,
      session_id: String(value.session_id || sessionId),
      run_id: String(value.run_id || ''),
      event_index: value.event_index != null && Number.isFinite(Number(value.event_index)) ? Number(value.event_index) : null,
      events: value.events.filter((event: unknown): event is StreamEvent => Boolean(event && typeof event === 'object')),
    };
    if (handlers.onBatch) handlers.onBatch(batch);
    else for (const event of batch.events) handlers.onEvent?.(event);
    return;
  }
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
    throw ApiError.fromResponse(response.status, data);
  }
  if (!response.body) throw new Error('服务器没有返回流式响应');
  handlers.onOpen?.();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const consumeFrames = (complete: boolean) => {
    buffer = buffer.replace(/\r\n/g, '\n');
    const frames = buffer.split('\n\n');
    buffer = complete ? '' : (frames.pop() || '');
    for (const frame of frames) {
      const data = frame.split('\n')
        .filter(line => line.startsWith('data:'))
        .map(line => line.slice(5).trimStart())
        .join('\n');
      if (!data) continue;
      try { deliverStreamPayload(JSON.parse(data), sessionId, handlers); } catch { /* Ignore malformed keep-alive frames. */ }
    }
  };
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    consumeFrames(false);
  }
  buffer += decoder.decode();
  consumeFrames(false);
  // An unterminated tail may be half of a JSON frame. Do not dispatch it or
  // advance the resume cursor; the server will replay it on reconnect.
}

export async function streamChat(sessionId: string, message: string, attachments: AttachmentRef[], handlers: StreamHandlers, signal: AbortSignal) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (SESSION_STATE_V2) headers['X-iGraph-Stream-Version'] = '2';
  const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/chat/stream`, {
    method: 'POST', signal, headers,
    body: JSON.stringify({ message, attachments: attachments.length ? attachments : undefined, reasoning_mode: 'direct', planning_mode: 'auto', stream: true, background: true }),
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

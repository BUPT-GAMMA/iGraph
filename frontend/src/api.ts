import type { AttachmentRef, Dataset, FeedbackRequest, GraphData, Message, Project, Session, StreamEvent } from './types';

const API_BASE = import.meta.env.VITE_API_BASE || '';
export const USER_KEY = 'igraph_user_id';

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
  if (!response.ok) throw new Error(data.detail || data.error || `请求失败 (${response.status})`);
  return data as T;
}

export async function validateUser(userId: string) {
  const data = await json<{ valid?: boolean; available_models?: string[]; default_model?: string }>('/api/auth/validate', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-User-ID': userId },
  });
  return data;
}

export async function listSessions() { return json<{ success?: boolean; sessions?: Session[] }>('/api/sessions'); }
export async function createSession(model = 'qwen3.6-plus', projectId?: string) {
  return json<{ success?: boolean; session_id: string; project_id?: string; active_dataset_id?: string | null; model?: string }>('/api/sessions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, project_id: projectId, reasoning_mode: 'direct', planning_mode: 'auto' }),
  });
}
export async function activateSession(id: string, signal?: AbortSignal) { return json<any>(`/api/sessions/${encodeURIComponent(id)}/activate`, { method: 'POST', signal }); }
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
export async function listProjectDatasets(projectId: string, signal?: AbortSignal) {
  return json<{ success?: boolean; project_id: string; default_dataset_id?: string | null; datasets?: Dataset[] }>(`/api/projects/${encodeURIComponent(projectId)}/datasets`, { signal });
}
export async function uploadProjectDataset(projectId: string, file: File, sessionId?: string) {
  const body = new FormData();
  body.append('file', file);
  if (sessionId) body.append('session_id', sessionId);
  return json<{ success?: boolean; dataset: Dataset; default_dataset_id?: string | null; activated_session_id?: string | null }>(`/api/projects/${encodeURIComponent(projectId)}/datasets`, { method: 'POST', body });
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
  return json<{ success?: boolean; session_id: string; project_id: string; active_dataset_id?: string | null }>(`/api/sessions/${encodeURIComponent(sessionId)}/dataset`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ active_dataset_id: datasetId }),
  });
}
export async function uploadFile(sessionId: string, file: File) {
  const body = new FormData(); body.append('file', file);
  return json<{ success: boolean; filename: string; file_token: string }>(`/api/sessions/${encodeURIComponent(sessionId)}/upload`, { method: 'POST', body });
}
export async function visualizeGraph(sessionId: string, options: Record<string, unknown> = {}, signal?: AbortSignal): Promise<GraphData> {
  return json<GraphData>(`/api/sessions/${encodeURIComponent(sessionId)}/graph/visualize`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ include_community: true, ...options }),
  });
}

export type StreamHandlers = { onEvent: (event: StreamEvent) => void };
export async function streamChat(sessionId: string, message: string, attachments: AttachmentRef[], handlers: StreamHandlers, signal: AbortSignal) {
  const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/chat/stream`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, attachments: attachments.length ? attachments : undefined, reasoning_mode: 'direct', planning_mode: 'auto', stream: true, background: true }),
  });
  if (!response.ok || !response.body) throw new Error(`流式请求失败 (${response.status})`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n'); buffer = lines.pop() || '';
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      try { handlers.onEvent(JSON.parse(line.slice(6))); } catch { /* ignore keep-alive or partial frames */ }
    }
  }
}

export async function streamFeedback(sessionId: string, feedback: FeedbackRequest, decision: 'approve' | 'reject', handlers: StreamHandlers, signal: AbortSignal) {
  const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/feedback/stream`, {
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      request_id: feedback.request_id || '',
      decision,
      patched_args: feedback.proposed_args || {},
      comment: '',
      tool_call_id: feedback.call_id || '',
      tool_name: feedback.tool_name || '',
    }),
  });
  if (!response.ok || !response.body) throw new Error(`确认请求失败 (${response.status})`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n'); buffer = lines.pop() || '';
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      try { handlers.onEvent(JSON.parse(line.slice(6))); } catch { /* ignore keep-alive or partial frames */ }
    }
  }
}

import { isGraphData } from './types';
import { createParser } from 'eventsource-parser';
import type { ActiveRun, AttachmentRef, ConstructionBuildOptions, ConstructionGraph, ConstructionOperation, ConstructionResult, ConstructionSchemaResponse, ConstructionSourcePreview, ConstructionState, CsvBundleSchema, Dataset, DatasetView, FeedbackRequest, GraphData, Message, Project, ProjectTrainingRun, ServerDataset, Session, StreamBatch, StreamEvent } from './types';
import { currentLanguage, tr } from './i18n';

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

export type ModelConfiguration = CustomModel & {
  config_id: string;
  revision: number;
  upstream_model_id: string;
  source: 'deployment' | 'custom';
  enabled: boolean;
  tools: boolean;
  vision: boolean;
  context_window: number;
  max_output_tokens: number;
  unavailable_reason?: string | null;
};
export type ModelCatalog = { models: ModelConfiguration[]; default_model: string };
export const listModels = () => json<ModelCatalog>('/api/models');

export function modelErrorMessage(code?: string | null) {
  const messages: Record<string, string> = {
    MODEL_REMOVED: tr('Model configuration removed. Select another model.', '模型配置已删除，请选择其他模型'),
    MODEL_DISABLED: tr('Model disabled. Select another model.', '模型已停用，请选择其他模型'),
    MODEL_NOT_CONFIGURED: tr('Endpoint or API key not configured', '尚未配置接口地址或密钥'),
    MODEL_NOT_ALLOWED: tr('Model is not available for this account', '此账户不可使用该模型'),
    MODEL_TOOLS_REQUIRED: tr('Graph analysis requires tool calling', '图分析需要模型支持工具调用'),
    MODEL_CONFIG_CHANGED: tr('Configuration changed. Refresh and try again.', '配置已变化，请刷新后重试'),
    MODEL_SELECTION_CHANGED: tr('The model changed in another page. Refresh this conversation before sending.', '模型已在其他页面更改，请重新打开此会话后再发送'),
    MODEL_IN_USE: tr('An active task uses this model. Wait for it to finish or stop it before deleting.', '有活动任务正在使用此模型，请等待结束或停止任务后再删除'),
    MODEL_AUTH_FAILED: tr('Authentication failed. Check the API key.', '认证失败，请检查 API Key'),
    MODEL_RATE_LIMITED: tr('Rate limit or quota reached', '调用频率或额度受限'),
    MODEL_TEST_FAILED: tr('The endpoint rejected this model or tool protocol. Check configuration.', '接口拒绝了该型号或工具协议，请检查配置'),
    MODEL_CONNECTION_FAILED: tr('Could not connect to the model endpoint', '无法连接模型接口'),
    MODEL_INVALID_RESPONSE: tr('The endpoint returned an unsupported response', '接口返回的数据格式不受支持'),
    MODEL_INVALID_CONFIG: tr('Enter a valid model ID and HTTP(S) endpoint without embedded credentials.', '请填写有效模型 ID 和 HTTP(S) 地址，地址中不能包含凭据'),
    MODEL_INVALID_LIMITS: tr('Output limit must be smaller than the context window.', '输出上限必须小于上下文窗口'),
    MODEL_CONTROLS_REQUIRED: tr('Enable IGRAPH_RUN_CANCEL_ENABLED on all services to use model management.', '请为所有服务启用 IGRAPH_RUN_CANCEL_ENABLED 后使用模型管理'),
    MODEL_CONTEXT_TOO_LARGE: tr('This model cannot fit the current input. Use a larger-context model or shorten the input.', '当前输入超出模型容量，请选择更大上下文的模型或缩短输入'),
    MODEL_MEDIA_UNSUPPORTED: tr('This model cannot read the attachments in this conversation. Select a compatible model.', '此模型无法读取会话中的附件，请选择支持这些附件的模型'),
    MODEL_RUN_CONFIG_UNAVAILABLE: tr('The task model configuration is unavailable. No alternate model was used.', '本轮模型配置暂不可用，未切换到其他模型'),
  };
  return code ? messages[code] || code : '';
}

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
  if (!headers.has('Accept-Language')) headers.set('Accept-Language', currentLanguage() === 'zh' ? 'zh-CN,zh;q=0.9,en;q=0.7' : 'en-US,en;q=0.9');
  if (!headers.has('X-User-ID')) {
    const storedUserId = getUserId();
    const userId = requestAccount?.userId ?? storedUserId;
    // A storage event can arrive after a click. Never send the old workspace's
    // content with an identity read from another tab; keep the header pinned.
    if (userId !== storedUserId) {
      throw new ApiError(409, tr('The account changed in another tab. Wait for it to sync, then try again.', '账户已在其他标签页切换，请等待同步后重试'), null, null, null, 'ACCOUNT_CHANGED');
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
          return `${location ? `${location}: ` : ''}${String(item.msg || item.message || tr('Invalid parameter', '参数无效'))}`;
        }).filter(Boolean).join(tr('; ', '；'))
      : '';
    const message = typeof detail === 'string'
      ? detail
      : validationMessage || (body?.code?.startsWith('MODEL_') ? modelErrorMessage(body.code) : '') || ({
        JOB_IDEMPOTENCY_CONFLICT: tr('The data-loading operation ID conflicts with another request. Try again, or contact an administrator if this continues.', '数据加载操作编号冲突，请重试；若持续出现，请联系维护者'),
        EDIT_TARGET_CHANGED: tr('The latest turn changed. Refresh and try again.', '最新一轮已变化，请刷新后重试'),
        EDIT_TARGET_NOT_FINISHED: tr('Wait for the current turn to finish before editing it.', '请等待本轮结束后再修改'),
        EDIT_UNAVAILABLE: tr('Conversation editing is not available on this server.', '当前服务暂不支持修改对话'),
      } as Record<string, string>)[body?.code] || String(body?.message || body?.error || tr(`Request failed (${status})`, `请求失败 (${status})`));
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

export async function listSessions(options: { cursor?: string; ids?: string[]; limit?: number } = {}) {
  const query = new URLSearchParams();
  if (options.cursor) query.set('cursor', options.cursor);
  if (options.limit) query.set('limit', String(options.limit));
  for (const id of options.ids || []) query.append('ids', id);
  return json<{ success?: boolean; sessions?: Session[]; next_cursor?: string | null; has_more?: boolean }>(`/api/sessions${query.size ? `?${query}` : ''}`);
}
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
    model_revision?: number;
    model_unavailable_reason?: string | null;
    default_model?: string;
    available_models?: string[];
  }>(`/api/sessions/${encodeURIComponent(id)}/activate`, { method: 'POST', signal });
}
export async function getSessionModel(id: string) {
  return json<{ model: string; model_revision: number; model_unavailable_reason?: string | null }>(`/api/sessions/${encodeURIComponent(id)}/model`);
}
export async function updateSessionModel(id: string, model: string, modelRevision = 0) {
  return json<{ success?: boolean; session_id: string; model: string; model_revision?: number; available_models?: string[]; default_model?: string }>(`/api/sessions/${encodeURIComponent(id)}/model`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model, model_revision: modelRevision }),
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
export async function listServerDatasets(signal?: AbortSignal) {
  return json<{ success?: boolean; datasets?: ServerDataset[] }>('/api/server-datasets', { signal });
}
export async function importServerDataset(projectId: string, serverDatasetId: string, sessionId?: string) {
  return json<{ success?: boolean; created?: boolean; dataset: Dataset; default_dataset_id?: string | null; activated_session_id?: string | null; dataset_view?: DatasetView | null }>(`/api/projects/${encodeURIComponent(projectId)}/datasets/from-server`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ server_dataset_id: serverDatasetId, session_id: sessionId || '' }),
  });
}
export async function uploadProjectDataset(projectId: string | null, file: File | File[], sessionId?: string, csvSchema?: CsvBundleSchema, name?: string, description?: string) {
  const body = new FormData();
  const selected = Array.isArray(file) ? file : [file];
  if (csvSchema) {
    selected.forEach(item => body.append('files', item));
    body.append('csv_schema', JSON.stringify(csvSchema));
    if (name) body.append('name', name);
  } else if (selected[0]) body.append('file', selected[0]);
  if (sessionId) body.append('session_id', sessionId);
  if (description !== undefined) body.append('description', description);
  return json<{ success?: boolean; dataset: Dataset; default_dataset_id?: string | null; activated_session_id?: string | null; dataset_view?: DatasetView | null }>(datasetScopeUrl(projectId), { method: 'POST', body });
}
export async function renameProjectDataset(projectId: string | null, datasetId: string, name: string) {
  return updateProjectDataset(projectId, datasetId, { name });
}
export async function updateProjectDataset(projectId: string | null, datasetId: string, patch: { name?: string; description?: string }) {
  return json<{ success?: boolean; dataset: Dataset }>(`${datasetScopeUrl(projectId)}/${encodeURIComponent(datasetId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
  });
}
export async function listConstructedDatasets(projectId: string, signal?: AbortSignal) {
  return json<{ datasets: Dataset[] }>(`/api/projects/${encodeURIComponent(projectId)}/constructed-datasets`, { signal });
}
export async function importConstructedDataset(projectId: string, datasetId: string) {
  return json<{ dataset: Dataset; default_dataset_id?: string | null }>(`${datasetScopeUrl(projectId)}/import-constructed`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_dataset_id: datasetId }),
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
export type UploadResult = { success: boolean; filename: string; file_token: string; dataset?: Dataset; construction_required?: boolean };

export async function uploadFile(sessionId: string, file: File, signal?: AbortSignal, onProgress?: (percent: number) => void): Promise<UploadResult> {
  const body = new FormData(); body.append('file', file);
  const path = `/api/sessions/${encodeURIComponent(sessionId)}/upload`;
  const account = requestAccount;
  const identity = account?.userId ?? getUserId();
  const accountChanged = () => identity !== getUserId() || (account !== undefined && account !== requestAccount);
  const accountError = () => new ApiError(409, tr('The account changed. Select the file again.', '账户已切换，请重新选择文件'), null, null, null, 'ACCOUNT_CHANGED');
  if (accountChanged()) throw accountError();
  const result = onProgress
    ? await new Promise<UploadResult>((resolve, reject) => {
      if (signal?.aborted) { reject(new DOMException(tr('Upload cancelled', '上传已取消'), 'AbortError')); return; }
      const xhr = new XMLHttpRequest();
      let settled = false;
      const abort = () => { xhr.abort(); finish(new DOMException(tr('Upload cancelled', '上传已取消'), 'AbortError')); };
      function finish(error?: Error, data?: UploadResult) {
        if (settled) return;
        settled = true;
        signal?.removeEventListener('abort', abort);
        if (error) reject(error); else resolve(data!);
      }
      xhr.open('POST', `${API_BASE}${path}`);
      if (identity) xhr.setRequestHeader('X-User-ID', identity);
      xhr.setRequestHeader('Accept-Language', currentLanguage() === 'zh' ? 'zh-CN,zh;q=0.9,en;q=0.7' : 'en-US,en;q=0.9');
      xhr.upload.onprogress = event => {
        if (accountChanged()) { finish(accountError()); xhr.abort(); return; }
        if (!settled && event.lengthComputable && event.total > 0) onProgress(Math.min(100, Math.round(event.loaded / event.total * 100)));
      };
      xhr.onload = () => {
        if (accountChanged()) { finish(accountError()); return; }
        let data: UploadResult;
        try { data = JSON.parse(xhr.responseText); }
        catch { finish(new ApiError(xhr.status, tr('The upload response was invalid. Try again.', '上传响应无效，请重试'))); return; }
        if (xhr.status < 200 || xhr.status >= 300) { finish(ApiError.fromResponse(xhr.status, data, xhr.getResponseHeader('Retry-After'))); return; }
        if (data?.success !== true || typeof data.file_token !== 'string' || !data.file_token) { finish(new ApiError(xhr.status, tr('The upload did not complete. Try again.', '上传未成功，请重试'))); return; }
        finish(undefined, data);
      };
      xhr.onerror = () => finish(new Error(tr('Upload failed. Check your connection and try again.', '上传失败，请检查网络后重试')));
      xhr.ontimeout = () => finish(new Error(tr('Upload timed out. Try again.', '上传超时，请重试')));
      xhr.onabort = () => finish(new DOMException(tr('Upload cancelled', '上传已取消'), 'AbortError'));
      signal?.addEventListener('abort', abort, { once: true });
      try { xhr.send(body); } catch (cause) { finish(cause instanceof Error ? cause : new Error(tr('Upload failed', '上传失败'))); }
    })
    : await json<UploadResult>(path, { method: 'POST', body, signal });
  if (accountChanged()) throw accountError();
  window.dispatchEvent(new CustomEvent('igraph:workspace-changed', { detail: { sessionId } }));
  return result;
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
export function getConstructionSchema(dataset: Dataset, signal?: AbortSignal) {
  return json<ConstructionSchemaResponse>(`${constructionUrl(dataset)}/schema`, { signal });
}
export function getConstructionSourcePreview(dataset: Dataset, signal?: AbortSignal, delimiter?: string) {
  const query = delimiter ? `?delimiter=${encodeURIComponent(delimiter)}` : '';
  return json<ConstructionSourcePreview>(`${constructionUrl(dataset)}/source-preview${query}`, { signal });
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
    method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ include_community: true, max_nodes: 800, ...options }),
  });
  return parseGraphDataResponse(value);
}

export function parseGraphDataResponse(value: unknown): GraphData {
  if (isGraphData(value)) return value;
  const payload = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const message = payload.success === false
    ? String(payload.error || tr('Graph visualization failed', '图谱可视化失败'))
    : tr('The server returned an incomplete graph structure', '服务返回的图结构不完整');
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
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new StreamProtocolError(tr('Invalid stream event', '无效的流事件'));
  const value = payload as Record<string, any>;
  if (value.protocol_version === 2 && Array.isArray(value.events)) {
    if (!value.events.every((event: unknown) => event && typeof event === 'object' && typeof (event as StreamEvent).type === 'string')
      || (value.event_index != null && (!Number.isSafeInteger(value.event_index) || value.event_index < 0))) throw new StreamProtocolError(tr('Invalid V2 event batch', '无效的 V2 事件批次'));
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
  if (value.protocol_version === 2 || typeof value.type !== 'string') throw new StreamProtocolError(tr('Invalid stream event', '无效的流事件'));
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
  if (!response.body) throw new Error(tr('The server did not return a streaming response', '服务器没有返回流式响应'));
  handlers.onOpen?.();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = createParser({
    onEvent: ({ data }) => {
      let payload: unknown;
      try { payload = JSON.parse(data); } catch { throw new StreamProtocolError(tr('Invalid stream-event JSON', '流事件 JSON 无效')); }
      deliverStreamPayload(payload, sessionId, handlers);
    },
    onError: error => { if (error.type === 'max-buffer-size-exceeded') throw new StreamProtocolError(tr('Stream event exceeded the buffer limit', '流事件超过缓冲限制')); },
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
  return json<{ run_cancel_v1: boolean; job_cancel_v1: boolean; edit_latest_turn_v1?: boolean }>('/api/capabilities', { signal });
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

export async function streamChat(sessionId: string, message: string, attachments: AttachmentRef[], handlers: StreamHandlers, signal: AbortSignal, startRequestId?: string, regenerateOf?: string, replaceRunId?: string, modelRevision?: number) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (SESSION_STATE_V2) headers['X-iGraph-Stream-Version'] = '2';
  const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/chat/stream`, {
    method: 'POST', signal, headers,
    body: JSON.stringify({ message, model_revision: modelRevision, start_request_id: startRequestId, ...(replaceRunId ? { replace_run_id: replaceRunId } : {}), ...(regenerateOf ? { regenerate: true, regenerate_of: regenerateOf } : {}), attachments: attachments.length ? attachments : undefined, reasoning_mode: 'direct', planning_mode: 'auto', stream: true, background: true }),
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

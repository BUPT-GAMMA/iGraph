import { useEffect, useReducer, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { activateSession, ApiError, createProject as addProject, createSession, deleteProject as removeProject, deleteProjectDataset, deleteSession as removeSession, getMessages, getUserId, listProjectConversations, listProjectDatasets, listProjects, listSessions, moveSession, renameProjectDataset, resumeRun, setDefaultProjectDataset, setSessionDataset, setUserId, streamChat, streamFeedback, updateProject as patchProject, updateSessionModel, uploadFile, uploadProjectDataset, validateUser, visualizeGraph } from './api';
import type { ActiveRun, AttachmentRef, Dataset, DatasetView, DraftAttachment, FeedbackRequest, GraphData, GraphViewState, Message, NewConversationDraft, Project, Session, StreamBatch, ToolCall } from './types';
import { RunConnectionManager } from './RunConnectionManager';
import { conversationReducer, createSessionView, initialConversationState, selectBottomInteraction, selectMessages, selectRunGate, selectRunTools } from './sessionState';
import { DatasetPanel } from './components/DatasetPanel';
import { ChatPane } from './components/ChatPane';
import { GraphWorkspace } from './components/GraphWorkspace';
import { Icon } from './components/Icon';
import { ManagementPanel } from './components/ManagementPanel';
import { DeleteProjectDialog, MoveConversationDialog, ProjectDialog } from './components/ProjectDialogs';
import { ProjectWorkspace } from './components/ProjectWorkspace';
import { Sidebar } from './components/Sidebar';
import { TopBar } from './components/TopBar';
import { parseWorkspaceRoute, writeWorkspaceRoute } from './navigation';
import './styles.css';

type GraphTab = 'Overview' | 'Graph' | 'Data' | 'ML' | 'Artifacts';
type ResizeSide = 'sidebar' | 'graph';
const SIDEBAR_DEFAULT = 258;
const GRAPH_DEFAULT = 382;
const SIDEBAR_MIN = 190;
const SIDEBAR_MAX = 420;
const GRAPH_MIN = 280;
const GRAPH_MAX = 720;
const MAIN_MIN = 420;
const DIVIDER_WIDTH = 6;
const COLLAPSED_PANEL_WIDTH = 52;
const DEFAULT_MODEL = 'qwen3.6-plus';
const demoMode = new URLSearchParams(window.location.search).get('demo') === '1';
const demoGraph: GraphData = {
  nodes: ['A', 'B', 'C', 'D', 'E', 'F'].map(id => ({ id, label: id, type: 'entity' })),
  edges: [{ src: 'A', dst: 'B' }, { src: 'A', dst: 'C' }, { src: 'B', dst: 'D' }, { src: 'C', dst: 'D' }, { src: 'D', dst: 'E' }, { src: 'E', dst: 'F' }],
  directed: true, num_nodes: 6, num_edges: 6,
};
const demoTrainingHistory = Array.from({ length: 24 }, (_, index) => {
  const epoch = index + 1;
  return {
    kind: 'train_epoch', epoch, epochs: 24,
    loss: Number((1.35 * Math.exp(-epoch / 7) + .28).toFixed(4)),
    train_acc: Number((.54 + .4 * (1 - Math.exp(-epoch / 8))).toFixed(4)),
    val_acc: Number((.5 + .34 * (1 - Math.exp(-epoch / 7)) - Math.max(0, epoch - 16) * .003).toFixed(4)),
  };
});
const demoMlTools: ToolCall[] = [{
  tool: 'train', call_id: 'demo-train', done: true,
  args: { task: 'node_classification', model: 'GCN', train_config: { epochs: 24, lr: .01 } },
  history: demoTrainingHistory,
  result: { task: 'node_classification', model: 'GCN', history: demoTrainingHistory, metrics: { train_loss: .3238, train_acc: .9201, val_acc: .8026, best_epoch: 19, trained_epochs: 24 } },
}];

function clamp(value: number, minimum: number, maximum: number) { return Math.min(Math.max(value, minimum), Math.max(minimum, maximum)); }

function storedWidth(key: string, fallback: number, minimum: number, maximum: number) {
  const value = Number(localStorage.getItem(key));
  return Number.isFinite(value) && value > 0 ? clamp(value, minimum, maximum) : fallback;
}

function ColumnDivider({ side, value, minimum, maximum, onPointerDown, onNudge, onReset }: { side: ResizeSide; value: number; minimum: number; maximum: number; onPointerDown: (side: ResizeSide, event: ReactPointerEvent<HTMLDivElement>) => void; onNudge: (side: ResizeSide, delta: number) => void; onReset: (side: ResizeSide) => void }) {
  const label = side === 'sidebar' ? '调整左侧栏宽度' : '调整图谱工作台宽度';
  function onKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const direction = event.key === 'ArrowRight' ? 1 : -1;
    onNudge(side, side === 'sidebar' ? direction * 16 : direction * -16);
  }
  return <div className={`column-divider column-divider-${side}`} role="separator" aria-label={label} aria-orientation="vertical" aria-valuemin={minimum} aria-valuemax={maximum} aria-valuenow={Math.round(value)} tabIndex={0} title={`${label}，双击恢复默认宽度`} onPointerDown={event => onPointerDown(side, event)} onKeyDown={onKeyDown} onDoubleClick={() => onReset(side)}><span /></div>;
}

function normalizeMessages(input: unknown): Message[] {
  if (!Array.isArray(input)) return [];
  return input.map((item, index) => ({ id: item?.id || item?.message_id || `history:${index}`, role: item?.role === 'user' ? 'user' : 'assistant', content: String(item?.content || ''), tools: item?.tools || [], plan: item?.plan || null, graphs: item?.graphs || [], reports: item?.reports || [] }));
}

function mergeSessionLists(current: Session[], incoming: Session[]) {
  const byId = new Map(current.map(session => [session.session_id, session]));
  for (const session of incoming) byId.set(session.session_id, { ...(byId.get(session.session_id) || {}), ...session });
  return [...byId.values()].sort((left, right) => String(right.updated_at || right.created_at || '').localeCompare(String(left.updated_at || left.created_at || '')));
}

function normalizedModels(input: unknown, selected = '') {
  const values = Array.isArray(input) ? input.filter((item): item is string => typeof item === 'string' && Boolean(item.trim())) : [];
  return Array.from(new Set([...values, selected].filter(Boolean)));
}

function requestWasAborted(cause: unknown) {
  return Boolean(cause && typeof cause === 'object' && 'name' in cause && (cause as { name?: string }).name === 'AbortError');
}

function unsupportedGraphView(datasetView: DatasetView | null | undefined): GraphViewState | null {
  if (datasetView?.kind !== 'graph_collection') return null;
  return { status: 'unsupported', data: null, code: 'NON_SINGLE_GRAPH', summary: datasetView.summary || {} };
}

function unsupportedGraphViewFromError(cause: unknown): GraphViewState | null {
  if (!(cause instanceof ApiError) || cause.code !== 'GRAPH_NOT_VISUALIZABLE') return null;
  const detail = cause.detail && typeof cause.detail === 'object' ? cause.detail as Record<string, unknown> : {};
  return unsupportedGraphView(detail.dataset_view as DatasetView | undefined);
}

function graphTotalNodes(graph: GraphData | null) {
  return graph ? Number(graph.total_nodes ?? graph.num_nodes ?? graph.nodes.length) : 0;
}

function graphTotalEdges(graph: GraphData | null) {
  return graph ? Number(graph.total_edges ?? graph.num_edges ?? graph.edges.length) : 0;
}

type PendingFirstTurn = {
  message: string;
  attachments: AttachmentRef[];
  pendingAttachments: Array<Pick<DraftAttachment, 'localId' | 'filename' | 'status' | 'error'>>;
  model: string;
  projectId: string;
  activeDatasetId: string | null;
};

function draftStorageKey(userId: string, projectId: string) {
  return `igraph-new-session-draft:v1:${userId}:${projectId}`;
}

function pendingFirstTurnStorageKey(userId: string, sessionId: string) {
  return `igraph-pending-first-turn:v1:${userId}:${sessionId}`;
}

function serializableDraft(draft: NewConversationDraft): NewConversationDraft {
  return {
    ...draft,
    phase: draft.phase === 'editing' || draft.phase === 'failed' ? draft.phase : 'failed',
    attachments: draft.attachments.map(attachment => ({
      localId: attachment.localId,
      filename: attachment.filename,
      fileToken: attachment.fileToken,
      status: attachment.fileToken ? 'uploaded' : 'needs_reselect',
      error: attachment.fileToken ? undefined : '刷新后需要重新选择文件',
    })),
    error: draft.phase === 'editing' || draft.phase === 'failed' ? draft.error : '创建流程被页面刷新中断，请重试',
  };
}

function readStoredDraft(userId: string, projectId: string): NewConversationDraft | null {
  try {
    const value = JSON.parse(localStorage.getItem(draftStorageKey(userId, projectId)) || 'null');
    if (!value || value.projectId !== projectId || typeof value.draftId !== 'string') return null;
    return serializableDraft(value as NewConversationDraft);
  } catch { return null; }
}

function readPendingFirstTurn(userId: string, sessionId: string): PendingFirstTurn | null {
  try {
    const value = JSON.parse(localStorage.getItem(pendingFirstTurnStorageKey(userId, sessionId)) || 'null');
    return value && typeof value.message === 'string' && Array.isArray(value.attachments) && Array.isArray(value.pendingAttachments)
      ? value as PendingFirstTurn
      : null;
  } catch { return null; }
}

function App() {
  const [userId, setUser] = useState(getUserId() || (demoMode ? 'demo' : ''));
  const [loginValue, setLoginValue] = useState(getUserId());
  const [loginError, setLoginError] = useState('');
  const [ready, setReady] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeProject, setActiveProject] = useState<Project | null>(null);
  const [conversation, dispatchConversation] = useReducer(conversationReducer, initialConversationState, state => {
    if (!demoMode) return state;
    const view = { ...createSessionView('demo-session', DEFAULT_MODEL, Date.now()), hydration: 'ready' as const, graph: { status: 'ready' as const, data: demoGraph } };
    return { activeSessionId: 'demo-session', activeDraftProjectId: null, viewsBySessionId: { 'demo-session': view }, draftsByProjectId: {} };
  });
  const [graphTab, setGraphTab] = useState<GraphTab>('Graph');
  const [graphOpen, setGraphOpen] = useState(true);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [workspaceError, setWorkspaceError] = useState('');
  const [datasetsByProject, setDatasetsByProject] = useState<Record<string, Dataset[]>>({});
  const [datasetOpen, setDatasetOpen] = useState(false);
  const [managementOpen, setManagementOpen] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [managementTab, setManagementTab] = useState<'memory' | 'skills'>('memory');
  const [defaultModel, setDefaultModel] = useState(DEFAULT_MODEL);
  const [availableModels, setAvailableModels] = useState<string[]>([DEFAULT_MODEL]);
  const [projectEditor, setProjectEditor] = useState<'create' | 'edit' | null>(null);
  const [projectEditorTarget, setProjectEditorTarget] = useState<Project | null>(null);
  const [deleteProjectTarget, setDeleteProjectTarget] = useState<Project | null>(null);
  const [moveSessionTarget, setMoveSessionTarget] = useState<Session | null>(null);
  const [dark, setDark] = useState(() => localStorage.getItem('igraph-theme') === 'dark');
  const [sidebarWidth, setSidebarWidth] = useState(() => storedWidth('igraph-layout-sidebar-width', window.innerWidth <= 1280 ? 230 : SIDEBAR_DEFAULT, SIDEBAR_MIN, SIDEBAR_MAX));
  const [graphWidth, setGraphWidth] = useState(() => storedWidth('igraph-layout-graph-width', window.innerWidth <= 1280 ? 330 : GRAPH_DEFAULT, GRAPH_MIN, GRAPH_MAX));
  const [graphLayoutVersion, setGraphLayoutVersion] = useState(0);
  const [composerFocusRequest, setComposerFocusRequest] = useState(0);
  const conversationRef = useRef(conversation);
  const connectionManagerRef = useRef(new RunConnectionManager(4));
  const hydrationVersionsRef = useRef(new Map<string, number>());
  const hydrationControllersRef = useRef(new Map<string, AbortController>());
  const draftCreationControllersRef = useRef(new Map<string, AbortController>());
  const preparationControllersRef = useRef(new Map<string, AbortController>());
  const graphVersionsRef = useRef(new Map<string, number>());
  const bodyRef = useRef<HTMLDivElement>(null);
  const resizeRef = useRef<{ side: ResizeSide; startX: number; startWidth: number } | null>(null);
  const layoutRef = useRef({ sidebarWidth, graphWidth, graphOpen, sidebarCollapsed, hasGraphPanel: false });

  conversationRef.current = conversation;
  const activeSessionId = conversation.activeSessionId;
  const activeSession = activeSessionId ? sessions.find(session => session.session_id === activeSessionId) || null : null;
  layoutRef.current = { sidebarWidth, graphWidth, graphOpen, sidebarCollapsed, hasGraphPanel: Boolean(activeSession) };
  const activeView = activeSessionId ? conversation.viewsBySessionId[activeSessionId] : null;
  const activeDraftProjectId = conversation.activeDraftProjectId;
  const activeDraft = activeDraftProjectId ? conversation.draftsByProjectId[activeDraftProjectId] || null : null;
  const draftProject = activeDraft ? projects.find(project => project.project_id === activeDraft.projectId) || null : null;
  const visibleProjectId = activeProject?.project_id || activeDraft?.projectId || activeSession?.project_id || '';
  const datasets = visibleProjectId ? datasetsByProject[visibleProjectId] || [] : [];
  const messages = selectMessages(activeView);
  const graphView: GraphViewState = activeView?.graph || { status: 'empty', data: null };
  const graph = graphView.status === 'ready' ? graphView.data : null;
  const selectedNode = activeView?.selectedNode || null;
  const composerValue = activeDraft?.text ?? activeView?.draft ?? '';
  const model = activeDraft?.model || activeView?.model || defaultModel;
  const runTools = selectRunTools(activeView);
  const plan = activeView?.activeRun?.plan || null;
  const runGate = selectRunGate(activeView);
  const bottomInteraction = selectBottomInteraction(activeView);
  const sendBlockedReason = activeDraft && activeDraft.phase !== 'editing' && activeDraft.phase !== 'failed'
    ? activeDraft.phase === 'creating_session'
      ? '正在创建会话，请稍候'
      : activeDraft.phase === 'uploading'
        ? '正在上传附件，请稍候'
        : '正在启动任务，请稍候'
    : runGate.reason === 'preparing'
      ? '正在准备会话或附件，请稍候'
      : runGate.reason === 'awaiting_feedback'
    ? '请先批准或拒绝当前待确认操作；草稿和附件会继续保留'
    : runGate.reason === 'submitting_feedback'
      ? '确认正在提交并核对状态；草稿和附件会继续保留'
      : runGate.reason
        ? '当前会话已有活动任务，完成后可发送下一条消息'
        : undefined;
  const loading = Boolean(activeDraft && !['editing', 'failed'].includes(activeDraft.phase))
    || Boolean(activeView?.activeRun && ['preparing', 'starting', 'streaming', 'reconnecting'].includes(activeView.activeRun.status));
  const uploading = activeDraft?.phase === 'uploading' || Boolean(activeView?.uploading);
  const error = activeDraft?.error || activeView?.error || workspaceError;

  function updateProjectDatasets(projectId: string, update: Dataset[] | ((current: Dataset[]) => Dataset[])) {
    setDatasetsByProject(current => ({
      ...current,
      [projectId]: typeof update === 'function' ? update(current[projectId] || []) : update,
    }));
  }

  function widthLimit(side: ResizeSide) {
    const bodyWidth = bodyRef.current?.clientWidth || window.innerWidth;
    if (side === 'sidebar') {
      const leftDivider = layoutRef.current.sidebarCollapsed ? 0 : DIVIDER_WIDTH;
      const graphOccupied = !layoutRef.current.hasGraphPanel
        ? 0
        : layoutRef.current.graphOpen
          ? layoutRef.current.graphWidth + DIVIDER_WIDTH
          : COLLAPSED_PANEL_WIDTH;
      const occupied = leftDivider + graphOccupied;
      return Math.min(SIDEBAR_MAX, bodyWidth - MAIN_MIN - occupied);
    }
    const sidebarOccupied = layoutRef.current.sidebarCollapsed ? COLLAPSED_PANEL_WIDTH : layoutRef.current.sidebarWidth + DIVIDER_WIDTH;
    return Math.min(GRAPH_MAX, bodyWidth - sidebarOccupied - MAIN_MIN - DIVIDER_WIDTH);
  }

  function updateColumnWidth(side: ResizeSide, value: number) {
    const minimum = side === 'sidebar' ? SIDEBAR_MIN : GRAPH_MIN;
    const next = clamp(value, minimum, widthLimit(side));
    if (side === 'sidebar') setSidebarWidth(next); else setGraphWidth(next);
  }

  function beginResize(side: ResizeSide, event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || window.innerWidth <= 900) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    resizeRef.current = { side, startX: event.clientX, startWidth: side === 'sidebar' ? sidebarWidth : graphWidth };
    document.body.classList.add('is-resizing-columns');
  }

  function nudgeResize(side: ResizeSide, delta: number) {
    updateColumnWidth(side, (side === 'sidebar' ? sidebarWidth : graphWidth) + delta);
    setGraphLayoutVersion(current => current + 1);
  }

  function resetResize(side: ResizeSide) {
    updateColumnWidth(side, side === 'sidebar' ? SIDEBAR_DEFAULT : GRAPH_DEFAULT);
    setGraphLayoutVersion(current => current + 1);
  }

  useEffect(() => {
    function onPointerMove(event: PointerEvent) {
      const resize = resizeRef.current;
      if (!resize) return;
      const delta = event.clientX - resize.startX;
      updateColumnWidth(resize.side, resize.startWidth + (resize.side === 'sidebar' ? delta : -delta));
    }
    function onPointerUp() {
      if (!resizeRef.current) return;
      resizeRef.current = null;
      document.body.classList.remove('is-resizing-columns');
      setGraphLayoutVersion(current => current + 1);
    }
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('pointercancel', onPointerUp);
    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
      window.removeEventListener('pointercancel', onPointerUp);
      document.body.classList.remove('is-resizing-columns');
    };
  }, []);

  useEffect(() => { localStorage.setItem('igraph-layout-sidebar-width', String(Math.round(sidebarWidth))); }, [sidebarWidth]);
  useEffect(() => { localStorage.setItem('igraph-layout-graph-width', String(Math.round(graphWidth))); }, [graphWidth]);
  useEffect(() => {
    function fitColumnsToViewport() {
      if (window.innerWidth <= 900) return;
      const bodyWidth = bodyRef.current?.clientWidth || window.innerWidth;
      const leftDivider = layoutRef.current.sidebarCollapsed ? 0 : DIVIDER_WIDTH;
      const collapsedGraphWidth = layoutRef.current.hasGraphPanel ? COLLAPSED_PANEL_WIDTH : 0;
      if (!layoutRef.current.graphOpen || !layoutRef.current.hasGraphPanel) {
        if (!layoutRef.current.sidebarCollapsed) setSidebarWidth(current => clamp(current, SIDEBAR_MIN, Math.min(SIDEBAR_MAX, bodyWidth - MAIN_MIN - leftDivider - collapsedGraphWidth)));
        return;
      }
      const sidebarTrack = layoutRef.current.sidebarCollapsed ? COLLAPSED_PANEL_WIDTH : layoutRef.current.sidebarWidth;
      const nextSidebar = layoutRef.current.sidebarCollapsed ? sidebarTrack : clamp(sidebarTrack, SIDEBAR_MIN, Math.min(SIDEBAR_MAX, bodyWidth - MAIN_MIN - GRAPH_MIN - DIVIDER_WIDTH * 2));
      const nextGraph = clamp(layoutRef.current.graphWidth, GRAPH_MIN, Math.min(GRAPH_MAX, bodyWidth - nextSidebar - MAIN_MIN - leftDivider - DIVIDER_WIDTH));
      if (!layoutRef.current.sidebarCollapsed) setSidebarWidth(nextSidebar);
      setGraphWidth(nextGraph);
    }
    fitColumnsToViewport();
    window.addEventListener('resize', fitColumnsToViewport);
    return () => window.removeEventListener('resize', fitColumnsToViewport);
  }, [activeSessionId, graphOpen, sidebarCollapsed]);

  function updateViewUrl(view: 'project' | 'session', id: string) {
    writeWorkspaceRoute({ kind: view, id });
  }

  function showWorkspaceHome(updateUrl = true, replaceUrl = false, message = '') {
    setActiveProject(null);
    dispatchConversation({ type: 'SET_ACTIVE_SESSION', sessionId: null });
    setGraphOpen(false);
    setDatasetOpen(false);
    setMoveSessionTarget(null);
    setMobileSidebarOpen(false);
    setWorkspaceError(message);
    if (updateUrl) writeWorkspaceRoute({ kind: 'home' }, replaceUrl);
  }

  function runStorageKey(sessionId: string) { return `igraph-active-run:v2:${userId}:${sessionId}`; }

  function persistRun(sessionId: string, runId: string, lastEventIndex: number) {
    if (!userId || !runId) return;
    sessionStorage.setItem(runStorageKey(sessionId), JSON.stringify({ runId, lastEventIndex }));
  }

  function clearPersistedRun(sessionId: string) { sessionStorage.removeItem(runStorageKey(sessionId)); }

  function persistPendingFirstTurn(sessionId: string, value: PendingFirstTurn) {
    if (!userId) return;
    localStorage.setItem(pendingFirstTurnStorageKey(userId, sessionId), JSON.stringify(value));
  }

  function clearPendingFirstTurn(sessionId: string) {
    if (userId) localStorage.removeItem(pendingFirstTurnStorageKey(userId, sessionId));
  }

  function clearStoredDraft(projectId: string) {
    if (userId) localStorage.removeItem(draftStorageKey(userId, projectId));
  }

  function persistedRun(sessionId: string) {
    try {
      const value = JSON.parse(sessionStorage.getItem(runStorageKey(sessionId)) || 'null');
      return value && typeof value.runId === 'string'
        ? { runId: value.runId, lastEventIndex: Number(value.lastEventIndex ?? -1) }
        : null;
    } catch { return null; }
  }

  function nextGraphVersion(sessionId: string, preserveCurrent = false) {
    const version = (graphVersionsRef.current.get(sessionId) || 0) + 1;
    graphVersionsRef.current.set(sessionId, version);
    dispatchConversation({ type: 'GRAPH_REQUEST_START', sessionId, requestVersion: version, preserveCurrent });
    return version;
  }

  async function refreshSessionGraph(sessionId: string, options: Record<string, unknown> = {}, reportError = false, requestVersion?: number, preserveCurrent = false) {
    const version = requestVersion ?? nextGraphVersion(sessionId, preserveCurrent);
    try {
      const nextGraph = await visualizeGraph(sessionId, options);
      dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: version, graph: { status: 'ready', data: nextGraph } });
      setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, has_graph: true } : session));
      return nextGraph;
    } catch (cause: any) {
      const unsupported = unsupportedGraphViewFromError(cause);
      let retainedCurrentGraph = false;
      if (unsupported) {
        dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: version, graph: unsupported });
      } else {
        const message = cause.message || '图谱加载失败';
        const currentGraph = conversationRef.current.viewsBySessionId[sessionId]?.graph;
        retainedCurrentGraph = Boolean(preserveCurrent && currentGraph?.status === 'ready');
        if (!retainedCurrentGraph) {
          dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: version, graph: { status: 'failed', data: null, error: message } });
        }
        if (reportError) dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: message } });
      }
      setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, has_graph: retainedCurrentGraph } : session));
      return null;
    }
  }

  async function reconcileAuthoritativeRun(sessionId: string, generation: string) {
    const requestVersion = (hydrationVersionsRef.current.get(sessionId) || 0) + 1;
    hydrationVersionsRef.current.set(sessionId, requestVersion);
    dispatchConversation({ type: 'HYDRATE_START', sessionId, requestVersion });
    try {
      const response = await activateSession(sessionId);
      const restoredMessages = response.messages === undefined
        ? normalizeMessages((await getMessages(sessionId)).messages)
        : normalizeMessages(response.messages);
      if (hydrationVersionsRef.current.get(sessionId) !== requestVersion) return null;
      const currentView = conversationRef.current.viewsBySessionId[sessionId];
      const selectedModel = String(response.model || currentView?.model || response.default_model || defaultModel);
      const graphVersion = nextGraphVersion(sessionId);
      dispatchConversation({
        type: 'HYDRATE_SUCCESS',
        sessionId,
        requestVersion,
        messages: restoredMessages,
        model: selectedModel,
        activeDatasetId: Object.prototype.hasOwnProperty.call(response, 'active_dataset_id')
          ? response.active_dataset_id
          : currentView?.activeDatasetId,
        activeRun: response.active_run,
        feedback: response.pending_feedback,
        generation,
        preserveMessages: Boolean(response.active_run && currentView?.messageOrder.length),
        datasetView: response.dataset_view,
        hasGraph: response.has_graph,
      });
      if (response.has_graph && response.dataset_view?.kind !== 'graph_collection') {
        void refreshSessionGraph(sessionId, { include_community: false }, false, graphVersion);
      }
      if (!response.active_run) {
        dispatchConversation({ type: 'CLEAR_ACTIVE_RUN', sessionId, generation });
        clearPersistedRun(sessionId);
      }
      setAvailableModels(normalizedModels(response.available_models, selectedModel));
      setSessions(current => current.map(session => session.session_id === sessionId ? {
        ...session,
        project_id: response.project_id || session.project_id,
        active_dataset_id: Object.prototype.hasOwnProperty.call(response, 'active_dataset_id') ? response.active_dataset_id : session.active_dataset_id,
        has_graph: response.has_graph,
        model: selectedModel,
      } : session));
      return { activeRun: response.active_run || null, feedback: response.pending_feedback || null };
    } catch (cause: any) {
      if (hydrationVersionsRef.current.get(sessionId) === requestVersion) {
        dispatchConversation({ type: 'HYDRATE_FAILED', sessionId, requestVersion, error: cause.message || '无法核对 Run 状态' });
      }
      throw cause;
    }
  }

  async function reconcileFeedbackOutcome(sessionId: string, generation: string) {
    try {
      const snapshot = await reconcileAuthoritativeRun(sessionId, generation);
      connectionManagerRef.current.release(sessionId, generation);
      if (snapshot?.activeRun && snapshot.activeRun.status.toLowerCase() !== 'paused') {
        openResumeConnection(sessionId, generation, snapshot.activeRun, conversationRef.current.activeSessionId === sessionId);
      }
    } catch {
      dispatchConversation({ type: 'SET_RUN_STATUS', sessionId, generation, status: 'submitting_feedback', error: '确认结果暂时无法核对，系统会自动重试' });
      connectionManagerRef.current.scheduleReconnect(sessionId, generation, () => {
        const restarted = connectionManagerRef.current.restart(sessionId, generation);
        if (restarted) void reconcileFeedbackOutcome(sessionId, generation);
      });
    }
  }

  function terminalEvent(batch: StreamBatch) {
    return batch.events.some(event => event.type === 'done' || event.type === 'error');
  }

  function feedbackEvent(batch: StreamBatch) {
    return batch.events.some(event => event.type === 'await_user_feedback' || String(event.event || event.type || '').toLowerCase().includes('runpaused'));
  }

  async function consumeRunConnection(
    sessionId: string,
    generation: string,
    connection: NonNullable<ReturnType<RunConnectionManager['get']>>,
    operation: 'start' | 'resume' | 'feedback',
    startPayload?: { message: string; attachments: Array<{ file_token: string; filename: string }> },
    pendingFeedback?: FeedbackRequest | null,
    feedbackDecision?: 'approve' | 'reject',
    resumeFromEventIndex?: number,
    feedbackDecisionId?: string,
    clearPendingOnOpen = false,
  ) {
    let runId = connection.runId || '';
    let lastEventIndex = resumeFromEventIndex ?? conversationRef.current.viewsBySessionId[sessionId]?.activeRun?.lastEventIndex ?? -1;
    let terminal = false;
    let awaitingFeedback = false;
    let feedbackAccepted = false;
    let feedbackSubmitFailed = false;
    let pendingFirstTurnCleared = false;
    const handlers = {
      onBatch: (incoming: StreamBatch) => {
        if (clearPendingOnOpen && !pendingFirstTurnCleared) {
          clearPendingFirstTurn(sessionId);
          pendingFirstTurnCleared = true;
        }
        const batch = { ...incoming, session_id: incoming.session_id || sessionId, run_id: incoming.run_id || runId };
        if (batch.session_id !== sessionId) return;
        if (batch.run_id) {
          runId = batch.run_id;
          connectionManagerRef.current.updateRunId(sessionId, generation, runId);
        }
        if (batch.event_index != null && Number.isFinite(Number(batch.event_index))) lastEventIndex = Number(batch.event_index);
        connectionManagerRef.current.receivedEvent(sessionId, generation);
        dispatchConversation({ type: 'APPLY_BATCH', sessionId, generation, batch });
        if (runId) persistRun(sessionId, runId, lastEventIndex);
        terminal ||= terminalEvent(batch);
        awaitingFeedback ||= feedbackEvent(batch);
        feedbackAccepted ||= batch.events.some(event => event.type === 'feedback_accepted' || (event.type === 'feedback_submit_error' && event.accepted === true));
        feedbackSubmitFailed ||= batch.events.some(event => event.type === 'feedback_submit_error' && event.accepted !== true);
      },
    };

    const reconcileUnknownFirstTurn = async () => {
      try {
        const activated = await activateSession(sessionId);
        if (activated.active_run) {
          clearPendingFirstTurn(sessionId);
          pendingFirstTurnCleared = true;
          runId = activated.active_run.run_id;
          lastEventIndex = activated.active_run.last_event_index;
          dispatchConversation({ type: 'ADOPT_RUN', sessionId, generation, activeRun: activated.active_run, feedback: activated.pending_feedback });
          connectionManagerRef.current.updateRunId(sessionId, generation, runId);
          if (activated.active_run.status.toLowerCase() !== 'paused') {
            const restarted = connectionManagerRef.current.restart(sessionId, generation);
            if (restarted) void consumeRunConnection(sessionId, generation, restarted, 'resume', undefined, undefined, undefined, lastEventIndex);
          } else connectionManagerRef.current.release(sessionId, generation);
          return;
        }
        dispatchConversation({ type: 'ROLLBACK_RUN_START', sessionId, generation, restoreDraft: startPayload?.message, restoreAttachments: startPayload?.attachments });
        dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: '任务尚未启动，请手工重试' } });
        connectionManagerRef.current.release(sessionId, generation);
      } catch (activateError: any) {
        dispatchConversation({ type: 'SET_RUN_STATUS', sessionId, generation, status: 'reconnecting', error: activateError.message || '正在核对首次任务是否已启动' });
        connectionManagerRef.current.scheduleReconnect(sessionId, generation, () => { void reconcileUnknownFirstTurn(); });
      }
    };

    try {
      if (operation === 'start' && startPayload) {
        await streamChat(sessionId, startPayload.message, startPayload.attachments, handlers, connection.controller.signal);
      } else if (operation === 'feedback' && pendingFeedback && feedbackDecision && feedbackDecisionId) {
        await streamFeedback(sessionId, pendingFeedback, feedbackDecision, feedbackDecisionId, handlers, connection.controller.signal);
      } else if (runId) {
        await resumeRun(sessionId, runId, lastEventIndex, handlers, connection.controller.signal);
      }
    } catch (cause: any) {
      if (requestWasAborted(cause)) return;
      if (operation === 'feedback') {
        feedbackSubmitFailed = true;
      } else if (operation === 'start' && cause instanceof ApiError && cause.status === 409) {
        dispatchConversation({ type: 'ROLLBACK_RUN_START', sessionId, generation, restoreDraft: startPayload?.message, restoreAttachments: startPayload?.attachments });
        connectionManagerRef.current.release(sessionId, generation);
        try {
          const snapshot = await reconcileAuthoritativeRun(sessionId, generation);
          if (snapshot?.activeRun && snapshot.activeRun.status.toLowerCase() !== 'paused') {
            if (clearPendingOnOpen) clearPendingFirstTurn(sessionId);
            openResumeConnection(sessionId, generation, snapshot.activeRun, conversationRef.current.activeSessionId === sessionId);
          }
        } catch (activateError: any) {
          dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: activateError.message || cause.message || '无法恢复当前活动 Run' } });
        }
        return;
      } else if (operation === 'start' && !runId && clearPendingOnOpen) {
        void reconcileUnknownFirstTurn();
        return;
      } else if (operation === 'start' && !runId) {
        try {
          const activated = await activateSession(sessionId);
          if (activated.active_run) {
            if (clearPendingOnOpen) clearPendingFirstTurn(sessionId);
            runId = activated.active_run.run_id;
            lastEventIndex = activated.active_run.last_event_index;
            dispatchConversation({ type: 'ADOPT_RUN', sessionId, generation, activeRun: activated.active_run, feedback: activated.pending_feedback });
            connectionManagerRef.current.updateRunId(sessionId, generation, runId);
          } else {
            dispatchConversation({ type: 'SET_RUN_STATUS', sessionId, generation, status: 'failed', error: cause.message || '任务启动失败，请手工重试' });
            connectionManagerRef.current.release(sessionId, generation);
            return;
          }
        } catch (activateError: any) {
          dispatchConversation({ type: 'SET_RUN_STATUS', sessionId, generation, status: 'failed', error: activateError.message || '无法确认任务是否已启动' });
          connectionManagerRef.current.release(sessionId, generation);
          return;
        }
      }
    }

    if (operation === 'feedback' && (feedbackSubmitFailed || !feedbackAccepted)) {
      void reconcileFeedbackOutcome(sessionId, generation);
      return;
    }

    if (terminal) {
      clearPersistedRun(sessionId);
      if (clearPendingOnOpen) clearPendingFirstTurn(sessionId);
      connectionManagerRef.current.release(sessionId, generation);
      if (startPayload?.attachments.length) dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { attachments: [] } });
      void refreshSessionGraph(sessionId, { include_community: false }, false, undefined, true);
      return;
    }
    if (awaitingFeedback) {
      connectionManagerRef.current.release(sessionId, generation);
      return;
    }
    if (!runId) {
      dispatchConversation({ type: 'SET_RUN_STATUS', sessionId, generation, status: 'failed', error: '流连接已结束，但服务器未返回 Run ID' });
      connectionManagerRef.current.release(sessionId, generation);
      return;
    }
    persistRun(sessionId, runId, lastEventIndex);
    dispatchConversation({ type: 'SET_RUN_STATUS', sessionId, generation, status: 'reconnecting' });
    connectionManagerRef.current.scheduleReconnect(sessionId, generation, () => {
      const restarted = connectionManagerRef.current.restart(sessionId, generation);
      if (restarted) void consumeRunConnection(sessionId, generation, restarted, 'resume');
    });
  }

  function openResumeConnection(sessionId: string, generation: string, activeRun: ActiveRun, foreground = conversationRef.current.activeSessionId === sessionId) {
    if (!activeRun.run_id || activeRun.status.toLowerCase() === 'paused' || connectionManagerRef.current.get(sessionId)) return;
    const retry = () => {
      const viewRun = conversationRef.current.viewsBySessionId[sessionId]?.activeRun;
      if (viewRun?.runId) openResumeConnection(sessionId, generation, { run_id: viewRun.runId, status: viewRun.status, last_event_index: viewRun.lastEventIndex }, conversationRef.current.activeSessionId === sessionId);
    };
    const connection = connectionManagerRef.current.acquire({
      sessionId,
      runId: activeRun.run_id,
      generation,
      foreground,
      retry,
      onDetached: () => dispatchConversation({ type: 'SET_RUN_STATUS', sessionId, generation, status: 'detached' }),
    });
    if (!connection) return;
    persistRun(sessionId, activeRun.run_id, activeRun.last_event_index);
    dispatchConversation({ type: 'SET_RUN_STATUS', sessionId, generation, status: 'reconnecting' });
    void consumeRunConnection(sessionId, generation, connection, 'resume', undefined, undefined, undefined, activeRun.last_event_index);
  }

  const selectSession = async (id: string, source = sessions, updateUrl = true, allowUnlisted = false) => {
    const listedSession = source.find(item => item.session_id === id);
    if (!listedSession && !allowUnlisted) {
      showWorkspaceHome(true, true, '该会话不存在或已被删除');
      return;
    }
    if (id === conversationRef.current.activeSessionId && !activeProject && listedSession) return;
    const session = listedSession || { session_id: id };
    setActiveProject(null);
    setWorkspaceError('');
    dispatchConversation({ type: 'SET_ACTIVE_SESSION', sessionId: id, model: session.model || defaultModel, accessedAt: Date.now() });
    dispatchConversation({ type: 'PATCH_VIEW', sessionId: id, patch: { selectedNode: null, error: undefined } });
    setGraphOpen(window.innerWidth > 900);
    if (updateUrl) updateViewUrl('session', id);

    hydrationControllersRef.current.get(id)?.abort();
    const controller = new AbortController();
    hydrationControllersRef.current.set(id, controller);
    const requestVersion = (hydrationVersionsRef.current.get(id) || 0) + 1;
    hydrationVersionsRef.current.set(id, requestVersion);
    dispatchConversation({ type: 'HYDRATE_START', sessionId: id, requestVersion });

    try {
      const response = await activateSession(id, controller.signal);
      const restoredMessages = response.messages === undefined
        ? normalizeMessages((await getMessages(id, controller.signal)).messages)
        : normalizeMessages(response.messages);
      if (hydrationVersionsRef.current.get(id) !== requestVersion) return;
      const selectedModel = String(response.model || session.model || response.default_model || defaultModel);
      const projectId = response.project_id || session.project_id;
      const activeDatasetId = Object.prototype.hasOwnProperty.call(response, 'active_dataset_id') ? response.active_dataset_id : session.active_dataset_id;
      const previousView = conversationRef.current.viewsBySessionId[id];
      const previousRun = previousView?.activeRun;
      const storedRun = persistedRun(id);
      const effectiveActiveRun = response.active_run ? {
        ...response.active_run,
        last_event_index: previousRun?.runId === response.active_run.run_id
          ? Math.max(response.active_run.last_event_index, previousRun.lastEventIndex)
          : response.active_run.last_event_index < 0 && storedRun?.runId === response.active_run.run_id
            ? storedRun.lastEventIndex
            : response.active_run.last_event_index,
      } : null;
      const generation = effectiveActiveRun && previousRun?.runId === effectiveActiveRun.run_id
        ? previousRun.generation
        : `resume:${effectiveActiveRun?.run_id || crypto.randomUUID()}`;
      const graphVersion = nextGraphVersion(id);
      dispatchConversation({
        type: 'HYDRATE_SUCCESS', sessionId: id, requestVersion, messages: restoredMessages,
        model: selectedModel, activeDatasetId, feedback: response.pending_feedback,
        activeRun: effectiveActiveRun, generation,
        preserveMessages: Boolean(previousView?.messageOrder.length && previousView.activeRun),
        datasetView: response.dataset_view,
        hasGraph: response.has_graph,
      });
      const pendingFirstTurn = readPendingFirstTurn(userId, id);
      if (pendingFirstTurn) {
        const alreadyStarted = Boolean(effectiveActiveRun)
          || restoredMessages.some(message => message.role === 'user' && message.content.trim() === pendingFirstTurn.message.trim());
        if (alreadyStarted) clearPendingFirstTurn(id);
        else dispatchConversation({
          type: 'PATCH_VIEW',
          sessionId: id,
          patch: {
            draft: pendingFirstTurn.message,
            attachments: pendingFirstTurn.attachments,
            pendingAttachments: pendingFirstTurn.pendingAttachments.map(attachment => ({
              ...attachment,
              status: 'needs_reselect',
              error: '刷新后需要重新选择文件',
            })),
          },
        });
      }
      setAvailableModels(normalizedModels(response.available_models, selectedModel));
      setSessions(current => mergeSessionLists(current, [{ ...session, project_id: projectId, active_dataset_id: activeDatasetId, has_graph: response.has_graph, model: selectedModel }]));

      if (projectId) {
        void listProjectDatasets(projectId, controller.signal).then(datasetResponse => {
          if (hydrationVersionsRef.current.get(id) !== requestVersion) return;
          updateProjectDatasets(projectId, datasetResponse.datasets || []);
          setProjects(current => current.map(project => project.project_id === projectId ? { ...project, default_dataset_id: datasetResponse.default_dataset_id } : project));
        }).catch(() => undefined);
      }

      if (response.has_graph && response.dataset_view?.kind !== 'graph_collection') {
        void refreshSessionGraph(id, { include_community: false }, false, graphVersion);
      }
      if (effectiveActiveRun) openResumeConnection(id, generation, effectiveActiveRun, true);
    } catch (cause: any) {
      if (!requestWasAborted(cause) && cause instanceof ApiError && cause.status === 404) {
        connectionManagerRef.current.abort(id);
        clearPersistedRun(id);
        dispatchConversation({ type: 'REMOVE_SESSION', sessionId: id });
        setSessions(current => current.filter(item => item.session_id !== id));
        if (conversationRef.current.activeSessionId === id) showWorkspaceHome(true, true, '该会话不存在或已被删除');
      } else if (!requestWasAborted(cause) && hydrationVersionsRef.current.get(id) === requestVersion) {
        dispatchConversation({ type: 'HYDRATE_FAILED', sessionId: id, requestVersion, error: cause.message || '会话切换失败' });
      }
    } finally {
      if (hydrationControllersRef.current.get(id) === controller) hydrationControllersRef.current.delete(id);
    }
  };

  const selectProject = async (id: string, source = projects, updateUrl = true) => {
    if (busy) return;
    const project = source.find(item => item.project_id === id);
    if (!project) {
      showWorkspaceHome(true, true, '该项目不存在或已被删除');
      return;
    }
    setBusy(true); setWorkspaceError('');
    try {
      const [conversationResponse, datasetResponse] = await Promise.all([listProjectConversations(id), listProjectDatasets(id)]);
      const conversations = conversationResponse.conversations || [];
      const nextProject = { ...project, conversation_count: conversations.length, default_dataset_id: datasetResponse.default_dataset_id };
      updateProjectDatasets(id, datasetResponse.datasets || []);
      setSessions(current => mergeSessionLists(current, conversations));
      setProjects(current => current.map(item => item.project_id === id ? nextProject : item));
      setActiveProject(nextProject);
      dispatchConversation({ type: 'SET_ACTIVE_SESSION', sessionId: null });
      setGraphOpen(false);
      if (updateUrl) updateViewUrl('project', id);
    } catch (cause: any) {
      if (cause instanceof ApiError && cause.status === 404) {
        setProjects(current => current.filter(item => item.project_id !== id));
        showWorkspaceHome(true, true, '该项目不存在或已被删除');
      } else setWorkspaceError(cause.message || '项目加载失败');
    }
    finally { setBusy(false); }
  };

  async function refresh() {
    const [sessionResponse, projectResponse] = await Promise.all([listSessions(), listProjects()]);
    const nextSessions = sessionResponse.sessions || [];
    const nextProjects = projectResponse.projects || [];
    setSessions(nextSessions); setProjects(nextProjects);
    const route = parseWorkspaceRoute(window.location.search);
    if (route.kind === 'new-session') {
      const project = nextProjects.find(item => item.project_id === route.projectId);
      if (!project) showWorkspaceHome(true, true, '该项目不存在或已被删除');
      else {
        const storedDraft = readStoredDraft(userId, route.projectId);
        const materialized = storedDraft && nextSessions.find(session => session.session_id === storedDraft.draftId);
        if (materialized) {
          clearStoredDraft(route.projectId);
          writeWorkspaceRoute({ kind: 'session', id: materialized.session_id }, true);
          await selectSession(materialized.session_id, nextSessions, false);
        } else newTask(route.projectId, nextProjects, false);
      }
    } else if (route.kind === 'project') {
      if (nextProjects.some(project => project.project_id === route.id)) await selectProject(route.id, nextProjects, false);
      else showWorkspaceHome(true, true, '该项目不存在或已被删除');
    } else if (route.kind === 'session') {
      await selectSession(route.id, nextSessions, false, true);
    } else showWorkspaceHome(false);
  }

  useEffect(() => {
    if (!userId || demoMode) return;
    validateUser(userId).then(response => {
      if (response.valid === false) throw new Error('用户 ID 无效');
      const selectedModel = response.default_model || DEFAULT_MODEL;
      setDefaultModel(selectedModel);
      setAvailableModels(normalizedModels(response.available_models, selectedModel));
      return refresh();
    }).catch((cause: any) => setWorkspaceError(cause.message || '无法加载会话'));
  }, [userId]);
  useEffect(() => {
    if (!userId || demoMode) return;
    function handleHistoryNavigation() {
      const route = parseWorkspaceRoute(window.location.search);
      if (route.kind === 'home') {
        showWorkspaceHome(false);
      } else if (route.kind === 'session') {
        void selectSession(route.id, sessions, false, true);
      } else if (route.kind === 'new-session' && projects.some(project => project.project_id === route.projectId)) {
        newTask(route.projectId, projects, false);
      } else if (route.kind === 'project' && projects.some(project => project.project_id === route.id)) {
        void selectProject(route.id, projects, false);
      } else showWorkspaceHome(true, true, '该项目不存在或已被删除');
    }
    window.addEventListener('popstate', handleHistoryNavigation);
    return () => window.removeEventListener('popstate', handleHistoryNavigation);
  }, [userId, sessions, projects, busy]);
  useEffect(() => () => connectionManagerRef.current.abortAll(), []);
  useEffect(() => () => {
    for (const controller of draftCreationControllersRef.current.values()) controller.abort();
    for (const controller of preparationControllersRef.current.values()) controller.abort();
  }, []);
  useEffect(() => {
    if (!userId) return;
    for (const draft of Object.values(conversation.draftsByProjectId)) {
      localStorage.setItem(draftStorageKey(userId, draft.projectId), JSON.stringify(serializableDraft(draft)));
    }
  }, [conversation.draftsByProjectId, userId]);
  useEffect(() => {
    function warnBeforeLeaving(event: BeforeUnloadEvent) {
      const hasVolatileFile = Object.values(conversationRef.current.draftsByProjectId)
        .some(draft => draft.attachments.some(attachment => Boolean(attachment.file)))
        || Object.values(conversationRef.current.viewsBySessionId)
          .some(view => view.pendingAttachments.some(attachment => Boolean(attachment.file)));
      if (!hasVolatileFile) return;
      event.preventDefault();
      event.returnValue = '';
    }
    window.addEventListener('beforeunload', warnBeforeLeaving);
    return () => window.removeEventListener('beforeunload', warnBeforeLeaving);
  }, []);
  useEffect(() => {
    if (!userId) return;
    for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
      const key = sessionStorage.key(index);
      if (key?.startsWith('igraph-active-run:') && !key.startsWith('igraph-active-run:v2:')) sessionStorage.removeItem(key);
    }
  }, [userId]);
  useEffect(() => {
    if (bottomInteraction.kind !== 'syncing_feedback' || bottomInteraction.reason !== 'missing_feedback' || !activeSessionId || !activeView?.activeRun) return;
    void reconcileAuthoritativeRun(activeSessionId, activeView.activeRun.generation).catch(() => undefined);
  }, [activeSessionId, activeView?.activeRun?.generation, activeView?.activeRun?.runId, bottomInteraction.kind, bottomInteraction.kind === 'syncing_feedback' ? bottomInteraction.reason : 'ready']);
  useEffect(() => { document.documentElement.dataset.theme = dark ? 'dark' : 'light'; localStorage.setItem('igraph-theme', dark ? 'dark' : 'light'); }, [dark]);
  useEffect(() => { setReady(true); }, []);

  async function login() {
    const value = loginValue.trim(); if (!value) return setLoginError('请输入用户 ID');
    try { const response = await validateUser(value); if (response.valid === false) throw new Error('用户 ID 无效'); const selectedModel = response.default_model || DEFAULT_MODEL; setDefaultModel(selectedModel); setAvailableModels(normalizedModels(response.available_models, selectedModel)); setUserId(value); setUser(value); setLoginError(''); } catch (cause: any) { setLoginError(cause.message || '登录失败'); }
  }
  function newTask(targetProjectId?: string, sourceProjects = projects, updateUrl = true) {
    const currentState = conversationRef.current;
    const defaultProject = sourceProjects.find(project => project.is_default);
    const projectId = targetProjectId
      || currentState.activeDraftProjectId
      || activeProject?.project_id
      || activeSession?.project_id
      || defaultProject?.project_id;
    const project = sourceProjects.find(item => item.project_id === projectId);
    if (!project) {
      setWorkspaceError('没有可用于新建会话的项目');
      return;
    }
    const existing = currentState.draftsByProjectId[project.project_id] || readStoredDraft(userId, project.project_id);
    const draft: NewConversationDraft = existing || {
      draftId: crypto.randomUUID(),
      projectId: project.project_id,
      phase: 'editing',
      text: '',
      model: activeSession?.model || defaultModel,
      activeDatasetId: project.default_dataset_id || null,
      attachments: [],
    };
    dispatchConversation({ type: 'OPEN_DRAFT', draft });
    setActiveProject(null);
    setGraphOpen(false);
    setDatasetOpen(false);
    setMoveSessionTarget(null);
    setMobileSidebarOpen(false);
    setWorkspaceError('');
    setComposerFocusRequest(current => current + 1);
    if (updateUrl) {
      const route = parseWorkspaceRoute(window.location.search);
      if (route.kind !== 'new-session' || route.projectId !== project.project_id) {
        writeWorkspaceRoute({ kind: 'new-session', projectId: project.project_id });
      }
    }
  }
  async function deleteTask(id: string) {
    if (!window.confirm('确认删除这个分析任务吗？')) return;
    const deleted = sessions.find(item => item.session_id === id);
    const deletingActiveSession = conversationRef.current.activeSessionId === id;
    const preparation = preparationControllersRef.current.get(id);
    preparation?.abort();
    preparationControllersRef.current.delete(id);
    try {
      await removeSession(id);
      connectionManagerRef.current.abort(id);
      hydrationControllersRef.current.get(id)?.abort();
      hydrationControllersRef.current.delete(id);
      clearPersistedRun(id);
      clearPendingFirstTurn(id);
      dispatchConversation({ type: 'REMOVE_SESSION', sessionId: id });
      setSessions(current => current.filter(item => item.session_id !== id));
      if (deleted?.project_id) setProjects(current => current.map(project => project.project_id === deleted.project_id ? { ...project, conversation_count: Math.max(0, (project.conversation_count || 0) - 1) } : project));
      if (activeProject?.project_id === deleted?.project_id) setActiveProject(current => current ? { ...current, conversation_count: Math.max(0, (current.conversation_count || 0) - 1) } : current);
      if (deletingActiveSession) showWorkspaceHome(true, true);
    } catch (cause: any) {
      const run = conversationRef.current.viewsBySessionId[id]?.activeRun;
      if (preparation && run?.status === 'preparing') dispatchConversation({ type: 'PREPARE_RUN_FAILED', sessionId: id, generation: run.generation, error: '删除任务失败，请重试' });
      setWorkspaceError(cause.message || '删除任务失败');
    }
  }
  async function saveProject(name: string, description: string) {
    if (projectEditor === 'edit' && projectEditorTarget) {
      const response = await patchProject(projectEditorTarget.project_id, { name, description });
      const updated = { ...projectEditorTarget, ...response.project };
      setProjects(current => current.map(project => project.project_id === updated.project_id ? updated : project));
      setActiveProject(current => current?.project_id === updated.project_id ? { ...current, ...updated } : current);
      setProjectEditor(null); setProjectEditorTarget(null);
      return;
    }
    const response = await addProject(name, description);
    const created = response.project;
    const nextProjects = [...projects, created];
    setProjects(nextProjects); setProjectEditor(null); setProjectEditorTarget(null);
    await selectProject(created.project_id, nextProjects);
  }
  async function deleteCurrentProject(action: 'move_to_general' | 'delete') {
    if (!deleteProjectTarget) return;
    const targetId = deleteProjectTarget.project_id;
    draftCreationControllersRef.current.get(targetId)?.abort();
    draftCreationControllersRef.current.delete(targetId);
    const affectedSessions = sessions.filter(session => session.project_id === targetId);
    const affectedSessionIds = new Set(affectedSessions.map(session => session.session_id));
    const deletingActiveSession = Boolean(activeSessionId && affectedSessionIds.has(activeSessionId));
    const deletingActiveProject = activeProject?.project_id === targetId;
    const deletingActiveDraft = conversationRef.current.activeDraftProjectId === targetId;
    const response = await removeProject(targetId, action);
    const generalProject = projects.find(project => project.project_id === response.general_project_id);
    const nextSessions = action === 'delete'
      ? sessions.filter(session => session.project_id !== targetId)
      : sessions.map(session => session.project_id === targetId ? { ...session, project_id: response.general_project_id, active_dataset_id: generalProject?.default_dataset_id, has_graph: false } : session);
    const nextProjects = projects
      .filter(project => project.project_id !== targetId)
      .map(project => project.project_id === response.general_project_id && action === 'move_to_general'
        ? { ...project, conversation_count: (project.conversation_count || 0) + response.affected_conversations }
        : project);
    if (action === 'delete') {
      for (const session of affectedSessions) {
        connectionManagerRef.current.abort(session.session_id);
        preparationControllersRef.current.get(session.session_id)?.abort();
        preparationControllersRef.current.delete(session.session_id);
        hydrationControllersRef.current.get(session.session_id)?.abort();
        hydrationControllersRef.current.delete(session.session_id);
        clearPersistedRun(session.session_id);
        clearPendingFirstTurn(session.session_id);
        dispatchConversation({ type: 'REMOVE_SESSION', sessionId: session.session_id });
      }
    } else {
      for (const session of affectedSessions) {
        dispatchConversation({ type: 'PATCH_VIEW', sessionId: session.session_id, patch: { activeDatasetId: generalProject?.default_dataset_id, selectedNode: null } });
        const graphVersion = nextGraphVersion(session.session_id);
        dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId: session.session_id, requestVersion: graphVersion, graph: { status: 'empty', data: null } });
      }
    }
    setSessions(nextSessions); setProjects(nextProjects); setDeleteProjectTarget(null);
    dispatchConversation({ type: 'REMOVE_DRAFT', projectId: targetId });
    clearStoredDraft(targetId);
    const general = nextProjects.find(project => project.project_id === response.general_project_id);
    if (deletingActiveDraft) showWorkspaceHome(true, true);
    else if (deletingActiveSession && action === 'delete') showWorkspaceHome(true, true);
    else if (deletingActiveProject) {
      if (general) await selectProject(general.project_id, nextProjects);
      else showWorkspaceHome(true, true);
    } else if (deletingActiveSession) {
      setActiveProject(null);
      setGraphOpen(false);
      setWorkspaceError('');
    }
  }
  async function moveCurrentSession(projectId: string) {
    if (!moveSessionTarget) return;
    const oldProjectId = moveSessionTarget.project_id;
    const graphVersion = nextGraphVersion(moveSessionTarget.session_id);
    const response = await moveSession(moveSessionTarget.session_id, projectId);
    setSessions(current => current.map(session => session.session_id === moveSessionTarget.session_id ? { ...session, project_id: projectId, active_dataset_id: response.active_dataset_id, has_graph: false } : session));
    dispatchConversation({ type: 'PATCH_VIEW', sessionId: moveSessionTarget.session_id, patch: { activeDatasetId: response.active_dataset_id } });
    dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId: moveSessionTarget.session_id, requestVersion: graphVersion, graph: { status: 'empty', data: null } });
    setProjects(current => current.map(project => {
      if (project.project_id === oldProjectId) return { ...project, conversation_count: Math.max(0, (project.conversation_count || 0) - 1) };
      if (project.project_id === projectId) return { ...project, conversation_count: (project.conversation_count || 0) + 1 };
      return project;
    }));
    setMoveSessionTarget(null);
  }

  function pendingFirstTurnSnapshot(
    message: string,
    attachments: AttachmentRef[],
    pendingAttachments: DraftAttachment[],
    modelValue: string,
    projectId: string,
    activeDatasetId: string | null,
  ): PendingFirstTurn {
    return {
      message,
      attachments,
      pendingAttachments: pendingAttachments.map(attachment => ({
        localId: attachment.localId,
        filename: attachment.filename,
        status: attachment.fileToken ? 'uploaded' : 'needs_reselect',
        error: attachment.fileToken ? undefined : '刷新后需要重新选择文件',
      })),
      model: modelValue,
      projectId,
      activeDatasetId,
    };
  }

  async function prepareAndStartFirstTurn(
    sessionId: string,
    generation: string,
    message: string,
    initialAttachments: AttachmentRef[],
    initialPending: DraftAttachment[],
    modelValue: string,
    projectId: string,
    activeDatasetId: string | null,
  ) {
    preparationControllersRef.current.get(sessionId)?.abort();
    const preparationController = new AbortController();
    preparationControllersRef.current.set(sessionId, preparationController);
    let attachments = [...initialAttachments];
    let pendingAttachments = [...initialPending];
    persistPendingFirstTurn(sessionId, pendingFirstTurnSnapshot(message, attachments, pendingAttachments, modelValue, projectId, activeDatasetId));

    for (const pending of initialPending) {
      if (pending.fileToken) {
        const attachment = { file_token: pending.fileToken, filename: pending.filename };
        if (!attachments.some(item => item.file_token === attachment.file_token)) attachments.push(attachment);
        pendingAttachments = pendingAttachments.filter(item => item.localId !== pending.localId);
        dispatchConversation({ type: 'RESOLVE_PENDING_ATTACHMENT', sessionId, localId: pending.localId, attachment });
        continue;
      }
      if (!pending.file) {
        const errorMessage = `附件「${pending.filename}」需要重新选择后才能发送`;
        dispatchConversation({ type: 'SET_PENDING_ATTACHMENT', sessionId, localId: pending.localId, status: 'needs_reselect', error: errorMessage });
        dispatchConversation({ type: 'PREPARE_RUN_FAILED', sessionId, generation, error: errorMessage });
        preparationControllersRef.current.delete(sessionId);
        persistPendingFirstTurn(sessionId, pendingFirstTurnSnapshot(message, attachments, pendingAttachments, modelValue, projectId, activeDatasetId));
        return;
      }
      dispatchConversation({ type: 'SET_PENDING_ATTACHMENT', sessionId, localId: pending.localId, status: 'uploading' });
      try {
        const uploaded = await uploadFile(sessionId, pending.file, preparationController.signal);
        const attachment = { file_token: uploaded.file_token, filename: uploaded.filename || pending.filename };
        attachments = [...attachments.filter(item => item.file_token !== attachment.file_token), attachment];
        pendingAttachments = pendingAttachments.filter(item => item.localId !== pending.localId);
        dispatchConversation({ type: 'RESOLVE_PENDING_ATTACHMENT', sessionId, localId: pending.localId, attachment });
        persistPendingFirstTurn(sessionId, pendingFirstTurnSnapshot(message, attachments, pendingAttachments, modelValue, projectId, activeDatasetId));
      } catch (cause: any) {
        if (requestWasAborted(cause)) return;
        const errorMessage = cause.message || `附件「${pending.filename}」上传失败`;
        dispatchConversation({ type: 'SET_PENDING_ATTACHMENT', sessionId, localId: pending.localId, status: 'failed', error: errorMessage });
        dispatchConversation({ type: 'PREPARE_RUN_FAILED', sessionId, generation, error: errorMessage });
        preparationControllersRef.current.delete(sessionId);
        persistPendingFirstTurn(sessionId, pendingFirstTurnSnapshot(message, attachments, pendingAttachments, modelValue, projectId, activeDatasetId));
        return;
      }
    }

    if (preparationController.signal.aborted) return;

    const retry = () => {
      const run = conversationRef.current.viewsBySessionId[sessionId]?.activeRun;
      if (run?.runId) openResumeConnection(sessionId, generation, { run_id: run.runId, status: run.status, last_event_index: run.lastEventIndex }, conversationRef.current.activeSessionId === sessionId);
    };
    const connection = connectionManagerRef.current.acquire({
      sessionId,
      generation,
      foreground: conversationRef.current.activeSessionId === sessionId,
      retry,
      onDetached: () => dispatchConversation({ type: 'SET_RUN_STATUS', sessionId, generation, status: 'detached' }),
    });
    if (!connection) {
      dispatchConversation({ type: 'PREPARE_RUN_FAILED', sessionId, generation, error: '当前会话仍有连接正在收尾，请稍后重试' });
      preparationControllersRef.current.delete(sessionId);
      return;
    }
    dispatchConversation({
      type: 'START_PREPARED_RUN',
      sessionId,
      generation,
      userMessage: { id: `user:${generation}`, role: 'user', content: message },
      assistantMessage: { id: `assistant:${generation}`, role: 'assistant', content: '', streaming: true, tools: [] },
    });
    setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, preview: message, last_message: message } : session));
    persistPendingFirstTurn(sessionId, pendingFirstTurnSnapshot(message, attachments, [], modelValue, projectId, activeDatasetId));
    preparationControllersRef.current.delete(sessionId);
    void consumeRunConnection(sessionId, generation, connection, 'start', { message, attachments }, undefined, undefined, undefined, undefined, true);
  }

  async function sendDraft() {
    const selectedProjectId = conversationRef.current.activeDraftProjectId;
    const draft = selectedProjectId ? conversationRef.current.draftsByProjectId[selectedProjectId] : null;
    if (!draft || !['editing', 'failed'].includes(draft.phase)) return;
    const projectId = draft.projectId;
    const message = draft.text.trim();
    if (!message) return;
    if (draftCreationControllersRef.current.has(projectId)) return;
    const creationController = new AbortController();
    draftCreationControllersRef.current.set(projectId, creationController);
    dispatchConversation({ type: 'PATCH_DRAFT', projectId, draftId: draft.draftId, patch: { phase: 'creating_session', error: undefined } });
    try {
      const response = await createSession({
        clientSessionId: draft.draftId,
        projectId,
        model: draft.model,
        activeDatasetId: draft.activeDatasetId,
        initialMessagePreview: message,
        signal: creationController.signal,
      });
      if (creationController.signal.aborted) return;
      const selectedModel = response.model || draft.model;
      const resolvedProjectId = response.project_id || projectId;
      const generation = crypto.randomUUID();
      const listedBefore = sessions.some(session => session.session_id === response.session_id);
      const nextSession: Session = {
        session_id: response.session_id,
        project_id: resolvedProjectId,
        active_dataset_id: response.active_dataset_id,
        title: response.title || message.slice(0, 36),
        preview: message,
        last_message: message,
        model: selectedModel,
        has_graph: false,
      };
      const wasActive = conversationRef.current.activeDraftProjectId === projectId
        && conversationRef.current.draftsByProjectId[projectId]?.draftId === draft.draftId;
      setAvailableModels(current => normalizedModels(current, selectedModel));
      setSessions(current => [nextSession, ...current.filter(item => item.session_id !== nextSession.session_id)]);
      if (!listedBefore) {
        setProjects(current => current.map(project => project.project_id === resolvedProjectId
          ? { ...project, conversation_count: (project.conversation_count || 0) + 1 }
          : project));
      }
      dispatchConversation({
        type: 'MATERIALIZE_DRAFT',
        projectId,
        draftId: draft.draftId,
        sessionId: response.session_id,
        generation,
        model: selectedModel,
        activeDatasetId: response.active_dataset_id,
        datasetView: response.dataset_view,
        accessedAt: Date.now(),
      });
      clearStoredDraft(projectId);
      if (wasActive) {
        setActiveProject(null);
        setGraphOpen(window.innerWidth > 900);
        writeWorkspaceRoute({ kind: 'session', id: response.session_id }, true);
      }
      void prepareAndStartFirstTurn(
        response.session_id,
        generation,
        message,
        draft.attachments.filter(attachment => Boolean(attachment.fileToken)).map(attachment => ({ file_token: attachment.fileToken!, filename: attachment.filename })),
        draft.attachments.filter(attachment => !attachment.fileToken),
        selectedModel,
        resolvedProjectId,
        response.active_dataset_id ?? draft.activeDatasetId,
      );
    } catch (cause: any) {
      if (requestWasAborted(cause)) return;
      let activeDatasetId = draft.activeDatasetId;
      if (cause instanceof ApiError && cause.code === 'DRAFT_DATASET_STALE') {
        try {
          const response = await listProjectDatasets(projectId);
          updateProjectDatasets(projectId, response.datasets || []);
          activeDatasetId = response.default_dataset_id || null;
          setProjects(current => current.map(project => project.project_id === projectId ? { ...project, default_dataset_id: activeDatasetId } : project));
        } catch { /* 保留服务端给出的原始冲突信息。 */ }
      }
      dispatchConversation({
        type: 'PATCH_DRAFT',
        projectId,
        draftId: draft.draftId,
        patch: { phase: 'failed', activeDatasetId, error: cause.message || '创建会话失败，请重试' },
      });
    } finally {
      if (draftCreationControllersRef.current.get(projectId) === creationController) draftCreationControllersRef.current.delete(projectId);
    }
  }

  async function send() {
    if (conversationRef.current.activeDraftProjectId) return sendDraft();
    if (!activeSession) return;
    const sessionId = activeSession.session_id;
    const targetView = conversationRef.current.viewsBySessionId[sessionId];
    const message = String(targetView?.draft || '').trim();
    if (!message) return;
    const gate = selectRunGate(targetView);
    if (!gate.canStartRun) {
      const blockedMessage = gate.reason === 'awaiting_feedback'
        ? '请先批准或拒绝当前待确认操作'
        : gate.reason === 'submitting_feedback'
          ? '确认正在提交，请等待状态核对完成'
          : '当前会话已有活动任务，请等待其完成';
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: blockedMessage } });
      return;
    }
    const pendingFirstTurn = readPendingFirstTurn(userId, sessionId);
    if (targetView?.pendingAttachments.length || pendingFirstTurn) {
      const generation = crypto.randomUUID();
      dispatchConversation({ type: 'BEGIN_RUN_PREPARATION', sessionId, generation });
      const sessionProjectId = activeSession.project_id || pendingFirstTurn?.projectId || '';
      void prepareAndStartFirstTurn(
        sessionId,
        generation,
        message,
        targetView?.attachments || pendingFirstTurn?.attachments || [],
        targetView?.pendingAttachments || [],
        targetView?.model || pendingFirstTurn?.model || defaultModel,
        sessionProjectId,
        targetView?.activeDatasetId ?? pendingFirstTurn?.activeDatasetId ?? null,
      );
      return;
    }
    const attachments = targetView?.attachments || [];
    const generation = crypto.randomUUID();
    const userMessage: Message = { id: `user:${generation}`, role: 'user', content: message };
    const assistantMessage: Message = { id: `assistant:${generation}`, role: 'assistant', content: '', streaming: true, tools: [] };
    dispatchConversation({ type: 'START_RUN', sessionId, generation, userMessage, assistantMessage });
    setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, preview: message, last_message: message } : session));
    const retry = () => {
      const run = conversationRef.current.viewsBySessionId[sessionId]?.activeRun;
      if (run?.runId) openResumeConnection(sessionId, generation, { run_id: run.runId, status: run.status, last_event_index: run.lastEventIndex }, conversationRef.current.activeSessionId === sessionId);
    };
    const connection = connectionManagerRef.current.acquire({
      sessionId,
      generation,
      foreground: true,
      retry,
      onDetached: () => dispatchConversation({ type: 'SET_RUN_STATUS', sessionId, generation, status: 'detached' }),
    });
    if (!connection) {
      dispatchConversation({ type: 'ROLLBACK_RUN_START', sessionId, generation, restoreDraft: message, restoreAttachments: attachments });
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: '当前会话仍有连接正在收尾，请稍后重试' } });
      return;
    }
    void consumeRunConnection(sessionId, generation, connection, 'start', { message, attachments });
  }
  async function respondToFeedback(decision: 'approve' | 'reject') {
    if (!activeSession) return;
    const sessionId = activeSession.session_id;
    const targetView = conversationRef.current.viewsBySessionId[sessionId];
    const gate = selectRunGate(targetView);
    const pending = targetView?.activeRun?.feedback;
    const run = targetView?.activeRun;
    if (!gate.canSubmitFeedback || !pending) return;
    if (!run?.runId) return dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: '当前确认请求缺少 Run ID，无法继续' } });
    const callId = String(pending.call_id || '');
    if (!callId) return dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: '当前确认请求缺少工具 Call ID，无法安全继续' } });
    if (connectionManagerRef.current.get(sessionId)) return;
    const generation = run.generation;
    const decisionId = crypto.randomUUID();
    const retry = () => openResumeConnection(sessionId, generation, { run_id: run.runId!, status: 'running', last_event_index: run.lastEventIndex }, conversationRef.current.activeSessionId === sessionId);
    const connection = connectionManagerRef.current.acquire({
      sessionId,
      runId: run.runId,
      generation,
      foreground: true,
      protected: true,
      retry,
      onDetached: () => dispatchConversation({ type: 'SET_RUN_STATUS', sessionId, generation, status: 'detached' }),
    });
    if (!connection) return;
    dispatchConversation({ type: 'BEGIN_FEEDBACK_SUBMISSION', sessionId, generation, runId: run.runId, callId, decisionId, decision });
    void consumeRunConnection(sessionId, generation, connection, 'feedback', undefined, pending, decision, undefined, decisionId);
  }
  async function upload(file: File) {
    const draftProjectId = conversationRef.current.activeDraftProjectId;
    const currentDraft = draftProjectId ? conversationRef.current.draftsByProjectId[draftProjectId] : null;
    if (currentDraft) {
      if (!['editing', 'failed'].includes(currentDraft.phase)) return;
      const replacement = currentDraft.attachments.find(attachment => attachment.filename === file.name && attachment.status === 'needs_reselect');
      const attachment: DraftAttachment = replacement
        ? { ...replacement, file, status: 'staged', error: undefined }
        : { localId: crypto.randomUUID(), filename: file.name, file, status: 'staged' };
      dispatchConversation({
        type: 'PATCH_DRAFT',
        projectId: currentDraft.projectId,
        draftId: currentDraft.draftId,
        patch: {
          phase: 'editing',
          error: undefined,
          attachments: replacement
            ? currentDraft.attachments.map(item => item.localId === replacement.localId ? attachment : item)
            : [...currentDraft.attachments, attachment],
        },
      });
      return;
    }
    if (!activeSession || uploading) return;
    const sessionId = activeSession.session_id;
    const currentView = conversationRef.current.viewsBySessionId[sessionId];
    const pendingFirstTurn = readPendingFirstTurn(userId, sessionId);
    if (currentView?.pendingAttachments.length || pendingFirstTurn) {
      const replacement = currentView.pendingAttachments.find(attachment => attachment.filename === file.name && ['needs_reselect', 'failed'].includes(attachment.status));
      const attachment: DraftAttachment = replacement
        ? { ...replacement, file, status: 'staged', error: undefined }
        : { localId: crypto.randomUUID(), filename: file.name, file, status: 'staged' };
      const nextPendingAttachments = replacement
        ? currentView.pendingAttachments.map(item => item.localId === replacement.localId ? attachment : item)
        : [...(currentView?.pendingAttachments || []), attachment];
      dispatchConversation({
        type: 'PATCH_VIEW',
        sessionId,
        patch: {
          pendingAttachments: nextPendingAttachments,
          error: undefined,
        },
      });
      if (pendingFirstTurn) persistPendingFirstTurn(sessionId, { ...pendingFirstTurn, pendingAttachments: nextPendingAttachments.map(item => ({ localId: item.localId, filename: item.filename, status: 'needs_reselect', error: '刷新后需要重新选择文件' })) });
      return;
    }
    dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { uploading: true, error: undefined } });
    try {
      const uploaded = await uploadFile(sessionId, file);
      const attachment = { file_token: uploaded.file_token, filename: uploaded.filename || file.name };
      const currentAttachments = conversationRef.current.viewsBySessionId[sessionId]?.attachments || [];
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { attachments: [...currentAttachments.filter(item => item.file_token !== attachment.file_token), attachment] } });
      if (!selectRunGate(conversationRef.current.viewsBySessionId[sessionId]).blocksSessionMutation) {
        dispatchConversation({ type: 'APPEND_MESSAGES', sessionId, messages: [{ role: 'user', content: `上传文件 ${attachment.filename}` }, { role: 'assistant', content: `文件「${attachment.filename}」已上传。请发送加载或分析指令。` }] });
      }
    } catch (cause: any) { dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: cause.message || '上传失败' } }); }
    finally { dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { uploading: false } }); }
  }
  function removeAttachment(attachmentId: string) {
    const draftProjectId = conversationRef.current.activeDraftProjectId;
    const currentDraft = draftProjectId ? conversationRef.current.draftsByProjectId[draftProjectId] : null;
    if (currentDraft && ['editing', 'failed'].includes(currentDraft.phase)) {
      dispatchConversation({ type: 'PATCH_DRAFT', projectId: currentDraft.projectId, draftId: currentDraft.draftId, patch: { attachments: currentDraft.attachments.filter(item => item.localId !== attachmentId) } });
      return;
    }
    if (!activeSession) return;
    const sessionId = activeSession.session_id;
    const currentView = conversationRef.current.viewsBySessionId[sessionId];
    if (currentView?.pendingAttachments.some(item => item.localId === attachmentId)) {
      const nextPendingAttachments = currentView.pendingAttachments.filter(item => item.localId !== attachmentId);
      dispatchConversation({ type: 'REMOVE_PENDING_ATTACHMENT', sessionId, localId: attachmentId });
      const pendingFirstTurn = readPendingFirstTurn(userId, sessionId);
      if (pendingFirstTurn) persistPendingFirstTurn(sessionId, { ...pendingFirstTurn, pendingAttachments: nextPendingAttachments.map(item => ({ localId: item.localId, filename: item.filename, status: 'needs_reselect', error: '刷新后需要重新选择文件' })) });
      return;
    }
    const nextAttachments = (currentView?.attachments || []).filter(item => item.file_token !== attachmentId);
    dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { attachments: nextAttachments } });
    const pendingFirstTurn = readPendingFirstTurn(userId, sessionId);
    if (pendingFirstTurn) persistPendingFirstTurn(sessionId, { ...pendingFirstTurn, attachments: nextAttachments });
  }
  async function changeModel(nextModel: string) {
    const draftProjectId = conversationRef.current.activeDraftProjectId;
    const currentDraft = draftProjectId ? conversationRef.current.draftsByProjectId[draftProjectId] : null;
    if (currentDraft) {
      if (!['editing', 'failed'].includes(currentDraft.phase) || nextModel === currentDraft.model) return;
      dispatchConversation({ type: 'PATCH_DRAFT', projectId: currentDraft.projectId, draftId: currentDraft.draftId, patch: { model: nextModel, phase: 'editing', error: undefined } });
      return;
    }
    if (!activeSession || busy || nextModel === model) return;
    const sessionId = activeSession.session_id;
    if (selectRunGate(conversationRef.current.viewsBySessionId[sessionId]).blocksSessionMutation) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: '活动 Run 完成前不能切换模型' } });
      return;
    }
    const previousModel = model;
    dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { model: nextModel, error: undefined } });
    setBusy(true);
    try {
      const response = await updateSessionModel(sessionId, nextModel);
      const selectedModel = response.model || nextModel;
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { model: selectedModel } });
      setAvailableModels(normalizedModels(response.available_models || availableModels, selectedModel));
      setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, model: selectedModel } : session));
    } catch (cause: any) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { model: previousModel, error: cause.message || '模型切换失败' } });
    } finally { setBusy(false); }
  }
  function openManagement(tab: 'memory' | 'skills') { setManagementTab(tab); setManagementOpen(true); }
  async function openDatasets() {
    const currentDraftProjectId = conversationRef.current.activeDraftProjectId;
    if (currentDraftProjectId) {
      setDatasetOpen(true);
      try { updateProjectDatasets(currentDraftProjectId, (await listProjectDatasets(currentDraftProjectId)).datasets || []); }
      catch (cause: any) {
        const draft = conversationRef.current.draftsByProjectId[currentDraftProjectId];
        if (draft) dispatchConversation({ type: 'PATCH_DRAFT', projectId: currentDraftProjectId, draftId: draft.draftId, patch: { error: cause.message || '项目数据集加载失败' } });
      }
      return;
    }
    if (!activeSession?.project_id) return setWorkspaceError('当前会话尚未关联项目');
    const projectId = activeSession.project_id;
    const sessionId = activeSession.session_id;
    if (selectRunGate(conversationRef.current.viewsBySessionId[sessionId]).blocksSessionMutation) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: '活动 Run 完成前不能切换数据集' } });
      return;
    }
    setDatasetOpen(true);
    try { updateProjectDatasets(projectId, (await listProjectDatasets(projectId)).datasets || []); }
    catch (cause: any) { dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: cause.message || '项目数据集加载失败' } }); }
  }
  async function chooseDataset(dataset: Dataset) {
    const draftProjectId = conversationRef.current.activeDraftProjectId;
    const currentDraft = draftProjectId ? conversationRef.current.draftsByProjectId[draftProjectId] : null;
    if (currentDraft) {
      dispatchConversation({ type: 'PATCH_DRAFT', projectId: currentDraft.projectId, draftId: currentDraft.draftId, patch: { activeDatasetId: dataset.dataset_id, phase: 'editing', error: undefined } });
      setDatasetOpen(false);
      return;
    }
    if (!activeSession || dataset.dataset_id === activeView?.activeDatasetId) return setDatasetOpen(false);
    const sessionId = activeSession.session_id;
    if (selectRunGate(conversationRef.current.viewsBySessionId[sessionId]).blocksSessionMutation) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: '活动 Run 完成前不能切换数据集' } });
      setDatasetOpen(false);
      return;
    }
    const targetView = conversationRef.current.viewsBySessionId[sessionId];
    const previousGraph = targetView?.graph || { status: 'empty' as const, data: null };
    const previousSelected = targetView?.selectedNode || null;
    const graphVersion = nextGraphVersion(sessionId);
    setBusy(true); dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: undefined } });
    try {
      const response = await setSessionDataset(sessionId, dataset.dataset_id);
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { activeDatasetId: dataset.dataset_id } });
      const unsupported = unsupportedGraphView(response.dataset_view);
      let nextGraph: GraphData | null = null;
      if (unsupported) dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: graphVersion, graph: unsupported });
      else nextGraph = await refreshSessionGraph(sessionId, {}, true, graphVersion);
      setDatasetOpen(false);
      setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, active_dataset_id: dataset.dataset_id, has_graph: Boolean(nextGraph) } : session));
    } catch (cause: any) {
      dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: graphVersion, graph: previousGraph });
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { selectedNode: previousSelected, error: cause.message || '数据集切换失败' } });
    }
    finally { setBusy(false); }
  }
  async function addProjectDataset(file: File, activateCurrentSession: boolean) {
    const draftProjectId = conversationRef.current.activeDraftProjectId;
    const projectId = activeProject?.project_id || draftProjectId || activeSession?.project_id;
    if (!projectId) return;
    const sessionId = activateCurrentSession ? activeSession?.session_id : undefined;
    if (sessionId && selectRunGate(conversationRef.current.viewsBySessionId[sessionId]).blocksSessionMutation) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: '活动 Run 完成前不能激活新数据集' } });
      return;
    }
    const targetView = sessionId ? conversationRef.current.viewsBySessionId[sessionId] : null;
    const previousGraph = targetView?.graph || { status: 'empty' as const, data: null };
    const previousSelected = targetView?.selectedNode || null;
    const graphVersion = sessionId ? nextGraphVersion(sessionId) : undefined;
    if (sessionId) dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { uploading: true, error: undefined } });
    else { setBusy(true); setWorkspaceError(''); }
    try {
      const response = await uploadProjectDataset(projectId, file, sessionId);
      const next = [...datasets.filter(item => item.dataset_id !== response.dataset.dataset_id), response.dataset]
        .map(item => ({ ...item, is_default: item.dataset_id === response.default_dataset_id }));
      updateProjectDatasets(projectId, next);
      setProjects(current => current.map(project => project.project_id === projectId ? { ...project, default_dataset_id: response.default_dataset_id } : project));
      setActiveProject(current => current?.project_id === projectId ? { ...current, default_dataset_id: response.default_dataset_id } : current);
      const currentDraft = conversationRef.current.draftsByProjectId[projectId];
      if (draftProjectId === projectId && currentDraft) {
        dispatchConversation({ type: 'PATCH_DRAFT', projectId, draftId: currentDraft.draftId, patch: { activeDatasetId: response.dataset.dataset_id, error: undefined } });
      }
      if (response.activated_session_id && sessionId && graphVersion !== undefined) {
        dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { activeDatasetId: response.dataset.dataset_id } });
        const unsupported = unsupportedGraphView(response.dataset_view);
        let nextGraph: GraphData | null = null;
        if (unsupported) dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: graphVersion, graph: unsupported });
        else nextGraph = await refreshSessionGraph(sessionId, {}, true, graphVersion);
        setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, active_dataset_id: response.dataset.dataset_id, has_graph: Boolean(nextGraph) } : session));
      }
    } catch (cause: any) {
      if (sessionId && graphVersion !== undefined) {
        dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: graphVersion, graph: previousGraph });
        dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { selectedNode: previousSelected, error: cause.message || '项目数据集上传失败' } });
      }
      else setWorkspaceError(cause.message || '项目数据集上传失败');
    } finally {
      if (sessionId) dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { uploading: false } });
      else setBusy(false);
    }
  }
  async function makeDefaultDataset(dataset: Dataset) {
    try {
      await setDefaultProjectDataset(dataset.project_id, dataset.dataset_id);
      updateProjectDatasets(dataset.project_id, current => current.map(item => ({ ...item, is_default: item.dataset_id === dataset.dataset_id })));
      setProjects(current => current.map(project => project.project_id === dataset.project_id ? { ...project, default_dataset_id: dataset.dataset_id } : project));
      setActiveProject(current => current?.project_id === dataset.project_id ? { ...current, default_dataset_id: dataset.dataset_id } : current);
    } catch (cause: any) { setWorkspaceError(cause.message || '默认数据集设置失败'); }
  }
  async function renameDataset(dataset: Dataset) {
    const name = window.prompt('数据集名称', dataset.name)?.trim();
    if (!name || name === dataset.name) return;
    try {
      const response = await renameProjectDataset(dataset.project_id, dataset.dataset_id, name);
      updateProjectDatasets(dataset.project_id, current => current.map(item => item.dataset_id === dataset.dataset_id ? { ...item, ...response.dataset } : item));
    } catch (cause: any) { setWorkspaceError(cause.message || '数据集重命名失败'); }
  }
  async function removeDataset(dataset: Dataset) {
    if (!window.confirm(`确认从项目中删除数据集「${dataset.name}」吗？`)) return;
    try {
      const response = await deleteProjectDataset(dataset.project_id, dataset.dataset_id);
      updateProjectDatasets(dataset.project_id, current => current.filter(item => item.dataset_id !== dataset.dataset_id));
      setProjects(current => current.map(project => project.project_id === dataset.project_id ? { ...project, default_dataset_id: response.default_dataset_id } : project));
      setActiveProject(current => current?.project_id === dataset.project_id ? { ...current, default_dataset_id: response.default_dataset_id } : current);
      setSessions(current => current.map(session => session.active_dataset_id === dataset.dataset_id ? { ...session, active_dataset_id: null, has_graph: false } : session));
      for (const session of sessions.filter(item => item.active_dataset_id === dataset.dataset_id)) {
        dispatchConversation({ type: 'PATCH_VIEW', sessionId: session.session_id, patch: { activeDatasetId: null } });
        const graphVersion = nextGraphVersion(session.session_id);
        dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId: session.session_id, requestVersion: graphVersion, graph: { status: 'empty', data: null } });
      }
    } catch (cause: any) { setWorkspaceError(cause.message || '数据集删除失败'); }
  }
  async function expandNode(node: Record<string, unknown>) {
    if (!activeSession) return;
    const sessionId = activeSession.session_id;
    const name = String(node.id ?? node.label ?? '').trim();
    if (!name) return;
    await refreshSessionGraph(sessionId, { node_names: [name], max_nodes: 200 }, true, undefined, true);
  }
  if (!ready) return null;
  if (!userId) return <div className="login-screen"><div className="login-box"><div className="brand-mark large"><Icon name="graph" size={24} /></div><h1>进入 iGraph</h1><p>登录后开始图谱分析</p><input autoFocus value={loginValue} onChange={event => setLoginValue(event.target.value)} onKeyDown={event => event.key === 'Enter' && login()} placeholder="用户 ID" /><button onClick={login}>登录</button>{loginError && <span className="login-error">{loginError}</span>}</div></div>;
  const activeAttachments = activeDraft
    ? activeDraft.attachments
    : [...(activeView?.attachments || []), ...(activeView?.pendingAttachments || [])];
  const layoutStyle = { '--sidebar-width': `${sidebarWidth}px`, '--graph-width': `${graphWidth}px`, '--collapsed-panel-width': `${COLLAPSED_PANEL_WIDTH}px` } as CSSProperties;
  const graphNodeCount = graphTotalNodes(graph);
  const graphEdgeCount = graphTotalEdges(graph);
  const sessionProject = projects.find(project => project.project_id === activeSession?.project_id);
  const workspaceProject = draftProject || sessionProject;
  const activeDatasetId = activeDraft?.activeDatasetId ?? activeView?.activeDatasetId;
  const activeDataset = datasets.find(dataset => dataset.dataset_id === activeDatasetId);
  const graphSummary = graphView.status === 'unsupported'
    ? `${Number(graphView.summary.num_graphs || 0).toLocaleString()} graphs`
    : graphView.status === 'ready'
      ? `${graphNodeCount.toLocaleString()} nodes · ${graphEdgeCount.toLocaleString()} edges`
      : graphView.status === 'loading'
        ? '正在加载图谱数据'
        : graphView.status === 'failed'
          ? '图谱加载失败'
          : '等待加载图谱数据';
  const runStatuses = Object.fromEntries(Object.entries(conversation.viewsBySessionId)
    .filter(([, view]) => Boolean(view.activeRun))
    .map(([sessionId, view]) => [sessionId, view.activeRun!.status]));
  return <div className="app-shell">
    <TopBar onMenu={() => setMobileSidebarOpen(true)} />
    <div ref={bodyRef} className={`app-body ${sidebarCollapsed ? 'sidebar-collapsed' : ''} ${activeSession ? graphOpen ? 'has-graph-workspace' : 'has-collapsed-graph-workspace' : 'graph-workspace-closed'}`} style={layoutStyle} onTransitionEnd={event => {
      if (event.target === event.currentTarget && event.propertyName === 'grid-template-columns') setGraphLayoutVersion(current => current + 1);
    }}>
      <Sidebar projects={projects} sessions={sessions} runStatuses={runStatuses} activeId={activeSession?.session_id} activeProjectId={activeProject?.project_id} userId={userId} dark={dark} onTheme={() => setDark(current => !current)} onMemory={() => openManagement('memory')} onSkills={() => openManagement('skills')} mobileOpen={mobileSidebarOpen} onCloseMobile={() => setMobileSidebarOpen(false)} onNew={() => newTask()} onNewProject={() => { setProjectEditorTarget(null); setProjectEditor('create'); }} onNewInProject={id => newTask(id)} onEditProject={project => { setProjectEditorTarget(project); setProjectEditor('edit'); }} onDeleteProject={setDeleteProjectTarget} onSelectProject={id => selectProject(id)} onSelect={id => selectSession(id)} onDelete={deleteTask} collapsed={sidebarCollapsed} onCollapse={() => { if (window.innerWidth <= 720) setMobileSidebarOpen(false); else setSidebarCollapsed(current => !current); }} />
      {!sidebarCollapsed && <ColumnDivider side="sidebar" value={sidebarWidth} minimum={SIDEBAR_MIN} maximum={SIDEBAR_MAX} onPointerDown={beginResize} onNudge={nudgeResize} onReset={resetResize} />}
      <div className="main-column">
        {activeProject ? <>
          <div className="workspace-error">{error && <><Icon name="close" size={14} /> {error}</>}</div>
          <ProjectWorkspace project={activeProject} datasets={datasets} busy={busy || uploading} onUpload={file => addProjectDataset(file, false)} onSetDefault={makeDefaultDataset} onRename={renameDataset} onDelete={removeDataset} />
        </> : <>
          <div className="workspace-context">
            <div className="context-icon"><Icon name="graph" size={18} /></div>
            <div>{workspaceProject && <button className="project-breadcrumb" onClick={() => selectProject(workspaceProject.project_id)}>{workspaceProject.name} /</button>}<strong>{activeDraft ? '新对话' : activeSession?.title || 'iGraph 工作台'}</strong>{!activeDraft && <span>{activeSession ? graphSummary : '选择已有会话，或新建任务'}</span>}</div>
            <div className="context-actions">
              {activeSession && <button onClick={() => setMoveSessionTarget(activeSession)}><Icon name="move" size={14} /> 移动</button>}
              {activeSession && !graphOpen && <button onClick={() => setGraphOpen(true)}><Icon name="graph" size={14} /> 展开图谱</button>}
            </div>
          </div>
          <div className="workspace-error">{error && <><Icon name="close" size={14} /> {error}</>}</div>
          {activeDraft
            ? <ChatPaneWithState conversationKey={`draft:${activeDraft.projectId}`} messages={[]} value={composerValue} model={model} models={availableModels} activeDatasetName={activeDataset?.name} onChange={(value: string) => dispatchConversation({ type: 'PATCH_DRAFT', projectId: activeDraft.projectId, draftId: activeDraft.draftId, patch: { text: value, phase: 'editing', error: undefined } })} onModelChange={changeModel} onDataset={openDatasets} onSend={send} onUpload={upload} onRemoveAttachment={removeAttachment} attachments={activeAttachments} loading={loading} uploading={uploading} disabled={busy || !['editing', 'failed'].includes(activeDraft.phase)} sendBlocked={!['editing', 'failed'].includes(activeDraft.phase)} runtimeControlsDisabled={!['editing', 'failed'].includes(activeDraft.phase)} sendBlockedReason={sendBlockedReason} runGateReason={null} interaction={{ kind: 'composer' }} hasGraph={false} onOpenGraph={() => undefined} runTools={[]} plan={null} focusRequest={composerFocusRequest} />
            : activeSession
            ? <ChatPaneWithState conversationKey={activeSession.session_id} messages={messages} value={composerValue} model={model} models={availableModels} activeDatasetName={activeDataset?.name} onChange={(value: string) => dispatchConversation({ type: 'PATCH_VIEW', sessionId: activeSession.session_id, patch: { draft: value } })} onModelChange={changeModel} onDataset={openDatasets} onSend={send} onUpload={upload} onRemoveAttachment={removeAttachment} attachments={activeAttachments} loading={loading} uploading={uploading} disabled={busy} sendBlocked={!runGate.canStartRun} runtimeControlsDisabled={runGate.blocksSessionMutation} sendBlockedReason={sendBlockedReason} runGateReason={runGate.reason} interaction={bottomInteraction} hasGraph={graphView.status === 'ready'} onOpenGraph={() => { setGraphOpen(true); setGraphTab('Graph'); }} onFeedback={respondToFeedback} runTools={runTools} plan={plan} focusRequest={composerFocusRequest} />
            : <div className="workspace-home"><div className="empty-state"><div className="empty-icon"><Icon name="graph" size={31} /></div><h1>开始新的图谱分析</h1><p>选择左侧已有会话，或创建一个新任务。</p><button className="primary-command workspace-home-action" onClick={() => newTask()} disabled={busy}><Icon name="plus" size={16} /> 新建任务</button></div></div>}
        </>}
      </div>
      {activeSession && <>{graphOpen && <ColumnDivider side="graph" value={graphWidth} minimum={GRAPH_MIN} maximum={GRAPH_MAX} onPointerDown={beginResize} onNudge={nudgeResize} onReset={resetResize} />}<GraphWorkspace view={graphView} datasetName={activeDataset?.name} messages={messages} liveTools={demoMode ? demoMlTools : runTools} tab={graphTab} selected={selectedNode} onTab={setGraphTab} onSelect={node => dispatchConversation({ type: 'PATCH_VIEW', sessionId: activeSession.session_id, patch: { selectedNode: node } })} onExpand={expandNode} collapsed={!graphOpen} onToggle={() => setGraphOpen(current => !current)} layoutVersion={graphLayoutVersion} resetKey={`${activeSession.session_id}:${activeView?.activeDatasetId || 'none'}`} /></>}
    </div>
    {datasetOpen && <DatasetPanel datasets={datasets} projectName={workspaceProject?.name} activeDatasetId={activeDatasetId} busy={busy || uploading} onSelect={chooseDataset} onUpload={file => addProjectDataset(file, Boolean(activeSession))} onRename={renameDataset} onDelete={removeDataset} onClose={() => setDatasetOpen(false)} />}
    {managementOpen && <ManagementPanel initialTab={managementTab} onClose={() => setManagementOpen(false)} />}
    {projectEditor && <ProjectDialog key={`${projectEditor}-${projectEditorTarget?.project_id || 'new'}`} project={projectEditor === 'edit' ? projectEditorTarget : null} onClose={() => { setProjectEditor(null); setProjectEditorTarget(null); }} onSave={saveProject} />}
    {deleteProjectTarget && <DeleteProjectDialog project={deleteProjectTarget} onClose={() => setDeleteProjectTarget(null)} onConfirm={deleteCurrentProject} />}
    {moveSessionTarget && <MoveConversationDialog session={moveSessionTarget} projects={projects} currentProjectId={moveSessionTarget.project_id} onClose={() => setMoveSessionTarget(null)} onConfirm={moveCurrentSession} />}
  </div>;
}

function ChatPaneWithState(props: any) {
  const summary = props.runGateReason === 'awaiting_feedback'
    ? '等待确认：请先批准或拒绝当前操作'
    : props.runGateReason === 'submitting_feedback'
      ? <><span className="loader" /> 正在提交确认并核对状态</>
      : props.loading
        ? <><span className="loader" /> Agent 正在分析图谱</>
        : props.hasGraph
          ? <><span className="status-dot" /> 图谱分析就绪</>
          : null;
  const planProgress = props.plan?.steps?.length
    ? <span className="run-count">{props.plan.steps.filter((step: any) => step.status === 'done').length}/{props.plan.steps.length} steps</span>
    : null;
  return <div className="chat-pane-shell">
    {(summary || planProgress) && <div className="run-summary">{summary}{planProgress}</div>}
    <div className="chat-pane-proxy"><ChatPane {...props} /></div>
  </div>;
}

export default App;

createRoot(document.getElementById('root')!).render(<App />);

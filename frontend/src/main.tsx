import { useEffect, useReducer, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { activateSession, ApiError, createProject as addProject, createSession, deleteProject as removeProject, deleteProjectDataset, deleteSession as removeSession, getMessages, getUserId, listProjectConversations, listProjectDatasets, listProjects, listSessions, moveSession, renameProjectDataset, resumeRun, setDefaultProjectDataset, setSessionDataset, setUserId, streamChat, streamFeedback, updateProject as patchProject, updateSessionModel, uploadFile, uploadProjectDataset, validateUser, visualizeGraph } from './api';
import type { ActiveRun, Dataset, FeedbackRequest, GraphData, Message, Project, Session, StreamBatch, ToolCall } from './types';
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
    const view = { ...createSessionView('demo-session', DEFAULT_MODEL, Date.now()), hydration: 'ready' as const, graph: demoGraph };
    return { activeSessionId: 'demo-session', viewsBySessionId: { 'demo-session': view } };
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
  const conversationRef = useRef(conversation);
  const connectionManagerRef = useRef(new RunConnectionManager(4));
  const hydrationVersionsRef = useRef(new Map<string, number>());
  const hydrationControllersRef = useRef(new Map<string, AbortController>());
  const graphVersionsRef = useRef(new Map<string, number>());
  const bodyRef = useRef<HTMLDivElement>(null);
  const resizeRef = useRef<{ side: ResizeSide; startX: number; startWidth: number } | null>(null);
  const layoutRef = useRef({ sidebarWidth, graphWidth, graphOpen, sidebarCollapsed, hasGraphPanel: false });

  conversationRef.current = conversation;
  const activeSessionId = conversation.activeSessionId;
  const activeSession = activeSessionId ? sessions.find(session => session.session_id === activeSessionId) || null : null;
  layoutRef.current = { sidebarWidth, graphWidth, graphOpen, sidebarCollapsed, hasGraphPanel: Boolean(activeSession) };
  const activeView = activeSessionId ? conversation.viewsBySessionId[activeSessionId] : null;
  const visibleProjectId = activeProject?.project_id || activeSession?.project_id || '';
  const datasets = visibleProjectId ? datasetsByProject[visibleProjectId] || [] : [];
  const messages = selectMessages(activeView);
  const graph = activeView?.graph || null;
  const selectedNode = activeView?.selectedNode || null;
  const composerValue = activeView?.draft || '';
  const model = activeView?.model || defaultModel;
  const runTools = selectRunTools(activeView);
  const plan = activeView?.activeRun?.plan || null;
  const runGate = selectRunGate(activeView);
  const bottomInteraction = selectBottomInteraction(activeView);
  const sendBlockedReason = runGate.reason === 'awaiting_feedback'
    ? '请先批准或拒绝当前待确认操作；草稿和附件会继续保留'
    : runGate.reason === 'submitting_feedback'
      ? '确认正在提交并核对状态；草稿和附件会继续保留'
      : runGate.reason
        ? '当前会话已有活动任务，完成后可发送下一条消息'
        : undefined;
  const loading = Boolean(activeView?.activeRun && ['starting', 'streaming', 'reconnecting'].includes(activeView.activeRun.status));
  const uploading = Boolean(activeView?.uploading);
  const error = activeView?.error || workspaceError;

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

  function persistedRun(sessionId: string) {
    try {
      const value = JSON.parse(sessionStorage.getItem(runStorageKey(sessionId)) || 'null');
      return value && typeof value.runId === 'string'
        ? { runId: value.runId, lastEventIndex: Number(value.lastEventIndex ?? -1) }
        : null;
    } catch { return null; }
  }

  function nextGraphVersion(sessionId: string) {
    const version = (graphVersionsRef.current.get(sessionId) || 0) + 1;
    graphVersionsRef.current.set(sessionId, version);
    dispatchConversation({ type: 'GRAPH_REQUEST_START', sessionId, requestVersion: version });
    return version;
  }

  async function refreshSessionGraph(sessionId: string, options: Record<string, unknown> = {}, reportError = false) {
    const version = nextGraphVersion(sessionId);
    try {
      const nextGraph = await visualizeGraph(sessionId, options);
      dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: version, graph: nextGraph });
      if (nextGraph?.nodes) setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, has_graph: true } : session));
      return nextGraph;
    } catch (cause: any) {
      if (reportError) dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: cause.message || '图谱加载失败' } });
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
      });
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
  ) {
    let runId = connection.runId || '';
    let lastEventIndex = resumeFromEventIndex ?? conversationRef.current.viewsBySessionId[sessionId]?.activeRun?.lastEventIndex ?? -1;
    let terminal = false;
    let awaitingFeedback = false;
    let feedbackAccepted = false;
    let feedbackSubmitFailed = false;
    const handlers = {
      onBatch: (incoming: StreamBatch) => {
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
            openResumeConnection(sessionId, generation, snapshot.activeRun, conversationRef.current.activeSessionId === sessionId);
          }
        } catch (activateError: any) {
          dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: activateError.message || cause.message || '无法恢复当前活动 Run' } });
        }
        return;
      } else if (operation === 'start' && !runId) {
        try {
          const activated = await activateSession(sessionId);
          if (activated.active_run) {
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
      connectionManagerRef.current.release(sessionId, generation);
      if (startPayload?.attachments.length) dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { attachments: [] } });
      void refreshSessionGraph(sessionId, { include_community: false });
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
      dispatchConversation({
        type: 'HYDRATE_SUCCESS', sessionId: id, requestVersion, messages: restoredMessages,
        model: selectedModel, activeDatasetId, feedback: response.pending_feedback,
        activeRun: effectiveActiveRun, generation,
        preserveMessages: Boolean(previousView?.messageOrder.length && previousView.activeRun),
      });
      setAvailableModels(normalizedModels(response.available_models, selectedModel));
      setSessions(current => mergeSessionLists(current, [{ ...session, project_id: projectId, active_dataset_id: activeDatasetId, has_graph: response.has_graph, model: selectedModel }]));

      if (projectId) {
        void listProjectDatasets(projectId, controller.signal).then(datasetResponse => {
          if (hydrationVersionsRef.current.get(id) !== requestVersion) return;
          updateProjectDatasets(projectId, datasetResponse.datasets || []);
          setProjects(current => current.map(project => project.project_id === projectId ? { ...project, default_dataset_id: datasetResponse.default_dataset_id } : project));
        }).catch(() => undefined);
      }

      if (response.has_graph) void refreshSessionGraph(id, { include_community: false });
      else {
        const graphVersion = nextGraphVersion(id);
        dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId: id, requestVersion: graphVersion, graph: null });
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
    if (route.kind === 'project') {
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
      } else if (projects.some(project => project.project_id === route.id)) {
        void selectProject(route.id, projects, false);
      } else showWorkspaceHome(true, true, '该项目不存在或已被删除');
    }
    window.addEventListener('popstate', handleHistoryNavigation);
    return () => window.removeEventListener('popstate', handleHistoryNavigation);
  }, [userId, sessions, projects, busy]);
  useEffect(() => () => connectionManagerRef.current.abortAll(), []);
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
  async function newTask(targetProjectId?: string) {
    if (busy) return; setBusy(true); setWorkspaceError('');
    const defaultProject = projects.find(project => project.is_default);
    const projectId = targetProjectId || activeProject?.project_id || activeSession?.project_id || defaultProject?.project_id;
    try {
      const response = await createSession(model, projectId);
      const selectedModel = response.model || model;
      const next: Session = { session_id: response.session_id, project_id: response.project_id || projectId, active_dataset_id: response.active_dataset_id, title: '新的图谱分析', model: selectedModel };
      setAvailableModels(current => normalizedModels(current, selectedModel));
      setSessions(current => [next, ...current.filter(item => item.session_id !== next.session_id)]);
      setProjects(current => current.map(project => project.project_id === next.project_id ? { ...project, conversation_count: (project.conversation_count || 0) + 1 } : project));
      setActiveProject(null);
      dispatchConversation({ type: 'SET_ACTIVE_SESSION', sessionId: next.session_id, model: selectedModel, accessedAt: Date.now() });
      setGraphOpen(window.innerWidth > 900);
      updateViewUrl('session', next.session_id);
    } catch (cause: any) { setWorkspaceError(cause.message || '创建任务失败'); } finally { setBusy(false); }
  }
  async function deleteTask(id: string) {
    if (!window.confirm('确认删除这个分析任务吗？')) return;
    const deleted = sessions.find(item => item.session_id === id);
    const deletingActiveSession = conversationRef.current.activeSessionId === id;
    try {
      await removeSession(id);
      connectionManagerRef.current.abort(id);
      hydrationControllersRef.current.get(id)?.abort();
      hydrationControllersRef.current.delete(id);
      clearPersistedRun(id);
      dispatchConversation({ type: 'REMOVE_SESSION', sessionId: id });
      setSessions(current => current.filter(item => item.session_id !== id));
      if (deleted?.project_id) setProjects(current => current.map(project => project.project_id === deleted.project_id ? { ...project, conversation_count: Math.max(0, (project.conversation_count || 0) - 1) } : project));
      if (activeProject?.project_id === deleted?.project_id) setActiveProject(current => current ? { ...current, conversation_count: Math.max(0, (current.conversation_count || 0) - 1) } : current);
      if (deletingActiveSession) showWorkspaceHome(true, true);
    } catch (cause: any) { setWorkspaceError(cause.message || '删除任务失败'); }
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
    const affectedSessions = sessions.filter(session => session.project_id === targetId);
    const affectedSessionIds = new Set(affectedSessions.map(session => session.session_id));
    const deletingActiveSession = Boolean(activeSessionId && affectedSessionIds.has(activeSessionId));
    const deletingActiveProject = activeProject?.project_id === targetId;
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
        hydrationControllersRef.current.get(session.session_id)?.abort();
        hydrationControllersRef.current.delete(session.session_id);
        clearPersistedRun(session.session_id);
        dispatchConversation({ type: 'REMOVE_SESSION', sessionId: session.session_id });
      }
    } else {
      for (const session of affectedSessions) {
        dispatchConversation({ type: 'PATCH_VIEW', sessionId: session.session_id, patch: { activeDatasetId: generalProject?.default_dataset_id, selectedNode: null } });
        const graphVersion = nextGraphVersion(session.session_id);
        dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId: session.session_id, requestVersion: graphVersion, graph: null });
      }
    }
    setSessions(nextSessions); setProjects(nextProjects); setDeleteProjectTarget(null);
    const general = nextProjects.find(project => project.project_id === response.general_project_id);
    if (deletingActiveSession && action === 'delete') showWorkspaceHome(true, true);
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
    nextGraphVersion(moveSessionTarget.session_id);
    const response = await moveSession(moveSessionTarget.session_id, projectId);
    setSessions(current => current.map(session => session.session_id === moveSessionTarget.session_id ? { ...session, project_id: projectId, active_dataset_id: response.active_dataset_id, has_graph: false } : session));
    dispatchConversation({ type: 'PATCH_VIEW', sessionId: moveSessionTarget.session_id, patch: { activeDatasetId: response.active_dataset_id } });
    const graphVersion = nextGraphVersion(moveSessionTarget.session_id);
    dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId: moveSessionTarget.session_id, requestVersion: graphVersion, graph: null });
    setProjects(current => current.map(project => {
      if (project.project_id === oldProjectId) return { ...project, conversation_count: Math.max(0, (project.conversation_count || 0) - 1) };
      if (project.project_id === projectId) return { ...project, conversation_count: (project.conversation_count || 0) + 1 };
      return project;
    }));
    setMoveSessionTarget(null);
  }
  async function send() {
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
    if (!activeSession || uploading) return;
    const sessionId = activeSession.session_id;
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
  function removeAttachment(fileToken: string) {
    if (!activeSession) return;
    const sessionId = activeSession.session_id;
    dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { attachments: (conversationRef.current.viewsBySessionId[sessionId]?.attachments || []).filter(item => item.file_token !== fileToken) } });
  }
  async function changeModel(nextModel: string) {
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
    if (!activeSession || dataset.dataset_id === activeSession.active_dataset_id) return setDatasetOpen(false);
    const sessionId = activeSession.session_id;
    if (selectRunGate(conversationRef.current.viewsBySessionId[sessionId]).blocksSessionMutation) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: '活动 Run 完成前不能切换数据集' } });
      setDatasetOpen(false);
      return;
    }
    nextGraphVersion(sessionId);
    setBusy(true); dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: undefined } });
    try {
      await setSessionDataset(sessionId, dataset.dataset_id);
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { activeDatasetId: dataset.dataset_id } });
      const nextGraph = await refreshSessionGraph(sessionId, {}, true);
      setDatasetOpen(false);
      if (conversationRef.current.activeSessionId === sessionId) setGraphOpen(window.innerWidth > 900);
      setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, active_dataset_id: dataset.dataset_id, has_graph: Boolean(nextGraph) } : session));
    } catch (cause: any) { dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: cause.message || '数据集切换失败' } }); }
    finally { setBusy(false); }
  }
  async function addProjectDataset(file: File, activateCurrentSession: boolean) {
    const projectId = activeProject?.project_id || activeSession?.project_id;
    if (!projectId) return;
    const sessionId = activateCurrentSession ? activeSession?.session_id : undefined;
    if (sessionId && selectRunGate(conversationRef.current.viewsBySessionId[sessionId]).blocksSessionMutation) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: '活动 Run 完成前不能激活新数据集' } });
      return;
    }
    if (sessionId) nextGraphVersion(sessionId);
    if (sessionId) dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { uploading: true, error: undefined } });
    else { setBusy(true); setWorkspaceError(''); }
    try {
      const response = await uploadProjectDataset(projectId, file, sessionId);
      const next = [...datasets.filter(item => item.dataset_id !== response.dataset.dataset_id), response.dataset]
        .map(item => ({ ...item, is_default: item.dataset_id === response.default_dataset_id }));
      updateProjectDatasets(projectId, next);
      setProjects(current => current.map(project => project.project_id === projectId ? { ...project, default_dataset_id: response.default_dataset_id } : project));
      setActiveProject(current => current?.project_id === projectId ? { ...current, default_dataset_id: response.default_dataset_id } : current);
      if (response.activated_session_id && sessionId) {
        dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { activeDatasetId: response.dataset.dataset_id } });
        const nextGraph = await refreshSessionGraph(sessionId, {}, true);
        if (conversationRef.current.activeSessionId === sessionId) setGraphOpen(window.innerWidth > 900);
        setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, active_dataset_id: response.dataset.dataset_id, has_graph: Boolean(nextGraph) } : session));
      }
    } catch (cause: any) {
      if (sessionId) dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: cause.message || '项目数据集上传失败' } });
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
        dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId: session.session_id, requestVersion: graphVersion, graph: null });
      }
    } catch (cause: any) { setWorkspaceError(cause.message || '数据集删除失败'); }
  }
  async function expandNode(node: Record<string, unknown>) {
    if (!activeSession) return;
    const sessionId = activeSession.session_id;
    const name = String(node.id ?? node.label ?? '').trim();
    if (!name) return;
    await refreshSessionGraph(sessionId, { node_names: [name], max_nodes: 200 }, true);
  }
  if (!ready) return null;
  if (!userId) return <div className="login-screen"><div className="login-box"><div className="brand-mark large"><Icon name="graph" size={24} /></div><h1>进入 iGraph</h1><p>登录后开始图谱分析</p><input autoFocus value={loginValue} onChange={event => setLoginValue(event.target.value)} onKeyDown={event => event.key === 'Enter' && login()} placeholder="用户 ID" /><button onClick={login}>登录</button>{loginError && <span className="login-error">{loginError}</span>}</div></div>;
  const activeAttachments = activeView?.attachments || [];
  const layoutStyle = { '--sidebar-width': `${sidebarWidth}px`, '--graph-width': `${graphWidth}px`, '--collapsed-panel-width': `${COLLAPSED_PANEL_WIDTH}px` } as CSSProperties;
  const graphNodeCount = graph ? (graph.total_nodes ?? graph.num_nodes ?? graph.nodes.length) : 0;
  const graphEdgeCount = graph ? (graph.total_edges ?? graph.num_edges ?? graph.edges.length) : 0;
  const sessionProject = projects.find(project => project.project_id === activeSession?.project_id);
  const activeDataset = datasets.find(dataset => dataset.dataset_id === activeSession?.active_dataset_id);
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
            <div>{sessionProject && <button className="project-breadcrumb" onClick={() => selectProject(sessionProject.project_id)}>{sessionProject.name} /</button>}<strong>{activeSession?.title || 'iGraph 工作台'}</strong><span>{activeSession ? (graph ? `${graphNodeCount.toLocaleString()} nodes · ${graphEdgeCount.toLocaleString()} edges` : '等待加载图谱数据') : '选择已有会话，或新建任务'}</span></div>
            <div className="context-actions">
              {activeSession && <button onClick={() => setMoveSessionTarget(activeSession)}><Icon name="move" size={14} /> 移动</button>}
              {activeSession && !graphOpen && <button onClick={() => setGraphOpen(true)}><Icon name="graph" size={14} /> 展开图谱</button>}
            </div>
          </div>
          <div className="workspace-error">{error && <><Icon name="close" size={14} /> {error}</>}</div>
          {activeSession
            ? <ChatPaneWithState conversationKey={activeSession.session_id} messages={messages} value={composerValue} model={model} models={availableModels} activeDatasetName={activeDataset?.name} onChange={(value: string) => dispatchConversation({ type: 'PATCH_VIEW', sessionId: activeSession.session_id, patch: { draft: value } })} onModelChange={changeModel} onDataset={openDatasets} onSend={send} onUpload={upload} onRemoveAttachment={removeAttachment} attachments={activeAttachments} loading={loading} uploading={uploading} disabled={busy} sendBlocked={!runGate.canStartRun} runtimeControlsDisabled={runGate.blocksSessionMutation} sendBlockedReason={sendBlockedReason} runGateReason={runGate.reason} interaction={bottomInteraction} hasGraph={!!graph} onOpenGraph={() => { setGraphOpen(true); setGraphTab('Graph'); }} onFeedback={respondToFeedback} runTools={runTools} plan={plan} />
            : <div className="workspace-home"><div className="empty-state"><div className="empty-icon"><Icon name="graph" size={31} /></div><h1>开始新的图谱分析</h1><p>选择左侧已有会话，或创建一个新任务。</p><button className="primary-command workspace-home-action" onClick={() => newTask()} disabled={busy}><Icon name="plus" size={16} /> 新建任务</button></div></div>}
        </>}
      </div>
      {activeSession && <>{graphOpen && <ColumnDivider side="graph" value={graphWidth} minimum={GRAPH_MIN} maximum={GRAPH_MAX} onPointerDown={beginResize} onNudge={nudgeResize} onReset={resetResize} />}<GraphWorkspace data={graph} messages={messages} liveTools={demoMode ? demoMlTools : runTools} tab={graphTab} selected={selectedNode} onTab={setGraphTab} onSelect={node => dispatchConversation({ type: 'PATCH_VIEW', sessionId: activeSession.session_id, patch: { selectedNode: node } })} onExpand={expandNode} collapsed={!graphOpen} onToggle={() => setGraphOpen(current => !current)} layoutVersion={graphLayoutVersion} /></>}
    </div>
    {datasetOpen && <DatasetPanel datasets={datasets} projectName={sessionProject?.name} activeDatasetId={activeSession?.active_dataset_id} busy={busy || uploading} onSelect={chooseDataset} onUpload={file => addProjectDataset(file, true)} onRename={renameDataset} onDelete={removeDataset} onClose={() => setDatasetOpen(false)} />}
    {managementOpen && <ManagementPanel initialTab={managementTab} onClose={() => setManagementOpen(false)} />}
    {projectEditor && <ProjectDialog key={`${projectEditor}-${projectEditorTarget?.project_id || 'new'}`} project={projectEditor === 'edit' ? projectEditorTarget : null} onClose={() => { setProjectEditor(null); setProjectEditorTarget(null); }} onSave={saveProject} />}
    {deleteProjectTarget && <DeleteProjectDialog project={deleteProjectTarget} onClose={() => setDeleteProjectTarget(null)} onConfirm={deleteCurrentProject} />}
    {moveSessionTarget && <MoveConversationDialog session={moveSessionTarget} projects={projects} currentProjectId={moveSessionTarget.project_id} onClose={() => setMoveSessionTarget(null)} onConfirm={moveCurrentSession} />}
  </div>;
}

function ChatPaneWithState(props: any) { return <div className="chat-pane-shell"><div className="run-summary">{props.runGateReason === 'awaiting_feedback' ? '等待确认：请先批准或拒绝当前操作' : props.runGateReason === 'submitting_feedback' ? <><span className="loader" /> 正在提交确认并核对状态</> : props.loading ? <><span className="loader" /> Agent 正在分析图谱</> : props.hasGraph ? <><span className="status-dot" /> 图谱分析就绪</> : '输入问题或粘贴边列表开始任务'}{props.plan?.steps?.length ? <span className="run-count">{props.plan.steps.filter((step: any) => step.status === 'done').length}/{props.plan.steps.length} steps</span> : null}</div><div className="chat-pane-proxy"><ChatPane {...props} /></div></div>; }

export default App;

createRoot(document.getElementById('root')!).render(<App />);

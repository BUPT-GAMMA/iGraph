import { listRemoteGraphs, selectRemoteGraph } from './api';
import type { RemoteConnection } from './types';
import { RemoteGraphConnections } from './components/RemoteGraphConnections';
import { remoteGraphError } from './remoteGraph';
import { getUserCases, openUserCase } from './api';
import { UserCaseGallery } from './components/UserCaseGallery';
import { UserCaseContext, localizeExampleName, userCaseForDataset, type UserCase } from './userCases';
import { useActionDialog } from './components/useActionDialog';
import { PopupPresence } from './components/PopupMotion';
import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react';
import { createRoot } from 'react-dom/client';
import { readComposerDraft, writeComposerDraft, clearComposerDraft } from './composerDrafts';
import { readPins } from './sessionPins';
import { forgetSessionActivity, pendingActivity, readSessionActivities, saveSessionActivity } from './sessionActivity';
import { activateSession, ApiError, bindRequestAccount, createProject as addProject, createSession, deleteCustomModel, deleteProject as removeProject, deleteProjectDataset, deleteSession as removeSession, getMessages, getUserId, importServerDataset, listCustomModels, listProjectConversations, listProjectDatasets, listProjects, listServerDatasets, listSessions, renameProjectDataset, saveCustomModel, setSessionDataset, updateProject as patchProject, updateSessionModel, uploadFile, uploadProjectDataset, validateUser, visualizeGraph, type CustomModel } from './api';
import type { ActiveRun, AttachmentRef, ConstructionState, CsvBundleSchema, Dataset, DatasetView, DraftAttachment, FeedbackRequest, GraphData, GraphViewState, Message, NewConversationDraft, Project, ServerDataset, Session, ToolCall } from './types';
import { RunConnectionManager } from './RunConnectionManager';
import { RunRecovery } from './RunRecovery';
import { RunCancellation } from './RunCancellation';
import { editableLatestTurn } from './latestTurn';
import { getCapabilities } from './api';
import { refreshEmptyDraftModel, draftScopeKey, conversationReducer, createSessionView, initialConversationState, latestMessageGraph, selectBottomInteraction, selectMessages, selectRunGate, selectRunTools } from './sessionState';
import { contextHighlights } from './graphContext';
import { DatasetPanel } from './components/DatasetPanel';
import { ConstructedDatasetPicker } from './components/ConstructedDatasetPicker';
import { HelpPage } from './components/HelpPage';
import { importConstructedDataset } from './api';
import { ChatPane } from './components/ChatPane';
import { GraphWorkspace } from './components/GraphWorkspace';
import { GraphConstructionPanel } from './components/GraphConstructionPanel';
import { CreateGraphWorkspace } from './components/CreateGraphWorkspace';
import { constructionLabel, isMolecularGraph, isPrebuiltGraph, isPtGraph, supportsConstruction } from './construction';
import { BrandLogo, Icon } from './components/Icon';
import { ManagementPanel } from './components/ManagementPanel';
import { DeleteProjectDialog, ProjectDialog } from './components/ProjectDialogs';
import { Sidebar } from './components/Sidebar';
import { TopBar } from './components/TopBar';
import { ModelSettings } from './components/ModelSettings';
import { listModels, getSessionModel, modelErrorMessage, type ModelCatalog } from './api';
import { parseWorkspaceRoute, writeWorkspaceRoute } from './navigation';
import { migrateGeneralDraft, readRecoveredDrafts, recoveredDraftsKey } from './draftMigration';
import { I18nProvider, tr, useI18n } from './i18n';
import { randomUUID } from './randomId';
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
  const { t } = useI18n();
  const label = side === 'sidebar' ? t('Resize sidebar', '调整左侧栏宽度') : t('Resize graph workspace', '调整图工作台宽度');
  function onKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const direction = event.key === 'ArrowRight' ? 1 : -1;
    onNudge(side, side === 'sidebar' ? direction * 16 : direction * -16);
  }
  return <div className={`column-divider column-divider-${side}`} role="separator" aria-label={label} aria-orientation="vertical" aria-valuemin={minimum} aria-valuemax={maximum} aria-valuenow={Math.round(value)} tabIndex={0} title={t(`${label}; double-click to restore the default width`, `${label}，双击恢复默认宽度`)} onPointerDown={event => onPointerDown(side, event)} onKeyDown={onKeyDown} onDoubleClick={() => onReset(side)}><span /></div>;
}

function normalizeMessages(input: unknown): Message[] {
  if (!Array.isArray(input)) return [];
  return input.map((item, index) => ({
    id: item?.id || item?.message_id || `history:${index}`,
    run_status: item?.run_status, run_id: item?.run_id, role: item?.role === 'user' ? 'user' : 'assistant',
    content: String(item?.content || ''), tools: item?.tools || [], plan: item?.plan || null,
    graphs: item?.graphs || [], reports: item?.reports || [],
    attachments: Array.isArray(item?.attachments) ? item.attachments.filter((file: any) =>
      typeof file?.filename === 'string' && typeof file?.file_token === 'string') : [],
    artifacts: Array.isArray(item?.artifacts) ? item.artifacts.filter((file: any) =>
      typeof file?.filename === 'string' && typeof file?.path === 'string') : [],
    processItems: Array.isArray(item?.processItems) ? item.processItems : [],
    thinking: Array.isArray(item?.thinking) ? item.thinking : [],
    regenerate_of: typeof item?.regenerate_of === 'string' ? item.regenerate_of : null,
  }));
}

function mergeSessionLists(current: Session[], incoming: Session[]) {
  const byId = new Map(current.map(session => [session.session_id, session]));
  for (const session of incoming) byId.set(session.session_id, { ...(byId.get(session.session_id) || {}), ...session });
  const activity = (session: Session) => {
    const raw = session.updated_at || session.created_at || 0;
    const numeric = Number(raw);
    return Number.isFinite(numeric) ? numeric * 1000 : Date.parse(String(raw)) || 0;
  };
  return [...byId.values()].sort((left, right) => activity(right) - activity(left));
}

function normalizedModels(input: unknown, selected = '') {
  const values = Array.isArray(input) ? input.filter((item): item is string => typeof item === 'string' && Boolean(item.trim())) : [];
  return Array.from(new Set([...values, selected].filter(Boolean)));
}

function requestWasAborted(cause: unknown) {
  return Boolean(cause && typeof cause === 'object' && 'name' in cause && (cause as { name?: string }).name === 'AbortError');
}

function unsupportedGraphView(datasetView: DatasetView | null | undefined): GraphViewState | null {
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
  return null;
}

function unsupportedGraphViewFromError(cause: unknown): GraphViewState | null {
  if (!(cause instanceof ApiError) || (
    cause.code !== 'GRAPH_NOT_VISUALIZABLE'
    && cause.code !== 'PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE'
  )) return null;
  const detail = cause.detail && typeof cause.detail === 'object' ? cause.detail as Record<string, unknown> : {};
  return unsupportedGraphView(detail.dataset_view as DatasetView | undefined);
}

type PendingFirstTurn = {
  message: string;
  attachments: AttachmentRef[];
  pendingAttachments: Array<Pick<DraftAttachment, 'localId' | 'filename' | 'status' | 'error'>>;
  model: string;
  projectId: string | null;
  activeDatasetId: string | null;
};

function draftStorageKey(userId: string, projectId: string | null) {
  return `igraph-new-session-draft:v1:${userId}:${draftScopeKey(projectId)}`;
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
      error: attachment.fileToken ? undefined : tr('Select the file again after refreshing', '刷新后需要重新选择文件'),
    })),
    error: draft.phase === 'editing' || draft.phase === 'failed' ? draft.error : tr('The creation flow was interrupted by a refresh. Try again.', '创建流程被页面刷新中断，请重试'),
  };
}

function readStoredDraft(userId: string, projectId: string | null): NewConversationDraft | null {
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
  const [userCases, setUserCases] = useState<UserCase[]>([]);
  const { t } = useI18n();
  const userId = getUserId();
  const { askConfirm, askPrompt, actionDialog } = useActionDialog(userId);
  const [ready, setReady] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionCursor, setSessionCursor] = useState<string | null>(null);
  const [loadingSessions, setLoadingSessions] = useState(false);
  const [sessionListError, setSessionListError] = useState('');
  const sessionPageRequest = useRef<object | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [recoveredDrafts, setRecoveredDrafts] = useState<NewConversationDraft[]>([]);
  const [conversation, dispatchConversation] = useReducer(conversationReducer, initialConversationState, state => {
    if (!demoMode) return state;
    const view = { ...createSessionView('demo-session', DEFAULT_MODEL, Date.now()), hydration: 'ready' as const, graph: { status: 'ready' as const, data: demoGraph } };
    return { activeSessionId: 'demo-session', activeDraftKey: null, viewsBySessionId: { 'demo-session': view }, draftsByScope: {} };
  });
  const [graphTab, setGraphTab] = useState<GraphTab>('Graph');
  const [graphOpen, setGraphOpen] = useState(true);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [workspaceError, setWorkspaceError] = useState('');
  const [datasetsByProject, setDatasetsByProject] = useState<Record<string, Dataset[]>>({});
  const [serverDatasets, setServerDatasets] = useState<ServerDataset[]>([]);
  const [datasetOpen, setDatasetOpen] = useState(false);
  const [remoteOpen, setRemoteOpen] = useState(false);
  const [remoteConnections, setRemoteConnections] = useState<RemoteConnection[]>([]);
  const [remoteLoadError, setRemoteLoadError] = useState('');
  const [remoteLoading, setRemoteLoading] = useState(false);
  const [datasetProjectId, setDatasetProjectId] = useState<string | undefined>();
  const [constructionDataset, setConstructionDataset] = useState<Dataset | null>(null);
  const [createGraphOpen, setCreateGraphOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(() => parseWorkspaceRoute(window.location.search).kind === 'help');
  const [importProject, setImportProject] = useState<Project | null>(null);
  const [managementOpen, setManagementOpen] = useState(false);
  const [modelsOpen, setModelsOpen] = useState(false);
  const [modelCatalog, setModelCatalog] = useState<ModelCatalog | null>(null);
  const [modelNotice, setModelNotice] = useState<{ sessionId: string; name: string } | null>(null);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [managementTab, setManagementTab] = useState<'memory' | 'skills'>('memory');
  const [defaultModel, setDefaultModel] = useState(DEFAULT_MODEL);
  const [availableModels, setAvailableModels] = useState<string[]>([DEFAULT_MODEL]);
  const [customModels, setCustomModels] = useState<CustomModel[]>([]);
  const [projectEditor, setProjectEditor] = useState<'create' | 'edit' | null>(null);
  const [projectEditorTarget, setProjectEditorTarget] = useState<Project | null>(null);
  const [deleteProjectTarget, setDeleteProjectTarget] = useState<Project | null>(null);
  const [dark, setDark] = useState(() => localStorage.getItem('igraph-theme') === 'dark');
  const [sidebarWidth, setSidebarWidth] = useState(() => storedWidth('igraph-layout-sidebar-width', window.innerWidth <= 1280 ? 230 : SIDEBAR_DEFAULT, SIDEBAR_MIN, SIDEBAR_MAX));
  const [graphWidth, setGraphWidth] = useState(() => storedWidth('igraph-layout-graph-width', window.innerWidth <= 1280 ? 330 : GRAPH_DEFAULT, GRAPH_MIN, GRAPH_MAX));
  const [composerFocusRequest, setComposerFocusRequest] = useState(0);
  const [cancelEnabled, setCancelEnabled] = useState(false);
  const [editEnabled, setEditEnabled] = useState(false);
  const conversationRef = useRef(conversation);
  const identityRef = useRef(userId);
  const accountEpochRef = useRef(0);
  identityRef.current = userId;
  const accountIsStale = (identity: string, epoch: number) => identityRef.current !== identity || accountEpochRef.current !== epoch;
  const renderAccountEpoch = accountEpochRef.current;
  const connectionManagerRef = useRef(new RunConnectionManager(4));
  const committedCursorsRef = useRef(new Map<string, { generation: string; index: number }>());
  const recoveryRef = useRef<RunRecovery | null>(null);
  const recoveryOptions = {
    controlEnabled: cancelEnabled,
    manager: connectionManagerRef.current,
    view: (id: string) => conversationRef.current.viewsBySessionId[id],
    identity: () => identityRef.current,
    paused: () => false,
    foreground: (id: string) => conversationRef.current.activeSessionId === id,
    dispatch: dispatchConversation,
    accepted: (id: string) => clearPendingFirstTurn(id),
    synchronize: (id: string, runId: string) => cancellationRef.current?.synchronize(id, runId),
    settled: (id: string) => {
      clearPersistedRun(id);
      const view = conversationRef.current.viewsBySessionId[id];
      if (view && (view.graph.status !== 'empty' || view.activeRun?.toolOrder.length)) {
        void refreshSessionGraph(id, { include_community: true }, false, undefined, true, 'background');
      }
    },
  };
  if (!recoveryRef.current) recoveryRef.current = new RunRecovery(recoveryOptions);
  else recoveryRef.current.configure(recoveryOptions);
  const cancellationRef = useRef<RunCancellation | null>(null);
  const cancellationOptions = {
    manager: connectionManagerRef.current, identity: () => identityRef.current,
    view: (id: string) => conversationRef.current.viewsBySessionId[id], dispatch: dispatchConversation,
    settled: (id: string, control: import('./api').RunControl) => {
      clearPersistedRun(id);
      if (!control.can_send && control.active_run) {
        const generation = `resume:${control.active_run.run_id}`;
        dispatchConversation({ type: 'ADOPT_RUN', sessionId: id, generation, activeRun: control.active_run });
        openResumeConnection(id, generation, control.active_run);
      }
    },
  };
  if (!cancellationRef.current) cancellationRef.current = new RunCancellation(cancellationOptions);
  else cancellationRef.current.configure(cancellationOptions);
  const stopJob = useCallback((sid: string, jid: string, notify: (status: string) => void) => {
    cancellationRef.current?.requestJob(sid, jid, notify);
  }, []);
  const hydrationVersionsRef = useRef(new Map<string, number>());
  const hydrationControllersRef = useRef(new Map<string, AbortController>());
  const draftCreationControllersRef = useRef(new Map<string, AbortController>());
  const preparationControllersRef = useRef(new Map<string, AbortController>());
  const uploadControllersRef = useRef(new Map<string, { sessionId: string; controller: AbortController }>());
  const composerSnapshotsRef = useRef(new Map<string, string>());
  const deletionRequestsRef = useRef(new Set<string>());
  const deletedProjectsRef = useRef(new Set<string>());
  const graphVersionsRef = useRef(new Map<string, number>());
  const bodyRef = useRef<HTMLDivElement>(null);
  const resizeRef = useRef<{ side: ResizeSide; startX: number; startWidth: number } | null>(null);
  const layoutRef = useRef({ sidebarWidth, graphWidth, graphOpen, sidebarCollapsed, hasGraphPanel: false });

  conversationRef.current = conversation;
  const activeSessionId = conversation.activeSessionId;
  const activeSession = activeSessionId ? sessions.find(session => session.session_id === activeSessionId) || null : null;
  layoutRef.current = { sidebarWidth, graphWidth, graphOpen, sidebarCollapsed, hasGraphPanel: Boolean(activeSession && !createGraphOpen) };
  const activeView = activeSessionId ? conversation.viewsBySessionId[activeSessionId] : null;
  const activeDraftKey = conversation.activeDraftKey;
  const activeDraft = activeDraftKey ? conversation.draftsByScope[activeDraftKey] || null : null;
  const draftProject = activeDraft ? projects.find(project => project.project_id === activeDraft.projectId) || null : null;
  const visibleProjectId = activeDraft?.projectId || activeSession?.project_id || null;
  const datasets = datasetsByProject[draftScopeKey(visibleProjectId)] || [];
  const hasPendingServerPreparation = serverDatasets.some(dataset =>
    ['not_prepared', 'queued', 'running'].includes(dataset.preparation?.status || '')
  );
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
      ? t('Creating conversation…', '正在创建会话，请稍候')
      : activeDraft.phase === 'uploading'
        ? t('Uploading attachments…', '正在上传附件，请稍候')
        : t('Starting task…', '正在启动任务，请稍候')
    : runGate.reason === 'preparing'
      ? t('Preparing the conversation or attachments…', '正在准备会话或附件，请稍候')
      : runGate.reason === 'awaiting_feedback'
    ? t('Approve or reject the pending operation first; your draft and attachments will be kept', '请先批准或拒绝当前待确认操作；草稿和附件会继续保留')
    : runGate.reason === 'submitting_feedback'
      ? t('Submitting the confirmation and checking status; your draft and attachments will be kept', '确认正在提交并核对状态；草稿和附件会继续保留')
      : runGate.reason
        ? t('This conversation has an active task. You can send another message when it finishes.', '当前会话已有活动任务，完成后可发送下一条消息')
        : undefined;
  const loading = Boolean(activeDraft && !['editing', 'failed'].includes(activeDraft.phase))
    || Boolean(activeView?.activeRun && ['preparing', 'starting', 'streaming', 'reconnecting'].includes(activeView.activeRun.status));
  const uploading = activeDraft?.phase === 'uploading' || Boolean(activeView?.uploading);
  const error = activeDraft?.error || activeView?.error || workspaceError;

  function updateProjectDatasets(projectId: string | null, update: Dataset[] | ((current: Dataset[]) => Dataset[])) {
    setDatasetsByProject(current => ({
      ...current,
      [draftScopeKey(projectId)]: typeof update === 'function' ? update(current[draftScopeKey(projectId)] || []) : update,
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
  }

  function resetResize(side: ResizeSide) {
    updateColumnWidth(side, side === 'sidebar' ? SIDEBAR_DEFAULT : GRAPH_DEFAULT);
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
  }, [activeSessionId, graphOpen, sidebarCollapsed, createGraphOpen]);

  function showWorkspaceHome(updateUrl = true, replaceUrl = false, message = '') {
    dispatchConversation({ type: 'SET_ACTIVE_SESSION', sessionId: null });
    setCreateGraphOpen(false);
    setHelpOpen(false);
    setConstructionDataset(null);
    setGraphOpen(false);
    setDatasetOpen(false);
    setMobileSidebarOpen(false);
    setWorkspaceError(message);
    if (updateUrl) writeWorkspaceRoute({ kind: 'home' }, replaceUrl);
  }

  function showHelp() {
    setManagementOpen(false);
    setHelpOpen(true);
    setMobileSidebarOpen(false);
  }

  function closeHelp() {
    setHelpOpen(false);
    if (parseWorkspaceRoute(window.location.search).kind === 'help') writeWorkspaceRoute({ kind: 'home' });
  }

  async function showCreateGraphWorkspace(updateUrl = true) {
    const identity = identityRef.current;
    dispatchConversation({ type: 'SET_ACTIVE_SESSION', sessionId: null });
    setCreateGraphOpen(true);
    setHelpOpen(false);
    setConstructionDataset(null);
    setGraphOpen(false);
    setDatasetOpen(false);
    setMobileSidebarOpen(false);
    setWorkspaceError('');
    if (updateUrl) writeWorkspaceRoute({ kind: 'create-graph' });
    setBusy(true);
    try {
      const response = await listProjectDatasets(null);
      if (identityRef.current === identity) updateProjectDatasets(null, response.datasets || []);
    } catch (cause: any) {
      if (identityRef.current === identity) setWorkspaceError(cause.message || tr('Could not load personal graph-construction sources', '个人构图数据源加载失败'));
    } finally {
      if (identityRef.current === identity) setBusy(false);
    }
  }

  function runStorageKey(sessionId: string) { return `igraph-active-run:v2:${userId}:${sessionId}`; }

  function watchPauseStorageKey(sessionId: string) { return `igraph-run-watch-paused:v1:${userId}:${sessionId}`; }

  function isWatchPausePersisted(sessionId: string) {
    // Upgrade migration only: an old observer pause never becomes a stop intent.
    try { sessionStorage.removeItem(watchPauseStorageKey(sessionId)); } catch { /* optional */ }
    return false;
  }

  function persistRun(sessionId: string, runId: string | undefined, lastAppliedEventIndex: number, startRequestId?: string) {
    if (!userId || (!runId && !startRequestId)) return;
    try { sessionStorage.setItem(runStorageKey(sessionId), JSON.stringify({ runId, startRequestId, lastAppliedEventIndex })); } catch { /* Current reception remains usable. */ }
  }

  function clearPersistedRun(sessionId: string) { try { sessionStorage.removeItem(runStorageKey(sessionId)); } catch { /* optional storage */ } }

  function persistPendingFirstTurn(sessionId: string, value: PendingFirstTurn) {
    if (!userId) return;
    try { localStorage.setItem(pendingFirstTurnStorageKey(userId, sessionId), JSON.stringify(value)); } catch { /* Refresh recovery is optional. */ }
  }

  function clearPendingFirstTurn(sessionId: string) {
    try { if (userId) localStorage.removeItem(pendingFirstTurnStorageKey(userId, sessionId)); } catch { /* optional storage */ }
  }

  function clearStoredDraft(projectId: string | null) {
    if (userId) localStorage.removeItem(draftStorageKey(userId, projectId));
  }

  function nextGraphVersion(sessionId: string, preserveCurrent = false) {
    const version = (graphVersionsRef.current.get(sessionId) || 0) + 1;
    graphVersionsRef.current.set(sessionId, version);
    dispatchConversation({ type: 'GRAPH_REQUEST_START', sessionId, requestVersion: version, preserveCurrent });
    return version;
  }

  async function refreshSessionGraph(sessionId: string, options: Record<string, unknown> = {}, reportError = false, requestVersion?: number, preserveCurrent = false, refreshMode?: 'background' | 'reset') {
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    const version = requestVersion ?? nextGraphVersion(sessionId, preserveCurrent);
    try {
      const currentGraph = conversationRef.current.viewsBySessionId[sessionId]?.graph;
      const highlights = options.highlights ?? (currentGraph?.status === 'ready' ? currentGraph.data.highlights : undefined);
      const nextGraph = await visualizeGraph(sessionId, refreshMode === 'background'
        ? { ...options, highlights: contextHighlights(highlights as GraphData['highlights']) }
        : options);
      if (accountIsStale(identity, accountEpoch)) return null;
      dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: version, graph: { status: 'ready', data: nextGraph }, refreshMode,
        requestedHighlights: refreshMode === 'background' ? highlights as GraphData['highlights'] : undefined });
      setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, has_graph: true } : session));
      return nextGraph;
    } catch (cause: any) {
      if (accountIsStale(identity, accountEpoch)) return null;
      const unsupported = unsupportedGraphViewFromError(cause);
      // A background refresh after a tool run (e.g. a remote-database answer)
      // can find no local graph at all: that is the empty state, not a failure.
      const notLoaded = refreshMode === 'background' && cause instanceof ApiError && cause.code === 'GRAPH_NOT_LOADED';
      let retainedCurrentGraph = false;
      if (unsupported || notLoaded) {
        dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: version, graph: unsupported || { status: 'empty', data: null }, refreshMode });
      } else {
        const message = cause.message || tr('Could not load the graph', '图谱加载失败');
        const currentGraph = conversationRef.current.viewsBySessionId[sessionId]?.graph;
        retainedCurrentGraph = Boolean(preserveCurrent && currentGraph?.status === 'ready');
        if (!retainedCurrentGraph) {
          dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: version, graph: { status: 'failed', data: null, error: message }, refreshMode });
        }
        if (reportError) dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: message } });
      }
      setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, has_graph: retainedCurrentGraph } : session));
      return null;
    }
  }
  function openResumeConnection(sessionId: string, generation: string, activeRun: ActiveRun, foreground = conversationRef.current.activeSessionId === sessionId) {
    recoveryRef.current!.open(sessionId, generation, activeRun, foreground);
  }

  async function consumeRunConnection(
    sessionId: string, generation: string, connection: NonNullable<ReturnType<RunConnectionManager['get']>>,
    operation: 'start' | 'resume' | 'feedback',
    startPayload?: { message: string; attachments: AttachmentRef[]; regenerateOf?: string; replaceRunId?: string }, pendingFeedback?: FeedbackRequest | null,
    feedbackDecision?: 'approve' | 'reject', _resumeFromEventIndex?: number, feedbackDecisionId?: string, _clearPendingOnOpen = false,
  ) {
    await recoveryRef.current!.consume(connection, operation, startPayload, pendingFeedback, feedbackDecision, feedbackDecisionId);
  }

  function terminateCurrentRun() {
    if (activeDraft) {
      draftCreationControllersRef.current.get(draftScopeKey(activeDraft.projectId))?.abort();
      draftCreationControllersRef.current.delete(draftScopeKey(activeDraft.projectId));
      dispatchConversation({ type: 'PATCH_DRAFT', projectId: activeDraft.projectId, draftId: activeDraft.draftId, patch: { phase: 'editing' } });
      return;
    }
    if (!activeSessionId) return;
    preparationControllersRef.current.get(activeSessionId)?.abort();
    hydrationControllersRef.current.get(activeSessionId)?.abort();
    hydrationVersionsRef.current.set(activeSessionId, (hydrationVersionsRef.current.get(activeSessionId) || 0) + 1);
    cancellationRef.current?.request(activeSessionId);
  }

  const selectSession = async (id: string, source = sessions, updateUrl = true, allowUnlisted = false) => {
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    const listedSession = source.find(item => item.session_id === id);
    const restoreComposer = !conversationRef.current.viewsBySessionId[id];
    if (!listedSession && !allowUnlisted) {
      showWorkspaceHome(true, true, t('This conversation does not exist or was deleted', '该会话不存在或已被删除'));
      return;
    }
    if (id === conversationRef.current.activeSessionId && !createGraphOpen && listedSession) return;
    const session = listedSession || { session_id: id };
    setCreateGraphOpen(false);
    setHelpOpen(false);
    setConstructionDataset(null);
    setWorkspaceError('');
    dispatchConversation({ type: 'SET_ACTIVE_SESSION', sessionId: id, model: session.model || defaultModel, accessedAt: Date.now() });
    dispatchConversation({ type: 'PATCH_VIEW', sessionId: id, patch: { selectedNode: null, error: undefined } });
    setGraphOpen(window.innerWidth > 900);
    if (updateUrl) writeWorkspaceRoute({ kind: 'session', id });

    // A live observer already owns recovery and its one control-query slot.
    // Selecting its cached view must not start a competing hydration request.
    if (connectionManagerRef.current.get(id) && conversationRef.current.viewsBySessionId[id]?.hydration === 'ready') return;

    hydrationControllersRef.current.get(id)?.abort();
    const controller = new AbortController();
    hydrationControllersRef.current.set(id, controller);
    const requestVersion = (hydrationVersionsRef.current.get(id) || 0) + 1;
    hydrationVersionsRef.current.set(id, requestVersion);
    dispatchConversation({ type: 'HYDRATE_START', sessionId: id, requestVersion });
    const observedGeneration = conversationRef.current.viewsBySessionId[id]?.activeRun?.generation;
    const cancelQuery = () => connectionManagerRef.current.cancelControl(id);
    controller.signal.addEventListener('abort', cancelQuery, { once: true });

    try {
      const response = await connectionManagerRef.current.query(id, `hydrate:${requestVersion}`, async signal => {
        const result = await activateSession(id, signal);
        return { ...result, messages: result.messages === undefined ? (await getMessages(id, signal)).messages : result.messages };
      });
      const restoredMessages = normalizeMessages(response.messages);
      if (controller.signal.aborted || accountIsStale(identity, accountEpoch) || hydrationVersionsRef.current.get(id) !== requestVersion) return;
      if (conversationRef.current.viewsBySessionId[id]?.activeRun?.generation !== observedGeneration) return;
      const selectedModel = String(response.model || session.model || response.default_model || defaultModel);
      const projectId = response.project_id !== undefined ? response.project_id : session.project_id || null;
      const activeDatasetId = Object.prototype.hasOwnProperty.call(response, 'active_dataset_id') ? response.active_dataset_id : session.active_dataset_id;
      const previousView = conversationRef.current.viewsBySessionId[id];
      const previousRun = previousView?.activeRun;
      const effectiveActiveRun = response.active_run ? {
        ...response.active_run,
        last_event_index: previousRun?.runId === response.active_run.run_id
          ? previousRun.lastAppliedEventIndex : -1,
      } : null;
      const generation = effectiveActiveRun && previousRun?.runId === effectiveActiveRun.run_id
        ? previousRun.generation
        : `resume:${effectiveActiveRun?.run_id || randomUUID()}`;
      const savedComposer = restoreComposer ? readComposerDraft(identity, id) : null;
      const graphVersion = nextGraphVersion(id, true);
      dispatchConversation({
        type: 'HYDRATE_SUCCESS', sessionId: id, requestVersion, messages: restoredMessages,
        model: selectedModel, modelRevision: response.model_revision, modelUnavailableReason: response.model_unavailable_reason,
        activeDatasetId, remoteGraph: response.remote_graph, remoteGraphUnavailableReason: response.remote_graph_unavailable_reason, feedback: response.pending_feedback,
        activeRun: effectiveActiveRun, generation, control: response.control,
        preserveMessages: Boolean(effectiveActiveRun && previousRun?.runId === effectiveActiveRun.run_id && previousRun.lastAppliedEventIndex >= 0),
        preserveComposer: previousRun?.preserveComposer || savedComposer?.preserveDuringRun,
        datasetView: response.dataset_view,
        hasGraph: response.has_graph,
      });
      if (savedComposer && !previousView?.draft && !previousView?.attachments.length && !previousView?.pendingAttachments.length) {
        const { preserveDuringRun, ...savedInputs } = savedComposer;
        dispatchConversation({ type: 'PATCH_VIEW', sessionId: id, patch: effectiveActiveRun && !preserveDuringRun
          ? { draft: savedComposer.draft } : savedInputs });
      }
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
              error: tr('Select the file again after refreshing', '刷新后需要重新选择文件'),
            })),
          },
        });
      }
      setAvailableModels(normalizedModels(response.available_models, selectedModel));
      setSessions(current => mergeSessionLists(current, [{ ...session, project_id: projectId, active_dataset_id: activeDatasetId, remote_graph: response.remote_graph, remote_graph_unavailable_reason: response.remote_graph_unavailable_reason, has_graph: response.has_graph, model: selectedModel }]));

      {
        void listProjectDatasets(projectId, controller.signal).then(datasetResponse => {
          if (controller.signal.aborted || accountIsStale(identity, accountEpoch) || hydrationVersionsRef.current.get(id) !== requestVersion) return;
          updateProjectDatasets(projectId, datasetResponse.datasets || []);
          setProjects(current => current.map(project => project.project_id === projectId ? { ...project, default_dataset_id: datasetResponse.default_dataset_id } : project));
        }).catch(() => undefined);
      }

      if (response.has_graph && response.dataset_view?.visualization.supported !== false) {
        void refreshSessionGraph(id, { include_community: true, highlights: latestMessageGraph(restoredMessages)?.highlights }, false, graphVersion, true, 'background');
      } else {
        dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId: id, requestVersion: graphVersion, graph: unsupportedGraphView(response.dataset_view) || { status: 'empty', data: null } });
      }
      isWatchPausePersisted(id);
      if (!effectiveActiveRun && !cancellationRef.current?.pending(id)) {
        connectionManagerRef.current.abort(id);
        // Retain a launch key until a control query resolves a lost first response.
        try {
          const saved = JSON.parse(sessionStorage.getItem(runStorageKey(id)) || 'null');
          if (!saved?.startRequestId) clearPersistedRun(id);
        } catch { clearPersistedRun(id); }
      }
    } catch (cause: any) {
      if (controller.signal.aborted || accountIsStale(identity, accountEpoch) || hydrationVersionsRef.current.get(id) !== requestVersion) return;
      if (!requestWasAborted(cause) && cause instanceof ApiError && cause.status === 404) {
        connectionManagerRef.current.abort(id);
        clearPersistedRun(id);
        forgetSessionActivity(identity, id);
        dispatchConversation({ type: 'REMOVE_SESSION', sessionId: id });
        cancellationRef.current?.forget(id);
        setSessions(current => current.filter(item => item.session_id !== id));
        if (conversationRef.current.activeSessionId === id) showWorkspaceHome(true, true, tr('This conversation does not exist or was deleted', '该会话不存在或已被删除'));
      } else if (!requestWasAborted(cause) && hydrationVersionsRef.current.get(id) === requestVersion) {
        dispatchConversation({ type: 'HYDRATE_FAILED', sessionId: id, requestVersion, error: cause.message || tr('Could not switch conversations', '会话切换失败') });
      }
    } finally {
      controller.signal.removeEventListener('abort', cancelQuery);
      if (hydrationControllersRef.current.get(id) === controller) hydrationControllersRef.current.delete(id);
    }
  };

  const openProjectDraft = async (id: string, source = projects) => {
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    const stale = () => accountIsStale(identity, accountEpoch) || deletedProjectsRef.current.has(`${identity}:${id}`);
    if (busy || stale()) return;
    const project = source.find(item => item.project_id === id);
    if (!project) {
      showWorkspaceHome(true, true, tr('This project does not exist or was deleted', '该项目不存在或已被删除'));
      return;
    }
    setCreateGraphOpen(false);
    setHelpOpen(false);
    setConstructionDataset(null);
    setBusy(true); setWorkspaceError('');
    try {
      const [conversationResponse, datasetResponse, serverDatasetResponse] = await Promise.all([
        listProjectConversations(id),
        listProjectDatasets(id),
        listServerDatasets(),
      ]);
      if (stale()) return;
      const conversations = conversationResponse.conversations || [];
      const nextProject = { ...project, conversation_count: conversations.length, default_dataset_id: datasetResponse.default_dataset_id };
      updateProjectDatasets(id, datasetResponse.datasets || []);
      setServerDatasets(serverDatasetResponse.datasets || []);
      setSessions(current => mergeSessionLists(current, conversations));
      setProjects(current => current.map(item => item.project_id === id ? nextProject : item));
      newTask(id, source.map(item => item.project_id === id ? nextProject : item), false);
      writeWorkspaceRoute({ kind: 'new-session', projectId: id }, true);
    } catch (cause: any) {
      if (stale()) return;
      if (cause instanceof ApiError && cause.status === 404) {
        setProjects(current => current.filter(item => item.project_id !== id));
        showWorkspaceHome(true, true, tr('This project does not exist or was deleted', '该项目不存在或已被删除'));
      } else setWorkspaceError(cause.message || tr('Could not load the project', '项目加载失败'));
    }
    finally { if (!accountIsStale(identity, accountEpoch)) setBusy(false); }
  };

  async function refresh(identity: string, stale: () => boolean) {
    const projectResponse = await listProjects();
    if (stale()) return;
    const legacyId = projectResponse.legacy_default_project_id;
    if (legacyId) {
      migrateGeneralDraft(identity, legacyId);
      const oldRoute = parseWorkspaceRoute(window.location.search);
      if (oldRoute.kind === 'project' && oldRoute.id === legacyId || oldRoute.kind === 'new-session' && oldRoute.projectId === legacyId) {
        writeWorkspaceRoute({ kind: 'new-session', projectId: null }, true);
      }
    }
    setRecoveredDrafts(readRecoveredDrafts(identity));
    const sessionResponse = await listSessions();
    if (stale()) return;
    let nextSessions = sessionResponse.sessions || [];
    setSessionCursor(sessionResponse.next_cursor || null);
    setSessionListError('');
    const activities = readSessionActivities(identity);
    const relevantActivities = activities.filter(item => pendingActivity(item) || item.completedRunId !== item.readRunId);
    const wantedIds = new Set([...readPins(identity), ...relevantActivities.map(item => item.sessionId)]);
    const missingPins = [...wantedIds].filter(id => !nextSessions.some(session => session.session_id === id));
    for (let offset = 0; offset < missingPins.length; offset += 50) {
      const pinnedResponse = await listSessions({ ids: missingPins.slice(offset, offset + 50) });
      if (stale()) return;
      nextSessions = mergeSessionLists(nextSessions, pinnedResponse.sessions || []);
    }
    const nextProjects = projectResponse.projects || [];
    setSessions(nextSessions); setProjects(nextProjects);
    const route = parseWorkspaceRoute(window.location.search);
    for (const activity of activities) {
      if (nextSessions.some(session => session.session_id === activity.sessionId)) {
        // Foreground activation owns its authoritative hydration. Avoid racing
        // it with a second observer for the saved (possibly older) run.
        const restored = route.kind === 'session' && route.id === activity.sessionId
          ? { ...activity, runId: undefined, startRequestId: undefined } : activity;
        dispatchConversation({ type: 'RESTORE_SESSION_ACTIVITY', activity: restored });
      } else if (wantedIds.has(activity.sessionId)) forgetSessionActivity(identity, activity.sessionId);
    }
    if (route.kind === 'help') {
      showHelp();
    } else if (route.kind === 'create-graph') {
      await showCreateGraphWorkspace(false);
    } else if (route.kind === 'new-session') {
      const project = nextProjects.find(item => item.project_id === route.projectId);
      if (route.projectId && !project) showWorkspaceHome(true, true, tr('This project does not exist or was deleted', '该项目不存在或已被删除'));
      else {
        const storedDraft = readStoredDraft(identity, route.projectId);
        const materialized = storedDraft && nextSessions.find(session => session.session_id === storedDraft.draftId);
        if (materialized) {
          clearStoredDraft(route.projectId);
          writeWorkspaceRoute({ kind: 'session', id: materialized.session_id }, true);
          await selectSession(materialized.session_id, nextSessions, false);
        } else newTask(route.projectId, nextProjects, false);
      }
    } else if (route.kind === 'project') {
      if (nextProjects.some(project => project.project_id === route.id)) await openProjectDraft(route.id, nextProjects);
      else showWorkspaceHome(true, true, t('This project does not exist or was deleted', '该项目不存在或已被删除'));
    } else if (route.kind === 'session') {
      await selectSession(route.id, nextSessions, false, true);
    } else showWorkspaceHome(false);
  }

  async function loadMoreSessions() {
    if (!sessionCursor || sessionPageRequest.current) return;
    const request = {};
    sessionPageRequest.current = request;
    const identity = identityRef.current;
    const epoch = accountEpochRef.current;
    setLoadingSessions(true);
    setSessionListError('');
    try {
      const response = await listSessions({ cursor: sessionCursor });
      if (accountIsStale(identity, epoch) || sessionPageRequest.current !== request) return;
      setSessions(current => mergeSessionLists(current, response.sessions || []));
      setSessionCursor(response.next_cursor || null);
    } catch {
      if (!accountIsStale(identity, epoch)) setSessionListError(tr('Could not load older tasks. Please retry.', '旧任务加载失败，请重试。'));
    } finally {
      if (sessionPageRequest.current === request) {
        sessionPageRequest.current = null;
        setLoadingSessions(false);
      }
    }
  }

  useLayoutEffect(() => bindRequestAccount(userId), [userId]);
  useEffect(() => {
    if (!userId || demoMode) return;
    const identity = userId;
    let disposed = false;
    const stale = () => disposed || identityRef.current !== identity;
    validateUser(identity).then(response => {
      if (stale()) return;
      if (response.valid === false) throw new Error(tr('Workspace unavailable', '工作区暂不可用'));
      const selectedModel = response.default_model || DEFAULT_MODEL;
      setDefaultModel(selectedModel);
      setAvailableModels(normalizedModels(response.available_models, selectedModel));
      return refresh(identity, stale);
    }).catch((cause: any) => { if (!stale()) setWorkspaceError(cause.message || tr('Could not load conversations', '无法加载会话')); });
    return () => { disposed = true; };
  }, [userId]);
  useEffect(() => {
    if (
      !userId
      || demoMode
      || (!datasetOpen && !hasPendingServerPreparation)
    ) return;
    let disposed = false;
    let inFlight = false;
    const refreshServerDatasets = () => {
      if (inFlight) return;
      inFlight = true;
      listServerDatasets()
        .then(response => { if (!disposed) setServerDatasets(response.datasets || []); })
        .catch(() => undefined)
        .finally(() => { inFlight = false; });
    };
    refreshServerDatasets();
    const timer = window.setInterval(refreshServerDatasets, 3000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [userId, datasetOpen, hasPendingServerPreparation]);
  useEffect(() => {
    if (!userId || demoMode) return;
    function handleHistoryNavigation() {
      const route = parseWorkspaceRoute(window.location.search);
      if (route.kind === 'home') {
        showWorkspaceHome(false);
      } else if (route.kind === 'help') {
        showHelp();
      } else if (route.kind === 'create-graph') {
        void showCreateGraphWorkspace(false);
      } else if (route.kind === 'session') {
        void selectSession(route.id, sessions, false, true);
      } else if (route.kind === 'new-session' && (!route.projectId || projects.some(project => project.project_id === route.projectId))) {
        newTask(route.projectId, projects, false);
      } else if (route.kind === 'project' && projects.some(project => project.project_id === route.id)) {
        void openProjectDraft(route.id, projects);
      } else showWorkspaceHome(true, true, t('This project does not exist or was deleted', '该项目不存在或已被删除'));
    }
    window.addEventListener('popstate', handleHistoryNavigation);
    return () => window.removeEventListener('popstate', handleHistoryNavigation);
  }, [userId, sessions, projects, busy]);
  useEffect(() => () => {
    cancelSessionUploads();
    recoveryRef.current?.reset();
    cancellationRef.current?.reset();
    for (const controller of preparationControllersRef.current.values()) controller.abort();
    preparationControllersRef.current.clear();
    for (const controller of draftCreationControllersRef.current.values()) controller.abort();
    draftCreationControllersRef.current.clear();
    for (const controller of hydrationControllersRef.current.values()) controller.abort();
    hydrationControllersRef.current.clear();
    hydrationVersionsRef.current.clear();
  }, [userId]);
  useEffect(() => {
    let disposed = false;
    let inFlight = false;
    let enabled = false;
    let attempt = 0;
    let controller: AbortController | undefined;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let timeoutTimer: ReturnType<typeof setTimeout> | undefined;
    setCancelEnabled(false);
    setEditEnabled(false);
    if (!userId || demoMode) return;
    async function refreshCapabilities() {
      if (disposed || inFlight || enabled) return;
      clearTimeout(retryTimer);
      inFlight = true;
      const request = controller = new AbortController();
      timeoutTimer = setTimeout(() => request.abort(), 5000);
      try {
        const value = await getCapabilities(request.signal);
        if (!disposed && !request.signal.aborted) {
          enabled = value.run_cancel_v1;
          setCancelEnabled(enabled);
          setEditEnabled(enabled && Boolean(value.edit_latest_turn_v1));
        }
      } catch { /* Startup and network failures are retried without restarting the run. */ }
      finally {
        clearTimeout(timeoutTimer);
        inFlight = false;
        if (!disposed && !enabled) {
          retryTimer = setTimeout(() => void refreshCapabilities(), Math.min(1000 * 2 ** attempt++, 30000));
        }
      }
    }
    void refreshCapabilities();
    window.addEventListener('online', refreshCapabilities);
    window.addEventListener('focus', refreshCapabilities);
    return () => {
      disposed = true;
      clearTimeout(retryTimer);
      clearTimeout(timeoutTimer);
      controller?.abort();
      window.removeEventListener('online', refreshCapabilities);
      window.removeEventListener('focus', refreshCapabilities);
    };
  }, [userId]);
  useEffect(() => {
    if (!userId || !cancelEnabled) return;
    for (const [sid, view] of Object.entries(conversation.viewsBySessionId)) {
      if (view.hydration !== 'ready') continue;
      cancellationRef.current?.restore(sid);
      cancellationRef.current?.observe(sid);
      if (!view.activeRun && !cancellationRef.current?.pending(sid)) {
        try {
          const saved = JSON.parse(sessionStorage.getItem(runStorageKey(sid)) || 'null');
          if (typeof saved?.startRequestId === 'string') {
            dispatchConversation({ type: 'BEGIN_RUN_PREPARATION', sessionId: sid, generation: saved.startRequestId });
            dispatchConversation({ type: 'SET_RECEPTION', sessionId: sid, generation: saved.startRequestId, reception: 'connecting', synchronized: false });
          }
        } catch { /* optional identity persistence */ }
      }
    }
  }, [conversation.viewsBySessionId, userId, cancelEnabled]);
  useEffect(() => {
    const manager = connectionManagerRef.current;
    for (const [id, view] of Object.entries(conversation.viewsBySessionId)) {
      const run = view.activeRun;
      if (!run) continue;
      const connection = manager.get(id);
      if (connection && (connection.generation !== run.generation || (run.runId && connection.runId && connection.runId !== run.runId))) manager.abort(id);
      if (run.serverCanSend || run.cancellation === 'confirmed' || (run.runId && ['completed', 'failed', 'cancelled'].includes(run.status) && run.synchronized !== false)) clearPersistedRun(id);
      else if (run.runId || run.startRequestId) persistRun(id, run.runId, run.lastAppliedEventIndex, run.startRequestId);
      if (!run.runId && run.startRequestId && run.reception === 'connecting' && !manager.get(id) && !manager.isCancelling(id)) {
        const pending = manager.acquire({ sessionId: id, generation: run.generation, foreground: id === activeSessionId,
          retry: () => undefined, onDetached: () => dispatchConversation({ type: 'SET_RECEPTION', sessionId: id, generation: run.generation, reception: 'capacity_suspended' }) });
        if (pending) void recoveryRef.current!.check(pending, true);
      }
      if (run.runId && run.reception === 'connecting' && !manager.get(id) && !isWatchPausePersisted(id)) {
        openResumeConnection(id, run.generation, { run_id: run.runId, status: run.status === 'awaiting_feedback' ? 'paused' : run.status, last_event_index: run.lastAppliedEventIndex });
      }
      const prior = committedCursorsRef.current.get(id);
      if (run.lastAppliedEventIndex >= 0 && (prior?.generation !== run.generation || run.lastAppliedEventIndex > prior.index)) manager.receivedEvent(id, run.generation);
      committedCursorsRef.current.set(id, { generation: run.generation, index: run.lastAppliedEventIndex });
    }
  }, [conversation.viewsBySessionId]);
  useLayoutEffect(() => {
    if (!userId || demoMode || getUserId() !== userId || identityRef.current !== userId) return;
    for (const view of Object.values(conversation.viewsBySessionId)) saveSessionActivity(userId, view);
  }, [conversation.viewsBySessionId, userId]);
  useEffect(() => {
    const wake = () => { if (document.visibilityState !== 'hidden') { cancellationRef.current?.wake(); recoveryRef.current?.wake(); } };
    window.addEventListener('online', wake);
    document.addEventListener('visibilitychange', wake);
    return () => { window.removeEventListener('online', wake); document.removeEventListener('visibilitychange', wake); };
  }, []);
  useEffect(() => () => {
    for (const controller of draftCreationControllersRef.current.values()) controller.abort();
    for (const controller of preparationControllersRef.current.values()) controller.abort();
  }, []);
  useEffect(() => {
    if (!userId) return;
    for (const draft of Object.values(conversation.draftsByScope)) {
      try { localStorage.setItem(draftStorageKey(userId, draft.projectId), JSON.stringify(serializableDraft(draft))); } catch { /* Keep the in-memory draft editable. */ }
    }
  }, [conversation.draftsByScope, userId]);
  useEffect(() => {
    if (!userId || identityRef.current !== userId || getUserId() !== userId) return;
    for (const view of Object.values(conversation.viewsBySessionId)) {
      if (view.hydration !== 'ready') continue;
      const protectedRun = selectRunGate(view).blocksSessionMutation;
      const key = JSON.stringify([userId, view.sessionId]);
      const snapshot = JSON.stringify([view.draft, view.attachments, view.pendingAttachments.map(item =>
        [item.localId, item.filename, item.status]), protectedRun, view.activeRun?.preserveComposer, view.activeRun?.status === 'preparing', view.model, view.activeDatasetId]);
      if (composerSnapshotsRef.current.get(key) === snapshot) continue;
      composerSnapshotsRef.current.set(key, snapshot);
      writeComposerDraft(userId, view.sessionId, protectedRun && !view.activeRun?.preserveComposer
        ? { draft: view.activeRun?.status === 'preparing' ? '' : view.draft, attachments: [], pendingAttachments: [] }
        : { ...view, preserveDuringRun: protectedRun && Boolean(view.activeRun?.preserveComposer) });
      const pendingFirstTurn = readPendingFirstTurn(userId, view.sessionId);
      if (pendingFirstTurn && !protectedRun) persistPendingFirstTurn(view.sessionId,
        pendingFirstTurnSnapshot(view.draft, view.attachments, view.pendingAttachments, view.model, pendingFirstTurn.projectId, view.activeDatasetId ?? null));
    }
  }, [conversation.viewsBySessionId, userId]);
  useEffect(() => {
    function warnBeforeLeaving(event: BeforeUnloadEvent) {
      const hasVolatileFile = Object.values(conversationRef.current.draftsByScope)
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
    const run = activeView.activeRun;
    if (run.runId) openResumeConnection(activeSessionId, run.generation, { run_id: run.runId, status: 'paused', last_event_index: run.lastAppliedEventIndex });
  }, [activeSessionId, activeView?.activeRun?.generation, activeView?.activeRun?.runId, bottomInteraction.kind, bottomInteraction.kind === 'syncing_feedback' ? bottomInteraction.reason : 'ready']);
  useEffect(() => { document.documentElement.dataset.theme = dark ? 'dark' : 'light'; localStorage.setItem('igraph-theme', dark ? 'dark' : 'light'); }, [dark]);
  useEffect(() => { setReady(true); }, []);
  useEffect(() => {
    if (!userId || demoMode) return;
    const identity = userId;
    let disposed = false;
    const stale = () => disposed || identityRef.current !== identity;
    const refresh = () => listModels().then(response => {
      if (stale()) return;
      applyModelCatalog(response);
    }).catch(() => undefined);
    void refresh();
    window.addEventListener('focus', refresh);
    return () => { disposed = true; window.removeEventListener('focus', refresh); };
  }, [userId]);

  function recoverOldDraft() {
    const restored = recoveredDrafts[0];
    if (!restored) return;
    const current = conversationRef.current.draftsByScope[draftScopeKey(null)];
    const remaining = recoveredDrafts.slice(1);
    if (current && current.draftId !== restored.draftId && (current.text || current.attachments.length)) remaining.push(current);
    try { localStorage.setItem(recoveredDraftsKey(userId), JSON.stringify(remaining)); } catch { return; }
    dispatchConversation({ type: 'REMOVE_DRAFT', projectId: null });
    dispatchConversation({ type: 'OPEN_DRAFT', draft: serializableDraft(restored) });
    setRecoveredDrafts(remaining);
  }

  useEffect(() => {
    if (!userId || demoMode) { setUserCases([]); return; }
    const controller = new AbortController();
    void getUserCases(controller.signal).then(result => { if (!controller.signal.aborted) setUserCases(result.cases); }).catch(() => undefined);
    return () => controller.abort();
  }, [userId]);
  async function selectUserCase(caseId: string, question?: string) {
    const identity = identityRef.current, epoch = accountEpochRef.current;
    setBusy(true); setWorkspaceError('');
    try {
      const result = await openUserCase(caseId);
      if (accountIsStale(identity, epoch)) return;
      const nextProjects = [...projects.filter(item => item.project_id !== result.project.project_id), result.project];
      setProjects(nextProjects);
      updateProjectDatasets(result.project.project_id, current => [...current.filter(item => item.dataset_id !== result.dataset.dataset_id), result.dataset]);
      const draft = newTask(result.project.project_id, nextProjects);
      if (draft && question) {
        const next = { ...draft, text: question, activeDatasetId: result.dataset.dataset_id };
        dispatchConversation({ type: 'PATCH_DRAFT', projectId: next.projectId, draftId: next.draftId, patch: { text: question, activeDatasetId: next.activeDatasetId } });
        setBusy(false);
        await sendDraft(false, next);
      }
    } catch (cause) { if (!accountIsStale(identity, epoch)) setWorkspaceError(cause instanceof Error ? cause.message : tr('Unable to open the example', '无法打开示例')); }
    finally { if (!accountIsStale(identity, epoch)) setBusy(false); }
  }
  function newTask(targetProjectId: string | null = null, sourceProjects = projects, updateUrl = true) {
    const projectId = targetProjectId;
    const project = sourceProjects.find(item => item.project_id === projectId);
    if (projectId && !project) {
      setWorkspaceError(tr('This project does not exist or was deleted', '该项目不存在或已被删除'));
      return;
    }
    const existing = conversationRef.current.draftsByScope[draftScopeKey(projectId)] || readStoredDraft(userId, projectId);
    const preferredModel = activeSession?.model || defaultModel;
    const draft = refreshEmptyDraftModel(existing || {
      draftId: randomUUID(), projectId, phase: 'editing', text: '',
      model: preferredModel,
      activeDatasetId: project?.default_dataset_id || null, attachments: [],
    }, preferredModel);
    dispatchConversation({ type: 'OPEN_DRAFT', draft, preferredModel });
    setCreateGraphOpen(false);
    setHelpOpen(false);
    setConstructionDataset(null);
    setGraphOpen(false);
    setDatasetOpen(false);
    setMobileSidebarOpen(false);
    setWorkspaceError('');
    setComposerFocusRequest(current => current + 1);
    if (updateUrl) {
      const route = parseWorkspaceRoute(window.location.search);
      if (route.kind !== 'new-session' || route.projectId !== projectId) {
        writeWorkspaceRoute({ kind: 'new-session', projectId });
      }
    }
    return draft;
  }
  async function deleteTask(id: string) {
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    const requestKey = `${accountEpoch}:${identity}:${id}`;
    if (deletionRequestsRef.current.has(requestKey)) return;
    if (!await askConfirm(tr('Delete this analysis task?', '确认删除这个分析任务吗？'))) return;
    if (accountIsStale(identity, accountEpoch) || deletionRequestsRef.current.has(requestKey)) return;
    deletionRequestsRef.current.add(requestKey);
    const deleted = sessions.find(item => item.session_id === id);
    const preparation = preparationControllersRef.current.get(id);
    preparation?.abort();
    preparationControllersRef.current.delete(id);
    try {
      await removeSession(id);
      if (accountIsStale(identity, accountEpoch)) return;
      const deletingActiveSession = conversationRef.current.activeSessionId === id;
      cancellationRef.current?.forget(id);
      connectionManagerRef.current.abort(id);
      hydrationControllersRef.current.get(id)?.abort();
      hydrationControllersRef.current.delete(id);
      clearPersistedRun(id);
      clearPendingFirstTurn(id);
      clearComposerDraft(identity, id); cancelSessionUploads(id);
      forgetSessionActivity(identity, id);
      dispatchConversation({ type: 'REMOVE_SESSION', sessionId: id });
      setSessions(current => current.filter(item => item.session_id !== id));
      if (deleted?.project_id) setProjects(current => current.map(project => project.project_id === deleted.project_id ? { ...project, conversation_count: Math.max(0, (project.conversation_count || 0) - 1) } : project));
      if (deletingActiveSession) showWorkspaceHome(true, true);
    } catch (cause: any) {
      if (accountIsStale(identity, accountEpoch)) return;
      const run = conversationRef.current.viewsBySessionId[id]?.activeRun;
      if (preparation && run?.status === 'preparing') dispatchConversation({ type: 'PREPARE_RUN_FAILED', sessionId: id, generation: run.generation, error: tr('Could not delete the task. Try again.', '删除任务失败，请重试') });
      setWorkspaceError(cause.message || tr('Could not delete the task', '删除任务失败'));
    } finally {
      deletionRequestsRef.current.delete(requestKey);
    }
  }
  async function saveProject(name: string, description: string) {
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    if (projectEditor === 'edit' && projectEditorTarget) {
      const response = await patchProject(projectEditorTarget.project_id, { name, description });
      if (accountIsStale(identity, accountEpoch)) return;
      const updated = { ...projectEditorTarget, ...response.project };
      setProjects(current => current.map(project => project.project_id === updated.project_id ? updated : project));
      setProjectEditor(null); setProjectEditorTarget(null);
      return;
    }
    const response = await addProject(name, description);
    if (accountIsStale(identity, accountEpoch)) return;
    const created = response.project;
    const nextProjects = [...projects, created];
    setProjects(nextProjects); setProjectEditor(null); setProjectEditorTarget(null);
    newTask(created.project_id, nextProjects);
  }
  async function deleteCurrentProject() {
    if (!deleteProjectTarget) return;
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    const targetId = deleteProjectTarget.project_id;
    await removeProject(targetId);
    if (accountIsStale(identity, accountEpoch)) return;
    deletedProjectsRef.current.add(`${identity}:${targetId}`);
    const affected = sessions.filter(session => session.project_id === targetId);
    const currentId = conversationRef.current.activeSessionId;
    const isVisible = Boolean(currentId && affected.some(item => item.session_id === currentId))
      || conversationRef.current.activeDraftKey === targetId
      || parseWorkspaceRoute(window.location.search).kind === 'project' && new URLSearchParams(window.location.search).get('project') === targetId;
    draftCreationControllersRef.current.get(targetId)?.abort();
    draftCreationControllersRef.current.delete(targetId);
    for (const session of affected) {
      const id = session.session_id;
      cancellationRef.current?.forget(id);
      connectionManagerRef.current.abort(id);
      preparationControllersRef.current.get(id)?.abort(); preparationControllersRef.current.delete(id);
      hydrationControllersRef.current.get(id)?.abort(); hydrationControllersRef.current.delete(id);
      clearPersistedRun(id); clearPendingFirstTurn(id);
      clearComposerDraft(identity, id); cancelSessionUploads(id);
      forgetSessionActivity(identity, id);
      dispatchConversation({ type: 'REMOVE_SESSION', sessionId: id });
    }
    setSessions(current => current.filter(item => item.project_id !== targetId));
    setProjects(current => current.filter(item => item.project_id !== targetId));
    setDeleteProjectTarget(null);
    dispatchConversation({ type: 'REMOVE_DRAFT', projectId: targetId });
    clearStoredDraft(targetId);
    setDatasetsByProject(current => { const next = { ...current }; delete next[targetId]; return next; });
    if (isVisible) newTask();
  }
  function pendingFirstTurnSnapshot(
    message: string,
    attachments: AttachmentRef[],
    pendingAttachments: DraftAttachment[],
    modelValue: string,
    projectId: string | null,
    activeDatasetId: string | null,
  ): PendingFirstTurn {
    return {
      message,
      attachments,
      pendingAttachments: pendingAttachments.map(attachment => ({
        localId: attachment.localId,
        filename: attachment.filename,
        status: attachment.fileToken ? 'uploaded' : 'needs_reselect',
        error: attachment.fileToken ? undefined : tr('Select the file again after refreshing', '刷新后需要重新选择文件'),
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
    projectId: string | null,
    activeDatasetId: string | null,
  ) {
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    preparationControllersRef.current.get(sessionId)?.abort();
    persistRun(sessionId, undefined, -1, generation);
    const preparationController = new AbortController();
    const stale = () => preparationController.signal.aborted || accountIsStale(identity, accountEpoch);
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
        const errorMessage = tr(`Select attachment “${pending.filename}” again before sending`, `附件「${pending.filename}」需要重新选择后才能发送`);
        dispatchConversation({ type: 'SET_PENDING_ATTACHMENT', sessionId, localId: pending.localId, status: 'needs_reselect', error: errorMessage });
        dispatchConversation({ type: 'PREPARE_RUN_FAILED', sessionId, generation, error: errorMessage });
        preparationControllersRef.current.delete(sessionId);
        persistPendingFirstTurn(sessionId, pendingFirstTurnSnapshot(message, attachments, pendingAttachments, modelValue, projectId, activeDatasetId));
        return;
      }
      dispatchConversation({ type: 'SET_PENDING_ATTACHMENT', sessionId, localId: pending.localId, status: 'uploading' });
      try {
        const uploaded = await uploadFile(sessionId, pending.file, preparationController.signal, progress => {
          if (!stale()) dispatchConversation({ type: 'SET_PENDING_ATTACHMENT', sessionId, localId: pending.localId, status: 'uploading', progress });
        });
        if (stale() || conversationRef.current.viewsBySessionId[sessionId]?.activeRun?.generation !== generation) return;
        if (uploaded.dataset) {
          const sourceDataset = uploaded.dataset;
          updateProjectDatasets(sourceDataset.project_id, current => [...current.filter(item => item.dataset_id !== sourceDataset.dataset_id), sourceDataset]);
        }
        const attachment = { file_token: uploaded.file_token, filename: uploaded.filename || pending.filename };
        attachments = [...attachments.filter(item => item.file_token !== attachment.file_token), attachment];
        pendingAttachments = pendingAttachments.filter(item => item.localId !== pending.localId);
        dispatchConversation({ type: 'RESOLVE_PENDING_ATTACHMENT', sessionId, localId: pending.localId, attachment });
        persistPendingFirstTurn(sessionId, pendingFirstTurnSnapshot(message, attachments, pendingAttachments, modelValue, projectId, activeDatasetId));
      } catch (cause: any) {
        if (stale() || conversationRef.current.viewsBySessionId[sessionId]?.activeRun?.generation !== generation || requestWasAborted(cause)) return;
        const errorMessage = cause.message || tr(`Could not upload attachment “${pending.filename}”`, `附件「${pending.filename}」上传失败`);
        dispatchConversation({ type: 'SET_PENDING_ATTACHMENT', sessionId, localId: pending.localId, status: 'failed', error: errorMessage });
        dispatchConversation({ type: 'PREPARE_RUN_FAILED', sessionId, generation, error: errorMessage });
        preparationControllersRef.current.delete(sessionId);
        persistPendingFirstTurn(sessionId, pendingFirstTurnSnapshot(message, attachments, pendingAttachments, modelValue, projectId, activeDatasetId));
        return;
      }
    }

    if (stale()) return;

    const retry = () => {
      const run = conversationRef.current.viewsBySessionId[sessionId]?.activeRun;
      if (run?.runId) openResumeConnection(sessionId, generation, { run_id: run.runId, status: run.status, last_event_index: run.lastAppliedEventIndex }, conversationRef.current.activeSessionId === sessionId);
    };
    const connection = connectionManagerRef.current.acquire({
      sessionId,
      generation,
      foreground: conversationRef.current.activeSessionId === sessionId,
      retry,
      onDetached: () => dispatchConversation({ type: 'SET_RECEPTION', sessionId, generation, reception: 'capacity_suspended', synchronized: false }),
    });
    if (!connection) {
      dispatchConversation({ type: 'PREPARE_RUN_FAILED', sessionId, generation, error: tr('A connection for this conversation is still closing. Try again shortly.', '当前会话仍有连接正在收尾，请稍后重试') });
      preparationControllersRef.current.delete(sessionId);
      return;
    }
    dispatchConversation({
      type: 'START_PREPARED_RUN',
      sessionId,
      generation,
      userMessage: { id: `user:${generation}`, role: 'user', content: message, attachments },
      assistantMessage: { id: `assistant:${generation}`, role: 'assistant', content: '', streaming: true, tools: [] },
    });
    setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, preview: message, last_message: message } : session));
    persistPendingFirstTurn(sessionId, pendingFirstTurnSnapshot(message, attachments, [], modelValue, projectId, activeDatasetId));
    preparationControllersRef.current.delete(sessionId);
    void consumeRunConnection(sessionId, generation, connection, 'start', { message, attachments }, undefined, undefined, undefined, undefined, true);
  }

  async function refreshRemoteConnections() {
    const identity = identityRef.current;
    const epoch = accountEpochRef.current;
    setRemoteLoading(true); setRemoteLoadError('');
    try {
      const result = await listRemoteGraphs();
      if (accountIsStale(identity, epoch)) return;
      setRemoteConnections(result.connections);
      // Deletion keeps the historical source but marks the current selection unavailable.
      for (const view of Object.values(conversationRef.current.viewsBySessionId)) {
        const binding = view.remoteGraph;
        if (!binding) continue;
        const current = result.connections.find(item => item.connection_id === binding.connection_id);
        if (!current) {
          dispatchConversation({ type: 'PATCH_VIEW', sessionId: view.sessionId, patch: { remoteGraphUnavailableReason: 'REMOTE_GRAPH_NOT_FOUND' } });
          continue;
        }
        const { connection_id, revision, name, kind, database } = current;
        const remoteGraph = { connection_id, revision, name, kind, database };
        dispatchConversation({ type: 'PATCH_VIEW', sessionId: view.sessionId, patch: { remoteGraph, remoteGraphUnavailableReason: null } });
        setSessions(sessions => sessions.map(session => session.session_id === view.sessionId
          ? { ...session, remote_graph: remoteGraph, remote_graph_unavailable_reason: null } : session));
      }
    } catch (cause) { if (!accountIsStale(identity, epoch)) setRemoteLoadError(remoteGraphError(cause)); }
    finally { if (!accountIsStale(identity, epoch)) setRemoteLoading(false); }
  }

  function openRemoteConnections() {
    setDatasetOpen(false); setRemoteOpen(true); void refreshRemoteConnections();
  }

  async function chooseRemoteConnection(connectionId: string | null) {
    const identity = identityRef.current;
    const epoch = accountEpochRef.current;
    const sessionId = conversationRef.current.activeSessionId || await sendDraft(true);
    if (accountIsStale(identity, epoch)) return;
    if (!sessionId) throw new Error('Session unavailable');
    const result = await selectRemoteGraph(sessionId, connectionId);
    if (accountIsStale(identity, epoch)) return;
    dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { remoteGraph: result.remote_graph, remoteGraphUnavailableReason: null } });
    setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, remote_graph: result.remote_graph, remote_graph_unavailable_reason: null } : session));
  }

  async function sendDraft(uploadOnly = false, draftOverride?: NewConversationDraft) {
    const selectedProjectId = conversationRef.current.activeDraftKey;
    const draft = draftOverride || (selectedProjectId ? conversationRef.current.draftsByScope[selectedProjectId] : null);
    if (!draft || !['editing', 'failed'].includes(draft.phase)) return;
    const unavailable = unavailableModel(draft.model);
    if (!uploadOnly && unavailable) {
      dispatchConversation({ type: 'PATCH_DRAFT', projectId: draft.projectId, draftId: draft.draftId, patch: { error: modelErrorMessage(unavailable) } });
      return;
    }
    const projectId = draft.projectId;
    const message = draft.text.trim();
    if (!message && !uploadOnly) return;
    if (draftCreationControllersRef.current.has(draftScopeKey(projectId))) return;
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    const creationController = new AbortController();
    const stale = () => creationController.signal.aborted || accountIsStale(identity, accountEpoch);
    const generation = randomUUID();
    draftCreationControllersRef.current.set(draftScopeKey(projectId), creationController);
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
      if (stale()) return;
      const selectedModel = response.model || draft.model;
      const resolvedProjectId = response.project_id !== undefined ? response.project_id : projectId;
      const listedBefore = sessions.some(session => session.session_id === response.session_id);
      const nextSession: Session = {
        session_id: response.session_id,
        project_id: resolvedProjectId,
        active_dataset_id: response.active_dataset_id,
        title: response.title || message.slice(0, 36) || tr('New conversation', '新对话'),
        preview: message,
        last_message: message,
        model: selectedModel,
        has_graph: false,
      };
      const wasActive = conversationRef.current.activeDraftKey === draftScopeKey(projectId)
        && conversationRef.current.draftsByScope[draftScopeKey(projectId)]?.draftId === draft.draftId;
      setAvailableModels(current => normalizedModels(current, selectedModel));
      setSessions(current => [nextSession, ...current.filter(item => item.session_id !== nextSession.session_id)]);
      if (!listedBefore) {
        setProjects(current => current.map(project => project.project_id === resolvedProjectId
          ? { ...project, conversation_count: (project.conversation_count || 0) + 1 }
          : project));
      }
      dispatchConversation({
        type: 'MATERIALIZE_DRAFT',
        prepareRun: !uploadOnly,
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
        setGraphOpen(window.innerWidth > 900);
        writeWorkspaceRoute({ kind: 'session', id: response.session_id }, true);
      }
      if (uploadOnly) {
        for (const attachment of draft.attachments.filter(item => !item.fileToken && item.file)) void uploadAttachment(response.session_id, attachment);
        return response.session_id;
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
      if (stale() || requestWasAborted(cause)) return;
      let activeDatasetId = draft.activeDatasetId;
      if (cause instanceof ApiError && cause.code === 'DRAFT_DATASET_STALE') {
        try {
          const response = await listProjectDatasets(projectId, creationController.signal);
          if (stale()) return;
          updateProjectDatasets(projectId, response.datasets || []);
          activeDatasetId = response.default_dataset_id || null;
          setProjects(current => current.map(project => project.project_id === projectId ? { ...project, default_dataset_id: activeDatasetId } : project));
        } catch {
          if (stale()) return;
          /* 保留服务端给出的原始冲突信息。 */
        }
      }
      if (stale()) return;
      dispatchConversation({
        type: 'PATCH_DRAFT',
        projectId,
        draftId: draft.draftId,
        patch: { phase: 'failed', activeDatasetId, error: cause.message || tr('Could not create the conversation. Try again.', '创建会话失败，请重试') },
      });
    } finally {
      if (draftCreationControllersRef.current.get(draftScopeKey(projectId)) === creationController) draftCreationControllersRef.current.delete(draftScopeKey(projectId));
    }
  }

  async function send(edit?: { message: string; attachments: AttachmentRef[]; replaceRunId: string }, messageOverride?: string) {
    if (conversationRef.current.activeDraftKey) return sendDraft();
    if (!activeSession) return;
    const sessionId = activeSession.session_id;
    const targetView = conversationRef.current.viewsBySessionId[sessionId];
    const unavailable = targetView?.modelUnavailableReason || unavailableModel(targetView?.model || defaultModel);
    if (unavailable) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: modelErrorMessage(unavailable) } });
      return;
    }
    const message = String(edit?.message ?? messageOverride ?? targetView?.draft ?? '').trim();
    if (!message) return;
    const gate = selectRunGate(targetView);
    if (!gate.canStartRun) {
      const blockedMessage = gate.reason === 'awaiting_feedback'
        ? tr('Approve or reject the pending operation first', '请先批准或拒绝当前待确认操作')
        : gate.reason === 'submitting_feedback'
          ? tr('The confirmation is being submitted. Wait for the status check to finish.', '确认正在提交，请等待状态核对完成')
          : tr('This conversation has an active task. Wait for it to finish.', '当前会话已有活动任务，请等待其完成');
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: blockedMessage } });
      return;
    }
    if (targetView?.uploading || [...uploadControllersRef.current.values()].some(item => item.sessionId === sessionId)) return;
    const pendingFirstTurn = readPendingFirstTurn(userId, sessionId);
    if (!edit && messageOverride === undefined && (targetView?.pendingAttachments.length || pendingFirstTurn)) {
      const generation = randomUUID();
      dispatchConversation({ type: 'BEGIN_RUN_PREPARATION', sessionId, generation });
      const sessionProjectId = activeSession.project_id ?? pendingFirstTurn?.projectId ?? null;
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
    if (edit && (!editEnabled || editableLatestTurn(selectMessages(targetView))?.assistant.run_id !== edit.replaceRunId)) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: tr('The latest turn changed. Refresh and try again.', '最新一轮已变化，请刷新后重试') } });
      return;
    }
    const attachments = messageOverride !== undefined ? [] : edit?.attachments ?? targetView?.attachments ?? [];
    const generation = randomUUID();
    const userMessage: Message = { id: `user:${generation}`, role: 'user', content: message, attachments };
    persistRun(sessionId, undefined, -1, generation);
    const assistantMessage: Message = { id: `assistant:${generation}`, role: 'assistant', content: '', streaming: true, tools: [] };
    dispatchConversation({ type: 'START_RUN', sessionId, generation, userMessage, assistantMessage, replaceRunId: edit?.replaceRunId, preserveComposer: messageOverride !== undefined });
    setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, preview: message, last_message: message } : session));
    const retry = () => {
      const run = conversationRef.current.viewsBySessionId[sessionId]?.activeRun;
      if (run?.runId) openResumeConnection(sessionId, generation, { run_id: run.runId, status: run.status, last_event_index: run.lastAppliedEventIndex }, conversationRef.current.activeSessionId === sessionId);
    };
    const connection = connectionManagerRef.current.acquire({
      sessionId,
      generation,
      foreground: true,
      retry,
      onDetached: () => dispatchConversation({ type: 'SET_RECEPTION', sessionId, generation, reception: 'capacity_suspended', synchronized: false }),
    });
    if (!connection) {
      dispatchConversation({ type: 'ROLLBACK_RUN_START', sessionId, generation, restoreDraft: messageOverride !== undefined ? targetView?.draft || '' : message, restoreAttachments: messageOverride !== undefined ? targetView?.attachments || [] : attachments });
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: tr('A connection for this conversation is still closing. Try again shortly.', '当前会话仍有连接正在收尾，请稍后重试') } });
      return;
    }
    void consumeRunConnection(sessionId, generation, connection, 'start', { message, attachments, replaceRunId: edit?.replaceRunId });
  }
  async function respondToFeedback(decision: 'approve' | 'reject') {
    if (!activeSession) return;
    const sessionId = activeSession.session_id;
    const targetView = conversationRef.current.viewsBySessionId[sessionId];
    const gate = selectRunGate(targetView);
    const pending = targetView?.activeRun?.feedback;
    const run = targetView?.activeRun;
    if (!gate.canSubmitFeedback || !pending) return;
    if (!run?.runId) return dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: tr('The confirmation request has no run ID and cannot continue', '当前确认请求缺少 Run ID，无法继续') } });
    const callId = String(pending.call_id || '');
    if (!callId) return dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: tr('The confirmation request has no tool call ID and cannot continue safely', '当前确认请求缺少工具 Call ID，无法安全继续') } });
    if (connectionManagerRef.current.get(sessionId)) return;
    const generation = run.generation;
    const decisionId = randomUUID();
    const retry = () => openResumeConnection(sessionId, generation, { run_id: run.runId!, status: 'running', last_event_index: run.lastAppliedEventIndex }, conversationRef.current.activeSessionId === sessionId);
    const connection = connectionManagerRef.current.acquire({
      sessionId,
      runId: run.runId,
      generation,
      foreground: true,
      protected: true,
      retry,
      onDetached: () => dispatchConversation({ type: 'SET_RECEPTION', sessionId, generation, reception: 'capacity_suspended', synchronized: false }),
    });
    if (!connection) return;
    dispatchConversation({ type: 'BEGIN_FEEDBACK_SUBMISSION', sessionId, generation, runId: run.runId, callId, decisionId, decision });
    void consumeRunConnection(sessionId, generation, connection, 'feedback', undefined, pending, decision, undefined, decisionId);
  }
  function cancelSessionUploads(sessionId?: string) {
    for (const [id, pending] of uploadControllersRef.current) {
      if (!sessionId || pending.sessionId === sessionId) {
        pending.controller.abort();
        uploadControllersRef.current.delete(id);
      }
    }
  }

  async function uploadAttachment(sessionId: string, attachment: DraftAttachment) {
    if (!attachment.file || uploadControllersRef.current.has(attachment.localId)) return;
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    const entry = { sessionId, controller: new AbortController() };
    const stale = () => entry.controller.signal.aborted || accountIsStale(identity, accountEpoch)
      || uploadControllersRef.current.get(attachment.localId) !== entry;
    uploadControllersRef.current.set(attachment.localId, entry);
    dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { uploading: true, error: undefined } });
    dispatchConversation({ type: 'SET_PENDING_ATTACHMENT', sessionId, localId: attachment.localId, status: 'uploading', progress: 0 });
    try {
      const uploaded = await uploadFile(sessionId, attachment.file, entry.controller.signal, progress => {
        if (!stale()) dispatchConversation({ type: 'SET_PENDING_ATTACHMENT', sessionId, localId: attachment.localId, status: 'uploading', progress });
      });
      if (stale()) return;
      if (uploaded.dataset) {
        const sourceDataset = uploaded.dataset;
        updateProjectDatasets(sourceDataset.project_id, current => [...current.filter(item => item.dataset_id !== sourceDataset.dataset_id), sourceDataset]);
        if (uploaded.construction_required && conversationRef.current.activeSessionId === sessionId) openConstruction(sourceDataset);
      }
      dispatchConversation({ type: 'RESOLVE_PENDING_ATTACHMENT', sessionId, localId: attachment.localId,
        attachment: { file_token: uploaded.file_token, filename: uploaded.filename || attachment.filename } });
    } catch (cause) {
      if (stale()) return;
      const error = cause instanceof Error ? cause.message : tr('Upload failed. Try again.', '上传失败，请重试');
      dispatchConversation({ type: 'SET_PENDING_ATTACHMENT', sessionId, localId: attachment.localId, status: 'failed', error });
    } finally {
      if (uploadControllersRef.current.get(attachment.localId) === entry) {
        uploadControllersRef.current.delete(attachment.localId);
        if (!accountIsStale(identity, accountEpoch)) dispatchConversation({ type: 'PATCH_VIEW', sessionId,
          patch: { uploading: [...uploadControllersRef.current.values()].some(item => item.sessionId === sessionId) } });
      }
    }
  }

  async function upload(file: File) {
    const state = conversationRef.current;
    const draft = state.activeDraftKey ? state.draftsByScope[state.activeDraftKey] : null;
    const sessionId = state.activeSessionId;
    const view = sessionId ? state.viewsBySessionId[sessionId] : null;
    if (draft && (!['editing', 'failed'].includes(draft.phase) || draftCreationControllersRef.current.has(draftScopeKey(draft.projectId)))) return;
    if (!draft && (!sessionId || !view || selectRunGate(view).blocksSessionMutation)) return;
    const pending = draft?.attachments || view?.pendingAttachments || [];
    const replacement = pending.find(item => item.filename === file.name && ['failed', 'needs_reselect'].includes(item.status));
    const attachment: DraftAttachment = { localId: replacement?.localId || randomUUID(), filename: file.name, file, status: 'staged' };
    const nextPending = [...pending.filter(item => item.localId !== attachment.localId), attachment];
    if (draft) {
      const nextDraft = { ...draft, attachments: nextPending };
      dispatchConversation({ type: 'PATCH_DRAFT', projectId: draft.projectId, draftId: draft.draftId, patch: { attachments: nextPending, error: undefined } });
      await sendDraft(true, nextDraft);
    } else if (sessionId) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { pendingAttachments: nextPending, error: undefined } });
      void uploadAttachment(sessionId, attachment);
    }
  }

  function retryAttachment(localId: string) {
    const sessionId = conversationRef.current.activeSessionId;
    if (!sessionId) return;
    const view = conversationRef.current.viewsBySessionId[sessionId];
    if (selectRunGate(view).blocksSessionMutation) return;
    const attachment = view?.pendingAttachments.find(item => item.localId === localId);
    if (attachment?.status === 'failed' && attachment.file) void uploadAttachment(sessionId, attachment);
  }

  function removeAttachment(attachmentId: string) {
    const draftProjectId = conversationRef.current.activeDraftKey;
    const currentDraft = draftProjectId ? conversationRef.current.draftsByScope[draftProjectId] : null;
    if (currentDraft && ['editing', 'failed'].includes(currentDraft.phase)) {
      dispatchConversation({ type: 'PATCH_DRAFT', projectId: currentDraft.projectId, draftId: currentDraft.draftId, patch: { attachments: currentDraft.attachments.filter(item => item.localId !== attachmentId) } });
      return;
    }
    if (!activeSession) return;
    const sessionId = activeSession.session_id;
    const currentView = conversationRef.current.viewsBySessionId[sessionId];
    if (selectRunGate(currentView).blocksSessionMutation) return;
    const pendingUpload = uploadControllersRef.current.get(attachmentId);
    if (pendingUpload?.sessionId === sessionId) {
      pendingUpload.controller.abort(); uploadControllersRef.current.delete(attachmentId);
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: {
        uploading: [...uploadControllersRef.current.values()].some(item => item.sessionId === sessionId),
      } });
    }
    if (currentView?.pendingAttachments.some(item => item.localId === attachmentId)) {
      const nextPendingAttachments = currentView.pendingAttachments.filter(item => item.localId !== attachmentId);
      dispatchConversation({ type: 'REMOVE_PENDING_ATTACHMENT', sessionId, localId: attachmentId });
      const pendingFirstTurn = readPendingFirstTurn(userId, sessionId);
      if (pendingFirstTurn) persistPendingFirstTurn(sessionId, { ...pendingFirstTurn, pendingAttachments: nextPendingAttachments.map(item => ({ localId: item.localId, filename: item.filename, status: 'needs_reselect', error: tr('Select the file again after refreshing', '刷新后需要重新选择文件') })) });
      return;
    }
    const nextAttachments = (currentView?.attachments || []).filter(item => item.file_token !== attachmentId);
    dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { attachments: nextAttachments } });
    const pendingFirstTurn = readPendingFirstTurn(userId, sessionId);
    if (pendingFirstTurn) persistPendingFirstTurn(sessionId, { ...pendingFirstTurn, attachments: nextAttachments });
  }
  async function changeModel(nextModel: string) {
    const draftProjectId = conversationRef.current.activeDraftKey;
    const currentDraft = draftProjectId ? conversationRef.current.draftsByScope[draftProjectId] : null;
    if (currentDraft) {
      if (!['editing', 'failed'].includes(currentDraft.phase) || nextModel === currentDraft.model) return;
      dispatchConversation({ type: 'PATCH_DRAFT', projectId: currentDraft.projectId, draftId: currentDraft.draftId, patch: { model: nextModel, modelSelectedByUser: true, phase: 'editing', error: undefined } });
      return;
    }
    if (!activeSession || busy || nextModel === model) return;
    const sessionId = activeSession.session_id;
    if (selectRunGate(conversationRef.current.viewsBySessionId[sessionId]).blocksSessionMutation) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: tr('You cannot switch models until the active run finishes', '活动 Run 完成前不能切换模型') } });
      return;
    }
    const previousModel = model;
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: undefined } });
    setBusy(true);
    try {
      const response = await updateSessionModel(sessionId, nextModel, conversationRef.current.viewsBySessionId[sessionId]?.modelRevision ?? 0);
      if (accountIsStale(identity, accountEpoch)) return;
      const selectedModel = response.model || nextModel;
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { model: selectedModel, modelRevision: response.model_revision ?? 0, modelUnavailableReason: null } });
      setAvailableModels(normalizedModels(response.available_models || availableModels, selectedModel));
      setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, model: selectedModel } : session));
      setModelNotice({ sessionId, name: modelCatalog?.models.find(item => item.config_id === selectedModel)?.display_name || selectedModel });
    } catch (cause: any) {
      if (accountIsStale(identity, accountEpoch)) return;
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { model: previousModel, error: cause.message || tr('Could not switch models', '模型切换失败') } });
      try {
        const selection = await getSessionModel(sessionId);
        if (!accountIsStale(identity, accountEpoch)) dispatchConversation({ type: 'PATCH_VIEW', sessionId,
          patch: { model: selection.model, modelRevision: selection.model_revision, modelUnavailableReason: selection.model_unavailable_reason } });
      } catch { /* Preserve the visible error; the next hydration retries. */ }
    } finally {
      if (!accountIsStale(identity, accountEpoch)) setBusy(false);
    }
  }
  async function addCustomModel(input: { model_id: string; display_name: string; base_url: string; api_key: string }) {
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    const response = await saveCustomModel(input);
    if (accountIsStale(identity, accountEpoch)) return;
    const saved = response.model;
    setCustomModels(current => [...current.filter(item => item.model_id !== saved.model_id), saved]);
    setAvailableModels(current => normalizedModels([...current, saved.model_id], saved.model_id));
    await changeModel(saved.model_id);
    void refreshModelCatalog();
  }
  async function removeCustomModel(modelId: string) {
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    await deleteCustomModel(modelId);
    if (accountIsStale(identity, accountEpoch)) return;
    setCustomModels(current => current.filter(item => item.model_id !== modelId));
    setAvailableModels(current => current.filter(item => item !== modelId));
  }
  function openManagement(tab: 'memory' | 'skills') { setHelpOpen(false); setManagementTab(tab); setManagementOpen(true); }
  function unavailableModel(id: string) {
    if (!modelCatalog) return null;
    const config = modelCatalog.models.find(item => item.config_id === id);
    return config ? config.unavailable_reason : 'MODEL_REMOVED';
  }
  function applyModelCatalog(catalog: ModelCatalog) {
    setModelCatalog(catalog);
    setDefaultModel(catalog.default_model);
    setCustomModels(catalog.models);
    setAvailableModels(catalog.models.filter(item => !item.unavailable_reason).map(item => item.config_id));
    // Refresh availability without mutating graph, messages or per-session selection.
    for (const view of Object.values(conversationRef.current.viewsBySessionId)) {
      const config = catalog.models.find(item => item.config_id === view.model);
      dispatchConversation({ type: 'PATCH_VIEW', sessionId: view.sessionId, patch: { modelUnavailableReason: config ? config.unavailable_reason : 'MODEL_REMOVED' } });
    }
  }
  async function refreshModelCatalog() {
    const identity = identityRef.current;
    const epoch = accountEpochRef.current;
    try { const catalog = await listModels(); if (!accountIsStale(identity, epoch)) applyModelCatalog(catalog); }
    catch { /* Keep the last known model list without blocking conversation history. */ }
  }
  async function openDatasets(targetProjectId?: string) {
    const key = conversationRef.current.activeDraftKey;
    const draft = key ? conversationRef.current.draftsByScope[key] : null;
    const projectId = targetProjectId ?? (draft ? draft.projectId : activeSession?.project_id || null);
    const sessionId = targetProjectId === undefined ? activeSession?.session_id : undefined;
    if (sessionId && selectRunGate(conversationRef.current.viewsBySessionId[sessionId]).blocksSessionMutation) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: t('You cannot switch datasets until the active run finishes', '活动 Run 完成前不能切换数据集') } });
      return;
    }
    setDatasetProjectId(targetProjectId);
    setDatasetOpen(true);
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    try {
      const [response, serverResponse] = await Promise.all([
        listProjectDatasets(projectId),
        listServerDatasets(),
      ]);
      if (accountIsStale(identity, accountEpoch)) return;
      updateProjectDatasets(projectId, response.datasets || []);
      setServerDatasets(serverResponse.datasets || []);
    } catch (cause: any) {
      if (accountIsStale(identity, accountEpoch)) return;
      setWorkspaceError(cause.message || tr('Could not load datasets', '数据集加载失败'));
    }
  }
  async function chooseDataset(dataset: Dataset) {
    if (dataset.project_id && (dataset.project_id !== visibleProjectId || (!activeDraft && !activeSession))) {
      const draft = newTask(dataset.project_id);
      if (draft) dispatchConversation({ type: 'PATCH_DRAFT', projectId: draft.projectId, draftId: draft.draftId, patch: { activeDatasetId: dataset.dataset_id } });
      return;
    }
    if (dataset.status === 'source_ready') { openConstruction(dataset); return; }
    const draftProjectId = conversationRef.current.activeDraftKey;
    const currentDraft = draftProjectId ? conversationRef.current.draftsByScope[draftProjectId] : null;
    if (currentDraft) {
      dispatchConversation({ type: 'PATCH_DRAFT', projectId: currentDraft.projectId, draftId: currentDraft.draftId, patch: { activeDatasetId: dataset.dataset_id, phase: 'editing', error: undefined } });
      setDatasetOpen(false);
      return;
    }
    if (!activeSession || dataset.dataset_id === activeView?.activeDatasetId) return setDatasetOpen(false);
    const sessionId = activeSession.session_id;
    if (selectRunGate(conversationRef.current.viewsBySessionId[sessionId]).blocksSessionMutation) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: tr('You cannot switch datasets until the active run finishes', '活动 Run 完成前不能切换数据集') } });
      setDatasetOpen(false);
      return;
    }
    const targetView = conversationRef.current.viewsBySessionId[sessionId];
    const previousGraph = targetView?.graph || { status: 'empty' as const, data: null };
    const previousSelected = targetView?.selectedNode || null;
    const graphVersion = nextGraphVersion(sessionId);
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    setBusy(true); dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: undefined } });
    try {
      const response = await setSessionDataset(sessionId, dataset.dataset_id);
      if (accountIsStale(identity, accountEpoch)) return;
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { activeDatasetId: dataset.dataset_id } });
      const unsupported = unsupportedGraphView(response.dataset_view);
      let nextGraph: GraphData | null = null;
      if (unsupported) dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: graphVersion, graph: unsupported });
      else nextGraph = await refreshSessionGraph(sessionId, {}, true, graphVersion);
      if (accountIsStale(identity, accountEpoch)) return;
      setDatasetOpen(false);
      setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, active_dataset_id: dataset.dataset_id, has_graph: Boolean(nextGraph || unsupported) } : session));
    } catch (cause: any) {
      if (accountIsStale(identity, accountEpoch)) return;
      dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: graphVersion, graph: previousGraph });
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { selectedNode: previousSelected, error: cause.message || tr('Could not switch datasets', '数据集切换失败') } });
    }
    finally {
      if (!accountIsStale(identity, accountEpoch)) setBusy(false);
    }
  }
  async function addProjectDataset(file: File, activateCurrentSession: boolean, targetProjectId?: string) {
    const draftProjectId = conversationRef.current.activeDraftKey;
    const draft = draftProjectId ? conversationRef.current.draftsByScope[draftProjectId] : null;
    const projectId = targetProjectId ?? (draft?.projectId || activeSession?.project_id || null);
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    const stale = () => accountIsStale(identity, accountEpoch) || Boolean(projectId && deletedProjectsRef.current.has(`${identity}:${projectId}`));
    const draftId = draft?.draftId;
    const sessionId = activateCurrentSession ? activeSession?.session_id : undefined;
    if (sessionId && selectRunGate(conversationRef.current.viewsBySessionId[sessionId]).blocksSessionMutation) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: tr('You cannot activate another dataset until the active run finishes', '活动 Run 完成前不能激活新数据集') } });
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
      if (stale()) return;
      updateProjectDatasets(projectId, current => [...current.filter(item => item.dataset_id !== response.dataset.dataset_id), response.dataset]
        .map(item => ({ ...item, is_default: item.dataset_id === response.default_dataset_id })));
      setProjects(current => current.map(project => project.project_id === projectId ? { ...project, default_dataset_id: response.default_dataset_id } : project));
      const currentDraft = conversationRef.current.draftsByScope[draftScopeKey(projectId)];
      if (response.dataset.status === 'source_ready') openConstruction(response.dataset);
      if (response.dataset.status === 'ready' && draftProjectId === draftScopeKey(projectId) && currentDraft && currentDraft.draftId === draftId) {
        dispatchConversation({ type: 'PATCH_DRAFT', projectId, draftId: currentDraft.draftId, patch: { activeDatasetId: response.dataset.dataset_id, error: undefined } });
      }
      if (response.activated_session_id && sessionId && graphVersion !== undefined) {
        dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { activeDatasetId: response.dataset.dataset_id } });
        const unsupported = unsupportedGraphView(response.dataset_view);
        let nextGraph: GraphData | null = null;
        if (unsupported) dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: graphVersion, graph: unsupported });
        else nextGraph = await refreshSessionGraph(sessionId, {}, true, graphVersion);
        if (stale()) return;
        setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, active_dataset_id: response.dataset.dataset_id, has_graph: Boolean(nextGraph || unsupported) } : session));
      }
    } catch (cause: any) {
      if (stale()) return;
      if (sessionId && graphVersion !== undefined) {
        dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: graphVersion, graph: previousGraph });
        dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { selectedNode: previousSelected, error: cause.message || tr('Could not upload the project dataset', '项目数据集上传失败') } });
      }
      else setWorkspaceError(cause.message || tr('Could not upload the project dataset', '项目数据集上传失败'));
    } finally {
      if (accountIsStale(identity, accountEpoch)) return;
      if (sessionId) dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { uploading: false } });
      else setBusy(false);
    }
  }
  async function addServerDataset(source: ServerDataset, activateCurrentSession: boolean, targetProjectId?: string) {
    const draftKey = conversationRef.current.activeDraftKey;
    const draft = draftKey ? conversationRef.current.draftsByScope[draftKey] : null;
    const projectId = targetProjectId ?? draft?.projectId ?? activeSession?.project_id ?? null;
    if (!projectId) {
      setWorkspaceError(tr('Choose a project before adding a server dataset', '请先选择项目，再添加服务器数据源'));
      return;
    }
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    const stale = () => accountIsStale(identity, accountEpoch) || deletedProjectsRef.current.has(`${identity}:${projectId}`);
    const sessionId = activateCurrentSession ? activeSession?.session_id : undefined;
    if (sessionId && selectRunGate(conversationRef.current.viewsBySessionId[sessionId]).blocksSessionMutation) {
      dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { error: tr('You cannot activate a server dataset until the active run finishes', '活动 Run 完成前不能激活服务器数据源') } });
      return;
    }
    const targetView = sessionId ? conversationRef.current.viewsBySessionId[sessionId] : null;
    const previousGraph = targetView?.graph || { status: 'empty' as const, data: null };
    const previousSelected = targetView?.selectedNode || null;
    const graphVersion = sessionId ? nextGraphVersion(sessionId) : undefined;
    if (sessionId) dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { uploading: true, error: undefined } });
    else { setBusy(true); setWorkspaceError(''); }
    try {
      const response = await importServerDataset(projectId, source.server_dataset_id, sessionId);
      if (stale()) return;
      updateProjectDatasets(projectId, current => [
        ...current.filter(item => item.dataset_id !== response.dataset.dataset_id),
        response.dataset,
      ].map(item => ({ ...item, is_default: item.dataset_id === response.default_dataset_id })));
      setProjects(current => current.map(project => project.project_id === projectId
        ? { ...project, default_dataset_id: response.default_dataset_id }
        : project));
      const currentDraft = conversationRef.current.draftsByScope[draftScopeKey(projectId)];
      if (currentDraft) {
        dispatchConversation({ type: 'PATCH_DRAFT', projectId, draftId: currentDraft.draftId, patch: { activeDatasetId: response.dataset.dataset_id, error: undefined } });
      }
      if (response.activated_session_id && sessionId && graphVersion !== undefined) {
        dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { activeDatasetId: response.dataset.dataset_id } });
        const unsupported = unsupportedGraphView(response.dataset_view);
        let nextGraph: GraphData | null = null;
        if (unsupported) dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: graphVersion, graph: unsupported });
        else nextGraph = await refreshSessionGraph(sessionId, {}, true, graphVersion);
        if (stale()) return;
        setSessions(current => current.map(session => session.session_id === sessionId
          ? { ...session, active_dataset_id: response.dataset.dataset_id, has_graph: Boolean(nextGraph || unsupported) }
          : session));
      }
      setDatasetOpen(false);
    } catch (cause: any) {
      if (stale()) return;
      if (sessionId && graphVersion !== undefined) {
        dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId, requestVersion: graphVersion, graph: previousGraph });
        dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { selectedNode: previousSelected, error: cause.message || tr('Could not activate the server dataset', '服务器数据源加载失败') } });
      } else setWorkspaceError(cause.message || tr('Could not add the server dataset', '服务器数据源添加失败'));
    } finally {
      if (accountIsStale(identity, accountEpoch)) return;
      if (sessionId) dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { uploading: false } });
      else setBusy(false);
    }
  }
  async function addConstructionDataset(file: File | File[], schema?: CsvBundleSchema, name?: string) {
    const identity = identityRef.current;
    setBusy(true);
    setWorkspaceError('');
    try {
      const response = await uploadProjectDataset(null, file, undefined, schema, name);
      if (identityRef.current !== identity) return;
      updateProjectDatasets(null, current => [
        ...current.filter(item => item.dataset_id !== response.dataset.dataset_id),
        response.dataset,
      ]);
      if (isPtGraph(response.dataset)) setConstructionDataset(null);
      else if (supportsConstruction(response.dataset)) openConstruction(response.dataset);
      else setWorkspaceError(tr('The file was saved, but this workspace supports SQL, CSV, TXT, or PT files.', '此文件已保存，但此工作台仅支持 SQL、CSV、TXT 或 PT 文件。'));
    } catch (cause: any) {
      if (identityRef.current === identity) setWorkspaceError(cause.message || tr('Could not upload the graph-construction source', '构图数据源上传失败'));
    } finally {
      if (identityRef.current === identity) setBusy(false);
    }
  }
  function datasetUpdated(dataset: Dataset) {
    updateProjectDatasets(dataset.project_id, current => current.map(item => item.dataset_id === dataset.dataset_id ? dataset : item));
    setConstructionDataset(current => current?.dataset_id === dataset.dataset_id ? dataset : current);
  }
  async function loadConstructedIntoProject(source: Dataset) {
    if (!importProject) return;
    const projectId = importProject.project_id;
    const identity = identityRef.current;
    const epoch = accountEpochRef.current;
    const result = await importConstructedDataset(projectId, source.dataset_id);
    if (accountIsStale(identity, epoch) || deletedProjectsRef.current.has(`${identity}:${projectId}`)) return;
    updateProjectDatasets(projectId, current => [...current, result.dataset]);
    setProjects(current => current.map(project => project.project_id === projectId ? { ...project, default_dataset_id: result.default_dataset_id, dataset_count: (project.dataset_count || 0) + 1 } : project));
    const draft = conversationRef.current.draftsByScope[draftScopeKey(projectId)];
    if (draft) dispatchConversation({ type: 'PATCH_DRAFT', projectId, draftId: draft.draftId, patch: { activeDatasetId: result.dataset.dataset_id } });
    setDatasetProjectId(projectId);
    setDatasetOpen(true);
  }
  function openConstruction(dataset: Dataset) {
    setDatasetOpen(false);
    setConstructionDataset(dataset);
  }
  function analyzeSourceGraph(dataset: Dataset) {
    if (busy || dataset.status !== 'ready' || !isPrebuiltGraph(dataset)) return;
    const draft = newTask(dataset.project_id);
    if (draft) dispatchConversation({ type: 'PATCH_DRAFT', projectId: draft.projectId, draftId: draft.draftId,
      patch: { activeDatasetId: dataset.dataset_id, phase: 'editing', error: undefined } });
  }
  function constructionStateUpdated(dataset: Dataset, state: ConstructionState) {
    const merge = (item: Dataset): Dataset => {
      const previous = item.metadata?.construction as Record<string, unknown> | undefined;
      const construction = { ...previous,
        status: state.status, revision: state.revision, node_count: state.node_count, edge_count: state.edge_count,
        source_type: state.source_type, source_filename: state.source_filename,
        algorithm_available: state.algorithm_available, message: state.message, directed: state.directed,
        build_id: state.build_id, failure_code: state.failure_code, updated_at: state.updated_at,
        last_build_status: state.last_build_status,
      };
      const metadata: Record<string, unknown> = { ...item.metadata, construction };
      if (state.status === 'ready') {
        if (state.node_count !== undefined) metadata.num_nodes = state.node_count;
        if (state.edge_count !== undefined) metadata.num_edges = state.edge_count;
        if (state.directed !== undefined) metadata.directed = state.directed;
      }
      return { ...item, status: state.status === 'ready' ? 'ready' : 'source_ready', metadata };
    };
    updateProjectDatasets(dataset.project_id, current => current.map(item => item.dataset_id === dataset.dataset_id ? merge(item) : item));
    setConstructionDataset(current => current?.dataset_id === dataset.dataset_id ? merge(current) : current);
  }
  function constructionSaved(dataset: Dataset, nextGraph: GraphData, revision: number) {
    if (accountIsStale(userId, renderAccountEpoch)) return;
    constructionStateUpdated(dataset, { dataset_id: dataset.dataset_id, status: 'ready', revision,
      node_count: nextGraph.nodes.length, edge_count: nextGraph.edges.length, directed: nextGraph.directed });
  }
  async function useConstructedDataset(dataset: Dataset) {
    if (dataset.project_id && (dataset.project_id !== visibleProjectId || (!activeDraft && !activeSession))) {
      await chooseDataset(dataset);
      return;
    }
    const current = conversationRef.current;
    const currentDraft = current.activeDraftKey ? current.draftsByScope[current.activeDraftKey] : null;
    if (currentDraft) {
      dispatchConversation({ type: 'PATCH_DRAFT', projectId: currentDraft.projectId, draftId: currentDraft.draftId, patch: { activeDatasetId: dataset.dataset_id, phase: 'editing', error: undefined } });
      return;
    }
    const sessionId = current.activeSessionId;
    if (!sessionId) return;
    if (selectRunGate(current.viewsBySessionId[sessionId]).blocksSessionMutation) throw new Error(tr('You can switch datasets after the current task finishes. The constructed graph has been saved.', '当前任务完成后才能切换数据集；构图结果已经保存。'));
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    await setSessionDataset(sessionId, dataset.dataset_id);
    if (accountIsStale(identity, accountEpoch)) return;
    dispatchConversation({ type: 'PATCH_VIEW', sessionId, patch: { activeDatasetId: dataset.dataset_id } });
    const loaded = await refreshSessionGraph(sessionId, {}, true);
    if (accountIsStale(identity, accountEpoch)) return;
    if (!loaded) throw new Error(tr('The dataset was selected, but its graph preview could not be loaded. Try again shortly.', '数据集已选中，但图预览加载失败，请稍后重试。'));
    setSessions(previous => previous.map(session => session.session_id === sessionId ? { ...session, active_dataset_id: dataset.dataset_id, has_graph: true } : session));
    setGraphOpen(true); setGraphTab('Graph');
  }
  async function renameDataset(dataset: Dataset) {
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    const name = await askPrompt(tr('Dataset name', '数据集名称'), dataset.name);
    if (!name || name === dataset.name || accountIsStale(identity, accountEpoch)) return;
    try {
      const response = await renameProjectDataset(dataset.project_id, dataset.dataset_id, name);
      if (accountIsStale(identity, accountEpoch)) return;
      updateProjectDatasets(dataset.project_id, current => current.map(item => item.dataset_id === dataset.dataset_id ? { ...item, ...response.dataset } : item));
    } catch (cause: any) {
      if (accountIsStale(identity, accountEpoch)) return;
      setWorkspaceError(cause.message || tr('Could not rename the dataset', '数据集重命名失败'));
    }
  }
  async function removeDataset(dataset: Dataset) {
    if (busy) return;
    const identity = identityRef.current;
    const accountEpoch = accountEpochRef.current;
    if (!await askConfirm(tr(`Delete dataset “${dataset.name}”?`, `确认删除数据集「${dataset.name}」吗？`))) return;
    if (accountIsStale(identity, accountEpoch)) return;
    setBusy(true);
    setWorkspaceError('');
    try {
      const response = await deleteProjectDataset(dataset.project_id, dataset.dataset_id);
      if (accountIsStale(identity, accountEpoch)) return;
      updateProjectDatasets(dataset.project_id, current => current.filter(item => item.dataset_id !== dataset.dataset_id));
      setProjects(current => current.map(project => project.project_id === dataset.project_id ? { ...project, default_dataset_id: response.default_dataset_id } : project));
      setSessions(current => current.map(session => session.active_dataset_id === dataset.dataset_id ? { ...session, active_dataset_id: null, has_graph: false } : session));
      for (const session of sessions.filter(item => item.active_dataset_id === dataset.dataset_id)) {
        dispatchConversation({ type: 'PATCH_VIEW', sessionId: session.session_id, patch: { activeDatasetId: null } });
        const graphVersion = nextGraphVersion(session.session_id);
        dispatchConversation({ type: 'GRAPH_REQUEST_SUCCESS', sessionId: session.session_id, requestVersion: graphVersion, graph: { status: 'empty', data: null } });
      }
    } catch (cause: any) {
      if (accountIsStale(identity, accountEpoch)) return;
      setWorkspaceError(cause.message || tr('Could not delete the dataset', '数据集删除失败'));
    } finally {
      if (!accountIsStale(identity, accountEpoch)) setBusy(false);
    }
  }
  async function expandNode(node: Record<string, unknown>) {
    if (!activeSession) return;
    const sessionId = activeSession.session_id;
    const name = String(node.id ?? node.label ?? '').trim();
    if (!name) return;
    await refreshSessionGraph(sessionId, { node_names: [name], max_nodes: 800 }, true, undefined, true);
  }
  if (!ready) return null;
  const activeAttachments = activeDraft
    ? activeDraft.attachments
    : [...(activeView?.attachments || []), ...(activeView?.pendingAttachments || [])];
  const layoutStyle = { '--sidebar-width': `${sidebarWidth}px`, '--graph-width': `${graphWidth}px`, '--collapsed-panel-width': `${COLLAPSED_PANEL_WIDTH}px` } as CSSProperties;
  const panelProjectId = datasetProjectId ?? visibleProjectId;
  const panelProject = projects.find(project => project.project_id === panelProjectId);
  const panelDatasets = datasetsByProject[draftScopeKey(panelProjectId)] || [];
  const panelMatchesConversation = panelProjectId === visibleProjectId;
  const activeDatasetId = activeDraft?.activeDatasetId ?? activeView?.activeDatasetId;
  const activeDataset = datasets.find(dataset => dataset.dataset_id === activeDatasetId);
  const runStatuses = Object.fromEntries(Object.entries(conversation.viewsBySessionId)
    .filter(([, view]) => Boolean(view.activeRun))
    .map(([sessionId, view]) => [sessionId, view.activeRun!.status]));
  const unreadCompletions = Object.fromEntries(Object.entries(conversation.viewsBySessionId)
    .filter(([, view]) => view.unreadCompletion)
    .map(([sessionId]) => [sessionId, true]));
  return <UserCaseContext.Provider value={userCases}><div className="app-shell">
    {actionDialog}
    <TopBar onMenu={() => setMobileSidebarOpen(true)} onHome={() => showWorkspaceHome()} onModels={userId && !demoMode && cancelEnabled ? () => setModelsOpen(true) : undefined} />
    <PopupPresence key={`modelsOpen-${userId}`}>{modelsOpen && <ModelSettings key={userId} onClose={() => setModelsOpen(false)} onChanged={applyModelCatalog} />}</PopupPresence>
    <div ref={bodyRef} className={`app-body ${sidebarCollapsed ? 'sidebar-collapsed' : ''} ${activeSession && !createGraphOpen ? graphOpen ? 'has-graph-workspace' : 'has-collapsed-graph-workspace' : 'graph-workspace-closed'}`} style={layoutStyle}>
      <Sidebar hasMoreSessions={Boolean(sessionCursor)} loadingSessions={loadingSessions} sessionListError={sessionListError} onLoadMoreSessions={() => void loadMoreSessions()} projects={projects.map(project => ({ ...project, name: localizeExampleName(project.name, userCases, t) || project.name }))} sessions={sessions} runStatuses={runStatuses} unreadCompletions={unreadCompletions} activeId={createGraphOpen ? undefined : activeSession?.session_id} activeProjectId={createGraphOpen ? undefined : activeDraft?.projectId || undefined} userId={userId} dark={dark} onTheme={() => setDark(current => !current)} onMemory={() => openManagement('memory')} onSkills={() => openManagement('skills')} onCreateGraph={() => void showCreateGraphWorkspace()} onHelp={() => showHelp()} helpActive={helpOpen} createGraphActive={createGraphOpen} mobileOpen={mobileSidebarOpen} onCloseMobile={() => setMobileSidebarOpen(false)} onNew={() => newTask()} onNewProject={() => { setProjectEditorTarget(null); setProjectEditor('create'); }} onNewInProject={id => newTask(id)} onProjectDatasets={id => void openDatasets(id)} onEditProject={project => { setProjectEditorTarget(projects.find(item => item.project_id === project.project_id) || project); setProjectEditor('edit'); }} onDeleteProject={project => setDeleteProjectTarget(projects.find(item => item.project_id === project.project_id) || project)} onSelect={id => selectSession(id)} onDelete={deleteTask} collapsed={sidebarCollapsed} onCollapse={() => { if (window.innerWidth <= 720) setMobileSidebarOpen(false); else setSidebarCollapsed(current => !current); }} />
      {!sidebarCollapsed && <ColumnDivider side="sidebar" value={sidebarWidth} minimum={SIDEBAR_MIN} maximum={SIDEBAR_MAX} onPointerDown={beginResize} onNudge={nudgeResize} onReset={resetResize} />}
      <div className="main-column">
        {createGraphOpen ? <>
          {workspaceError && <div className="workspace-error" role="alert"><Icon name="close" size={14} /> {workspaceError}</div>}
          {constructionDataset
            ? <GraphConstructionPanel key={constructionDataset.dataset_id} dataset={constructionDataset} embedded onClose={() => setConstructionDataset(null)} onSaved={constructionSaved} onStateChange={constructionStateUpdated} onDatasetUpdated={datasetUpdated} />
            : <CreateGraphWorkspace datasets={datasetsByProject[draftScopeKey(null)] || []} busy={busy} onUpload={addConstructionDataset} onConstruct={openConstruction} onAnalyze={analyzeSourceGraph} onDelete={removeDataset} onStateChange={constructionStateUpdated} />}
        </> : <>
          {error && <div className="workspace-error" role="alert"><Icon name="close" size={14} /> {error}</div>}
          {modelNotice?.sessionId === activeSession?.session_id && modelNotice && <div className="model-switch-notice" role="status">{t(`Next message: ${modelNotice.name}`, `下一条消息将使用：${modelNotice.name}`)}<button className="plain-icon" onClick={() => setModelNotice(null)} aria-label={t('Dismiss model notice', '关闭模型切换提示')}><Icon name="close" size={12} /></button></div>}
          {activeDraft
            ? <ChatPaneWithState contextModel={modelCatalog?.models.find(item => item.config_id === model)} conversationKey={`draft:${activeDraft.projectId}`} emptyProjectName={localizeExampleName(draftProject?.name, userCases, t)} emptyContent={!activeDraft.projectId && !activeDataset && userCases.length ? <UserCaseGallery cases={userCases} busy={busy} onSelect={(id, question) => void selectUserCase(id, question)} /> : undefined} canCancel={!["editing", "failed"].includes(activeDraft.phase)} onCancel={terminateCurrentRun} messages={[]} value={composerValue} model={model} models={availableModels} customModels={customModels} onManageModels={modelCatalog ? () => setModelsOpen(true) : undefined} onSaveCustomModel={addCustomModel} onDeleteCustomModel={removeCustomModel} exampleCase={userCaseForDataset(activeDataset, userCases)} activeDatasetName={localizeExampleName(activeDataset?.name, userCases, t)} onRemoteGraph={openRemoteConnections} onChange={(value: string) => dispatchConversation({ type: 'PATCH_DRAFT', projectId: activeDraft.projectId, draftId: activeDraft.draftId, patch: { text: value, phase: 'editing', error: undefined } })} onModelChange={changeModel} onDataset={() => void openDatasets()} onSend={() => void send()} onUpload={upload} onRetryAttachment={retryAttachment} onRemoveAttachment={removeAttachment} attachments={activeAttachments} loading={loading} uploading={uploading} disabled={busy || !['editing', 'failed'].includes(activeDraft.phase)} sendBlocked={!['editing', 'failed'].includes(activeDraft.phase)} runtimeControlsDisabled={!['editing', 'failed'].includes(activeDraft.phase)} sendBlockedReason={sendBlockedReason} runGateReason={null} interaction={{ kind: 'composer' }} hasGraph={false} onOpenGraph={() => undefined} runTools={[]} plan={null} focusRequest={composerFocusRequest} />
            : activeSession
            ? <ChatPaneWithState contextSessionId={activeSession.session_id} contextModel={modelCatalog?.models.find(item => item.config_id === model)} remoteSource={activeView?.remoteGraph ? `${tr("Remote", "远程")}: ${activeView.remoteGraph.kind === 'falkordb' ? 'FalkorDB' : 'Neo4j'} · ${activeView.remoteGraph.name} · ${activeView.remoteGraph.database}` : undefined} artifactSessionId={activeSession.session_id} onEditLatest={editEnabled ? (message: Message, runId: string, content: string) => void send({ message: content, attachments: message.attachments || [], replaceRunId: runId }) : undefined} conversationKey={activeSession.session_id} messages={messages} value={composerValue} model={model} models={availableModels} customModels={customModels} onManageModels={modelCatalog ? () => setModelsOpen(true) : undefined} onSaveCustomModel={addCustomModel} onDeleteCustomModel={removeCustomModel} exampleCase={userCaseForDataset(activeDataset, userCases)} activeDatasetName={localizeExampleName(activeDataset?.name, userCases, t)} onRemoteGraph={openRemoteConnections} onChange={(value: string) => dispatchConversation({ type: 'PATCH_VIEW', sessionId: activeSession.session_id, patch: { draft: value } })} onModelChange={changeModel} onDataset={openDatasets} onSend={() => void send()} onUpload={upload} onRetryAttachment={retryAttachment} onRemoveAttachment={removeAttachment} attachments={activeAttachments} loading={loading} uploading={uploading} disabled={busy} sendBlocked={!runGate.canStartRun} runtimeControlsDisabled={runGate.blocksSessionMutation} sendBlockedReason={sendBlockedReason} canCancel={cancelEnabled && Boolean(activeView?.activeRun) && !runGate.canStartRun} cancellation={activeView?.activeRun?.cancellation} onCancel={terminateCurrentRun} runGateReason={runGate.reason} interaction={bottomInteraction} hasGraph={graphView.status === 'ready' || (graphView.status === 'unsupported' && graphView.code === 'PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE')} onOpenGraph={() => { setGraphOpen(true); setGraphTab('Graph'); }} onFeedback={respondToFeedback} runTools={runTools} plan={plan} focusRequest={composerFocusRequest} />
            : <div className="workspace-home"><div className="empty-state"><div className="empty-icon"><BrandLogo /></div><h1>{t('Start a new graph analysis', '开始新的图分析')}</h1><button className="primary-command workspace-home-action" onClick={() => newTask()} disabled={busy}><Icon name="plus" size={16} /> {t('New task', '新建任务')}</button></div><UserCaseGallery cases={userCases} busy={busy} onSelect={(id, question) => void selectUserCase(id, question)} /></div>}
        </>}
      </div>
      {activeSession && !createGraphOpen && <>{graphOpen && <ColumnDivider side="graph" value={graphWidth} minimum={GRAPH_MIN} maximum={GRAPH_MAX} onPointerDown={beginResize} onNudge={nudgeResize} onReset={resetResize} />}<GraphWorkspace datasetId={activeDataset?.dataset_id} molecularCase={userCaseForDataset(activeDataset, userCases)?.id === 'borylation'} onGraphQuery={prompt => void send(undefined, prompt)} graphQueryPending={!runGate.canStartRun || uploading || busy} onGraphReset={() => void refreshSessionGraph(activeSession.session_id, {}, true, undefined, true, 'reset')} onConstruct={() => activeDataset && (supportsConstruction(activeDataset) || isMolecularGraph(activeDataset)) ? openConstruction(activeDataset) : void openDatasets()} constructionLabel={activeDataset && (supportsConstruction(activeDataset) || isMolecularGraph(activeDataset)) ? constructionLabel(activeDataset) : t('Build graph', '构图')} onStopJob={cancelEnabled ? stopJob : undefined} data={graph} view={graphView} datasetName={activeDataset?.name} messages={messages} liveTools={demoMode ? demoMlTools : runTools} projectId={activeSession.project_id} sessionId={activeSession.session_id} tab={graphTab} selected={selectedNode} onTab={setGraphTab} onSelect={node => dispatchConversation({ type: 'PATCH_VIEW', sessionId: activeSession.session_id, patch: { selectedNode: node } })} onExpand={expandNode} collapsed={!graphOpen} onToggle={() => setGraphOpen(current => !current)} /></>}
    </div>
    {activeDraft && !activeDraft.projectId && recoveredDrafts.length > 0 && <div className="recovered-draft-notice" role="status"><button className="text-button" onClick={recoverOldDraft}>{t(`Restore pre-upgrade drafts (${recoveredDrafts.length})`, `恢复升级前草稿（${recoveredDrafts.length}）`)}</button></div>}
    <PopupPresence key={`remoteOpen-${userId}-${activeSessionId || activeDraft?.draftId || "none"}`}>{remoteOpen && <RemoteGraphConnections connections={remoteConnections} selectedId={activeView?.remoteGraph?.connection_id || null} unavailableReason={activeView?.remoteGraphUnavailableReason} loadError={remoteLoadError} busy={remoteLoading || busy || uploading || runGate.blocksSessionMutation} onSelect={chooseRemoteConnection} onChanged={refreshRemoteConnections} onClose={() => setRemoteOpen(false)} />}</PopupPresence>
    <PopupPresence key={`datasetOpen-${userId}`}>{datasetOpen && <DatasetPanel onRemoteGraph={openRemoteConnections} onDatasetUpdated={datasetUpdated} onLoadConstructed={panelProject ? () => { setDatasetOpen(false); setImportProject(panelProject); } : undefined} onConstruct={openConstruction} datasets={panelDatasets} serverDatasets={serverDatasets} projectName={localizeExampleName(panelProject?.name, userCases, t)} activeDatasetId={panelMatchesConversation ? activeDatasetId : null} busy={busy || uploading} onSelect={chooseDataset} onUpload={file => addProjectDataset(file, panelMatchesConversation && Boolean(activeSession), datasetProjectId)} onUseServer={panelProjectId ? dataset => addServerDataset(dataset, panelMatchesConversation && Boolean(activeSession), datasetProjectId) : undefined} onRename={renameDataset} onDelete={removeDataset} onClose={() => setDatasetOpen(false)} />}</PopupPresence>
    <PopupPresence key={`constructionDataset-${userId}`}>{constructionDataset && !createGraphOpen && <GraphConstructionPanel key={constructionDataset.dataset_id} dataset={constructionDataset} onClose={() => setConstructionDataset(null)} onSaved={constructionSaved} onStateChange={constructionStateUpdated} onDatasetUpdated={datasetUpdated} onUse={activeSession || activeDraft ? useConstructedDataset : undefined} />}</PopupPresence>
    <PopupPresence key={`importProject-${userId}`}>{importProject && <ConstructedDatasetPicker key={`${userId}:${importProject.project_id}`} projectId={importProject.project_id} projectName={localizeExampleName(importProject.name, userCases, t) || importProject.name} onImport={loadConstructedIntoProject} onClose={() => setImportProject(null)} />}</PopupPresence>
    <PopupPresence key={`managementOpen-${userId}`}>{managementOpen && <ManagementPanel initialTab={managementTab} onClose={() => setManagementOpen(false)} />}</PopupPresence>
    <PopupPresence key={`helpOpen-${userId}`}>{helpOpen && <HelpPage onClose={closeHelp} />}</PopupPresence>
    <PopupPresence key={`projectEditor-${userId}`}>{projectEditor && <ProjectDialog key={`${projectEditor}-${projectEditorTarget?.project_id || 'new'}`} project={projectEditor === 'edit' ? projectEditorTarget : null} onClose={() => { setProjectEditor(null); setProjectEditorTarget(null); }} onSave={saveProject} />}</PopupPresence>
    <PopupPresence key={`deleteProjectTarget-${userId}`}>{deleteProjectTarget && <DeleteProjectDialog project={deleteProjectTarget} onClose={() => setDeleteProjectTarget(null)} onConfirm={deleteCurrentProject} />}</PopupPresence>
  </div></UserCaseContext.Provider>;
}

function ChatPaneWithState({ hasGraph, ...props }: any) {
  const { t } = useI18n();
  const summary = props.cancellation
    ? props.cancellation === 'unconfirmed' ? t('Stop is not confirmed', '停止请求尚未确认') : props.cancellation === 'confirmed' ? t('Task ended', '任务已结束')
      : props.cancellation === 'saving' ? t('Stopped · saving results', '已停止，正在保存结果') : t('Stopping…', '正在停止…')
    : props.runGateReason === 'awaiting_feedback'
    ? t('Waiting for confirmation: approve or reject the current operation', '等待确认：请先批准或拒绝当前操作')
    : props.runGateReason === 'submitting_feedback'
      ? <><span className="loader" /> {t('Submitting confirmation and checking status', '正在提交确认并核对状态')}</>
      : props.loading
        ? <><span className="loader" /> {t('The assistant is analyzing the graph', '助手正在分析图')}</>
        : hasGraph
          ? <><span className="status-dot" /> {t('Graph analysis ready', '图分析就绪')}</>
          : null;
  const planProgress = props.plan?.steps?.length
    ? <span className="run-count">{t(`${props.plan.steps.filter((step: any) => step.status === 'done').length}/${props.plan.steps.length} steps`, `${props.plan.steps.filter((step: any) => step.status === 'done').length}/${props.plan.steps.length} 步`)}</span>
    : null;
  return <div className="chat-pane-shell">
    {(summary || planProgress) && <div className="run-summary">{summary}{planProgress}</div>}
    <div className="chat-pane-proxy">{props.remoteSource && <div className="remote-source-indicator">{props.remoteSource}</div>}<ChatPane {...props} /></div>
  </div>;
}

export default App;

createRoot(document.getElementById('root')!).render(<I18nProvider><App /></I18nProvider>);

import { useEffect, useRef, useState } from 'react';
import type { CSSProperties, Dispatch, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, SetStateAction } from 'react';
import { createRoot } from 'react-dom/client';
import { activateSession, createProject as addProject, createSession, deleteProject as removeProject, deleteProjectDataset, deleteSession as removeSession, getMessages, getUserId, listProjectConversations, listProjectDatasets, listProjects, listSessions, moveSession, renameProjectDataset, setDefaultProjectDataset, setSessionDataset, setUserId, streamChat, streamFeedback, updateProject as patchProject, updateSessionModel, uploadFile, uploadProjectDataset, validateUser, visualizeGraph } from './api';
import type { AttachmentRef, Dataset, FeedbackRequest, GraphData, Message, Project, Session, StreamEvent, ToolCall } from './types';
import { DatasetPanel } from './components/DatasetPanel';
import { ChatPane } from './components/ChatPane';
import { GraphWorkspace } from './components/GraphWorkspace';
import { Icon } from './components/Icon';
import { ManagementPanel } from './components/ManagementPanel';
import { DeleteProjectDialog, MoveConversationDialog, ProjectDialog } from './components/ProjectDialogs';
import { ProjectWorkspace } from './components/ProjectWorkspace';
import { Sidebar } from './components/Sidebar';
import { TopBar } from './components/TopBar';
import './styles.css';

type GraphTab = 'Overview' | 'Graph' | 'Data' | 'ML' | 'Artifacts';
type RunTool = ToolCall;
type ResizeSide = 'sidebar' | 'graph';
type SessionViewSnapshot = {
  messages: Message[];
  graph: GraphData | null;
  feedback: FeedbackRequest | null;
  datasets: Dataset[];
  model: string;
  availableModels: string[];
  activeDatasetId?: string | null;
};
const SIDEBAR_DEFAULT = 258;
const GRAPH_DEFAULT = 382;
const SIDEBAR_MIN = 190;
const SIDEBAR_MAX = 420;
const GRAPH_MIN = 280;
const GRAPH_MAX = 720;
const MAIN_MIN = 420;
const DIVIDER_WIDTH = 6;
const DEFAULT_MODEL = 'qwen3.6-plus';
const SESSION_VIEW_CACHE_LIMIT = 12;
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
  return input.map(item => ({ role: item?.role === 'user' ? 'user' : 'assistant', content: String(item?.content || ''), tools: item?.tools || [], plan: item?.plan || null, graphs: item?.graphs || [], reports: item?.reports || [] }));
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

function cacheSessionView(cache: Map<string, SessionViewSnapshot>, sessionId: string, snapshot: SessionViewSnapshot) {
  cache.delete(sessionId);
  cache.set(sessionId, snapshot);
  while (cache.size > SESSION_VIEW_CACHE_LIMIT) {
    const oldest = cache.keys().next().value;
    if (!oldest) break;
    cache.delete(oldest);
  }
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
  return updates.reduce((current, update) => update && typeof update === 'object' && !Array.isArray(update)
    ? appendTrainingProgress(current, update as Record<string, unknown>)
    : current, history);
}

function normalizeFeedbackRequest(event: StreamEvent): FeedbackRequest | null {
  const raw = event.request || event.payload?.request || event.payload || {};
  if (!raw || typeof raw !== 'object') return null;
  const request = raw as Record<string, any>;
  const proposedArgs = request.proposed_args ?? request.tool_args ?? request.arguments ?? {};
  return {
    request_id: String(request.request_id ?? request.requestId ?? event.run_id ?? ''),
    call_id: String(request.call_id ?? request.callId ?? request.tool_call_id ?? request.toolCallId ?? ''),
    tool_name: String(request.tool_name ?? request.toolName ?? request.proposed_tool ?? request.name ?? ''),
    proposed_args: proposedArgs && typeof proposedArgs === 'object' && !Array.isArray(proposedArgs) ? proposedArgs : {},
    description: String(request.description ?? request.message ?? request.user_message ?? '该工具调用需要确认'),
  };
}

function applyEvent(event: StreamEvent, setMessages: Dispatch<SetStateAction<Message[]>>, setGraph: Dispatch<SetStateAction<GraphData | null>>, setRunTools: Dispatch<SetStateAction<RunTool[]>>, setPlan: Dispatch<SetStateAction<any>>, setLoading: Dispatch<SetStateAction<boolean>>, setFeedback?: Dispatch<SetStateAction<FeedbackRequest | null>>) {
  if (event.type === 'token') setMessages(current => current.map((message, index) => index === current.length - 1 ? { ...message, content: message.content + String(event.content || ''), streaming: true } : message));
  if (event.type === 'tool_call') setRunTools(current => [...current, { tool: event.tool, call_id: event.call_id, args: event.args ?? event.arguments ?? {}, done: false }]);
  if (event.type === 'tool_result') setRunTools(current => {
    const result = decodedToolResult(event.result);
    const resultHistory = result && typeof result === 'object' && Array.isArray((result as any).history) ? (result as any).history : null;
    const callId = String(event.call_id || '');
    let targetIndex = callId ? current.findIndex(tool => String(tool.call_id || '') === callId) : -1;
    if (targetIndex < 0) targetIndex = current.map(tool => tool.tool).lastIndexOf(String(event.tool || ''));
    if (targetIndex < 0) return current;
    return current.map((tool, index) => index === targetIndex
      ? { ...tool, result, history: mergeTrainingHistory(tool.history, resultHistory), done: event.done !== false }
      : tool);
  });
  if (event.type === 'tool_progress') setRunTools(current => {
    const callId = String(event.call_id || '');
    const toolName = String(event.tool || '');
    let targetIndex = callId ? current.findIndex(tool => String((tool as any).call_id || '') === callId) : -1;
    if (targetIndex < 0) targetIndex = current.map(tool => tool.tool).lastIndexOf(toolName);
    if (targetIndex < 0) return current;
    const progress = event.progress && typeof event.progress === 'object' ? event.progress : {};
    const terminal = progress.kind === 'train_done' || progress.kind === 'train_early_stop';
    return current.map((tool, index) => index === targetIndex ? { ...tool, progress, history: appendTrainingProgress(tool.history, progress), done: Boolean(tool.done || terminal) } : tool);
  });
  const eventName = String(event.type || event.event || '').toLowerCase();
  if (event.type === 'await_user_feedback' || eventName.includes('runpaused') || eventName.includes('await_user_feedback') || eventName.includes('confirmation_required')) {
    setFeedback?.(normalizeFeedbackRequest(event));
    setLoading(false);
    setMessages(current => current.map(message => ({ ...message, streaming: false })));
  }
  if (event.type === 'plan_ready') setPlan({ completed: false, steps: event.steps || [] });
  if (event.type === 'plan_step') setPlan((current: any) => ({ ...(current || {}), steps: [...(current?.steps || []).filter((step: any) => step.step_id !== event.step?.step_id), event.step] }));
  if (event.type === 'graph_visualize' && event.data?.nodes) setGraph({ success: true, nodes: event.data.nodes, edges: (event.data.links || []).map((link: any) => ({ source: link.source, target: link.target, src: link.source, dst: link.target })), directed: event.data.directed, num_nodes: event.data.num_nodes, num_edges: event.data.num_edges, total_nodes: event.data.total_nodes, total_edges: event.data.total_edges, truncated: event.data.truncated, truncated_to: event.data.truncated_to });
  if (event.type === 'done') { setLoading(false); setFeedback?.(null); setMessages(current => current.map(message => ({ ...message, streaming: false }))); }
  if (event.type === 'error') { setLoading(false); setFeedback?.(null); setMessages(current => current.map((message, index) => index === current.length - 1 ? { ...message, content: event.error || '分析失败', streaming: false } : message)); }
}

function App() {
  const [userId, setUser] = useState(getUserId() || (demoMode ? 'demo' : ''));
  const [loginValue, setLoginValue] = useState(getUserId());
  const [loginError, setLoginError] = useState('');
  const [ready, setReady] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeProject, setActiveProject] = useState<Project | null>(null);
  const [activeSession, setActiveSession] = useState<Session | null>(demoMode ? { session_id: 'demo-session', title: '图谱分析演示', has_graph: true } : null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [graph, setGraph] = useState<GraphData | null>(demoMode ? demoGraph : null);
  const [selectedNode, setSelectedNode] = useState<Record<string, unknown> | null>(null);
  const [graphTab, setGraphTab] = useState<GraphTab>('Graph');
  const [graphOpen, setGraphOpen] = useState(true);
  const [composerValue, setComposerValue] = useState('');
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [datasetOpen, setDatasetOpen] = useState(false);
  const [managementOpen, setManagementOpen] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [managementTab, setManagementTab] = useState<'memory' | 'skills'>('memory');
  const [model, setModel] = useState(DEFAULT_MODEL);
  const [availableModels, setAvailableModels] = useState<string[]>([DEFAULT_MODEL]);
  const [projectEditor, setProjectEditor] = useState<'create' | 'edit' | null>(null);
  const [projectEditorTarget, setProjectEditorTarget] = useState<Project | null>(null);
  const [deleteProjectTarget, setDeleteProjectTarget] = useState<Project | null>(null);
  const [moveSessionTarget, setMoveSessionTarget] = useState<Session | null>(null);
  const [dark, setDark] = useState(() => localStorage.getItem('igraph-theme') === 'dark');
  const [runTools, setRunTools] = useState<RunTool[]>([]);
  const [plan, setPlan] = useState<any>(null);
  const [feedback, setFeedback] = useState<FeedbackRequest | null>(null);
  const [attachmentsBySession, setAttachmentsBySession] = useState<Record<string, AttachmentRef[]>>({});
  const [sidebarWidth, setSidebarWidth] = useState(() => storedWidth('igraph-layout-sidebar-width', window.innerWidth <= 1280 ? 230 : SIDEBAR_DEFAULT, SIDEBAR_MIN, SIDEBAR_MAX));
  const [graphWidth, setGraphWidth] = useState(() => storedWidth('igraph-layout-graph-width', window.innerWidth <= 1280 ? 330 : GRAPH_DEFAULT, GRAPH_MIN, GRAPH_MAX));
  const [graphLayoutVersion, setGraphLayoutVersion] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  const activeSessionRef = useRef<Session | null>(activeSession);
  const sessionViewRef = useRef<SessionViewSnapshot>({ messages, graph, feedback, datasets, model, availableModels });
  const sessionViewCacheRef = useRef<Map<string, SessionViewSnapshot>>(new Map());
  const sessionSwitchRef = useRef<{ sequence: number; controller: AbortController } | null>(null);
  const sessionSwitchForegroundRef = useRef(false);
  const sessionSwitchSequenceRef = useRef(0);
  const bodyRef = useRef<HTMLDivElement>(null);
  const resizeRef = useRef<{ side: ResizeSide; startX: number; startWidth: number } | null>(null);
  const layoutRef = useRef({ sidebarWidth, graphWidth, graphOpen });

  layoutRef.current = { sidebarWidth, graphWidth, graphOpen };
  activeSessionRef.current = activeSession;
  sessionViewRef.current = { messages, graph, feedback, datasets, model, availableModels, activeDatasetId: activeSession?.active_dataset_id };

  function widthLimit(side: ResizeSide) {
    const bodyWidth = bodyRef.current?.clientWidth || window.innerWidth;
    if (side === 'sidebar') {
      const occupied = layoutRef.current.graphOpen ? layoutRef.current.graphWidth + DIVIDER_WIDTH * 2 : DIVIDER_WIDTH;
      return Math.min(SIDEBAR_MAX, bodyWidth - MAIN_MIN - occupied);
    }
    return Math.min(GRAPH_MAX, bodyWidth - layoutRef.current.sidebarWidth - MAIN_MIN - DIVIDER_WIDTH * 2);
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
      if (!layoutRef.current.graphOpen) {
        setSidebarWidth(current => clamp(current, SIDEBAR_MIN, Math.min(SIDEBAR_MAX, bodyWidth - MAIN_MIN - DIVIDER_WIDTH)));
        return;
      }
      const nextSidebar = clamp(layoutRef.current.sidebarWidth, SIDEBAR_MIN, Math.min(SIDEBAR_MAX, bodyWidth - MAIN_MIN - GRAPH_MIN - DIVIDER_WIDTH * 2));
      const nextGraph = clamp(layoutRef.current.graphWidth, GRAPH_MIN, Math.min(GRAPH_MAX, bodyWidth - nextSidebar - MAIN_MIN - DIVIDER_WIDTH * 2));
      setSidebarWidth(nextSidebar);
      setGraphWidth(nextGraph);
    }
    fitColumnsToViewport();
    window.addEventListener('resize', fitColumnsToViewport);
    return () => window.removeEventListener('resize', fitColumnsToViewport);
  }, [graphOpen]);

  function updateViewUrl(view: 'project' | 'session', id: string) {
    const url = new URL(window.location.href);
    url.searchParams.delete('project');
    url.searchParams.delete('session');
    url.searchParams.set(view, id);
    window.history.pushState({}, '', url);
  }

  const selectSession = async (id: string, source = sessions, updateUrl = true) => {
    if ((busy && !sessionSwitchForegroundRef.current) || (id === activeSessionRef.current?.session_id && !activeProject)) return;

    const previousSession = activeSessionRef.current;
    if (previousSession) cacheSessionView(sessionViewCacheRef.current, previousSession.session_id, { ...sessionViewRef.current });

    sessionSwitchRef.current?.controller.abort();
    const controller = new AbortController();
    const sequence = ++sessionSwitchSequenceRef.current;
    sessionSwitchRef.current = { sequence, controller };
    sessionSwitchForegroundRef.current = true;
    const isCurrent = () => sessionSwitchRef.current?.sequence === sequence;
    const session = (source.find(item => item.session_id === id) || { session_id: id }) as Session;
    const cached = sessionViewCacheRef.current.get(id);

    setBusy(true); setError(''); setActiveProject(null); setSelectedNode(null); setRunTools([]); setPlan(null);
    setActiveSession({
      ...session,
      active_dataset_id: cached?.activeDatasetId ?? session.active_dataset_id,
      has_graph: cached?.graph ? true : session.has_graph,
      model: cached?.model || session.model,
    });
    setMessages(cached?.messages || []);
    setGraph(cached?.graph || null);
    setFeedback(cached?.feedback || null);
    setDatasets(cached?.datasets || (previousSession?.project_id === session.project_id ? sessionViewRef.current.datasets : []));
    if (cached?.model) setModel(cached.model);
    if (cached?.availableModels?.length) setAvailableModels(cached.availableModels);
    setGraphOpen(window.innerWidth > 900);
    if (updateUrl) updateViewUrl('session', id);

    let busyReleased = false;
    try {
      const response = await activateSession(id, controller.signal);
      if (!isCurrent()) return;
      const selectedModel = String(response.model || session.model || response.default_model || DEFAULT_MODEL);
      setModel(selectedModel);
      setAvailableModels(normalizedModels(response.available_models, selectedModel));
      const projectId = response.project_id || session.project_id;
      const restoredMessages = response.messages === undefined
        ? normalizeMessages((await getMessages(id, controller.signal)).messages)
        : normalizeMessages(response.messages);
      if (!isCurrent()) return;
      const pendingFeedback = response.pending_feedback || null;
      const activeDatasetId = Object.prototype.hasOwnProperty.call(response, 'active_dataset_id') ? response.active_dataset_id : session.active_dataset_id;
      const reusableCachedGraph = cached && cached.activeDatasetId === activeDatasetId ? cached.graph : null;
      setActiveSession({ ...session, project_id: projectId, active_dataset_id: activeDatasetId, has_graph: response.has_graph, model: selectedModel });
      setSessions(current => current.map(item => item.session_id === id ? { ...item, project_id: projectId, active_dataset_id: activeDatasetId, has_graph: response.has_graph, model: selectedModel } : item));
      setFeedback(pendingFeedback);
      setMessages(restoredMessages);
      if (cached?.graph && !reusableCachedGraph) setGraph(null);
      cacheSessionView(sessionViewCacheRef.current, id, {
        messages: restoredMessages,
        graph: reusableCachedGraph || null,
        feedback: pendingFeedback,
        datasets: cached?.datasets || [],
        model: selectedModel,
        availableModels: normalizedModels(response.available_models, selectedModel),
        activeDatasetId,
      });

      const datasetRequest = projectId ? listProjectDatasets(projectId, controller.signal) : null;
      const graphRequest = response.has_graph
        ? visualizeGraph(id, { include_community: false }, controller.signal).catch(() => null)
        : Promise.resolve<GraphData | null>(null);

      if (datasetRequest) {
        const datasetResponse = await datasetRequest;
        if (!isCurrent()) return;
        const nextDatasets = datasetResponse.datasets || [];
        setDatasets(nextDatasets);
        setProjects(current => current.map(project => project.project_id === projectId ? { ...project, default_dataset_id: datasetResponse.default_dataset_id } : project));
        const currentSnapshot = sessionViewCacheRef.current.get(id);
        if (currentSnapshot) cacheSessionView(sessionViewCacheRef.current, id, { ...currentSnapshot, datasets: nextDatasets });
      } else setDatasets([]);
      if (isCurrent()) {
        setBusy(false);
        sessionSwitchForegroundRef.current = false;
        busyReleased = true;
      }

      const nextGraph = await graphRequest;
      if (!isCurrent() || activeSessionRef.current?.session_id !== id) return;
      if (nextGraph) setGraph(nextGraph);
      else if (!response.has_graph) setGraph(null);
      const currentSnapshot = sessionViewCacheRef.current.get(id);
      if (currentSnapshot) cacheSessionView(sessionViewCacheRef.current, id, { ...currentSnapshot, graph: nextGraph || currentSnapshot.graph });
    } catch (cause: any) {
      if (!requestWasAborted(cause) && isCurrent()) setError(cause.message || '会话切换失败');
    } finally {
      if (isCurrent()) {
        sessionSwitchRef.current = null;
        if (!busyReleased) {
          sessionSwitchForegroundRef.current = false;
          setBusy(false);
        }
      }
    }
  };

  const selectProject = async (id: string, source = projects, updateUrl = true) => {
    if (busy) return;
    const project = source.find(item => item.project_id === id);
    if (!project) return setError('项目不存在或已被删除');
    setBusy(true); setError('');
    try {
      const [conversationResponse, datasetResponse] = await Promise.all([listProjectConversations(id), listProjectDatasets(id)]);
      const conversations = conversationResponse.conversations || [];
      const nextProject = { ...project, conversation_count: conversations.length, default_dataset_id: datasetResponse.default_dataset_id };
      setDatasets(datasetResponse.datasets || []);
      setSessions(current => mergeSessionLists(current, conversations));
      setProjects(current => current.map(item => item.project_id === id ? nextProject : item));
      setActiveProject(nextProject);
      setActiveSession(null);
      setMessages([]); setGraph(null); setFeedback(null); setSelectedNode(null); setComposerValue(''); setGraphOpen(false);
      if (updateUrl) updateViewUrl('project', id);
    } catch (cause: any) { setError(cause.message || '项目加载失败'); }
    finally { setBusy(false); }
  };

  async function refresh() {
    const [sessionResponse, projectResponse] = await Promise.all([listSessions(), listProjects()]);
    const nextSessions = sessionResponse.sessions || [];
    const nextProjects = projectResponse.projects || [];
    setSessions(nextSessions); setProjects(nextProjects);
    const query = new URLSearchParams(window.location.search);
    const projectId = query.get('project');
    const sessionId = query.get('session');
    if (projectId && nextProjects.some(project => project.project_id === projectId)) await selectProject(projectId, nextProjects, false);
    else if (sessionId) await selectSession(sessionId, nextSessions, false);
    else if (nextSessions[0]) await selectSession(nextSessions[0].session_id, nextSessions, false);
    else if (nextProjects[0]) await selectProject(nextProjects[0].project_id, nextProjects, false);
  }

  useEffect(() => {
    if (!userId || demoMode) return;
    validateUser(userId).then(response => {
      if (response.valid === false) throw new Error('用户 ID 无效');
      const selectedModel = response.default_model || DEFAULT_MODEL;
      setModel(selectedModel);
      setAvailableModels(normalizedModels(response.available_models, selectedModel));
      return refresh();
    }).catch((cause: any) => setError(cause.message || '无法加载会话'));
  }, [userId]);
  useEffect(() => { document.documentElement.dataset.theme = dark ? 'dark' : 'light'; localStorage.setItem('igraph-theme', dark ? 'dark' : 'light'); }, [dark]);
  useEffect(() => { setReady(true); }, []);

  async function login() {
    const value = loginValue.trim(); if (!value) return setLoginError('请输入用户 ID');
    try { const response = await validateUser(value); if (response.valid === false) throw new Error('用户 ID 无效'); const selectedModel = response.default_model || DEFAULT_MODEL; setModel(selectedModel); setAvailableModels(normalizedModels(response.available_models, selectedModel)); setUserId(value); setUser(value); setLoginError(''); } catch (cause: any) { setLoginError(cause.message || '登录失败'); }
  }
  async function newTask(targetProjectId?: string) {
    if (busy || loading) return; setBusy(true); setError('');
    const defaultProject = projects.find(project => project.is_default);
    const projectId = targetProjectId || activeProject?.project_id || activeSession?.project_id || defaultProject?.project_id;
    try {
      const response = await createSession(model, projectId);
      const selectedModel = response.model || model;
      const next: Session = { session_id: response.session_id, project_id: response.project_id || projectId, active_dataset_id: response.active_dataset_id, title: '新的图谱分析', model: selectedModel };
      setModel(selectedModel);
      setAvailableModels(current => normalizedModels(current, selectedModel));
      setSessions(current => [next, ...current.filter(item => item.session_id !== next.session_id)]);
      setProjects(current => current.map(project => project.project_id === next.project_id ? { ...project, conversation_count: (project.conversation_count || 0) + 1 } : project));
      setActiveProject(null); setActiveSession(next); setMessages([]); setGraph(null); setGraphOpen(window.innerWidth > 900); setSelectedNode(null); setComposerValue(''); updateViewUrl('session', next.session_id);
    } catch (cause: any) { setError(cause.message || '创建任务失败'); } finally { setBusy(false); }
  }
  async function deleteTask(id: string) {
    if (!window.confirm('确认删除这个分析任务吗？')) return;
    const deleted = sessions.find(item => item.session_id === id);
    try {
      await removeSession(id);
      const next = sessions.filter(item => item.session_id !== id);
      setSessions(next);
      if (deleted?.project_id) setProjects(current => current.map(project => project.project_id === deleted.project_id ? { ...project, conversation_count: Math.max(0, (project.conversation_count || 0) - 1) } : project));
      if (activeProject?.project_id === deleted?.project_id) setActiveProject(current => current ? { ...current, conversation_count: Math.max(0, (current.conversation_count || 0) - 1) } : current);
      if (activeSession?.session_id === id) { setActiveSession(null); setMessages([]); setGraph(null); if (next[0]) await selectSession(next[0].session_id, next); }
    } catch (cause: any) { setError(cause.message || '删除任务失败'); }
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
    setSessions(nextSessions); setProjects(nextProjects); setDeleteProjectTarget(null);
    const general = nextProjects.find(project => project.project_id === response.general_project_id);
    if (general) await selectProject(general.project_id, nextProjects);
  }
  async function moveCurrentSession(projectId: string) {
    if (!moveSessionTarget) return;
    const oldProjectId = moveSessionTarget.project_id;
    const response = await moveSession(moveSessionTarget.session_id, projectId);
    setSessions(current => current.map(session => session.session_id === moveSessionTarget.session_id ? { ...session, project_id: projectId, active_dataset_id: response.active_dataset_id, has_graph: false } : session));
    setActiveSession(current => current?.session_id === moveSessionTarget.session_id ? { ...current, project_id: projectId, active_dataset_id: response.active_dataset_id, has_graph: false } : current);
    if (activeSession?.session_id === moveSessionTarget.session_id) { setGraph(null); setDatasets([]); }
    setProjects(current => current.map(project => {
      if (project.project_id === oldProjectId) return { ...project, conversation_count: Math.max(0, (project.conversation_count || 0) - 1) };
      if (project.project_id === projectId) return { ...project, conversation_count: (project.conversation_count || 0) + 1 };
      return project;
    }));
    setMoveSessionTarget(null);
  }
  async function send() {
    const message = composerValue.trim(); if (!message || loading || !activeSession) return;
    const sessionId = activeSession.session_id;
    const attachments = attachmentsBySession[sessionId] || [];
    let streamFailed = false;
    setComposerValue(''); setLoading(true); setError(''); setFeedback(null); setRunTools([]); setPlan(null); setMessages(current => [...current, { role: 'user', content: message }, { role: 'assistant', content: '', streaming: true, tools: [] }]);
    const controller = new AbortController(); abortRef.current = controller;
    try {
      await streamChat(sessionId, message, attachments, { onEvent: event => { if (event.type === 'error') streamFailed = true; if (event.run_id) sessionStorage.setItem(`igraph-active-run:${sessionId}`, JSON.stringify({ runId: event.run_id, lastEventIndex: Number(event.event_index ?? -1) })); if (event.type === 'done' || event.type === 'error') sessionStorage.removeItem(`igraph-active-run:${sessionId}`); applyEvent(event, setMessages, setGraph, setRunTools, setPlan, setLoading, setFeedback); } }, controller.signal);
      if (!streamFailed && attachments.length) setAttachmentsBySession(current => ({ ...current, [sessionId]: [] }));
      try {
        const nextGraph = await visualizeGraph(sessionId);
        if (nextGraph?.nodes) {
          setGraph(nextGraph);
          setActiveSession(current => current?.session_id === sessionId ? { ...current, has_graph: true } : current);
          setSessions(current => current.map(session => session.session_id === sessionId ? { ...session, has_graph: true } : session));
        }
      } catch { /* The session may still have no graph after a regular chat turn. */ }
    } catch (cause: any) { if (cause.name !== 'AbortError') { setError(cause.message || '网络连接失败'); setLoading(false); } } finally { abortRef.current = null; }
  }
  async function respondToFeedback(decision: 'approve' | 'reject') {
    if (!activeSession || !feedback || loading) return;
    const sessionId = activeSession.session_id;
    const pending = feedback;
    const controller = new AbortController(); abortRef.current = controller;
    setFeedback(null); setLoading(true); setError('');
    try {
      await streamFeedback(sessionId, pending, decision, { onEvent: event => { if (event.run_id) sessionStorage.setItem(`igraph-active-run:${sessionId}`, JSON.stringify({ runId: event.run_id, lastEventIndex: Number(event.event_index ?? -1) })); if (event.type === 'done' || event.type === 'error') sessionStorage.removeItem(`igraph-active-run:${sessionId}`); applyEvent(event, setMessages, setGraph, setRunTools, setPlan, setLoading, setFeedback); } }, controller.signal);
      if (decision === 'reject') setMessages(current => [...current, { role: 'assistant', content: '已拒绝执行训练操作。' }]);
    } catch (cause: any) { if (cause.name !== 'AbortError') { setError(cause.message || '确认请求失败'); setLoading(false); } }
    finally { abortRef.current = null; }
  }
  async function upload(file: File) {
    if (!activeSession || uploading) return;
    const sessionId = activeSession.session_id;
    setUploading(true); setError('');
    try {
      const uploaded = await uploadFile(sessionId, file);
      const attachment = { file_token: uploaded.file_token, filename: uploaded.filename || file.name };
      setAttachmentsBySession(current => ({ ...current, [sessionId]: [...(current[sessionId] || []).filter(item => item.file_token !== attachment.file_token), attachment] }));
      setMessages(current => [...current, { role: 'user', content: `上传文件 ${attachment.filename}` }, { role: 'assistant', content: `文件「${attachment.filename}」已上传。请发送加载或分析指令。` }]);
    } catch (cause: any) { setError(cause.message || '上传失败'); }
    finally { setUploading(false); }
  }
  function removeAttachment(fileToken: string) {
    if (!activeSession) return;
    const sessionId = activeSession.session_id;
    setAttachmentsBySession(current => ({ ...current, [sessionId]: (current[sessionId] || []).filter(item => item.file_token !== fileToken) }));
  }
  async function changeModel(nextModel: string) {
    if (!activeSession || busy || loading || nextModel === model) return;
    const previousModel = model;
    setModel(nextModel); setBusy(true); setError('');
    try {
      const response = await updateSessionModel(activeSession.session_id, nextModel);
      const selectedModel = response.model || nextModel;
      setModel(selectedModel);
      setAvailableModels(normalizedModels(response.available_models || availableModels, selectedModel));
      setActiveSession(current => current ? { ...current, model: selectedModel } : current);
      setSessions(current => current.map(session => session.session_id === activeSession.session_id ? { ...session, model: selectedModel } : session));
    } catch (cause: any) {
      setModel(previousModel);
      setError(cause.message || '模型切换失败');
    } finally { setBusy(false); }
  }
  function openManagement(tab: 'memory' | 'skills') { setManagementTab(tab); setManagementOpen(true); }
  async function openDatasets() {
    if (!activeSession?.project_id) return setError('当前会话尚未关联项目');
    setDatasetOpen(true);
    try { setDatasets((await listProjectDatasets(activeSession.project_id)).datasets || []); }
    catch (cause: any) { setError(cause.message || '项目数据集加载失败'); }
  }
  async function chooseDataset(dataset: Dataset) {
    if (!activeSession || dataset.dataset_id === activeSession.active_dataset_id) return setDatasetOpen(false);
    setBusy(true); setError('');
    try {
      await setSessionDataset(activeSession.session_id, dataset.dataset_id);
      const nextGraph = await visualizeGraph(activeSession.session_id);
      setDatasetOpen(false); setGraph(nextGraph); setGraphOpen(window.innerWidth > 900);
      setActiveSession(current => current ? { ...current, active_dataset_id: dataset.dataset_id, has_graph: true } : current);
      setSessions(current => current.map(session => session.session_id === activeSession.session_id ? { ...session, active_dataset_id: dataset.dataset_id, has_graph: true } : session));
    } catch (cause: any) { setError(cause.message || '数据集切换失败'); }
    finally { setBusy(false); }
  }
  async function addProjectDataset(file: File, activateCurrentSession: boolean) {
    const projectId = activeProject?.project_id || activeSession?.project_id;
    if (!projectId) return;
    setUploading(true); setError('');
    try {
      const response = await uploadProjectDataset(projectId, file, activateCurrentSession ? activeSession?.session_id : undefined);
      const next = [...datasets.filter(item => item.dataset_id !== response.dataset.dataset_id), response.dataset]
        .map(item => ({ ...item, is_default: item.dataset_id === response.default_dataset_id }));
      setDatasets(next);
      setProjects(current => current.map(project => project.project_id === projectId ? { ...project, default_dataset_id: response.default_dataset_id } : project));
      setActiveProject(current => current?.project_id === projectId ? { ...current, default_dataset_id: response.default_dataset_id } : current);
      if (response.activated_session_id && activeSession) {
        const nextGraph = await visualizeGraph(activeSession.session_id);
        setGraph(nextGraph); setGraphOpen(window.innerWidth > 900);
        setActiveSession(current => current ? { ...current, active_dataset_id: response.dataset.dataset_id, has_graph: true } : current);
        setSessions(current => current.map(session => session.session_id === activeSession.session_id ? { ...session, active_dataset_id: response.dataset.dataset_id, has_graph: true } : session));
      }
    } catch (cause: any) { setError(cause.message || '项目数据集上传失败'); }
    finally { setUploading(false); }
  }
  async function makeDefaultDataset(dataset: Dataset) {
    try {
      await setDefaultProjectDataset(dataset.project_id, dataset.dataset_id);
      setDatasets(current => current.map(item => ({ ...item, is_default: item.dataset_id === dataset.dataset_id })));
      setProjects(current => current.map(project => project.project_id === dataset.project_id ? { ...project, default_dataset_id: dataset.dataset_id } : project));
      setActiveProject(current => current?.project_id === dataset.project_id ? { ...current, default_dataset_id: dataset.dataset_id } : current);
    } catch (cause: any) { setError(cause.message || '默认数据集设置失败'); }
  }
  async function renameDataset(dataset: Dataset) {
    const name = window.prompt('数据集名称', dataset.name)?.trim();
    if (!name || name === dataset.name) return;
    try {
      const response = await renameProjectDataset(dataset.project_id, dataset.dataset_id, name);
      setDatasets(current => current.map(item => item.dataset_id === dataset.dataset_id ? { ...item, ...response.dataset } : item));
    } catch (cause: any) { setError(cause.message || '数据集重命名失败'); }
  }
  async function removeDataset(dataset: Dataset) {
    if (!window.confirm(`确认从项目中删除数据集「${dataset.name}」吗？`)) return;
    try {
      const response = await deleteProjectDataset(dataset.project_id, dataset.dataset_id);
      setDatasets(current => current.filter(item => item.dataset_id !== dataset.dataset_id));
      setProjects(current => current.map(project => project.project_id === dataset.project_id ? { ...project, default_dataset_id: response.default_dataset_id } : project));
      setActiveProject(current => current?.project_id === dataset.project_id ? { ...current, default_dataset_id: response.default_dataset_id } : current);
      setSessions(current => current.map(session => session.active_dataset_id === dataset.dataset_id ? { ...session, active_dataset_id: null, has_graph: false } : session));
      if (activeSession?.active_dataset_id === dataset.dataset_id) { setActiveSession(current => current ? { ...current, active_dataset_id: null, has_graph: false } : current); setGraph(null); }
    } catch (cause: any) { setError(cause.message || '数据集删除失败'); }
  }
  async function expandNode(node: Record<string, unknown>) {
    if (!activeSession) return;
    const name = String(node.id ?? node.label ?? '').trim();
    if (!name) return;
    try {
      const expanded = await visualizeGraph(activeSession.session_id, { node_names: [name], max_nodes: 200 });
      if (expanded?.nodes?.length) setGraph(expanded);
    } catch (cause: any) { setError(cause.message || '节点邻居加载失败'); }
  }
  if (!ready) return null;
  if (!userId) return <div className="login-screen"><div className="login-box"><div className="brand-mark large"><Icon name="graph" size={24} /></div><h1>进入 iGraph</h1><p>登录后开始图谱分析</p><input autoFocus value={loginValue} onChange={event => setLoginValue(event.target.value)} onKeyDown={event => event.key === 'Enter' && login()} placeholder="用户 ID" /><button onClick={login}>登录</button>{loginError && <span className="login-error">{loginError}</span>}</div></div>;
  const activeAttachments = activeSession ? attachmentsBySession[activeSession.session_id] || [] : [];
  const layoutStyle = { '--sidebar-width': `${sidebarWidth}px`, '--graph-width': `${graphWidth}px` } as CSSProperties;
  const graphNodeCount = graph ? (graph.total_nodes ?? graph.num_nodes ?? graph.nodes.length) : 0;
  const graphEdgeCount = graph ? (graph.total_edges ?? graph.num_edges ?? graph.edges.length) : 0;
  const sessionProject = projects.find(project => project.project_id === activeSession?.project_id);
  const activeDataset = datasets.find(dataset => dataset.dataset_id === activeSession?.active_dataset_id);
  return <div className="app-shell">
    <TopBar onMenu={() => setMobileSidebarOpen(true)} />
    <div ref={bodyRef} className={`app-body ${graphOpen && activeSession ? 'has-graph-workspace' : 'graph-workspace-closed'}`} style={layoutStyle}>
      <Sidebar projects={projects} sessions={sessions} activeId={activeSession?.session_id} activeProjectId={activeProject?.project_id} userId={userId} dark={dark} onTheme={() => setDark(current => !current)} onMemory={() => openManagement('memory')} onSkills={() => openManagement('skills')} mobileOpen={mobileSidebarOpen} onCloseMobile={() => setMobileSidebarOpen(false)} onNew={() => newTask()} onNewProject={() => { setProjectEditorTarget(null); setProjectEditor('create'); }} onNewInProject={id => newTask(id)} onEditProject={project => { setProjectEditorTarget(project); setProjectEditor('edit'); }} onDeleteProject={setDeleteProjectTarget} onSelectProject={id => selectProject(id)} onSelect={id => selectSession(id)} onDelete={deleteTask} collapsed={false} onCollapse={() => setMobileSidebarOpen(false)} />
      <ColumnDivider side="sidebar" value={sidebarWidth} minimum={SIDEBAR_MIN} maximum={SIDEBAR_MAX} onPointerDown={beginResize} onNudge={nudgeResize} onReset={resetResize} />
      <div className="main-column">
        {activeProject ? <>
          <div className="workspace-error">{error && <><Icon name="close" size={14} /> {error}</>}</div>
          <ProjectWorkspace project={activeProject} datasets={datasets} busy={busy || uploading} onUpload={file => addProjectDataset(file, false)} onSetDefault={makeDefaultDataset} onRename={renameDataset} onDelete={removeDataset} />
        </> : <>
          <div className="workspace-context">
            <div className="context-icon"><Icon name="graph" size={18} /></div>
            <div>{sessionProject && <button className="project-breadcrumb" onClick={() => selectProject(sessionProject.project_id)}>{sessionProject.name} /</button>}<strong>{activeSession?.title || '新的图谱分析'}</strong><span>{graph ? `${graphNodeCount.toLocaleString()} nodes · ${graphEdgeCount.toLocaleString()} edges` : '等待加载图谱数据'}</span></div>
            <div className="context-actions">
              {activeSession && <button onClick={() => setMoveSessionTarget(activeSession)}><Icon name="move" size={14} /> 移动</button>}
              <button onClick={() => setGraphOpen(true)} disabled={!graph}><Icon name="graph" size={14} /> 打开图谱</button>
            </div>
          </div>
          <div className="workspace-error">{error && <><Icon name="close" size={14} /> {error}</>}</div>
          <ChatPaneWithState messages={messages} value={composerValue} model={model} models={availableModels} activeDatasetName={activeDataset?.name} onChange={setComposerValue} onModelChange={changeModel} onDataset={openDatasets} onSend={send} onUpload={upload} onRemoveAttachment={removeAttachment} attachments={activeAttachments} loading={loading} uploading={uploading} disabled={busy || !activeSession} hasGraph={!!graph} onOpenGraph={() => { setGraphOpen(true); setGraphTab('Graph'); }} onFeedback={respondToFeedback} feedback={feedback} runTools={runTools} plan={plan} />
        </>}
      </div>
      {graphOpen && activeSession && <><ColumnDivider side="graph" value={graphWidth} minimum={GRAPH_MIN} maximum={GRAPH_MAX} onPointerDown={beginResize} onNudge={nudgeResize} onReset={resetResize} /><GraphWorkspace data={graph} messages={messages} liveTools={demoMode ? demoMlTools : runTools} tab={graphTab} selected={selectedNode} onTab={setGraphTab} onSelect={setSelectedNode} onExpand={expandNode} onClose={() => setGraphOpen(false)} layoutVersion={graphLayoutVersion} /></>}
    </div>
    {datasetOpen && <DatasetPanel datasets={datasets} projectName={sessionProject?.name} activeDatasetId={activeSession?.active_dataset_id} busy={busy || uploading} onSelect={chooseDataset} onUpload={file => addProjectDataset(file, true)} onSetDefault={makeDefaultDataset} onRename={renameDataset} onDelete={removeDataset} onClose={() => setDatasetOpen(false)} />}
    {managementOpen && <ManagementPanel initialTab={managementTab} onClose={() => setManagementOpen(false)} />}
    {projectEditor && <ProjectDialog key={`${projectEditor}-${projectEditorTarget?.project_id || 'new'}`} project={projectEditor === 'edit' ? projectEditorTarget : null} onClose={() => { setProjectEditor(null); setProjectEditorTarget(null); }} onSave={saveProject} />}
    {deleteProjectTarget && <DeleteProjectDialog project={deleteProjectTarget} onClose={() => setDeleteProjectTarget(null)} onConfirm={deleteCurrentProject} />}
    {moveSessionTarget && <MoveConversationDialog session={moveSessionTarget} projects={projects} currentProjectId={moveSessionTarget.project_id} onClose={() => setMoveSessionTarget(null)} onConfirm={moveCurrentSession} />}
  </div>;
}

function ChatPaneWithState(props: any) { return <div className="chat-pane-shell"><div className="run-summary">{props.loading ? <><span className="loader" /> Agent 正在分析图谱</> : props.hasGraph ? <><span className="status-dot" /> 图谱分析就绪</> : '输入问题或粘贴边列表开始任务'}{props.plan?.steps?.length ? <span className="run-count">{props.plan.steps.filter((step: any) => step.status === 'done').length}/{props.plan.steps.length} steps</span> : null}</div><div className="chat-pane-proxy"><ChatPane {...props} /></div></div>; }

export default App;

createRoot(document.getElementById('root')!).render(<App />);

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-dom/client', () => ({ createRoot: () => ({ render: () => undefined }) }));
vi.mock('./api', async () => ({
  ...await vi.importActual('./api'),
  validateUser: vi.fn(), listSessions: vi.fn(), listProjects: vi.fn(), listCustomModels: vi.fn(),
  listProjectDatasets: vi.fn(), activateSession: vi.fn(), getCapabilities: vi.fn(),
  streamChat: vi.fn(), resumeRun: vi.fn(), visualizeGraph: vi.fn(), getRunControl: vi.fn(), getRunSnapshot: vi.fn(),
}));

import App from './main';
import { USER_KEY, activateSession, getCapabilities, getRunControl, getRunSnapshot, listCustomModels, listProjectDatasets, listProjects, listSessions, resumeRun, streamChat, validateUser, visualizeGraph } from './api';
import { readComposerDraft, writeComposerDraft } from './composerDrafts';

const composerPlaceholder = '今天帮你分析什么？粘贴边列表，或直接用自然语言提问';
const graph = { nodes: ['a', 'b', 'c'].map(id => ({ id })), edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }], directed: true };
const highlightedGraph = {
  nodes: [{ id: 'outside-preview-1' }, { id: 'outside-preview-2' }],
  edges: [{ source: 'outside-preview-1', target: 'outside-preview-2' }],
  highlights: { paths: [{ node_sequence: ['outside-preview-1', 'outside-preview-2'], label: '分析路径' }] },
};

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear(); sessionStorage.clear();
  localStorage.setItem(USER_KEY, 'admin');
  window.history.replaceState({}, '', '/?session=A');
  vi.mocked(validateUser).mockResolvedValue({ valid: true, default_model: 'model' });
  vi.mocked(listSessions).mockResolvedValue({ sessions: [{ session_id: 'A', title: 'Session A' }, { session_id: 'B', title: 'Session B' }] });
  vi.mocked(listProjects).mockResolvedValue({ projects: [] });
  vi.mocked(listCustomModels).mockResolvedValue({ models: [] });
  vi.mocked(listProjectDatasets).mockResolvedValue({ project_id: null, datasets: [] });
  vi.mocked(activateSession).mockResolvedValue({ model: 'model', has_graph: true, messages: [], active_run: null });
  vi.mocked(getCapabilities).mockResolvedValue({ run_cancel_v1: false, job_cancel_v1: false, edit_latest_turn_v1: false });
  vi.mocked(visualizeGraph).mockResolvedValue(graph);
  vi.mocked(getRunControl).mockResolvedValue({ run_id: null, start_request_id: null, registration_status: 'not_seen', cancel_status: 'none', execution_stopped: true, terminal_persisted: true, can_send: true, revision: 0, active_run: null });
  vi.mocked(streamChat).mockImplementation(async (_session, _message, _attachments, _handlers, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true });
  }));
  vi.mocked(resumeRun).mockImplementation(async (_session, _run, _cursor, _handlers, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true });
  }));
});
afterEach(cleanup);

async function openExploration() {
  const view = render(<App />);
  await screen.findByPlaceholderText(composerPlaceholder);
  await waitFor(() => expect(visualizeGraph).toHaveBeenCalled());
  const workspace = view.container.querySelector('.graph-workspace') as HTMLElement;
  fireEvent.click(within(workspace).getByRole('tab', { name: '图谱' }));
  fireEvent.click(await within(workspace).findByRole('button', { name: '图探索' }));
  return { ...view, workspace };
}

describe('actual App graph exploration', () => {
  it('starts a new analysis turn while preserving the composer and excluding its attachments', async () => {
    writeComposerDraft('admin', 'A', { draft: '尚未发送的另一个问题', attachments: [{ filename: 'private.csv', file_token: 'private-token' }], pendingAttachments: [] });
    const { container, unmount } = await openExploration();
    fireEvent.change(screen.getByRole('textbox', { name: '图谱问题' }), { target: { value: '请找出相关论文' } });
    fireEvent.click(screen.getByRole('button', { name: '定位并显示' }));
    await waitFor(() => expect(streamChat).toHaveBeenCalledOnce());
    const call = vi.mocked(streamChat).mock.calls[0];
    expect(call[0]).toBe('A');
    expect(call[1]).toContain('请找出相关论文');
    expect(call[2]).toEqual([]);
    expect(call[7]).toBeUndefined(); // This is not an edit of an earlier run.
    act(() => call[3].onOpen?.());
    expect((screen.getByPlaceholderText(composerPlaceholder) as HTMLTextAreaElement).value).toBe('尚未发送的另一个问题');
    expect(screen.getByRole('button', { name: '移除 private.csv' })).toBeTruthy();
    expect(container.querySelector('.user-message')?.textContent || container.textContent).toContain('请找出相关论文');
    expect((screen.getByRole('button', { name: '正在对话中处理…' }) as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect(readComposerDraft('admin', 'A')).toMatchObject({
      draft: '尚未发送的另一个问题', attachments: [{ filename: 'private.csv', file_token: 'private-token' }], preserveDuringRun: true,
    }));
    unmount();
    vi.mocked(activateSession).mockResolvedValue({ model: 'model', has_graph: true, messages: [{ role: 'user', content: call[1], attachments: [] }], active_run: { run_id: 'graph-run', status: 'running', last_event_index: 0 } });
    render(<App />);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledOnce());
    expect((screen.getByPlaceholderText(composerPlaceholder) as HTMLTextAreaElement).value).toBe('尚未发送的另一个问题');
    expect(screen.getByRole('button', { name: '移除 private.csv' })).toBeTruthy();
    expect(readComposerDraft('admin', 'A')?.attachments).toEqual([{ filename: 'private.csv', file_token: 'private-token' }]);
    expect(streamChat).toHaveBeenCalledOnce();
  });

  it('renders highlighted path results from the actual session stream', async () => {
    const { workspace } = await openExploration();
    fireEvent.click(screen.getByRole('button', { name: '最短路径' }));
    fireEvent.change(screen.getByRole('textbox', { name: '起点节点' }), { target: { value: 'a' } });
    fireEvent.change(screen.getByRole('textbox', { name: '终点节点' }), { target: { value: 'remote-node-not-in-preview' } });
    fireEvent.click(screen.getByRole('button', { name: '查找并高亮路径' }));
    await waitFor(() => expect(streamChat).toHaveBeenCalledOnce());
    const handlers = vi.mocked(streamChat).mock.calls[0][3];
    act(() => handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'path-run', event_index: 0, events: [{
      type: 'graph_visualize', data: {
        nodes: [{ id: 'a' }, { id: 'remote-node-not-in-preview' }],
        edges: [{ source: 'a', target: 'remote-node-not-in-preview' }], directed: true,
        highlights: { paths: [{ node_sequence: ['a', 'remote-node-not-in-preview'], label: '最短路径' }] },
      },
    }] }));
    await waitFor(() => expect(workspace.querySelectorAll('.graph-link.is-path-highlight')).toHaveLength(1));
    expect(workspace.querySelector('[data-node-id="remote-node-not-in-preview"]')).toBeTruthy();
    expect(workspace.querySelectorAll('.graph-node')).toHaveLength(4);
    expect(workspace.querySelectorAll('.graph-node-dot:not(.is-highlighted)')).toHaveLength(2);
    expect(workspace.querySelectorAll('.graph-link:not(.is-path-highlight)')).toHaveLength(2);
  });

  it('keeps a delayed full-graph refresh scoped to its original conversation', async () => {
    const { workspace } = await openExploration();
    let resolveRefresh!: (value: typeof graph) => void;
    vi.mocked(visualizeGraph).mockImplementationOnce(() => new Promise(resolve => { resolveRefresh = resolve; }));
    fireEvent.click(within(workspace).getByRole('button', { name: '恢复全图' }));
    await waitFor(() => expect(visualizeGraph).toHaveBeenCalledTimes(2));
    act(() => {
      window.history.pushState({}, '', '/?session=B');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    await waitFor(() => expect(activateSession).toHaveBeenLastCalledWith('B', expect.any(AbortSignal)));
    await waitFor(() => expect(visualizeGraph).toHaveBeenCalledTimes(3));
    await act(async () => resolveRefresh({ ...graph, nodes: [{ id: 'only-in-A' }], edges: [] }));
    expect(workspace.querySelector('[data-node-id="only-in-A"]')).toBeNull();
    expect(window.location.search).toBe('?session=B');
  });

  it.each([false, true])('retains highlighted nodes after settlement until explicitly reset (run control: %s)', async controlEnabled => {
    vi.mocked(getCapabilities).mockResolvedValue({ run_cancel_v1: controlEnabled, job_cancel_v1: false, edit_latest_turn_v1: false });
    if (controlEnabled) vi.mocked(getRunControl).mockResolvedValue({
      run_id: 'highlight-run', start_request_id: null, registration_status: 'registered', run_status: 'completed',
      cancel_status: 'none', execution_stopped: true, terminal_persisted: true, can_send: true, revision: 3, active_run: null,
    });
    let finishStream!: () => void;
    vi.mocked(streamChat).mockImplementation(() => new Promise(resolve => { finishStream = resolve; }));
    vi.mocked(getRunSnapshot).mockResolvedValue({
      session_id: 'A', run_id: 'highlight-run', status: 'completed',
      messages: [{ id: 'assistant:highlight-run', role: 'assistant', content: '已高亮', graphs: [highlightedGraph] }],
      active_run: null, pending_feedback: null, snapshot_complete: true, resume_cursor: null,
    });
    const { workspace } = await openExploration();
    fireEvent.change(screen.getByRole('textbox', { name: '图谱问题' }), { target: { value: '高亮分析路径' } });
    fireEvent.click(screen.getByRole('button', { name: '定位并显示' }));
    await waitFor(() => expect(streamChat).toHaveBeenCalledOnce());
    const handlers = vi.mocked(streamChat).mock.calls[0][3];
    act(() => handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'highlight-run', event_index: 0,
      events: [{ type: 'graph_visualize', data: highlightedGraph }] }));
    await waitFor(() => expect(workspace.querySelectorAll('.graph-node-dot.is-highlighted')).toHaveLength(2));
    await act(async () => {
      handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'highlight-run', event_index: 1,
        events: [{ type: 'done', content: '已高亮', message_id: 'assistant:highlight-run' }] });
      finishStream();
    });
    await waitFor(() => expect(getRunSnapshot).toHaveBeenCalled());
    await waitFor(() => expect(visualizeGraph).toHaveBeenCalledTimes(2));
    expect(workspace.querySelectorAll('.graph-node-dot.is-highlighted')).toHaveLength(2);
    expect(visualizeGraph).toHaveBeenLastCalledWith('A', expect.objectContaining({ highlights: highlightedGraph.highlights }));
    expect(workspace.querySelectorAll('.graph-node')).toHaveLength(5);
    expect(workspace.querySelectorAll('.graph-node-dot:not(.is-highlighted)')).toHaveLength(3);
    expect(workspace.querySelectorAll('.graph-link:not(.is-path-highlight)')).toHaveLength(2);
    expect(workspace.querySelector('[data-node-id="outside-preview-2"]')).toBeTruthy();

    fireEvent.click(within(workspace).getByRole('button', { name: '恢复全图' }));
    await waitFor(() => expect(visualizeGraph).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(workspace.querySelectorAll('.graph-node')).toHaveLength(3));
    expect(workspace.querySelectorAll('.graph-node-dot.is-highlighted')).toHaveLength(0);
    expect(workspace.querySelector('.graph-legend')?.textContent || '').not.toContain('路径高亮');
  });

  it('retains the saved highlighted subgraph when opening a session with a different full-graph preview', async () => {
    vi.mocked(activateSession).mockResolvedValue({ model: 'model', has_graph: true, active_run: null,
      messages: [{ role: 'assistant', content: '已高亮', graphs: [highlightedGraph] }] });
    const { workspace } = await openExploration();
    expect(workspace.querySelectorAll('.graph-node-dot.is-highlighted')).toHaveLength(2);
    expect(visualizeGraph).toHaveBeenLastCalledWith('A', expect.objectContaining({ highlights: highlightedGraph.highlights }));
    expect(workspace.querySelectorAll('.graph-node')).toHaveLength(5);
    expect(workspace.querySelectorAll('.graph-node-dot:not(.is-highlighted)')).toHaveLength(3);
    expect(workspace.querySelectorAll('.graph-link:not(.is-path-highlight)')).toHaveLength(2);
  });

  it('rejects an older graph response that arrives after a new streamed visualization', async () => {
    const { workspace } = await openExploration();
    let resolveRefresh!: (value: typeof graph) => void;
    vi.mocked(visualizeGraph).mockImplementationOnce(() => new Promise(resolve => { resolveRefresh = resolve; }));
    fireEvent.click(within(workspace).getByRole('button', { name: '恢复全图' }));
    await waitFor(() => expect(visualizeGraph).toHaveBeenCalledTimes(2));
    fireEvent.change(screen.getByRole('textbox', { name: '图谱问题' }), { target: { value: '高亮分析路径' } });
    fireEvent.click(screen.getByRole('button', { name: '定位并显示' }));
    await waitFor(() => expect(streamChat).toHaveBeenCalledOnce());
    act(() => vi.mocked(streamChat).mock.calls[0][3].onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'new-result', event_index: 0,
      events: [{ type: 'graph_visualize', data: highlightedGraph }] }));
    await act(async () => resolveRefresh(graph));
    expect(workspace.querySelectorAll('.graph-node-dot.is-highlighted')).toHaveLength(2);
    expect(workspace.querySelector('[data-node-id="outside-preview-2"]')).toBeTruthy();
  });

  it('replaces the analysis layer while keeping ordinary context and avoiding stale result nodes', async () => {
    const { workspace } = await openExploration();
    fireEvent.change(screen.getByRole('textbox', { name: '图谱问题' }), { target: { value: '高亮节点' } });
    fireEvent.click(screen.getByRole('button', { name: '定位并显示' }));
    await waitFor(() => expect(streamChat).toHaveBeenCalledOnce());
    const handlers = vi.mocked(streamChat).mock.calls[0][3];
    act(() => handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'layers', event_index: 0,
      events: [{ type: 'graph_visualize', data: highlightedGraph }] }));
    act(() => handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'layers', event_index: 1,
      events: [{ type: 'graph_visualize', data: { nodes: [{ id: 'latest-result' }], edges: [],
        highlights: { nodes: [{ ids: ['latest-result'] }] } } }] }));
    expect(workspace.querySelectorAll('.graph-node-dot.is-highlighted')).toHaveLength(1);
    expect(workspace.querySelectorAll('.graph-node-dot:not(.is-highlighted)')).toHaveLength(3);
    expect(workspace.querySelectorAll('.graph-link')).toHaveLength(2);
    expect(workspace.querySelector('[data-node-id="outside-preview-2"]')).toBeNull();
    expect(workspace.querySelector('[data-node-id="latest-result"]')).toBeTruthy();
  });
});

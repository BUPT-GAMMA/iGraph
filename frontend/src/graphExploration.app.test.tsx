import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-dom/client', () => ({ createRoot: () => ({ render: () => undefined }) }));
vi.mock('./api', async () => ({
  ...await vi.importActual('./api'),
  validateUser: vi.fn(), listSessions: vi.fn(), listProjects: vi.fn(), listCustomModels: vi.fn(),
  listProjectDatasets: vi.fn(), activateSession: vi.fn(), getCapabilities: vi.fn(),
  streamChat: vi.fn(), resumeRun: vi.fn(), visualizeGraph: vi.fn(), getRunControl: vi.fn(),
}));

import App from './main';
import { USER_KEY, activateSession, getCapabilities, getRunControl, listCustomModels, listProjectDatasets, listProjects, listSessions, resumeRun, streamChat, validateUser, visualizeGraph } from './api';
import { readComposerDraft, writeComposerDraft } from './composerDrafts';

const composerPlaceholder = '今天帮你分析什么？粘贴边列表，或直接用自然语言提问';
const graph = { nodes: ['a', 'b', 'c'].map(id => ({ id })), edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }], directed: true };

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear(); sessionStorage.clear();
  localStorage.setItem(USER_KEY, 'exploration-user');
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
    writeComposerDraft('exploration-user', 'A', { draft: '尚未发送的另一个问题', attachments: [{ filename: 'private.csv', file_token: 'private-token' }], pendingAttachments: [] });
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
    await waitFor(() => expect(readComposerDraft('exploration-user', 'A')).toMatchObject({
      draft: '尚未发送的另一个问题', attachments: [{ filename: 'private.csv', file_token: 'private-token' }], preserveDuringRun: true,
    }));
    unmount();
    vi.mocked(activateSession).mockResolvedValue({ model: 'model', has_graph: true, messages: [{ role: 'user', content: call[1], attachments: [] }], active_run: { run_id: 'graph-run', status: 'running', last_event_index: 0 } });
    render(<App />);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledOnce());
    expect((screen.getByPlaceholderText(composerPlaceholder) as HTMLTextAreaElement).value).toBe('尚未发送的另一个问题');
    expect(screen.getByRole('button', { name: '移除 private.csv' })).toBeTruthy();
    expect(readComposerDraft('exploration-user', 'A')?.attachments).toEqual([{ filename: 'private.csv', file_token: 'private-token' }]);
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
    expect(workspace.querySelectorAll('.graph-node')).toHaveLength(2);
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
});

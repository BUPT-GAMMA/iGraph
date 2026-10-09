import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('react-dom/client', () => ({ createRoot: () => ({ render: () => {} }) }));
vi.mock('./components/GraphWorkspace', () => ({ GraphWorkspace: () => null }));
vi.mock('./api', async () => ({ ...await vi.importActual('./api'),
  validateUser: vi.fn().mockResolvedValue({ valid: true, default_model: 'model' }),
  listSessions: vi.fn().mockResolvedValue({ sessions: [{ session_id: 'A', title: 'Session A' }, { session_id: 'B', title: 'Session B' }] }),
  listProjects: vi.fn().mockResolvedValue({ projects: [] }), listModels: vi.fn(),
  listProjectConversations: vi.fn().mockResolvedValue({ conversations: [] }),
  listProjectDatasets: vi.fn().mockResolvedValue({ project_id: null, datasets: [], default_dataset_id: null }),
  activateSession: vi.fn(), resumeRun: vi.fn(), getRunSnapshot: vi.fn(), streamChat: vi.fn(),
  getCapabilities: vi.fn().mockResolvedValue({ run_cancel_v1: true, job_cancel_v1: true }),
  cancelRun: vi.fn(), getRunControl: vi.fn(), deleteSession: vi.fn(),
  updateSessionModel: vi.fn(),
  visualizeGraph: vi.fn().mockRejectedValue(new Error('no graph')),
}));
import App from './main';
import { activityKey, readSessionActivities } from './sessionActivity';
import { USER_KEY, activateSession, cancelRun, deleteSession, getCapabilities, getRunControl, getRunSnapshot, listModels, listProjectConversations, listProjectDatasets, listProjects, listSessions, resumeRun, streamChat, updateSessionModel, validateUser, visualizeGraph, type ModelCatalog } from './api';

const catalog = (...ids: string[]): ModelCatalog => ({ default_model: 'model', models: ids.map(id => ({ config_id: id, model_id: id, upstream_model_id: id, display_name: id, base_url: '', source: 'custom', revision: 1, enabled: true, tools: true, vision: false, context_window: 32768, max_output_tokens: 4096 })) });

const active = (content = 'A') => ({ model: 'model', has_graph: false,
  messages: [{ id: 'assistant:r', role: 'assistant' as const, content }],
  active_run: { run_id: 'r', status: 'running', last_event_index: 100 } });
const complete = () => ({ ...active('ABC'), active_run: null });
function navigate(id: string) {
  act(() => { window.history.pushState({}, '', `/?session=${id}`); window.dispatchEvent(new PopStateEvent('popstate')); });
}
let restoreStorage = () => {};
beforeEach(() => {
  sessionStorage.clear(); localStorage.clear(); localStorage.setItem(USER_KEY, 'admin'); window.history.replaceState({}, '', '/?session=A');
  vi.mocked(validateUser).mockReset().mockResolvedValue({ valid: true, default_model: 'model' });
  vi.mocked(listProjects).mockReset().mockResolvedValue({ projects: [] });
  vi.mocked(listSessions).mockReset().mockResolvedValue({ sessions: [{ session_id: 'A', title: 'Session A' }, { session_id: 'B', title: 'Session B' }] });
  vi.mocked(listModels).mockReset().mockResolvedValue(catalog('model', 'old-choice'));
  vi.mocked(listProjectConversations).mockReset().mockResolvedValue({ conversations: [] });
  vi.mocked(listProjectDatasets).mockReset().mockResolvedValue({ project_id: null, datasets: [], default_dataset_id: null });
  vi.mocked(activateSession).mockReset(); vi.mocked(resumeRun).mockReset(); vi.mocked(getRunSnapshot).mockReset();
  vi.mocked(getCapabilities).mockReset().mockResolvedValue({ run_cancel_v1: true, job_cancel_v1: true });
  vi.mocked(getRunControl).mockReset().mockResolvedValue({ run_id: 'r', start_request_id: null, registration_status: 'registered', run_status: 'completed',
    cancel_status: 'none', execution_stopped: true, terminal_persisted: true, can_send: true, active_run: null, revision: 3 });
  vi.mocked(visualizeGraph).mockClear();
  vi.mocked(deleteSession).mockReset().mockResolvedValue({ success: true, delete_status: 'requested' });
  vi.mocked(updateSessionModel).mockReset().mockResolvedValue({ session_id: 'A', model: 'model', available_models: ['model'] });
  vi.mocked(streamChat).mockReset().mockImplementation(async (_s, _m, _a, _h, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true });
  }));
  vi.mocked(resumeRun).mockImplementation(async (_s, _r, _c, _h, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true });
  }));
  vi.mocked(getRunSnapshot).mockResolvedValue({ session_id: 'A', run_id: 'r', status: 'completed', messages: complete().messages,
    active_run: null, pending_feedback: null, snapshot_complete: true, resume_cursor: null });
  vi.mocked(cancelRun).mockReset().mockResolvedValue({ run_id: 'r', start_request_id: null, registration_status: 'registered', run_status: 'cancelled',
    cancel_status: 'confirmed', execution_stopped: true, terminal_persisted: true, can_send: true, active_run: null, revision: 3 });
});
afterEach(() => { cleanup(); restoreStorage(); restoreStorage = () => {}; });

describe('actual App recovery', () => {
  it('resumes a background run after remount without posting another question', async () => {
    vi.mocked(activateSession).mockImplementation(async id => id === 'A' ? active() : { ...complete(), messages: [] });
    const first = render(<App />);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledOnce());
    navigate('B');
    await waitFor(() => expect(first.container.querySelector('.session-row.selected')?.textContent).toContain('Session B'));
    await waitFor(() => expect(readSessionActivities('admin').find(item => item.sessionId === 'A')?.runId).toBe('r'));
    first.unmount();
    vi.mocked(resumeRun).mockClear();
    vi.mocked(activateSession).mockClear();
    const second = render(<App />);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledOnce());
    expect(vi.mocked(resumeRun).mock.calls[0].slice(0, 3)).toEqual(['A', 'r', -1]);
    expect(vi.mocked(activateSession).mock.calls.every(call => call[0] === 'B')).toBe(true);
    await waitFor(() => expect(second.container.querySelector('.session-row.selected')?.textContent).toContain('Session B'));
    expect(second.container.querySelector('.session-run-indicator.is-running')).toBeTruthy();
    expect(streamChat).not.toHaveBeenCalled();
    expect(cancelRun).not.toHaveBeenCalled();
  });

  it('restores an offline completion, keeps its blue dot across reloads, and clears it permanently when opened', async () => {
    window.history.replaceState({}, '', '/?session=B');
    localStorage.setItem(activityKey('admin', 'A'), JSON.stringify({ sessionId: 'A', runId: 'r', status: 'streaming' }));
    vi.mocked(activateSession).mockResolvedValue({ ...complete(), messages: [] });
    vi.mocked(getCapabilities).mockResolvedValue({ run_cancel_v1: false, job_cancel_v1: false });
    vi.mocked(resumeRun).mockImplementationOnce(async (sessionId, runId, _cursor, handlers) => {
      handlers.onBatch?.({ protocol_version: 2, session_id: sessionId, run_id: runId, event_index: 0, events: [{ type: 'done', content: 'Finished offline' }] });
    });
    const first = render(<App />);
    await screen.findByRole('status', { name: '回复已完成' });
    await waitFor(() => expect(readSessionActivities('admin').find(item => item.sessionId === 'A')?.completedRunId).toBe('r'));
    first.unmount();
    const second = render(<App />);
    await screen.findByRole('status', { name: '回复已完成' });
    fireEvent.click(screen.getByText('Session A'));
    await waitFor(() => expect(screen.queryByRole('status', { name: '回复已完成' })).toBeNull());
    await waitFor(() => expect(readSessionActivities('admin').find(item => item.sessionId === 'A')?.readRunId).toBe('r'));
    second.unmount();
    window.history.replaceState({}, '', '/?session=B');
    render(<App />);
    await screen.findByText('Session A');
    expect(screen.queryByRole('status', { name: '回复已完成' })).toBeNull();
    expect(resumeRun).toHaveBeenCalledOnce();
    expect(streamChat).not.toHaveBeenCalled();
  });

  it('discards activity for deleted tasks without restoring its run', async () => {
    localStorage.setItem(activityKey('admin', 'deleted'), JSON.stringify({ sessionId: 'deleted', runId: 'old', status: 'streaming' }));
    vi.mocked(listSessions).mockImplementation(async options => options?.ids ? { sessions: [] } : { sessions: [{ session_id: 'A', title: 'Session A' }] });
    vi.mocked(activateSession).mockResolvedValue({ ...complete(), messages: [] });
    render(<App />);
    await screen.findByText('Session A');
    expect(localStorage.getItem(activityKey('admin', 'deleted'))).toBeNull();
    expect(resumeRun).not.toHaveBeenCalled();
  });

  it.each(['storage', 'focus', 'visibility'])('keeps the workspace and stream when legacy identity changes on %s', async trigger => {
    vi.mocked(activateSession).mockResolvedValue(active('workspace response'));
    const { container } = render(<App />);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledOnce());
    const signal = vi.mocked(resumeRun).mock.calls[0][4];
    act(() => {
      localStorage.setItem(USER_KEY, 'old-account');
      if (trigger === 'storage') window.dispatchEvent(new StorageEvent('storage', { key: USER_KEY, newValue: 'old-account', storageArea: localStorage }));
      else if (trigger === 'focus') window.dispatchEvent(new Event('focus'));
      else document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(signal.aborted).toBe(false);
    expect(window.location.search).toBe('?session=A');
    expect(container.querySelector('.account-area')).toBeNull();
    expect(container.textContent).toContain('workspace response');
    expect(cancelRun).not.toHaveBeenCalled();
  });

  it('opens a deep link with no saved user', async () => {
    localStorage.removeItem(USER_KEY);
    vi.mocked(activateSession).mockResolvedValue(complete());
    render(<App />);
    await screen.findByText('ABC');
    expect(screen.queryByRole('button', { name: '打开登录' })).toBeNull();
    expect(activateSession).toHaveBeenCalledWith('A', expect.anything());
  });
  it.each(['streaming', 'before response'])('removes an accepted deletion during %s and ignores late output', async phase => {
    vi.mocked(activateSession).mockResolvedValue(phase === 'streaming' ? active() : { ...complete(), messages: [] });
    const { container } = render(<App />);
    if (phase === 'before response') {
      const composer = await screen.findByPlaceholderText('今天帮你分析什么？粘贴边列表，或直接用自然语言提问');
      fireEvent.change(composer, { target: { value: '新消息' } });
      fireEvent.keyDown(composer, { key: 'Enter' });
      await waitFor(() => expect(streamChat).toHaveBeenCalledOnce());
    } else await waitFor(() => expect(resumeRun).toHaveBeenCalledOnce());
    const call = phase === 'streaming' ? vi.mocked(resumeRun).mock.calls[0] : vi.mocked(streamChat).mock.calls[0];
    fireEvent.click(screen.getAllByRole('button', { name: '删除任务' })[0]);
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    await waitFor(() => expect(deleteSession).toHaveBeenCalledWith('A'));
    await waitFor(() => expect(call[4].aborted).toBe(true));
    expect(screen.getAllByRole('button', { name: '删除任务' })).toHaveLength(1);
    expect(window.location.search).not.toContain('session=A');
    act(() => call[3].onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'r', event_index: 0,
      events: [{ type: 'token', content: '迟到的旧会话输出' }] }));
    expect(container.textContent).not.toContain('迟到的旧会话输出');
    expect(container.textContent).not.toContain('请求失败');
  });
  it('deduplicates deletion and preserves a session selected while acknowledgement was pending', async () => {
    let accepted!: (value: { success: boolean; delete_status: 'requested' }) => void;
    vi.mocked(deleteSession).mockImplementation(async () => new Promise(resolve => { accepted = resolve; }));
    vi.mocked(activateSession).mockImplementation(async id => id === 'A' ? active() : { ...complete(), messages: [] });
    const { container } = render(<App />);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledOnce());
    const remove = screen.getAllByRole('button', { name: '删除任务' })[0];
    fireEvent.click(remove); fireEvent.click(remove);
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    await waitFor(() => expect(deleteSession).toHaveBeenCalledOnce());
    fireEvent.click(remove);
    expect(deleteSession).toHaveBeenCalledOnce();
    navigate('B');
    await waitFor(() => expect(container.querySelector('.session-row.selected .recent-session-name')?.textContent).toBe('Session B'));
    await act(async () => accepted({ success: true, delete_status: 'requested' }));
    expect(window.location.search).toContain('session=B');
    expect(container.querySelector('.session-row.selected .recent-session-name')?.textContent).toBe('Session B');
  });
  it.each([false, true])('accepts the next question while completed content and graph requests remain pending (existing graph: %s)', async hasGraph => {
    vi.mocked(activateSession).mockResolvedValue({ ...complete(), messages: [], has_graph: hasGraph });
    vi.mocked(getRunSnapshot).mockImplementation(async () => new Promise(() => {}));
    if (hasGraph) vi.mocked(visualizeGraph).mockImplementationOnce(async () => new Promise(() => {}));
    const { container } = render(<App />);
    const composer = await screen.findByPlaceholderText('今天帮你分析什么？粘贴边列表，或直接用自然语言提问');
    fireEvent.change(composer, { target: { value: '你好' } });
    fireEvent.keyDown(composer, { key: 'Enter' });
    await waitFor(() => expect(streamChat).toHaveBeenCalledOnce());
    const handlers = vi.mocked(streamChat).mock.calls[0][3];
    act(() => handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'r', event_index: 0,
      events: [{ type: 'done', content: '你好！', message_id: 'assistant:r' }] }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '终止当前任务' })).toBeNull());
    await waitFor(() => expect(getRunSnapshot).toHaveBeenCalledOnce());
    fireEvent.change(composer, { target: { value: '下一个问题' } });
    expect((container.querySelector('.send-button') as HTMLButtonElement).disabled).toBe(false);
    fireEvent.keyDown(composer, { key: 'Enter' });
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(2));
    expect(cancelRun).not.toHaveBeenCalled();
    if (!hasGraph) expect(visualizeGraph).not.toHaveBeenCalled();
  });
  it('can stop a new request before response headers, a run ID or any output arrive', async () => {
    vi.mocked(activateSession).mockResolvedValue({ ...complete(), messages: [] });
    render(<App />);
    const composer = await screen.findByPlaceholderText('今天帮你分析什么？粘贴边列表，或直接用自然语言提问');
    fireEvent.change(composer, { target: { value: 'waiting before first token' } });
    fireEvent.keyDown(composer, { key: 'Enter' });
    await waitFor(() => expect(streamChat).toHaveBeenCalledOnce());
    const stop = screen.getByRole('button', { name: '终止当前任务' }) as HTMLButtonElement;
    expect(stop.disabled).toBe(false);
    fireEvent.click(stop);
    await waitFor(() => expect(cancelRun).toHaveBeenCalledWith('A',
      { start_request_id: vi.mocked(streamChat).mock.calls[0][5] }, expect.any(String), expect.any(AbortSignal)));
    await waitFor(() => expect(vi.mocked(streamChat).mock.calls[0][4].aborted).toBe(true));
    expect(streamChat).toHaveBeenCalledOnce();
  });
  it.each(['unavailable', 'network failure'])('recovers stop controls after an initial capability %s', async mode => {
    if (mode === 'unavailable') vi.mocked(getCapabilities).mockResolvedValueOnce({ run_cancel_v1: false, job_cancel_v1: false });
    else vi.mocked(getCapabilities).mockRejectedValueOnce(new TypeError('offline'));
    vi.mocked(activateSession).mockResolvedValue(active());
    render(<App />);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledOnce());
    expect(screen.queryByRole('button', { name: '终止当前任务' })).toBeNull();
    fireEvent(window, new Event('online'));
    const stop = await screen.findByRole('button', { name: '终止当前任务' });
    fireEvent.click(stop);
    await waitFor(() => expect(cancelRun).toHaveBeenCalledOnce());
    expect(resumeRun).toHaveBeenCalledOnce();
  });
  it('keeps immediate termination usable when recovery and cancellation storage writes fail', async () => {
    const write = Storage.prototype.setItem;
    const storageSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function(this: Storage, key: string, value: string) {
      if (key.startsWith('igraph-active-run:') || key.startsWith('igraph-cancel:')) throw new DOMException('Quota exceeded', 'QuotaExceededError');
      return write.call(this, key, value);
    });
    restoreStorage = () => storageSpy.mockRestore();
    vi.mocked(activateSession).mockResolvedValue(active());
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '终止当前任务' }));
    await waitFor(() => expect(cancelRun).toHaveBeenCalledOnce());
    await waitFor(() => expect(vi.mocked(resumeRun).mock.calls[0][4].aborted).toBe(true));
  });
  it('ignores a persisted cursor on refresh and rebuilds from -1', async () => {
    sessionStorage.setItem('igraph-active-run:v2:admin:A', JSON.stringify({ runId: 'r', lastEventIndex: 99 }));
    vi.mocked(activateSession).mockResolvedValue(active());
    render(<App />);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledTimes(1));
    expect(vi.mocked(resumeRun).mock.calls[0][2]).toBe(-1);
  });
  it('preserves results after termination and switching back without repeating the stop', async () => {
    vi.mocked(activateSession).mockImplementation(async id => id === 'A' ? active() : { ...active(''), active_run: null });
    const { container } = render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '终止当前任务' }));
    await waitFor(() => expect(cancelRun).toHaveBeenCalledOnce());
    navigate('B');
    await waitFor(() => expect(container.querySelector('.session-row.selected .recent-session-name')?.textContent).toBe('Session B'));
    vi.mocked(activateSession).mockResolvedValue(complete()); navigate('A');
    await waitFor(() => expect(container.querySelector('.session-row.selected .recent-session-name')?.textContent).toBe('Session A'));
    await waitFor(() => expect(container.querySelector('.message-content')?.textContent).toBe('ABC'));
    expect((container.querySelector('.send-button') as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByRole('button', { name: '继续接收' })).toBeNull();
    expect(cancelRun).toHaveBeenCalledOnce();
  });
  it('clears legacy observer pauses without turning them into cancellation', async () => {
    sessionStorage.setItem('igraph-run-watch-paused:v1:admin:A', '1');
    vi.mocked(activateSession).mockResolvedValue(active());
    vi.mocked(resumeRun).mockImplementationOnce(async (_s, _r, _c, handlers) => {
      handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'r', event_index: 0, events: [{ type: 'done', content: 'ABC' }] });
    });
    const { container } = render(<App />);
    await waitFor(() => expect(container.querySelector('.message-content')?.textContent).toBe('ABC'));
    await waitFor(() => expect((container.querySelector('.send-button') as HTMLButtonElement).disabled).toBe(false));
    expect(resumeRun).toHaveBeenCalledTimes(1);
    expect(cancelRun).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('igraph-run-watch-paused:v1:admin:A')).toBeNull();
  });
});

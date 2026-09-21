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
  sessionStorage.clear(); localStorage.clear(); localStorage.setItem(USER_KEY, 'recovery-user'); window.history.replaceState({}, '', '/?session=A');
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
    await waitFor(() => expect(readSessionActivities('recovery-user').find(item => item.sessionId === 'A')?.runId).toBe('r'));
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
    localStorage.setItem(activityKey('recovery-user', 'A'), JSON.stringify({ sessionId: 'A', runId: 'r', status: 'streaming' }));
    vi.mocked(activateSession).mockResolvedValue({ ...complete(), messages: [] });
    vi.mocked(getCapabilities).mockResolvedValue({ run_cancel_v1: false, job_cancel_v1: false });
    vi.mocked(resumeRun).mockImplementationOnce(async (sessionId, runId, _cursor, handlers) => {
      handlers.onBatch?.({ protocol_version: 2, session_id: sessionId, run_id: runId, event_index: 0, events: [{ type: 'done', content: 'Finished offline' }] });
    });
    const first = render(<App />);
    await screen.findByRole('status', { name: '回复已完成' });
    await waitFor(() => expect(readSessionActivities('recovery-user').find(item => item.sessionId === 'A')?.completedRunId).toBe('r'));
    first.unmount();
    const second = render(<App />);
    await screen.findByRole('status', { name: '回复已完成' });
    fireEvent.click(screen.getByText('Session A'));
    await waitFor(() => expect(screen.queryByRole('status', { name: '回复已完成' })).toBeNull());
    await waitFor(() => expect(readSessionActivities('recovery-user').find(item => item.sessionId === 'A')?.readRunId).toBe('r'));
    second.unmount();
    window.history.replaceState({}, '', '/?session=B');
    render(<App />);
    await screen.findByText('Session A');
    expect(screen.queryByRole('status', { name: '回复已完成' })).toBeNull();
    expect(resumeRun).toHaveBeenCalledOnce();
    expect(streamChat).not.toHaveBeenCalled();
  });

  it('discards activity for deleted tasks without restoring its run', async () => {
    localStorage.setItem(activityKey('recovery-user', 'deleted'), JSON.stringify({ sessionId: 'deleted', runId: 'old', status: 'streaming' }));
    vi.mocked(listSessions).mockImplementation(async options => options?.ids ? { sessions: [] } : { sessions: [{ session_id: 'A', title: 'Session A' }] });
    vi.mocked(activateSession).mockResolvedValue({ ...complete(), messages: [] });
    render(<App />);
    await screen.findByText('Session A');
    expect(localStorage.getItem(activityKey('recovery-user', 'deleted'))).toBeNull();
    expect(resumeRun).not.toHaveBeenCalled();
  });

  it.each(['storage', 'focus', 'visibility'])('synchronizes accounts on %s and discards the old stream', async trigger => {
    vi.mocked(activateSession).mockResolvedValue(active('old account response'));
    const { container } = render(<App />);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledOnce());
    const call = vi.mocked(resumeRun).mock.calls[0];
    vi.mocked(listSessions).mockResolvedValue({ sessions: [{ session_id: 'next', title: 'Next Account Session' }] });
    act(() => {
      localStorage.setItem(USER_KEY, 'next-user');
      if (trigger === 'storage') window.dispatchEvent(new StorageEvent('storage', { key: USER_KEY, newValue: 'next-user', storageArea: localStorage }));
      else if (trigger === 'focus') window.dispatchEvent(new Event('focus'));
      else document.dispatchEvent(new Event('visibilitychange'));
    });
    await screen.findByRole('button', { name: '账户：next-user' });
    await screen.findByText('Next Account Session');
    expect(call[4].aborted).toBe(true);
    expect(window.location.search).toBe('');
    act(() => call[3].onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'r', event_index: 101,
      events: [{ type: 'token', content: 'late private output' }] }));
    expect(container.textContent).not.toContain('old account response');
    expect(container.textContent).not.toContain('late private output');
    expect(cancelRun).not.toHaveBeenCalled();
  });

  it.each([USER_KEY, null])('clears the workspace when another tab removes the account (%s)', async key => {
    vi.mocked(activateSession).mockResolvedValue(active());
    render(<App />);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledOnce());
    const signal = vi.mocked(resumeRun).mock.calls[0][4];
    act(() => {
      if (key === null) localStorage.clear(); else localStorage.removeItem(USER_KEY);
      window.dispatchEvent(new StorageEvent('storage', { key, newValue: null, storageArea: localStorage }));
    });
    await screen.findByRole('button', { name: '打开登录' });
    expect(signal.aborted).toBe(true);
    expect(screen.queryByText('Session A')).toBeNull();
    expect(screen.queryByRole('button', { name: '发送' })).toBeNull();
  });

  it('does not overwrite a synchronized identity with an old login response', async () => {
    window.history.replaceState({}, '', '/');
    let finishLogin!: (value: { valid: boolean }) => void;
    const pending = new Promise<{ valid: boolean }>(resolve => { finishLogin = resolve; });
    render(<App />);
    await waitFor(() => expect(listSessions).toHaveBeenCalled());
    vi.mocked(validateUser).mockImplementation(identity => identity === 'stale-login' ? pending : Promise.resolve({ valid: true, default_model: 'model' }));
    fireEvent.click(screen.getByRole('button', { name: '账户：recovery-user' }));
    fireEvent.click(screen.getByRole('button', { name: '使用其他用户登录' }));
    fireEvent.change(screen.getByLabelText('用户 ID'), { target: { value: 'stale-login' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    await waitFor(() => expect(validateUser).toHaveBeenCalledWith('stale-login'));
    act(() => {
      localStorage.setItem(USER_KEY, 'next-user');
      window.dispatchEvent(new StorageEvent('storage', { key: USER_KEY, newValue: 'next-user', storageArea: localStorage }));
    });
    await screen.findByRole('button', { name: '账户：next-user' });
    await act(async () => { finishLogin({ valid: true }); await pending; });
    expect(localStorage.getItem(USER_KEY)).toBe('next-user');
    expect(screen.queryByRole('button', { name: '账户：stale-login' })).toBeNull();
  });

  it.each(['streaming', 'before response'])('removes an accepted deletion during %s and ignores late output', async phase => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
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
    await waitFor(() => expect(deleteSession).toHaveBeenCalledWith('A'));
    await waitFor(() => expect(call[4].aborted).toBe(true));
    expect(screen.getAllByRole('button', { name: '删除任务' })).toHaveLength(1);
    expect(window.location.search).not.toContain('session=A');
    act(() => call[3].onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'r', event_index: 0,
      events: [{ type: 'token', content: '迟到的旧会话输出' }] }));
    expect(container.textContent).not.toContain('迟到的旧会话输出');
    expect(container.textContent).not.toContain('请求失败');
    confirm.mockRestore();
  });
  it('deduplicates deletion and preserves a session selected while acknowledgement was pending', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    let accepted!: (value: { success: boolean; delete_status: 'requested' }) => void;
    vi.mocked(deleteSession).mockImplementation(async () => new Promise(resolve => { accepted = resolve; }));
    vi.mocked(activateSession).mockImplementation(async id => id === 'A' ? active() : { ...complete(), messages: [] });
    const { container } = render(<App />);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledOnce());
    const remove = screen.getAllByRole('button', { name: '删除任务' })[0];
    fireEvent.click(remove); fireEvent.click(remove);
    expect(deleteSession).toHaveBeenCalledOnce();
    navigate('B');
    await waitFor(() => expect(container.querySelector('.session-row.selected .recent-session-name')?.textContent).toBe('Session B'));
    await act(async () => accepted({ success: true, delete_status: 'requested' }));
    expect(window.location.search).toContain('session=B');
    expect(container.querySelector('.session-row.selected .recent-session-name')?.textContent).toBe('Session B');
    confirm.mockRestore();
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
    sessionStorage.setItem('igraph-active-run:v2:recovery-user:A', JSON.stringify({ runId: 'r', lastEventIndex: 99 }));
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
    sessionStorage.setItem('igraph-run-watch-paused:v1:recovery-user:A', '1');
    vi.mocked(activateSession).mockResolvedValue(active());
    vi.mocked(resumeRun).mockImplementationOnce(async (_s, _r, _c, handlers) => {
      handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'r', event_index: 0, events: [{ type: 'done', content: 'ABC' }] });
    });
    const { container } = render(<App />);
    await waitFor(() => expect(container.querySelector('.message-content')?.textContent).toBe('ABC'));
    await waitFor(() => expect((container.querySelector('.send-button') as HTMLButtonElement).disabled).toBe(false));
    expect(resumeRun).toHaveBeenCalledTimes(1);
    expect(cancelRun).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('igraph-run-watch-paused:v1:recovery-user:A')).toBeNull();
  });
  it('ignores previous-account loads that finish after switching identities', async () => {
    let resolveOldProjects!: (value: { projects: Array<{ project_id: string; user_id: string; name: string }> }) => void;
    let resolveOldModels!: (value: ModelCatalog) => void;
    const oldProjects = new Promise<{ projects: Array<{ project_id: string; user_id: string; name: string }> }>(resolve => { resolveOldProjects = resolve; });
    const oldModels = new Promise<ModelCatalog>(resolve => { resolveOldModels = resolve; });
    vi.mocked(listProjects)
      .mockReset()
      .mockImplementationOnce(() => oldProjects)
      .mockResolvedValue({ projects: [] });
    vi.mocked(listSessions)
      .mockReset()
      .mockResolvedValue({ sessions: [{ session_id: 'next-session', title: 'Next Account Session' }] });
    vi.mocked(listModels)
      .mockReset()
      .mockImplementationOnce(() => oldModels)
      .mockResolvedValue(catalog('model', 'next-custom'));
    window.history.replaceState({}, '', '/');
    const { container } = render(<App />);
    await waitFor(() => expect(listProjects).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(listModels).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: '账户：recovery-user' }));
    fireEvent.click(screen.getByRole('button', { name: '使用其他用户登录' }));
    fireEvent.change(screen.getByLabelText('用户 ID'), { target: { value: 'next-user' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));

    await waitFor(() => expect(container.textContent).toContain('Next Account Session'));
    await waitFor(() => expect(listProjects).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(listModels).toHaveBeenCalledTimes(2));
    await act(async () => {
      resolveOldProjects({ projects: [{ project_id: 'old-project', user_id: 'recovery-user', name: 'Old Account Project' }] });
      resolveOldModels(catalog('old-custom'));
      await Promise.resolve();
    });

    expect(listSessions).toHaveBeenCalledOnce();
    expect(container.textContent).not.toContain('Old Account Project');
    fireEvent.click(within(screen.getByRole('complementary')).getByRole('button', { name: '新建任务' }));
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }));
    expect(screen.getByRole('button', { name: 'next-custom' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'old-custom' })).toBeNull();
  });
  it('releases the global busy state when an account changes during project loading', async () => {
    let resolveOldConversations!: (value: { conversations: [] }) => void;
    const oldConversations = new Promise<{ conversations: [] }>(resolve => { resolveOldConversations = resolve; });
    vi.mocked(listProjects)
      .mockReset()
      .mockResolvedValueOnce({ projects: [{ project_id: 'old-project', user_id: 'recovery-user', name: 'Old Account Project' }] })
      .mockResolvedValue({ projects: [] });
    vi.mocked(listSessions).mockReset().mockResolvedValue({ sessions: [] });
    vi.mocked(listProjectConversations).mockReset().mockImplementationOnce(() => oldConversations);
    window.history.replaceState({}, '', '/?project=old-project');
    const { container } = render(<App />);
    await waitFor(() => expect(listProjectConversations).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole('button', { name: '账户：recovery-user' }));
    fireEvent.click(screen.getByRole('button', { name: '使用其他用户登录' }));
    fireEvent.change(screen.getByLabelText('用户 ID'), { target: { value: 'next-user' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));

    await waitFor(() => expect(screen.getByRole('button', { name: '账户：next-user' })).toBeTruthy());
    await waitFor(() => expect(container.querySelector('.workspace-home-action')).not.toBeNull());
    const homeAction = container.querySelector('.workspace-home-action') as HTMLButtonElement;
    expect(homeAction.disabled).toBe(false);
    await act(async () => {
      resolveOldConversations({ conversations: [] });
      await Promise.resolve();
    });
    expect(homeAction.disabled).toBe(false);
  });
  it('ignores an old model update after switching away from and back to the same account', async () => {
    let resolveOldUpdate!: (value: { session_id: string; model: string; available_models: string[] }) => void;
    const oldUpdate = new Promise<{ session_id: string; model: string; available_models: string[] }>(resolve => { resolveOldUpdate = resolve; });
    let recoveryValidationCount = 0;
    vi.mocked(validateUser).mockReset().mockImplementation(async identity => {
      if (identity === 'next-user') return { valid: true, default_model: 'next-model', available_models: ['next-model'] };
      recoveryValidationCount += 1;
      return recoveryValidationCount === 1
        ? { valid: true, default_model: 'model', available_models: ['model', 'old-choice'] }
        : { valid: true, default_model: 'fresh-a-model', available_models: ['fresh-a-model'] };
    });
    vi.mocked(listSessions)
      .mockReset()
      .mockResolvedValueOnce({ sessions: [{ session_id: 'A', title: 'Session A', model: 'model' }] })
      .mockResolvedValue({ sessions: [] });
    vi.mocked(activateSession).mockResolvedValue({ ...complete(), available_models: ['model', 'old-choice'] });
    vi.mocked(updateSessionModel).mockReturnValue(oldUpdate);

    render(<App />);
    const modelTrigger = await screen.findByRole('button', { name: '选择模型' });
    fireEvent.click(modelTrigger);
    fireEvent.click(within(screen.getByRole('group', { name: '可用模型' })).getByRole('button', { name: 'old-choice' }));
    await waitFor(() => expect(updateSessionModel).toHaveBeenCalledWith('A', 'old-choice', 0));
    vi.mocked(listModels).mockResolvedValue({ ...catalog('fresh-a-model'), default_model: 'fresh-a-model' });

    act(() => window.history.replaceState({}, '', '/'));
    fireEvent.click(screen.getByRole('button', { name: '账户：recovery-user' }));
    fireEvent.click(screen.getByRole('button', { name: '使用其他用户登录' }));
    fireEvent.change(screen.getByLabelText('用户 ID'), { target: { value: 'next-user' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));

    await screen.findByRole('button', { name: '账户：next-user' });
    fireEvent.click(screen.getByRole('button', { name: '账户：next-user' }));
    fireEvent.click(screen.getByRole('button', { name: '使用其他用户登录' }));
    fireEvent.change(screen.getByLabelText('用户 ID'), { target: { value: 'recovery-user' } });
    fireEvent.click(screen.getByRole('button', { name: '登录' }));

    await screen.findByRole('button', { name: '账户：recovery-user' });
    fireEvent.click(within(screen.getByRole('complementary')).getByRole('button', { name: '新建任务' }));
    const nextTrigger = await screen.findByRole('button', { name: '选择模型' });
    expect(nextTrigger.textContent).toContain('fresh-a-model');

    await act(async () => {
      resolveOldUpdate({ session_id: 'A', model: 'old-choice', available_models: ['old-choice', 'leaked-model'] });
      await oldUpdate;
    });

    expect(nextTrigger.textContent).toContain('fresh-a-model');
    expect((nextTrigger as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole('button', { name: '发送' }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(nextTrigger);
    const modelGroup = screen.getByRole('group', { name: '可用模型' });
    expect(within(modelGroup).queryByRole('button', { name: 'old-choice' })).toBeNull();
    expect(within(modelGroup).queryByRole('button', { name: 'leaked-model' })).toBeNull();
  });
});

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('react-dom/client', () => ({ createRoot: () => ({ render: () => {} }) }));
vi.mock('./components/GraphWorkspace', () => ({ GraphWorkspace: () => null }));
vi.mock('./api', async () => ({ ...await vi.importActual('./api'),
  getUserId: () => 'recovery-user',
  validateUser: vi.fn().mockResolvedValue({ valid: true, default_model: 'model' }),
  listSessions: vi.fn().mockResolvedValue({ sessions: [{ session_id: 'A', title: 'Session A' }, { session_id: 'B', title: 'Session B' }] }),
  listProjects: vi.fn().mockResolvedValue({ projects: [] }), listCustomModels: vi.fn().mockResolvedValue({ models: [] }),
  activateSession: vi.fn(), resumeRun: vi.fn(), getRunSnapshot: vi.fn(), streamChat: vi.fn(),
  getCapabilities: vi.fn().mockResolvedValue({ run_cancel_v1: true, job_cancel_v1: true }),
  cancelRun: vi.fn(), getRunControl: vi.fn(),
  visualizeGraph: vi.fn().mockRejectedValue(new Error('no graph')),
}));
import App from './main';
import { activateSession, cancelRun, getCapabilities, getRunControl, getRunSnapshot, resumeRun, streamChat, visualizeGraph } from './api';

const active = (content = 'A') => ({ model: 'model', has_graph: false,
  messages: [{ id: 'assistant:r', role: 'assistant' as const, content }],
  active_run: { run_id: 'r', status: 'running', last_event_index: 100 } });
const complete = () => ({ ...active('ABC'), active_run: null });
function navigate(id: string) {
  act(() => { window.history.pushState({}, '', `/?session=${id}`); window.dispatchEvent(new PopStateEvent('popstate')); });
}
let restoreStorage = () => {};
beforeEach(() => {
  sessionStorage.clear(); localStorage.clear(); window.history.replaceState({}, '', '/?session=A');
  vi.mocked(activateSession).mockReset(); vi.mocked(resumeRun).mockReset(); vi.mocked(getRunSnapshot).mockReset();
  vi.mocked(getCapabilities).mockReset().mockResolvedValue({ run_cancel_v1: true, job_cancel_v1: true });
  vi.mocked(getRunControl).mockReset().mockResolvedValue({ run_id: 'r', start_request_id: null, registration_status: 'registered', run_status: 'completed',
    cancel_status: 'none', execution_stopped: true, terminal_persisted: true, can_send: true, active_run: null, revision: 3 });
  vi.mocked(visualizeGraph).mockClear();
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
    await waitFor(() => expect(container.querySelector('.workspace-context strong')?.textContent).toBe('Session B'));
    vi.mocked(activateSession).mockResolvedValue(complete()); navigate('A');
    await waitFor(() => expect(container.querySelector('.workspace-context strong')?.textContent).toBe('Session A'));
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
});

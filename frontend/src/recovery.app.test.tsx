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
  visualizeGraph: vi.fn().mockRejectedValue(new Error('no graph')),
}));
import App from './main';
import { activateSession, getRunSnapshot, resumeRun, streamChat } from './api';

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
  vi.mocked(resumeRun).mockImplementation(async (_s, _r, _c, _h, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true });
  }));
  vi.mocked(getRunSnapshot).mockResolvedValue({ session_id: 'A', run_id: 'r', status: 'completed', messages: complete().messages,
    active_run: null, pending_feedback: null, snapshot_complete: true, resume_cursor: null });
});
afterEach(() => { cleanup(); restoreStorage(); restoreStorage = () => {}; });

describe('actual App recovery', () => {
  it('keeps receiving and pausing usable when recovery storage writes fail', async () => {
    const write = Storage.prototype.setItem;
    const storageSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function(this: Storage, key: string, value: string) {
      if (key.startsWith('igraph-active-run:') || key.startsWith('igraph-run-watch-paused:')) throw new DOMException('Quota exceeded', 'QuotaExceededError');
      return write.call(this, key, value);
    });
    restoreStorage = () => storageSpy.mockRestore();
    vi.mocked(activateSession).mockResolvedValue(active());
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '暂停接收（Agent 将继续运行）' }));
    expect(await screen.findByRole('button', { name: '继续接收' })).toBeDefined();
    expect(vi.mocked(resumeRun).mock.calls[0][4].aborted).toBe(true);
  });
  it('ignores a persisted cursor on refresh and rebuilds from -1', async () => {
    sessionStorage.setItem('igraph-active-run:v2:recovery-user:A', JSON.stringify({ runId: 'r', lastEventIndex: 99 }));
    vi.mocked(activateSession).mockResolvedValue(active());
    render(<App />);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledTimes(1));
    expect(vi.mocked(resumeRun).mock.calls[0][2]).toBe(-1);
  });
  it('restores completed text and unlocks the paused session after switching back', async () => {
    vi.mocked(activateSession).mockImplementation(async id => id === 'A' ? active() : { ...active(''), active_run: null });
    const { container } = render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '暂停接收（Agent 将继续运行）' }));
    await screen.findByRole('button', { name: '继续接收' });
    navigate('B');
    await waitFor(() => expect(container.querySelector('.workspace-context strong')?.textContent).toBe('Session B'));
    vi.mocked(activateSession).mockResolvedValue(complete()); navigate('A');
    await waitFor(() => expect(container.querySelector('.workspace-context strong')?.textContent).toBe('Session A'));
    await waitFor(() => expect(container.querySelector('.message-content')?.textContent).toBe('ABC'));
    expect((container.querySelector('.send-button') as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByRole('button', { name: '继续接收' })).toBeNull();
  });
  it('continues a paused observer and applies a complete snapshot', async () => {
    vi.mocked(activateSession).mockResolvedValue(active());
    const { container } = render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '暂停接收（Agent 将继续运行）' }));
    vi.mocked(resumeRun).mockImplementationOnce(async (_s, _r, _c, handlers) => {
      handlers.onBatch?.({ protocol_version: 2, session_id: 'A', run_id: 'r', event_index: 0, events: [{ type: 'done', content: 'ABC' }] });
    });
    fireEvent.click(await screen.findByRole('button', { name: '继续接收' }));
    await waitFor(() => expect(container.querySelector('.message-content')?.textContent).toBe('ABC'));
    await waitFor(() => expect((container.querySelector('.send-button') as HTMLButtonElement).disabled).toBe(false));
    expect(resumeRun).toHaveBeenCalledTimes(2);
  });
});

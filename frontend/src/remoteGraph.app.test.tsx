import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
vi.mock('react-dom/client', () => ({ createRoot: () => ({ render: () => {} }) }));
vi.mock('./components/GraphWorkspace', () => ({ GraphWorkspace: () => null }));
vi.mock('./api', async () => ({ ...await vi.importActual('./api'),
  validateUser: vi.fn().mockResolvedValue({ valid: true, default_model: 'model' }),
  listSessions: vi.fn().mockResolvedValue({ sessions: [{ session_id: 's', title: 'Analysis', active_dataset_id: 'local' }] }),
  listProjects: vi.fn().mockResolvedValue({ projects: [] }), listModels: vi.fn().mockResolvedValue({ default_model: 'model', models: [] }),
  listProjectDatasets: vi.fn().mockResolvedValue({ datasets: [{ dataset_id: 'local', name: 'Local A', original_filename: 'a.json', status: 'ready' }] }),
  listServerDatasets: vi.fn().mockResolvedValue({ datasets: [] }),
  getCapabilities: vi.fn().mockResolvedValue({ run_cancel_v1: false }),
  activateSession: vi.fn(), visualizeGraph: vi.fn().mockRejectedValue(new Error('no graph')),
  listRemoteGraphs: vi.fn(), selectRemoteGraph: vi.fn(), setSessionDataset: vi.fn(),
  createSession: vi.fn().mockResolvedValue({ session_id: 'new-s', model: 'model', active_dataset_id: null }),
}));
import App from './main';
import { I18nProvider } from './i18n';
import { USER_KEY, activateSession, listRemoteGraphs, selectRemoteGraph, setSessionDataset, createSession } from './api';
const connection = { connection_id: 'c', revision: 2, name: 'Cora', kind: 'neo4j' as const, database: 'neo4j', uri: 'bolt+s://db', username: 'reader', has_password: true };
const binding = { connection_id: 'c', revision: 2, name: 'Cora', kind: 'neo4j' as const, database: 'neo4j' };
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); localStorage.setItem(USER_KEY, 'alice');
  window.history.replaceState({}, '', '/?session=s'); vi.clearAllMocks();
  vi.mocked(activateSession).mockResolvedValue({ model: 'model', active_dataset_id: 'local', remote_graph: null, messages: [], active_run: null });
  vi.mocked(listRemoteGraphs).mockReset().mockResolvedValue({ connections: [connection] });
  vi.mocked(selectRemoteGraph).mockResolvedValue({ remote_graph: binding });
});
afterEach(cleanup);
it('selects remote B while retaining local A and restores both after remount', async () => {
  const first = render(<I18nProvider initialLanguage="en"><App /></I18nProvider>);
  await screen.findByRole('button', { name: /Local A/ });
  await userEvent.click(screen.getByRole('button', { name: 'Remote databases' }));
  await userEvent.click(await screen.findByRole('button', { name: 'Use Cora' }));
  await waitFor(() => expect(selectRemoteGraph).toHaveBeenCalledWith('s', 'c'));
  expect(screen.getByRole('button', { name: /Local A/ })).toBeTruthy();
  expect(setSessionDataset).not.toHaveBeenCalled();
  first.unmount();
  vi.mocked(activateSession).mockResolvedValue({ model: 'model', active_dataset_id: 'local', remote_graph: binding, messages: [], active_run: null });
  render(<I18nProvider initialLanguage="en"><App /></I18nProvider>);
  await screen.findByRole('button', { name: /Local A/ });
  await screen.findByText('Remote: Cora · neo4j');
  expect(setSessionDataset).not.toHaveBeenCalled();
});
it('creates an empty conversation for remote selection without requiring a local upload', async () => {
  window.history.replaceState({}, '', '/?new=1');
  render(<I18nProvider initialLanguage="en"><App /></I18nProvider>);
  await userEvent.click(await screen.findByRole('button', { name: 'Remote databases' }));
  await userEvent.click(await screen.findByRole('button', { name: 'Use Cora' }));
  await waitFor(() => expect(selectRemoteGraph).toHaveBeenCalledWith('new-s', 'c'));
  expect(createSession).toHaveBeenCalled(); expect(setSessionDataset).not.toHaveBeenCalled();
});

it('reconciles an edited selected connection while preserving historical query sources', async () => {
  const historic = { success: true, query_id: 'old-query', source: binding, columns: ['count'], rows: [[7]], returned_rows: 1, truncated: false, preview_truncated: false, elapsed_ms: 1 };
  vi.mocked(activateSession).mockResolvedValue({ model: 'model', active_dataset_id: 'local', remote_graph: binding,
    messages: [{ id: 'old-answer', role: 'assistant', content: 'Previous answer', tools: [{ tool: 'query_remote_graph', done: true, result: historic }] }], active_run: null });
  vi.mocked(listRemoteGraphs).mockResolvedValue({ connections: [{ ...connection, name: 'New target', database: 'other', revision: 3 }] });
  render(<I18nProvider initialLanguage="en"><App /></I18nProvider>);
  await screen.findByText('Remote: Cora · neo4j');
  await userEvent.click(screen.getByRole('button', { name: 'Remote databases' }));
  await screen.findByText('Remote: New target · other');
  expect(screen.getByText('Cora · neo4j')).toBeTruthy();
  expect(screen.getByRole('button', { name: /Local A/ })).toBeTruthy();
});

it.each(['loaded', 'pending', 'error'])('clears remote panel state on account switch (%s)', async mode => {
  let resolveOld!: (value: { connections: typeof connection[] }) => void;
  if (mode === 'pending') vi.mocked(listRemoteGraphs).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
  if (mode === 'error') vi.mocked(listRemoteGraphs).mockRejectedValueOnce({ code: 'REMOTE_GRAPH_UNREACHABLE' });
  render(<I18nProvider initialLanguage="en"><App /></I18nProvider>);
  await userEvent.click(await screen.findByRole('button', { name: 'Remote databases' }));
  if (mode === 'loaded') await screen.findByRole('button', { name: 'Use Cora' });
  if (mode === 'error') await screen.findByRole('alert');
  act(() => {
    localStorage.setItem(USER_KEY, 'bob');
    window.dispatchEvent(new StorageEvent('storage', { key: USER_KEY, newValue: 'bob', storageArea: localStorage }));
  });
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Remote databases' })).toBeNull());
  await userEvent.click(screen.getAllByRole('button', { name: 'New task' })[0]);
  // Keep B's own list pending: opening it must not expose any stale list/error from A.
  let resolveNew!: (value: { connections: typeof connection[] }) => void;
  vi.mocked(listRemoteGraphs).mockImplementationOnce(() => new Promise(resolve => { resolveNew = resolve; }));
  await userEvent.click(await screen.findByRole('button', { name: 'Remote databases' }));
  const dialog = screen.getByRole('dialog', { name: 'Remote databases' });
  expect(within(dialog).queryByText('Cora')).toBeNull();
  expect(within(dialog).queryByRole('alert')).toBeNull();
  if (mode === 'pending') await act(async () => resolveOld({ connections: [connection] }));
  expect(within(dialog).queryByText('Cora')).toBeNull();
  await act(async () => resolveNew({ connections: [] }));
  expect((within(dialog).getByRole('button', { name: 'New connection' }) as HTMLButtonElement).disabled).toBe(false);
});

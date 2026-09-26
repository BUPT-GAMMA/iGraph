import { cleanup, render, screen, waitFor } from '@testing-library/react';
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
  vi.mocked(listRemoteGraphs).mockResolvedValue({ connections: [connection] });
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

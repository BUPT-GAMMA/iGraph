import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { I18nProvider } from '../i18n';
import { RemoteGraphConnections } from './RemoteGraphConnections';
import { saveRemoteGraph, testRemoteGraph, deleteRemoteGraph } from '../api';
vi.mock('../api', async () => ({ ...await vi.importActual('../api'), saveRemoteGraph: vi.fn(), testRemoteGraph: vi.fn(), deleteRemoteGraph: vi.fn() }));
const connection = { connection_id: 'c', revision: 2, name: 'Cora', kind: 'neo4j' as const, uri: 'bolt+s://db', database: 'neo4j', username: 'reader', has_password: true };
beforeEach(() => { vi.clearAllMocks(); localStorage.setItem('igraph_language', 'en'); });
afterEach(cleanup);
const mount = (selectedId: string | null = null) => {
  const onSelect = vi.fn(), onChanged = vi.fn();
  render(<I18nProvider initialLanguage="en"><RemoteGraphConnections connections={[connection]} selectedId={selectedId} onSelect={onSelect} onChanged={onChanged} onClose={vi.fn()} /></I18nProvider>);
  return { onSelect, onChanged };
};
it('selects and unselects a remote source independently', async () => {
  const { onSelect } = mount('c');
  await userEvent.click(screen.getByRole('button', { name: 'Use Cora' }));
  expect(onSelect).toHaveBeenCalledWith('c');
  await userEvent.click(screen.getByRole('button', { name: 'Disconnect from conversation' }));
  expect(onSelect).toHaveBeenLastCalledWith(null);
});
it('tests unsaved input without saving and retains form on failure', async () => {
  mount();
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'New' } });
  fireEvent.change(screen.getByLabelText('URI'), { target: { value: 'bolt+s://new' } });
  fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'reader' } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'secret' } });
  vi.mocked(testRemoteGraph).mockRejectedValue({ code: 'REMOTE_GRAPH_AUTH_FAILED' });
  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
  await screen.findByRole('alert');
  expect(testRemoteGraph).toHaveBeenCalledWith(expect.objectContaining({ name: 'New', password: 'secret' }));
  expect(saveRemoteGraph).not.toHaveBeenCalled();
  expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('secret');
});
it('edits without exposing a saved password, preserves blank password and deletes by revision', async () => {
  const { onChanged } = mount();
  await userEvent.click(screen.getByRole('button', { name: 'Edit Cora' }));
  expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('');
  vi.mocked(saveRemoteGraph).mockResolvedValue({ connection });
  await userEvent.click(screen.getByRole('button', { name: 'Save connection' }));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  expect(saveRemoteGraph).toHaveBeenCalledWith(expect.objectContaining({ connection_id: 'c', revision: 2, password: '' }));
  await userEvent.click(screen.getByRole('button', { name: 'Delete Cora' }));
  expect(deleteRemoteGraph).toHaveBeenCalledWith('c', 2);
});

it.each([
  ['unknown', 'Database read-only permissions are unknown'],
  ['not_read_only', 'This account has write or administrative permissions'],
  ['verified_read_only', 'Database read-only permissions verified'],
] as const)('reports connection usability separately from %s permissions', async (status, text) => {
  mount();
  vi.mocked(testRemoteGraph).mockResolvedValue({ success: true, connection_usable: true, database_read_only: status, database: 'neo4j' });
  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
  expect((await screen.findByRole('status')).textContent).toContain('Connection usable.');
  expect(screen.getByRole('status').textContent).toContain(text);
  expect(saveRemoteGraph).not.toHaveBeenCalled();
});

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { I18nProvider } from '../i18n';
import { RemoteGraphConnections } from './RemoteGraphConnections';
import { listRemoteGraphTypes, saveRemoteGraph, testRemoteGraph, deleteRemoteGraph } from '../api';
vi.mock('../api', async () => ({ ...await vi.importActual('../api'), listRemoteGraphTypes: vi.fn(), saveRemoteGraph: vi.fn(), testRemoteGraph: vi.fn(), deleteRemoteGraph: vi.fn() }));
const types = [{ kind: 'neo4j' as const, display_name: 'Neo4j', query_language: 'cypher', uri_example: 'bolt+s://db.example.com:7687', target_label: 'Database', username_required: true, support_status: 'verified' },
  { kind: 'falkordb' as const, display_name: 'FalkorDB', query_language: 'cypher', uri_example: 'rediss://db.example.com:6379', target_label: 'Graph', username_required: false, support_status: 'experimental' }];
const connection = { connection_id: 'c', revision: 2, name: 'Cora', kind: 'neo4j' as const, uri: 'bolt+s://db', database: 'neo4j', username: 'reader', has_password: true };
beforeEach(() => { vi.clearAllMocks(); localStorage.setItem('igraph_language', 'en'); vi.mocked(listRemoteGraphTypes).mockResolvedValue({ types }); });
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
  await userEvent.click(screen.getByRole('button', { name: 'New connection' }));
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
  await userEvent.click(screen.getByRole('button', { name: 'Save and use' }));
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
  await userEvent.click(screen.getByRole('button', { name: 'New connection' }));
  vi.mocked(testRemoteGraph).mockResolvedValue({ success: true, connection_usable: true, database_read_only: status, database: 'neo4j' });
  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
  expect((await screen.findByRole('status')).textContent).toContain('Connection usable.');
  expect(screen.getByRole('status').textContent).toContain(text);
  expect(saveRemoteGraph).not.toHaveBeenCalled();
  expect((screen.getByRole('button', { name: 'Save and use' }) as HTMLButtonElement).disabled).toBe(false);
});

it('starts with an empty list and opens a separate form', async () => {
  render(<I18nProvider initialLanguage="en"><RemoteGraphConnections connections={[]} selectedId={null} onSelect={vi.fn()} onClose={vi.fn()} /></I18nProvider>);
  expect(screen.queryByLabelText('Name')).toBeNull();
  expect(screen.getByText('No connections yet')).toBeTruthy();
  await userEvent.click(screen.getByRole('button', { name: 'New connection' }));
  expect(screen.getByLabelText('Name')).toBeTruthy();
  await userEvent.click(screen.getByRole('button', { name: 'Back to connections' }));
  expect(screen.queryByLabelText('Name')).toBeNull();
});
it('saves once and retries only binding after a binding failure', async () => {
  const { onSelect } = mount();
  onSelect.mockRejectedValueOnce({ code: 'REMOTE_GRAPH_UNREACHABLE' }).mockResolvedValue(undefined);
  vi.mocked(saveRemoteGraph).mockResolvedValue({ connection });
  await userEvent.click(screen.getByRole('button', { name: 'Edit Cora' }));
  await userEvent.click(screen.getByRole('button', { name: 'Save and use' }));
  expect(await screen.findByText(/Saved, not in use/)).toBeTruthy();
  await userEvent.click(screen.getByRole('button', { name: 'Retry use' }));
  await waitFor(() => expect(screen.queryByLabelText('Name')).toBeNull());
  expect(saveRemoteGraph).toHaveBeenCalledTimes(1);
  expect(onSelect).toHaveBeenCalledTimes(2);
  expect(onSelect).toHaveBeenLastCalledWith('c');
});
it('invalidates a successful test on edits and shows pending status', async () => {
  mount();
  await userEvent.click(screen.getByRole('button', { name: 'Edit Cora' }));
  let resolve!: (value: Awaited<ReturnType<typeof testRemoteGraph>>) => void;
  vi.mocked(testRemoteGraph).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
  expect(screen.getByRole('status').textContent).toContain('Testing');
  resolve({ success: true, connection_usable: true, database_read_only: 'unknown', database: 'neo4j' });
  await screen.findByText(/Connection usable/);
  fireEvent.change(screen.getByLabelText('URI'), { target: { value: 'bolt://changed' } });
  expect(screen.queryByText(/Connection usable/)).toBeNull();
});
it('does not bind after a pending save completes on an unmounted account panel', async () => {
  let resolve!: (value: Awaited<ReturnType<typeof saveRemoteGraph>>) => void;
  vi.mocked(saveRemoteGraph).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  const { onSelect, onChanged } = mount();
  await userEvent.click(screen.getByRole('button', { name: 'Edit Cora' }));
  await userEvent.click(screen.getByRole('button', { name: 'Save and use' }));
  cleanup();
  resolve({ connection });
  await new Promise(r => setTimeout(r, 0));
  expect(onSelect).not.toHaveBeenCalled(); expect(onChanged).not.toHaveBeenCalled();
});
it('prevents mutations during a run', () => {
  render(<I18nProvider initialLanguage="en"><RemoteGraphConnections connections={[connection]} selectedId="c" busy onSelect={vi.fn()} onClose={vi.fn()} /></I18nProvider>);
  for (const name of ['New connection', 'Use Cora', 'Edit Cora', 'Delete Cora', 'Disconnect from conversation']) {
    expect((screen.getByRole('button', { name }) as HTMLButtonElement).disabled).toBe(true);
  }
});
it.each(['REMOTE_GRAPH_UNREACHABLE', 'REMOTE_GRAPH_AUTH_FAILED', 'REMOTE_GRAPH_INVALID_CONFIG'])('shows a controlled connection failure: %s', async code => {
  mount();
  await userEvent.click(screen.getByRole('button', { name: 'Edit Cora' }));
  vi.mocked(testRemoteGraph).mockRejectedValue({ code, message: 'private-password' });
  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
  expect((await screen.findByRole('alert')).textContent).toContain(code);
  expect(screen.queryByText(/private-password/)).toBeNull();
  expect(saveRemoteGraph).not.toHaveBeenCalled();
});
it('does not bind into a different account while the old panel is still mounted', async () => {
  localStorage.setItem('igraph_user_id', 'alice');
  let resolve!: (value: Awaited<ReturnType<typeof saveRemoteGraph>>) => void;
  vi.mocked(saveRemoteGraph).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  const { onSelect, onChanged } = mount();
  await userEvent.click(screen.getByRole('button', { name: 'Edit Cora' }));
  await userEvent.click(screen.getByRole('button', { name: 'Save and use' }));
  localStorage.setItem('igraph_user_id', 'bob');
  resolve({ connection });
  await new Promise(r => setTimeout(r, 0));
  expect(onSelect).not.toHaveBeenCalled(); expect(onChanged).not.toHaveBeenCalled();
});
it('moves keyboard focus into the form and supports Escape to close', async () => {
  const onClose = vi.fn();
  render(<I18nProvider initialLanguage="en"><RemoteGraphConnections connections={[]} selectedId={null} onSelect={vi.fn()} onClose={onClose} /></I18nProvider>);
  await userEvent.click(screen.getByRole('button', { name: 'New connection' }));
  expect(document.activeElement).toBe(screen.getByLabelText('Name'));
  await userEvent.keyboard('{Escape}');
  expect(onClose).toHaveBeenCalled();
});
it('changes a new connection type and clears target, secret and tested state', async () => {
  mount();
  await userEvent.click(screen.getByRole('button', { name: 'New connection' }));
  await screen.findByRole('combobox', { name: 'Database type' });
  fireEvent.change(screen.getByLabelText('URI'), { target: { value: 'bolt://old' } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'old-secret' } });
  expect(screen.getByText(/Changing type will clear/)).toBeTruthy();
  vi.mocked(testRemoteGraph).mockResolvedValue({ success: true, connection_usable: true, database_read_only: 'unknown', database: 'neo4j' });
  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
  await screen.findByText(/Connection usable/);
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Database type' }), 'falkordb');
  expect(screen.queryByText(/Connection usable/)).toBeNull();
  expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('');
  expect((screen.getByLabelText('URI') as HTMLInputElement).value).toBe('');
  expect((screen.getByLabelText('Graph') as HTMLInputElement).value).toBe('');
  expect(screen.getByText(/Changing type clears/)).toBeTruthy();
});
it('ignores a late test result from a previous type', async () => {
  let resolve!: (value: Awaited<ReturnType<typeof testRemoteGraph>>) => void;
  vi.mocked(testRemoteGraph).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  mount();
  await userEvent.click(screen.getByRole('button', { name: 'New connection' }));
  await screen.findByRole('combobox', { name: 'Database type' });
  await userEvent.click(screen.getByRole('button', { name: 'Test connection' }));
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Database type' }), 'falkordb');
  resolve({ success: true, connection_usable: true, database_read_only: 'verified_read_only', database: 'neo4j' });
  await new Promise(r => setTimeout(r, 0));
  expect(screen.queryByText(/Connection usable/)).toBeNull();
  expect((screen.getByRole('button', { name: 'Test connection' }) as HTMLButtonElement).disabled).toBe(false);
});
it('keeps saved connection kind immutable and labels Falkor graph target', async () => {
  const falkor = { ...connection, kind: 'falkordb' as const, database: 'cora' };
  render(<I18nProvider initialLanguage="en"><RemoteGraphConnections connections={[falkor]} selectedId={null} onSelect={vi.fn()} onClose={vi.fn()} /></I18nProvider>);
  await userEvent.click(screen.getByRole('button', { name: 'Edit Cora' }));
  expect(screen.queryByRole('combobox', { name: 'Database type' })).toBeNull();
  expect(screen.getByText('FalkorDB')).toBeTruthy();
  expect(screen.getByLabelText('Graph')).toBeTruthy();
});
it('submits Falkor kind, permits optional username, and retries only binding', async () => {
  const { onSelect } = mount();
  onSelect.mockRejectedValueOnce(new Error('unavailable')).mockResolvedValue(undefined);
  const falkor = { ...connection, kind: 'falkordb' as const, database: 'cora' };
  vi.mocked(saveRemoteGraph).mockResolvedValue({ connection: falkor });
  await userEvent.click(screen.getByRole('button', { name: 'New connection' }));
  await userEvent.selectOptions(await screen.findByRole('combobox', { name: 'Database type' }), 'falkordb');
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Cora' } });
  fireEvent.change(screen.getByLabelText('URI'), { target: { value: 'rediss://db:6379' } });
  fireEvent.change(screen.getByLabelText('Graph'), { target: { value: 'cora' } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'secret' } });
  await userEvent.click(screen.getByRole('button', { name: 'Save and use' }));
  expect(await screen.findByText(/Saved, not in use/)).toBeTruthy();
  expect(saveRemoteGraph).toHaveBeenCalledWith(expect.objectContaining({ kind: 'falkordb', database: 'cora', username: '', password: 'secret' }));
  await userEvent.click(screen.getByRole('button', { name: 'Retry use' }));
  await waitFor(() => expect(onSelect).toHaveBeenCalledTimes(2));
  expect(saveRemoteGraph).toHaveBeenCalledTimes(1);
});
it('shows a retry action when the type catalog cannot load', async () => {
  vi.mocked(listRemoteGraphTypes).mockRejectedValueOnce(new Error('offline'));
  mount();
  await screen.findByRole('button', { name: 'Retry database types' });
  expect((screen.getByRole('button', { name: 'New connection' }) as HTMLButtonElement).disabled).toBe(true);
  await userEvent.click(screen.getByRole('button', { name: 'Retry database types' }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'New connection' }) as HTMLButtonElement).disabled).toBe(false));
});
it('uses only registered types when Neo4j is unavailable', async () => {
  let resolve!: (value: { types: typeof types }) => void;
  vi.mocked(listRemoteGraphTypes).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  mount();
  await userEvent.click(screen.getByRole('button', { name: 'New connection' }));
  const selector = await screen.findByRole('combobox', { name: 'Database type' }) as HTMLSelectElement;
  resolve({ types: [types[1]] });
  await waitFor(() => expect(selector.disabled).toBe(false));
  expect(selector.value).toBe('falkordb');
  expect(screen.getByLabelText('Graph')).toBeTruthy();
  expect(screen.queryByRole('option', { name: 'Neo4j' })).toBeNull();
});
it('disables type switching during an active run', async () => {
  const view = (busy: boolean) => <I18nProvider initialLanguage="en"><RemoteGraphConnections connections={[]} selectedId={null} busy={busy} onSelect={vi.fn()} onClose={vi.fn()} /></I18nProvider>;
  const { rerender } = render(view(false));
  await userEvent.click(screen.getByRole('button', { name: 'New connection' }));
  const selector = await screen.findByRole('combobox', { name: 'Database type' });
  rerender(view(true));
  expect((selector as HTMLSelectElement).disabled).toBe(true);
});

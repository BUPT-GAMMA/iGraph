import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../i18n';
import { Playground } from './Playground';
import { getPlayground, getPlaygroundConnection, listPlaygrounds, savePlaygroundConnection, sendPlaygroundTurn } from '../api';

vi.mock('../api', async importOriginal => ({ ...await importOriginal<typeof import('../api')>(),
  listPlaygrounds: vi.fn(), getPlayground: vi.fn(), getPlaygroundConnection: vi.fn(),
  sendPlaygroundTurn: vi.fn(), savePlaygroundConnection: vi.fn() }));

const connection = { revision: 0, configured: false, base_url: 'https://tokensave.pro/v1', model: 'gpt-5.4-mini' };
const state = { id: 'one', revision: 2, status: 'draft' as const, messages: [{ role: 'user' as const, text: 'Campus collaboration' }],
  plan: null, live_reply: '', error: null, confirmable: true, confirmed: false, result: null };

describe('Playground', () => {
  afterEach(cleanup);
  beforeEach(() => { vi.resetAllMocks(); vi.mocked(listPlaygrounds).mockResolvedValue({ scenarios: [] }); });

  it('requires the account connection before chat and removes the API key from the form after saving', async () => {
    vi.mocked(getPlaygroundConnection).mockResolvedValue(connection);
    vi.mocked(savePlaygroundConnection).mockResolvedValue({ ...connection, revision: 1, configured: true });
    render(<I18nProvider initialLanguage="en"><Playground onPending={vi.fn()} onReady={vi.fn()} onOpen={vi.fn()} /></I18nProvider>);
    const key = await screen.findByLabelText('API Key');
    expect(screen.queryByRole('textbox', { name: 'Message the scenario agent' })).toBeNull();
    expect((screen.getByLabelText('API URL') as HTMLInputElement).value).toBe(connection.base_url);
    fireEvent.change(key, { target: { value: 'personal-test-value' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await screen.findByRole('textbox', { name: 'Message the scenario agent' });
    expect(screen.queryByLabelText('API Key')).toBeNull();
    expect(savePlaygroundConnection).toHaveBeenCalledWith({ base_url: connection.base_url, api_key: 'personal-test-value', model: connection.model, revision: 0 });
  });

  it('confirms an existing proposal and opens the resulting project', async () => {
    vi.mocked(getPlaygroundConnection).mockResolvedValue({ ...connection, revision: 3, configured: true });
    vi.mocked(listPlaygrounds).mockResolvedValue({ scenarios: [{ id: 'one', title: 'Campus' }] });
    const project = { project_id: 'p', user_id: 'u', name: 'Campus' };
    vi.mocked(getPlayground).mockResolvedValueOnce(state).mockResolvedValue({ ...state, status: 'ready', confirmable: false,
      result: { project, dataset_id: 'd', node_count: 100, edge_count: 170 } });
    vi.mocked(sendPlaygroundTurn).mockResolvedValue({ ...state, status: 'building', confirmed: true });
    const open = vi.fn();
    render(<I18nProvider initialLanguage="en"><Playground onPending={vi.fn()} onReady={vi.fn()} onOpen={open} /></I18nProvider>);
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm and build' }));
    await waitFor(() => expect(sendPlaygroundTurn).toHaveBeenCalledWith('one', expect.objectContaining({ confirm: true, revision: 2, connection_revision: 3 })));
    fireEvent.click(await screen.findByRole('button', { name: 'Open for analysis' }));
    expect(open).toHaveBeenCalledWith(project);
  });
});

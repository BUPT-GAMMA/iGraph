import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider } from './i18n';

vi.mock('react-dom/client', () => ({ createRoot: () => ({ render: () => undefined }) }));
vi.mock('./components/GraphWorkspace', () => ({ GraphWorkspace: () => null }));
vi.mock('./api', async () => ({
  ...await vi.importActual('./api'),
  getUserId: () => 'help-user',
  validateUser: vi.fn().mockResolvedValue({ valid: true, default_model: 'model' }),
  listProjects: vi.fn().mockResolvedValue({ projects: [] }),
  listSessions: vi.fn().mockResolvedValue({ sessions: [] }),
  listCustomModels: vi.fn().mockResolvedValue({ models: [] }),
  getCapabilities: vi.fn().mockResolvedValue({ run_cancel_v1: true, job_cancel_v1: true }),
  listProjectDatasets: vi.fn().mockResolvedValue({ datasets: [], default_dataset_id: null }),
}));

import App from './main';

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear();
  window.history.replaceState({}, '', '/');
});
afterEach(cleanup);

describe('actual App help destination', () => {
  it('opens help as an independent drawer without leaving the workspace', async () => {
    render(<I18nProvider initialLanguage="zh"><App /></I18nProvider>);
    const entry = await screen.findByRole('button', { name: '帮助' });
    expect(screen.getByRole('heading', { name: '开始新的图分析' })).toBeTruthy();
    fireEvent.click(entry);
    expect(await screen.findByRole('heading', { name: '帮助与能力清单' })).toBeTruthy();
    expect(screen.getByRole('dialog', { name: '帮助与能力清单' })).toBeTruthy();
    expect(screen.getByRole('tabpanel', { name: '使用流程' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: '开始新的图分析' })).toBeTruthy();
    expect(entry.getAttribute('aria-current')).toBe('page');
    expect(window.location.search).toBe('');
    fireEvent.click(screen.getByRole('button', { name: '关闭帮助' }));
    await waitFor(() => expect(screen.queryByRole('heading', { name: '帮助与能力清单' })).toBeNull());
    expect(window.location.search).toBe('');
    expect(entry.getAttribute('aria-current')).toBeNull();
  });

  it('restores a direct help URL and follows browser navigation into and out of help', async () => {
    window.history.replaceState({}, '', '/?view=help');
    render(<I18nProvider initialLanguage="en"><App /></I18nProvider>);
    expect(await screen.findByRole('heading', { name: 'Help & capabilities' })).toBeTruthy();
    expect(screen.getByRole('dialog', { name: 'Help & capabilities' })).toBeTruthy();
    expect(screen.getByRole('tabpanel', { name: 'Usage pipeline' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Help' }).getAttribute('aria-current')).toBe('page');
    window.history.replaceState({}, '', '/');
    fireEvent.popState(window);
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Help & capabilities' })).toBeNull());
    window.history.replaceState({}, '', '/?view=help');
    fireEvent.popState(window);
    expect(await screen.findByRole('heading', { name: 'Help & capabilities' })).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: /Graph learning models/ }));
    expect(screen.getByRole('heading', { name: 'GCN' })).toBeTruthy();
  });
});

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AuthRoot } from './AuthRoot';
import { AUTH_ACCOUNT_CHANGED_EVENT, AUTH_EXPIRED_EVENT, USER_KEY } from '../api';
import { I18nProvider } from '../i18n';

afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); window.history.replaceState({}, '', '/'); });
const show = (workspace = vi.fn((user: { user_id: string }, signOut: () => Promise<void>) => <section key={user.user_id}><p>Workspace: {user.user_id}</p><button onClick={() => void signOut()}>Exit</button></section>)) => {
  render(<I18nProvider initialLanguage="zh"><AuthRoot>{workspace}</AuthRoot></I18nProvider>);
  return workspace;
};

describe('server verified workspace boundary', () => {
  it('does not mount a workspace just because an identity exists in storage', async () => {
    localStorage.setItem(USER_KEY, 'admin');
    let resolve!: (value: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(done => { resolve = done; })));
    const workspace = show();
    expect(workspace).not.toHaveBeenCalled();
    await act(async () => { resolve(new Response('{}', { status: 401 })); });
    expect(screen.getByLabelText('用户名')).toBeTruthy();
    expect(workspace).not.toHaveBeenCalled();
    expect(localStorage.getItem(USER_KEY)).toBe('');
  });

  it('uses the cookie identity and clears another user’s deep link', async () => {
    localStorage.setItem(USER_KEY, 'old-user'); window.history.replaceState({}, '', '/?session=private-old-session');
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ user_id: 'alice' })));
    vi.stubGlobal('fetch', fetcher); show();
    await waitFor(() => expect(screen.getByText('Workspace: alice')).toBeTruthy());
    expect(localStorage.getItem(USER_KEY)).toBe('alice');
    expect(window.location.search).toBe('');
    expect(fetcher.mock.calls[0][1].credentials).toBe('include');
  });

  it('removes all workspace content when authentication expires', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ user_id: 'alice' }))));
    show(); await waitFor(() => expect(screen.getByText('Workspace: alice')).toBeTruthy());
    act(() => { window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT)); });
    expect(screen.queryByText('Workspace: alice')).toBeNull();
    expect(screen.getByLabelText('用户名')).toBeTruthy();
    expect(screen.getByRole('status').textContent).toContain('登录已过期');
  });

  it('revalidates the server session when another tab changes accounts', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ user_id: 'alice' }))).mockResolvedValueOnce(new Response(JSON.stringify({ user_id: 'bob' })));
    vi.stubGlobal('fetch', fetcher); show();
    await waitFor(() => expect(screen.getByText('Workspace: alice')).toBeTruthy());
    act(() => { localStorage.setItem(USER_KEY, 'bob'); window.dispatchEvent(new StorageEvent('storage', { key: USER_KEY, storageArea: localStorage })); });
    await waitFor(() => expect(screen.getByText('Workspace: bob')).toBeTruthy());
    expect(screen.queryByText('Workspace: alice')).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('revalidates an account mismatch and signs out through the server', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ user_id: 'alice' }))).mockResolvedValueOnce(new Response(JSON.stringify({ user_id: 'bob' }))).mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetcher); show();
    await waitFor(() => expect(screen.getByText('Workspace: alice')).toBeTruthy());
    act(() => { window.dispatchEvent(new Event(AUTH_ACCOUNT_CHANGED_EVENT)); });
    await waitFor(() => expect(screen.getByText('Workspace: bob')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Exit' }));
    await waitFor(() => expect(screen.getByLabelText('用户名')).toBeTruthy());
    expect(fetcher.mock.calls[2][0]).toBe('/api/auth/logout');
    expect(localStorage.getItem(USER_KEY)).toBe('');
  });
});

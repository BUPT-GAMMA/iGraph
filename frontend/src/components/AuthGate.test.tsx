import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { AuthGate, useAccount } from './AuthGate';
import { ApiError, getAuthSession, setAuthSession, signIn, signOut, signUp } from '../api';
import { I18nProvider } from '../i18n';

vi.mock('../api', async () => ({ ...await vi.importActual('../api'), getAuthSession: vi.fn(),
  signIn: vi.fn(), signUp: vi.fn(), signOut: vi.fn(), setAuthSession: vi.fn() }));
const session = { user: { user_id: 'u_alice', username: 'alice' }, csrf_token: 'csrf-test' };
function Workspace() {
  const account = useAccount()!;
  return <div>private-workspace-{account.username}<button onClick={account.logout}>退出</button></div>;
}
function mount() { return render(<I18nProvider><AuthGate><Workspace /></AuthGate></I18nProvider>); }
beforeEach(() => {
  vi.clearAllMocks(); localStorage.setItem('igraph-language', 'zh');
  vi.mocked(getAuthSession).mockRejectedValue(new ApiError(401, 'Sign in'));
});
afterEach(cleanup);

it('guards the workspace until login and clears it on logout', async () => {
  vi.mocked(signIn).mockResolvedValue(session); vi.mocked(signOut).mockResolvedValue({});
  mount();
  expect(screen.queryByText(/private-workspace/)).toBeNull();
  await screen.findByRole('heading', { name: '登录 iGraph' });
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'alice' } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'test-password' } });
  fireEvent.click(screen.getByRole('button', { name: '登录' }));
  await screen.findByText('private-workspace-alice');
  expect(signIn).toHaveBeenCalledWith('alice', 'test-password');
  expect(setAuthSession).toHaveBeenCalledWith(session);
  fireEvent.click(screen.getByText('退出'));
  await screen.findByRole('heading', { name: '登录 iGraph' });
  expect(screen.queryByText(/private-workspace/)).toBeNull();
  expect(setAuthSession).toHaveBeenLastCalledWith(null);
});

it('validates registration confirmation and enters the new workspace', async () => {
  vi.mocked(signUp).mockResolvedValue(session);
  mount();
  fireEvent.click(await screen.findByText('还没有账户？立即注册'));
  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'alice' } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'test-password' } });
  fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'different-password' } });
  fireEvent.click(screen.getByRole('button', { name: '注册并进入' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '两次密码不一致。');
  expect(signUp).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'test-password' } });
  fireEvent.click(screen.getByRole('button', { name: '注册并进入' }));
  await screen.findByText('private-workspace-alice');
});

it('restores server identity and unmounts stale tabs without storing credentials', async () => {
  vi.mocked(getAuthSession).mockResolvedValue(session);
  mount(); await screen.findByText('private-workspace-alice');
  await act(async () => window.dispatchEvent(new StorageEvent('storage', { key: 'igraph-auth-event', newValue: 'changed' })));
  await waitFor(() => expect(screen.queryByText(/private-workspace/)).toBeNull());
  expect(JSON.stringify(localStorage)).not.toContain('csrf-test');
});

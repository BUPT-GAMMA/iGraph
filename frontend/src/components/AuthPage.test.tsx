import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AuthPage } from './AuthPage';
import { I18nProvider } from '../i18n';

afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); });
const show = (authenticated = vi.fn()) => render(<I18nProvider initialLanguage="zh"><AuthPage onAuthenticated={authenticated} /></I18nProvider>);

describe('password account form', () => {
  it('does not submit registration when passwords differ', () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher); show();
    fireEvent.click(screen.getByRole('button', { name: '注册' }));
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText('密码', { exact: true }), { target: { value: 'password123' } });
    fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'password456' } });
    fireEvent.click(screen.getByRole('button', { name: '创建账户' }));
    expect(screen.getByRole('alert').textContent).toContain('两次输入的密码不一致');
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('registers with a normalized username and cookie credentials', async () => {
    const authenticated = vi.fn();
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ user_id: 'alice', is_admin: false })));
    vi.stubGlobal('fetch', fetcher); show(authenticated);
    fireEvent.click(screen.getByRole('button', { name: '注册' }));
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'ALICE' } });
    fireEvent.change(screen.getByLabelText('密码', { exact: true }), { target: { value: 'password123' } });
    fireEvent.change(screen.getByLabelText('确认密码'), { target: { value: 'password123' } });
    expect(screen.getByLabelText('密码', { exact: true }).getAttribute('autocomplete')).toBe('new-password');
    fireEvent.click(screen.getByRole('button', { name: '创建账户' }));
    await waitFor(() => expect(authenticated).toHaveBeenCalledWith({ user_id: 'alice', is_admin: false }));
    const [path, request] = fetcher.mock.calls[0];
    expect(path).toBe('/api/auth/register');
    expect(request.credentials).toBe('include');
    expect(JSON.parse(request.body)).toEqual({ username: 'alice', password: 'password123' });
    expect(new Headers(request.headers).has('X-User-ID')).toBe(false);
  });

  it('shows an actionable login error and allows another attempt', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('{}', { status: 401 }));
    vi.stubGlobal('fetch', fetcher); show();
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText('密码', { exact: true }), { target: { value: 'password123' } });
    fireEvent.click(screen.getByRole('button', { name: '登录并进入工作区' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('用户名或密码不正确'));
    expect((screen.getByRole('button', { name: '登录并进入工作区' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('toggles password visibility without submitting the form', () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher); show();
    expect(screen.getByLabelText('密码', { exact: true }).getAttribute('type')).toBe('password');
    fireEvent.click(screen.getByRole('button', { name: '显示密码' }));
    expect(screen.getByLabelText('密码', { exact: true }).getAttribute('type')).toBe('text');
    expect(screen.getByRole('button', { name: '隐藏密码' }).getAttribute('aria-pressed')).toBe('true');
    expect(fetcher).not.toHaveBeenCalled();
  });
});

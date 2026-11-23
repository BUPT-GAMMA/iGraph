import { createContext, useContext, useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { ApiError, changePassword, getAuthSession, setAuthSession, signIn, signOut, signUp, type AuthSession } from '../api';
import { useI18n } from '../i18n';
import { BrandLogo } from './Icon';
import './AuthGate.css';

type AccountContext = { username: string; logout: () => Promise<void>; openPassword: () => void };
const Account = createContext<AccountContext | null>(null);
export const useAccount = () => useContext(Account);

export function AuthGate({ children }: { children: ReactNode }) {
  const { t, language, setLanguage } = useI18n();
  const [session, setSession] = useState<AuthSession | null>(null);
  const [checking, setChecking] = useState(true);
  const [registering, setRegistering] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [newPassword, setNewPassword] = useState('');

  function accept(next: AuthSession | null) {
    if (!next) window.history.replaceState({}, '', window.location.pathname);
    setAuthSession(next); setSession(next); setPassword(''); setConfirmation(''); setNewPassword('');
  }
  useEffect(() => {
    let active = true;
    getAuthSession().then(value => { if (active) accept(value); }).catch(cause => {
      if (active && !(cause instanceof ApiError && cause.status === 401)) setError(t('Could not connect. Try again.', '连接失败，请稍后重试。'));
    }).finally(() => { if (active) setChecking(false); });
    const expired = () => { accept(null); setError(t('Please sign in again.', '登录已失效或账户已切换，请重新登录。')); };
    window.addEventListener('igraph-auth-expired', expired);
    // Another tab signing in/out must unmount this workspace before another request.
    const changed = (event: StorageEvent) => { if (event.key === 'igraph-auth-event') expired(); };
    window.addEventListener('storage', changed);
    return () => { active = false; window.removeEventListener('igraph-auth-expired', expired); window.removeEventListener('storage', changed); };
  }, []);

  function announce() { try { localStorage.setItem('igraph-auth-event', String(Date.now())); } catch { /* optional cross-tab signal */ } }
  async function submit(event: FormEvent) {
    event.preventDefault(); setError('');
    if (registering && password !== confirmation) { setError(t('Passwords do not match.', '两次密码不一致。')); return; }
    setBusy(true);
    try { accept(await (registering ? signUp : signIn)(username, password)); announce(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : t('Please try again.', '请稍后重试。')); }
    finally { setBusy(false); }
  }
  async function logout() {
    setError('');
    try { await signOut(); accept(null); announce(); setPasswordOpen(false); }
    catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) { accept(null); announce(); }
      else setError(t('Sign out failed. Please try again.', '退出失败，请重试。'));
    }
  }
  async function updatePassword(event: FormEvent) {
    event.preventDefault(); setError('');
    if (newPassword !== confirmation) { setError(t('Passwords do not match.', '两次新密码不一致。')); return; }
    setBusy(true);
    try { accept(await changePassword(password, newPassword)); setPasswordOpen(false); announce(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : t('Please try again.', '请重试。')); }
    finally { setBusy(false); }
  }

  if (checking) return <main className="auth-page"><div role="status" className="auth-card"><BrandLogo /><p>{t('Restoring your workspace…', '正在恢复你的工作区…')}</p></div></main>;
  if (session) return <Account.Provider value={{ username: session.user.username, logout, openPassword: () => { setError(''); setPasswordOpen(true); } }}>
    <div key={session.user.user_id} className="authenticated-workspace">{children}</div>
    {error && !passwordOpen && <div className="auth-session-error" role="alert">{error}<button onClick={() => setError('')}>{t('Close', '关闭')}</button></div>}
    {passwordOpen && <div className="auth-overlay"><section className="auth-card" role="dialog" aria-modal="true" aria-labelledby="password-title"><h2 id="password-title">{t('Change password', '修改密码')}</h2>
      <form onSubmit={updatePassword}><label>{t('Current password', '当前密码')}<input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required maxLength={128} /></label>
        <label>{t('New password', '新密码')}<input type="password" autoComplete="new-password" value={newPassword} onChange={e => setNewPassword(e.target.value)} required minLength={10} maxLength={128} /></label>
        <label>{t('Confirm password', '确认密码')}<input type="password" autoComplete="new-password" value={confirmation} onChange={e => setConfirmation(e.target.value)} required minLength={10} maxLength={128} /></label>
        {error && <p role="alert" className="auth-error">{error}</p>}<button className="auth-submit" disabled={busy}>{t('Save password', '保存密码')}</button>
        <button type="button" className="auth-switch" disabled={busy} onClick={() => { setPasswordOpen(false); setPassword(''); setConfirmation(''); setNewPassword(''); setError(''); }}>{t('Cancel', '取消')}</button>
      </form></section></div>}
  </Account.Provider>;
  return <main className="auth-page"><section className="auth-card">
    <div className="auth-heading"><span className="auth-brand"><BrandLogo /><strong>iGraph</strong></span><button className="auth-language" type="button" onClick={() => setLanguage(language === 'zh' ? 'en' : 'zh')}>{language === 'zh' ? 'EN' : '中文'}</button></div>
    <h1>{registering ? t('Create your account', '创建你的账户') : t('Welcome back', '登录 iGraph')}</h1>
    <form onSubmit={submit}>
      <label>{t('Username', '用户名')}<input autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} required minLength={registering ? 3 : 1} maxLength={32} pattern={registering ? '[A-Za-z0-9][A-Za-z0-9_.\\-]{2,31}' : undefined} /></label>
      {registering && <small>{t('3–32 letters, numbers, dots, underscores, or hyphens.', '3–32 位字母、数字、点、下划线或短横线。')}</small>}
      <label>{t('Password', '密码')}<input type="password" autoComplete={registering ? 'new-password' : 'current-password'} value={password} onChange={e => setPassword(e.target.value)} required minLength={registering ? 10 : 1} maxLength={128} /></label>
      {registering && <><small>{t('Use at least 10 characters.', '密码至少 10 个字符。')}</small><label>{t('Confirm password', '确认密码')}<input type="password" autoComplete="new-password" value={confirmation} onChange={e => setConfirmation(e.target.value)} required maxLength={128} /></label></>}
      {error && <p role="alert" className="auth-error">{error}</p>}
      <button className="auth-submit" disabled={busy}>{busy ? t('Please wait…', '请稍候…') : registering ? t('Create account', '注册并进入') : t('Sign in', '登录')}</button>
    </form>
    <button type="button" className="auth-switch" disabled={busy} onClick={() => { setRegistering(!registering); setPassword(''); setConfirmation(''); setError(''); }}>{registering ? t('Already have an account? Sign in', '已有账户？去登录') : t('New to iGraph? Create an account', '还没有账户？立即注册')}</button>
  </section></main>;
}

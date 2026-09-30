import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { AUTH_ACCOUNT_CHANGED_EVENT, AUTH_EXPIRED_EVENT, ApiError, getCurrentUser, getUserId, logoutUser, setUserId, USER_KEY, type AuthUser } from '../api';
import { useI18n } from '../i18n';
import { writeWorkspaceRoute } from '../navigation';
import { AuthPage } from './AuthPage';
import { BrandLogo } from './Icon';

/** Only a verified server session can mount the user's workspace. */
export function AuthRoot({ children }: { children: (user: AuthUser, signOut: () => Promise<void>) => ReactNode }) {
  const { t } = useI18n();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState('');
  const requestVersion = useRef(0);
  const lastIdentity = useRef(getUserId());
  const translation = useRef(t);
  translation.current = t;

  const acceptUser = useCallback((next: AuthUser) => {
    if (!next || typeof next.user_id !== 'string' || !next.user_id) throw new Error('Invalid account response');
    if (lastIdentity.current && lastIdentity.current !== next.user_id) writeWorkspaceRoute({ kind: 'home' }, true);
    lastIdentity.current = next.user_id;
    setUserId(next.user_id);
    setUser(next);
    setNotice('');
    setLoading(false);
  }, []);

  const restore = useCallback(async (blockWorkspace = false) => {
    const version = ++requestVersion.current;
    if (blockWorkspace) setLoading(true);
    try {
      const next = await getCurrentUser();
      if (version !== requestVersion.current) return;
      acceptUser(next);
    } catch (cause) {
      if (version !== requestVersion.current) return;
      if (cause instanceof ApiError && cause.status === 401) {
        setUserId('');
        setUser(null);
      } else {
        setNotice(translation.current('Could not connect to the server. Check your connection and try again.', '暂时无法连接服务器，请检查连接后重试。'));
        if (blockWorkspace) setUser(null);
      }
      setLoading(false);
    }
  }, [acceptUser]);

  useEffect(() => {
    void restore(true);
    const expired = () => {
      requestVersion.current += 1;
      setUserId('');
      setUser(null);
      setLoading(false);
      setNotice(translation.current('Your session has expired. Sign in to continue.', '登录已过期，请重新登录以继续。'));
    };
    const stored = (event: StorageEvent) => {
      if (event.storageArea === localStorage && (event.key === USER_KEY || event.key === null)) void restore(true);
    };
    const focus = () => { if (document.visibilityState === 'visible') void restore(); };
    const accountChanged = () => { void restore(true); };
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    window.addEventListener(AUTH_ACCOUNT_CHANGED_EVENT, accountChanged);
    window.addEventListener('storage', stored);
    window.addEventListener('focus', focus);
    document.addEventListener('visibilitychange', focus);
    return () => {
      requestVersion.current += 1;
      window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
      window.removeEventListener(AUTH_ACCOUNT_CHANGED_EVENT, accountChanged);
      window.removeEventListener('storage', stored);
      window.removeEventListener('focus', focus);
      document.removeEventListener('visibilitychange', focus);
    };
  }, [restore]);

  async function signOut() {
    await logoutUser();
    requestVersion.current += 1;
    setUserId('');
    setUser(null);
    setNotice('');
    writeWorkspaceRoute({ kind: 'home' }, true);
  }

  if (loading) return <main className="auth-restoring" aria-busy="true"><div className="auth-brand-mark"><BrandLogo /></div><span className="loader" /><p role="status">{t('Opening your workspace…', '正在打开你的工作区…')}</p></main>;
  if (!user) return <AuthPage notice={notice} onAuthenticated={next => { requestVersion.current += 1; acceptUser(next); }} />;
  return <>{children(user, signOut)}</>;
}

import { useEffect, useRef, useState, type FormEvent } from 'react';
import { authenticateUser, ApiError, type AuthUser } from '../api';
import { useI18n } from '../i18n';
import { BrandLogo, Icon } from './Icon';
import './AuthPage.css';

type Props = { onAuthenticated: (user: AuthUser) => void; notice?: string };

function GraphArtwork() {
  return <div className="auth-graph-art" aria-hidden="true">
    <svg viewBox="0 0 580 390" fill="none">
      <defs><radialGradient id="auth-glow"><stop stopColor="#3b82f6" stopOpacity=".15" /><stop offset="1" stopColor="#3b82f6" stopOpacity="0" /></radialGradient></defs>
      <ellipse cx="280" cy="215" rx="225" ry="175" fill="url(#auth-glow)" />
      <g className="auth-art-edges"><path d="M113 172 233 105 327 191 448 111M233 105 266 294 113 172 101 283M327 191 266 294 435 289 448 111M327 191 435 289M113 172 327 191M233 105 381 43M448 111 519 217M101 283 168 353M266 294 168 353M435 289 519 217" /></g>
      <g className="auth-art-satellites"><circle cx="381" cy="43" r="6" /><circle cx="519" cy="217" r="7" /><circle cx="168" cy="353" r="6" /><circle cx="101" cy="283" r="9" /></g>
      <g className="auth-art-node"><circle cx="113" cy="172" r="28" /><path d="M104 179v-14h18v14h-18Zm0-9h18m-12-5v14m6-14v14" /><circle cx="233" cy="105" r="24" /><path d="M225 109h16m-8-13v18m-8-13h16" /><circle cx="448" cy="111" r="25" /><ellipse cx="448" cy="105" rx="9" ry="3.5" /><path d="M439 105v12c0 5 18 5 18 0v-12m-18 6c0 5 18 5 18 0" /><circle cx="435" cy="289" r="25" /><path d="m427 293 5-9 5 6 7-11m-17 17h18" /><circle cx="266" cy="294" r="30" /><path d="M254 296h24m-24-7h24m-24 14h15" /></g>
      <g className="auth-art-center"><circle cx="327" cy="191" r="54" className="auth-art-halo" /><circle cx="327" cy="191" r="38" /><circle cx="315" cy="191" r="4" /><circle cx="337" cy="180" r="4" /><circle cx="337" cy="202" r="4" /><path d="m319 189 14-7m-14 11 14 7" /></g>
    </svg>
    <div className="auth-graph-caption"><span /><span>DATA</span><i /><span>RELATIONS</span><i /><span>INSIGHTS</span></div>
  </div>;
}

export function AuthPage({ onAuthenticated, notice = '' }: Props) {
  const { t, language, toggleLanguage } = useI18n();
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [invalidField, setInvalidField] = useState('');
  const [dark, setDark] = useState(() => localStorage.getItem('igraph-theme') === 'dark');
  const submission = useRef(false);
  const mounted = useRef(true);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    localStorage.setItem('igraph-theme', dark ? 'dark' : 'light');
  }, [dark]);

  function changeMode(next: 'login' | 'register') {
    if (pending || mode === next) return;
    setMode(next); setPassword(''); setConfirmation(''); setShowPassword(false); setError(''); setInvalidField('');
  }
  function changed() { setError(''); setInvalidField(''); }
  function invalid(field: string, message: string) { setInvalidField(field); setError(message); }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submission.current) return;
    const normalized = username.trim().toLowerCase();
    if (!/^[a-z0-9_-]{3,32}$/.test(normalized)) return invalid('username', t('Use 3–32 letters, numbers, underscores or hyphens for your username.', '用户名需为 3–32 位英文字母、数字、下划线或短横线。'));
    if (password.length < 8 || password.length > 128) return invalid('password', t('Your password must contain 8–128 characters.', '密码长度需为 8–128 位。'));
    if (mode === 'register' && confirmation !== password) return invalid('confirmation', t('The passwords do not match.', '两次输入的密码不一致。'));
    submission.current = true;
    setPending(true); setError(''); setInvalidField('');
    try {
      const user = await authenticateUser(mode, normalized, password);
      if (mounted.current) onAuthenticated(user);
    } catch (cause) {
      if (!mounted.current) return;
      const message = cause instanceof ApiError && cause.status === 401
        ? t('The username or password is incorrect.', '用户名或密码不正确。')
        : cause instanceof ApiError && cause.status === 409
          ? t('This username is already registered. Sign in or choose another one.', '该用户名已注册，请登录或换一个用户名。')
          : cause instanceof ApiError && cause.status === 429
            ? t('Too many attempts. Please try again shortly.', '尝试次数较多，请稍后再试。')
            : cause instanceof ApiError ? cause.message : t('Could not connect to the server. Check your connection and try again.', '暂时无法连接服务器，请检查连接后重试。');
      setError(message);
    } finally {
      submission.current = false;
      if (mounted.current) setPending(false);
    }
  }

  const feedback = error || notice;
  return <main className="auth-page">
    <section className="auth-story" aria-labelledby="auth-story-title">
      <a className="auth-brand" href="/" aria-label="iGraph"><span className="auth-brand-mark"><BrandLogo /></span><strong>iGraph<span>GRAPH INTELLIGENCE</span></strong></a>
      <div className="auth-story-copy"><span className="auth-eyebrow"><i />{t('YOUR GRAPH, YOUR WORKSPACE', '你的图谱 · 你的工作区')}</span><h1 id="auth-story-title">{t('Find the connections.', '发现数据中的关系。')}<br /><span>{t('See the possibilities.', '看见更多可能。')}</span></h1><p>{t('Connect your data, explore the relationships, and turn questions into meaningful insights.', '连接你的数据，探索隐藏的关系，\n让每一个问题都有迹可循。')}</p></div>
      <GraphArtwork />
      <div className="auth-story-footer"><span><Icon name="graph" size={16} />{t('Build your graphs', '构建知识图谱')}</span><span><Icon name="database" size={16} />{t('Connect databases', '连接图数据库')}</span><span><Icon name="spark" size={16} />{t('Explore with AI', '开启智能分析')}</span></div>
    </section>
    <section className="auth-form-side" aria-label={t('Account access', '账户登录与注册')}>
      <div className="auth-page-tools"><button type="button" onClick={toggleLanguage} aria-label={language === 'en' ? '切换到中文' : 'Switch to English'}>{language === 'en' ? '中文' : 'EN'}</button><button type="button" onClick={() => setDark(value => !value)} aria-label={dark ? t('Switch to light theme', '切换到浅色主题') : t('Switch to dark theme', '切换到深色主题')}><Icon name={dark ? 'sun' : 'moon'} size={18} /></button></div>
      <div className="auth-card">
        <div className="auth-card-heading"><h2>{mode === 'login' ? t('Welcome back', '欢迎回来') : t('Create your account', '创建你的账户')}</h2><p>{mode === 'login' ? t('Sign in and continue exploring your graphs.', '登录，继续探索属于你的图谱。') : t('A personal space for your data and discoveries.', '为你的数据与发现，创建一个专属空间。')}</p></div>
        <div className="auth-mode-switch" role="group" aria-label={t('Choose sign in or registration', '选择登录或注册')}><button type="button" aria-pressed={mode === 'login'} className={mode === 'login' ? 'active' : ''} onClick={() => changeMode('login')} disabled={pending}>{t('Sign in', '登录')}</button><button type="button" aria-pressed={mode === 'register'} className={mode === 'register' ? 'active' : ''} onClick={() => changeMode('register')} disabled={pending}>{t('Create account', '注册')}</button></div>
        <form className="auth-form" onSubmit={submit} noValidate aria-busy={pending}>
          <div className="auth-field"><label htmlFor="auth-username">{t('Username', '用户名')}</label><input id="auth-username" name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} autoFocus value={username} maxLength={32} onChange={event => { setUsername(event.target.value); changed(); }} disabled={pending} aria-invalid={invalidField === 'username' || undefined} aria-describedby={invalidField === 'username' ? 'auth-feedback' : undefined} /></div>
          <div className="auth-field"><label htmlFor="auth-password">{t('Password', '密码')}</label><div className="auth-password-wrap"><input id="auth-password" name="password" type={showPassword ? 'text' : 'password'} autoComplete={mode === 'login' ? 'current-password' : 'new-password'} value={password} maxLength={128} onChange={event => { setPassword(event.target.value); changed(); }} disabled={pending} aria-invalid={invalidField === 'password' || undefined} aria-describedby={invalidField === 'password' ? 'auth-feedback' : undefined} /><button type="button" disabled={pending} onClick={() => setShowPassword(value => !value)} aria-label={showPassword ? t('Hide password', '隐藏密码') : t('Show password', '显示密码')} aria-pressed={showPassword}><Icon name={showPassword ? 'eye-off' : 'eye'} size={18} /></button></div></div>
          {mode === 'register' && <div className="auth-field"><label htmlFor="auth-confirmation">{t('Confirm password', '确认密码')}</label><input id="auth-confirmation" name="password_confirmation" type={showPassword ? 'text' : 'password'} autoComplete="new-password" value={confirmation} maxLength={128} onChange={event => { setConfirmation(event.target.value); changed(); }} disabled={pending} aria-invalid={invalidField === 'confirmation' || undefined} aria-describedby={invalidField === 'confirmation' ? 'auth-feedback' : undefined} /></div>}
          {feedback && <div className={`auth-feedback ${error ? 'is-error' : ''}`} id="auth-feedback" role={error ? 'alert' : 'status'}><Icon name="info" size={16} /><span>{feedback}</span></div>}
          <button className="auth-submit" type="submit" disabled={pending}>{pending ? <><span className="loader" />{mode === 'login' ? t('Signing in…', '正在登录…') : t('Creating account…', '正在创建账户…')}</> : <>{mode === 'login' ? t('Sign in to workspace', '登录并进入工作区') : t('Create my account', '创建账户')}<Icon name="arrow-right" size={18} /></>}</button>
        </form>
      </div>
    </section>
  </main>;
}

import { Popup, PopupPresence } from './PopupMotion';
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { Project, Session } from '../types';
import { Icon } from './Icon';
import { getUserId } from '../api';
import { useI18n, type Translate } from '../i18n';
import { pinKey, readPins } from '../sessionPins';

function sessionTitle(session: Session, t: Translate) { return session.title || session.preview || session.last_message || t('Untitled analysis', '未命名分析'); }
function noopLoginValueChange(_value: string) { /* Optional account controls are omitted in isolated component tests. */ }
function noopLogin() { /* Optional account controls are omitted in isolated component tests. */ }

type Props = {
  projects: Project[];
  sessions: Session[];
  hasMoreSessions?: boolean;
  loadingSessions?: boolean;
  sessionListError?: string;
  onLoadMoreSessions?: () => void;
  runStatuses?: Record<string, string>;
  unreadCompletions?: Record<string, boolean>;
  activeId?: string;
  activeProjectId?: string;
  userId: string;
  loginValue?: string;
  loginError?: string;
  loginPending?: boolean;
  onLoginValueChange?: (value: string) => void;
  onLogin?: () => void | Promise<void>;
  dark: boolean;
  onTheme: () => void;
  onMemory: () => void;
  onSkills: () => void;
  onCreateGraph: () => void;
  onHelp?: () => void;
  helpActive?: boolean;
  createGraphActive?: boolean;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onNew: () => void;
  onNewProject: () => void;
  onNewInProject: (id: string) => void;
  onProjectDatasets: (id: string) => void;
  onEditProject: (project: Project) => void;
  onDeleteProject: (project: Project) => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  collapsed: boolean;
  onCollapse: () => void;
};

function runStatusLabel(status: string | undefined, t: Translate) {
  if (status === 'preparing') return t('Preparing', '准备中');
  if (status === 'awaiting_feedback') return t('Awaiting confirmation', '等待确认');
  if (status === 'submitting_feedback') return t('Submitting confirmation', '确认提交中');
  if (status === 'reconnecting') return t('Reconnecting', '正在重连');
  if (status === 'detached') return t('Running in background', '后台运行');
  if (status === 'starting' || status === 'streaming') return t('Running', '正在运行');
  if (status === 'failed') return t('Run failed', '运行失败');
  return '';
}

const ACTIVE_RUN_STATUSES = new Set(['preparing', 'starting', 'streaming', 'submitting_feedback', 'reconnecting', 'detached']);

function SessionRunIndicator({ status, unread, t }: { status?: string; unread?: boolean; t: Translate }) {
  const statusLabel = runStatusLabel(status, t);
  if (ACTIVE_RUN_STATUSES.has(status || '')) {
    return <i className={`session-run-indicator is-running ${status || ''}`} role="status" aria-label={statusLabel} title={statusLabel} />;
  }
  if (unread) {
    const label = t('New completed response', '回复已完成');
    return <i className="session-run-indicator is-unread" role="status" aria-label={label} title={label} />;
  }
  if (!statusLabel) return null;
  return <i className={`session-run-indicator is-static ${status || ''}`} role="status" aria-label={statusLabel} title={statusLabel} />;
}

export function Sidebar({ projects, sessions, hasMoreSessions = false, loadingSessions = false, sessionListError = '', onLoadMoreSessions, runStatuses = {}, unreadCompletions = {}, activeId, activeProjectId, userId, loginValue = '', loginError = '', loginPending = false, onLoginValueChange = noopLoginValueChange, onLogin = noopLogin, dark, onTheme, onMemory, onSkills, onCreateGraph, onHelp, helpActive = false, createGraphActive = false, mobileOpen, onCloseMobile, onNew, onNewProject, onNewInProject, onProjectDatasets, onEditProject, onDeleteProject, onSelect, onDelete, collapsed, onCollapse }: Props) {
  const { language, toggleLanguage, t } = useI18n();
  const [pinState, setPinState] = useState(() => ({ userId, ids: readPins(userId) }));
  const pinned = pinState.userId === userId ? pinState.ids : readPins(userId);
  useEffect(() => {
    const refresh = () => setPinState({ userId, ids: readPins(userId) });
    const changed = (event: StorageEvent) => { if (event.key === null || event.key === pinKey(userId)) refresh(); };
    refresh(); window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, [userId]);
  function togglePin(id: string) {
    if (!userId || getUserId() !== userId) return;
    const next = new Set(pinned);
    if (next.has(id)) next.delete(id); else next.add(id);
    setPinState({ userId, ids: next });
    try { localStorage.setItem(pinKey(userId), JSON.stringify([...next])); } catch { /* Keep in-memory ordering. */ }
  }
  const orderSessions = (items: Session[]) => [...items].sort((a, b) => Number(pinned.has(b.session_id)) - Number(pinned.has(a.session_id)));
  const titleFor = (session: Session) => sessionTitle(session, t);
  const statusFor = (status?: string) => runStatusLabel(status, t);
  const pinButton = (session: Session) => <button type="button" className={`session-pin ${pinned.has(session.session_id) ? 'is-pinned' : ''}`}
    aria-label={pinned.has(session.session_id) ? t(`Unpin ${titleFor(session)}`, `取消置顶 ${titleFor(session)}`) : t(`Pin ${titleFor(session)}`, `置顶 ${titleFor(session)}`)} title={pinned.has(session.session_id) ? t('Unpin', '取消置顶') : t('Pin', '置顶')}
    aria-pressed={pinned.has(session.session_id)} onClick={event => { event.stopPropagation(); togglePin(session.session_id); }}><Icon name="pin" size={13} /></button>;
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(new Set());
  const [menuProjectId, setMenuProjectId] = useState<string | null>(null);
  const [menuPosition, setMenuPosition] = useState<{ left: number; top: number } | null>(null);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [switchingAccount, setSwitchingAccount] = useState(false);
  const projectMenuRef = useRef<HTMLDivElement | null>(null);
  const projectMenuTriggerRef = useRef<HTMLButtonElement | null>(null);
  const accountMenuRef = useRef<HTMLDivElement | null>(null);
  const accountTriggerRef = useRef<HTMLButtonElement | null>(null);
  const independentSessions = orderSessions(sessions.filter(session => !session.project_id));
  const activeSessionProjectId = sessions.find(session => session.session_id === activeId)?.project_id;

  const menuProject = projects.find(project => project.project_id === menuProjectId);

  useEffect(() => {
    const projectId = activeProjectId || activeSessionProjectId;
    if (!projectId) return;
    setExpandedProjects(current => current.has(projectId) ? current : new Set([...current, projectId]));
  }, [activeProjectId, activeSessionProjectId]);

  useEffect(() => {
    if (!menuProjectId) return;
    projectMenuRef.current?.querySelector<HTMLButtonElement>('button')?.focus();
    function closeOnOutside(event: MouseEvent) {
      const target = event.target as Node;
      if (projectMenuRef.current?.contains(target) || projectMenuTriggerRef.current?.contains(target)) return;
      setMenuProjectId(null);
      setMenuPosition(null);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== 'Escape') return;
      setMenuProjectId(null);
      setMenuPosition(null);
      projectMenuTriggerRef.current?.focus();
    }
    function closeOnViewportChange() {
      setMenuProjectId(null);
      setMenuPosition(null);
    }
    document.addEventListener('mousedown', closeOnOutside);
    document.addEventListener('keydown', closeOnEscape);
    document.addEventListener('scroll', closeOnViewportChange, true);
    window.addEventListener('resize', closeOnViewportChange);
    return () => {
      document.removeEventListener('mousedown', closeOnOutside);
      document.removeEventListener('keydown', closeOnEscape);
      document.removeEventListener('scroll', closeOnViewportChange, true);
      window.removeEventListener('resize', closeOnViewportChange);
    };
  }, [menuProjectId]);

  useEffect(() => {
    if (!accountMenuOpen) return;
    function closeOnOutside(event: MouseEvent) {
      const target = event.target as Node;
      if (accountMenuRef.current?.contains(target) || accountTriggerRef.current?.contains(target)) return;
      setAccountMenuOpen(false);
      setSwitchingAccount(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== 'Escape') return;
      setAccountMenuOpen(false);
      setSwitchingAccount(false);
      accountTriggerRef.current?.focus();
    }
    document.addEventListener('mousedown', closeOnOutside);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', closeOnOutside);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [accountMenuOpen]);

  useEffect(() => {
    if (!userId) return;
    setAccountMenuOpen(false);
    setSwitchingAccount(false);
  }, [userId]);

  function toggleProjectMenu(project: Project, trigger: HTMLButtonElement) {
    if (menuProjectId === project.project_id) {
      setMenuProjectId(null);
      setMenuPosition(null);
      return;
    }
    const rect = trigger.getBoundingClientRect();
    const menuWidth = 176;
    const menuHeight = 88;
    const left = Math.max(8, Math.min(rect.right - menuWidth, window.innerWidth - menuWidth - 8));
    const top = rect.bottom + menuHeight + 8 <= window.innerHeight
      ? rect.bottom + 6
      : Math.max(8, rect.top - menuHeight - 6);
    projectMenuTriggerRef.current = trigger;
    setMenuPosition({ left, top });
    setMenuProjectId(project.project_id);
  }

  function closeProjectMenu() {
    setMenuProjectId(null);
    setMenuPosition(null);
  }

  function toggleProject(projectId: string) {
    setExpandedProjects(current => {
      const next = new Set(current);
      if (next.has(projectId)) next.delete(projectId); else next.add(projectId);
      return next;
    });
  }

  function createStandaloneTask() {
    if (!userId) {
      setAccountMenuOpen(true);
      return;
    }
    onNew();
    onCloseMobile();
  }

  const effectivelyCollapsed = collapsed && !mobileOpen;
  const accountFormVisible = !userId || switchingAccount;

  function requireAccount(action: () => void) {
    if (userId) action();
    else setAccountMenuOpen(true);
  }

  return <>
  <aside className={`sidebar ${effectivelyCollapsed ? 'is-collapsed' : ''} ${mobileOpen ? 'mobile-open' : ''}`}>
    <div className="sidebar-head"><button className="sidebar-collapse" onClick={onCollapse} aria-label={mobileOpen ? t('Close left sidebar', '关闭左侧栏') : effectivelyCollapsed ? t('Expand left sidebar', '展开左侧栏') : t('Collapse left sidebar', '收起左侧栏')} title={mobileOpen ? t('Close left sidebar', '关闭左侧栏') : effectivelyCollapsed ? t('Expand left sidebar', '展开左侧栏') : t('Collapse left sidebar', '收起左侧栏')}><Icon name={effectivelyCollapsed ? 'panel-left-open' : 'panel-left-close'} size={18} /></button></div>
    <div className="sidebar-content" aria-hidden={effectivelyCollapsed}>
    <button className="new-task sidebar-primary-action" onClick={createStandaloneTask}>
      <span className="sidebar-primary-action-icon sidebar-new-task-icon"><Icon name="plus" size={18} /></span>
      <span className="sidebar-primary-action-label">{t('New task', '新建任务')}</span>
    </button>
    <nav className="sidebar-create-graph" aria-label={t('Graph construction workspace', '构图工作区')}>
      <button className={`sidebar-primary-action ${createGraphActive ? 'active' : ''}`} aria-current={createGraphActive ? 'page' : undefined} onClick={() => { onCreateGraph(); onCloseMobile(); }}>
        <span className="sidebar-primary-action-icon sidebar-create-graph-icon"><Icon name="graph-create" size={19} /></span>
        <span className="sidebar-primary-action-label">{t('Create your graph', '创建你的图')}</span>
      </button>
    </nav>
    <nav className="sidebar-utilities" aria-label={t('Knowledge and capability management', '知识与能力管理')}>
      <button onClick={() => requireAccount(() => { onMemory(); onCloseMobile(); })}><Icon name="spark" size={16} /><span>{t('Memory', '记忆')}</span></button>
      <button onClick={() => requireAccount(() => { onSkills(); onCloseMobile(); })}><Icon name="cube" size={16} /><span>{t('Skills', '技能')}</span></button>
      {onHelp && <button className={helpActive ? 'active' : ''} aria-current={helpActive ? 'page' : undefined} onClick={() => { onHelp(); onCloseMobile(); }}><Icon name="file" size={16} /><span>{t('Help', '帮助')}</span></button>}
    </nav>
    <div className="sidebar-scroll">
      <section className="sidebar-projects">
        <div className="sidebar-section-head"><span>{t('Projects', '项目')}</span><button onClick={() => requireAccount(() => { onNewProject(); onCloseMobile(); })} title={t('Create project', '创建项目')} aria-label={t('Create project', '创建项目')}><Icon name="plus" size={15} /></button></div>
        <div className="project-list">
          {projects.length === 0 ? null : projects.map(project => {
            const expanded = expandedProjects.has(project.project_id);
            const projectSessions = orderSessions(sessions.filter(session => session.project_id === project.project_id));
            return <div className={`project-tree-node ${expanded ? 'expanded' : ''}`} key={project.project_id}>
              <div className={`project-row ${activeProjectId === project.project_id ? 'selected' : ''}`}>
                <button className="project-row-main" onClick={() => { closeProjectMenu(); toggleProject(project.project_id); }} aria-expanded={expanded} title={project.name}><span className="project-row-icon"><Icon name={expanded ? 'folder-open' : 'folder'} size={17} /></span><strong>{project.name}</strong></button>
                <button className="project-inline-action" onClick={() => { closeProjectMenu(); onProjectDatasets(project.project_id); onCloseMobile(); }} title={t('Project datasets', '项目数据集')} aria-label={t(`${project.name} project datasets`, `${project.name} 项目数据集`)}><Icon name="database" size={15} /></button>
                <button className="project-inline-action" onClick={event => toggleProjectMenu(project, event.currentTarget)} title={t('Project actions', '项目操作')} aria-label={t(`${project.name} project actions`, `${project.name} 项目操作`)} aria-haspopup="menu" aria-expanded={menuProjectId === project.project_id}><Icon name="more" size={20} /></button>
                <button className="project-inline-action" onClick={() => { onNewInProject(project.project_id); onCloseMobile(); }} title={t(`Create a conversation in ${project.name}`, `在 ${project.name} 中新建会话`)} aria-label={t(`Create a conversation in ${project.name}`, `在 ${project.name} 中新建会话`)}><Icon name="plus" size={15} /></button>
              </div>
              <div className="project-session-collapse" aria-hidden={!expanded} {...(!expanded ? { inert: '' } : {})}><div className="project-session-clip"><div className="project-session-list">
                {projectSessions.length === 0 ? <div className="project-tree-empty">{t('No conversations', '暂无会话')}</div> : projectSessions.map(session => <div className={`project-session-row ${activeId === session.session_id ? 'selected' : ''}`} key={session.session_id}>
                  <button className="project-session-main" onClick={() => { onSelect(session.session_id); onCloseMobile(); }} title={titleFor(session)}><Icon name="file" size={13} /><SessionRunIndicator status={runStatuses[session.session_id]} unread={unreadCompletions[session.session_id]} t={t} /><span>{titleFor(session)}</span></button>
                  {pinButton(session)}<button className="project-session-delete" onClick={() => onDelete(session.session_id)} title={t('Delete conversation', '删除会话')} aria-label={t('Delete conversation', '删除会话')}><Icon name="trash" size={12} /></button>
                </div>)}
              </div></div></div>
            </div>;
          })}
        </div>
      </section>
      <section className="sidebar-recents">
        <div className="sidebar-section-head"><span>{t('Recent tasks', '最近任务')}</span><button onClick={() => requireAccount(() => { onNew(); onCloseMobile(); })} title={t('Create standalone conversation', '新建独立会话')} aria-label={t('Create standalone conversation', '新建独立会话')}><Icon name="plus" size={15} /></button></div>
        <div className="session-list">
          {independentSessions.length === 0 ? <div className="sidebar-empty">{t('No tasks', '暂无任务')}</div> : independentSessions.map(session => <div className={`session-row ${activeId === session.session_id ? 'selected' : ''}`} key={session.session_id} title={titleFor(session)} onClick={() => { onSelect(session.session_id); onCloseMobile(); }}>
            <Icon name="file" size={15} /><div className="session-copy"><strong><span className="recent-session-name">{titleFor(session)}</span></strong><span>{statusFor(runStatuses[session.session_id]) || (session.has_graph ? t('Graph loaded', '已加载图') : t('Ready to analyze', '待开始分析'))}</span></div><SessionRunIndicator status={runStatuses[session.session_id]} unread={unreadCompletions[session.session_id]} t={t} />{pinButton(session)}<button className="session-delete" onClick={event => { event.stopPropagation(); onDelete(session.session_id); }} title={t('Delete task', '删除任务')} aria-label={t('Delete task', '删除任务')}><Icon name="trash" size={14} /></button>
          </div>)}
        </div>
      </section>
    </div>
    <div className="sidebar-pagination">
      {sessionListError && <span role="alert">{sessionListError}</span>}
      {hasMoreSessions && <button className="text-button" onClick={onLoadMoreSessions} disabled={loadingSessions}>{loadingSessions ? t('Loading…', '加载中…') : sessionListError ? t('Retry loading tasks', '重试加载任务') : t('Load older tasks', '加载更多任务')}</button>}
    </div>
    <div className="sidebar-footer">
      <div className="account-area">
        <button ref={accountTriggerRef} className="account-trigger" onClick={() => { setAccountMenuOpen(current => !current); setSwitchingAccount(false); }} aria-label={userId ? t(`Account: ${userId}`, `账户：${userId}`) : t('Open sign in', '打开登录')} aria-haspopup="dialog" aria-expanded={accountMenuOpen}>
          <span className={`avatar ${userId ? '' : 'guest'}`}>{(userId || t('G', '访')).slice(0, 1).toUpperCase()}</span>
          <span className="account-copy"><strong>{userId || t('Not signed in', '未登录')}</strong><span>{userId ? t('Personal workspace', '个人工作区') : t('Select avatar to sign in', '点击头像登录')}</span></span>
          <Icon name="chevron" size={14} />
        </button>
        <PopupPresence>{accountMenuOpen ? <Popup.div variant="popover" ref={accountMenuRef} className="account-popover" role="dialog" aria-label={t('Account', '账户')}>
          <div className="account-popover-head"><span>{userId ? t('Current account', '当前账户') : t('Welcome to iGraph', '欢迎使用 iGraph')}</span><strong>{userId || t('Sign in to sync your projects and conversations', '登录后同步你的项目与会话')}</strong></div>
          {accountFormVisible ? <form className="account-login-form" onSubmit={event => { event.preventDefault(); void onLogin(); }}>
            <label htmlFor="sidebar-user-id">{t('User ID', '用户 ID')}</label>
            <input id="sidebar-user-id" autoFocus value={loginValue} onChange={event => onLoginValueChange(event.target.value)} placeholder={t('Enter your user ID', '请输入用户 ID')} autoComplete="username" disabled={loginPending} />
            {loginError ? <span className="account-login-error" role="alert">{loginError}</span> : null}
            <div className="account-login-actions">{userId ? <button type="button" className="account-cancel" onClick={() => setSwitchingAccount(false)}>{t('Cancel', '取消')}</button> : null}<button type="submit" className="account-login-submit" disabled={loginPending || !loginValue.trim()}>{loginPending ? t('Verifying…', '正在验证…') : t('Sign in', '登录')}</button></div>
          </form> : <button className="account-switch" type="button" onClick={() => setSwitchingAccount(true)}><Icon name="move" size={15} /> {t('Sign in as another user', '使用其他用户登录')}</button>}
        </Popup.div> : null}</PopupPresence>
      </div>
      <button className="sidebar-theme" onClick={toggleLanguage} aria-label={language === 'en' ? 'Switch to Chinese' : '切换到英文'} title={language === 'en' ? 'Switch to Chinese' : '切换到英文'}><span lang={language === 'en' ? 'zh-CN' : 'en'}>{language === 'en' ? '中' : 'EN'}</span></button>
      <button className="sidebar-theme" onClick={onTheme} aria-label={dark ? t('Switch to light theme', '切换到浅色主题') : t('Switch to dark theme', '切换到深色主题')} title={dark ? t('Switch to light theme', '切换到浅色主题') : t('Switch to dark theme', '切换到深色主题')}><Icon name={dark ? 'sun' : 'moon'} size={17} /></button>
    </div>
    </div>
  </aside>
  {createPortal(<PopupPresence>{menuProject && menuPosition ? <Popup.div key={menuProject.project_id} variant="popover" ref={projectMenuRef} className="project-action-menu project-action-popover" role="menu" aria-label={t(`${menuProject.name} project actions`, `${menuProject.name} 项目操作`)} style={menuPosition}>
    <button role="menuitem" onClick={() => { closeProjectMenu(); onEditProject(menuProject); }}><Icon name="edit" size={15} /> {t('Edit project', '编辑项目')}</button>
    <><div className="project-action-separator" role="separator" /><button role="menuitem" className="danger" onClick={() => { closeProjectMenu(); onDeleteProject(menuProject); }}><Icon name="trash" size={15} /> {t('Delete project', '删除项目')}</button></>
  </Popup.div> : null}</PopupPresence>, document.body)}
  <button className={`mobile-sidebar-backdrop ${mobileOpen ? 'is-visible' : ''}`} onClick={onCloseMobile} aria-label={t('Close sidebar', '关闭侧栏')} aria-hidden={!mobileOpen} tabIndex={mobileOpen ? 0 : -1} />
  </>;
}

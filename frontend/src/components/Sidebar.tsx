import { useEffect, useState } from 'react';
import type { Session } from '../types';
import { Icon } from './Icon';
import { getUserId } from '../api';
import { useI18n, type Translate } from '../i18n';
import { pinKey, readPins } from '../sessionPins';
import { useAccount } from './AuthGate';
import './SidebarAccount.css';
import './Sidebar.css';

function sessionTitle(session: Session, t: Translate) { return session.title || session.preview || session.last_message || t('Untitled analysis', '未命名分析'); }

type Props = {
  sessions: Session[];
  hasMoreSessions?: boolean;
  loadingSessions?: boolean;
  sessionListError?: string;
  onLoadMoreSessions?: () => void;
  runStatuses?: Record<string, string>;
  unreadCompletions?: Record<string, boolean>;
  activeId?: string;
  userId: string;
  dark: boolean;
  onTheme: () => void;
  onMemory: () => void;
  onSkills: () => void;
  onPlayground?: () => void;
  playgroundActive?: boolean;
  onHelp?: () => void;
  helpActive?: boolean;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onNew: () => void;
  onProjects: () => void;
  projectsActive?: boolean;
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

export function Sidebar({ sessions, hasMoreSessions = false, loadingSessions = false, sessionListError = '', onLoadMoreSessions, runStatuses = {}, unreadCompletions = {}, activeId, userId, dark, onTheme, onMemory, onSkills, onPlayground, playgroundActive, onHelp, helpActive = false, mobileOpen, onCloseMobile, onNew, onProjects, projectsActive = false, onSelect, onDelete, collapsed, onCollapse }: Props) {
  const { language, toggleLanguage, t } = useI18n();
  const account = useAccount();
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
  const recentSessions = orderSessions(sessions);

  function createStandaloneTask() {
    onNew();
    onCloseMobile();
  }

  const effectivelyCollapsed = collapsed && !mobileOpen;

  function runAction(action: () => void) { action(); }

  return <>
  <aside className={`sidebar ${effectivelyCollapsed ? 'is-collapsed' : ''} ${mobileOpen ? 'mobile-open' : ''}`}>
    <div className="sidebar-head"><button className="sidebar-collapse" onClick={onCollapse} aria-label={mobileOpen ? t('Close left sidebar', '关闭左侧栏') : effectivelyCollapsed ? t('Expand left sidebar', '展开左侧栏') : t('Collapse left sidebar', '收起左侧栏')} title={mobileOpen ? t('Close left sidebar', '关闭左侧栏') : effectivelyCollapsed ? t('Expand left sidebar', '展开左侧栏') : t('Collapse left sidebar', '收起左侧栏')}><Icon name={effectivelyCollapsed ? 'panel-left-open' : 'panel-left-close'} size={18} /></button></div>
    <div className="sidebar-content" aria-hidden={effectivelyCollapsed}>
    <button className="new-task sidebar-primary-action" onClick={createStandaloneTask}>
      <span className="sidebar-primary-action-icon sidebar-new-task-icon"><Icon name="plus" size={18} /></span>
      <span className="sidebar-primary-action-label">{t('New task', '新建任务')}</span>
    </button>
    <nav className="sidebar-utilities" aria-label={t('Knowledge and capability management', '知识与能力管理')}>
      {onPlayground && <button className={playgroundActive ? 'active' : ''} aria-current={playgroundActive ? 'page' : undefined} onClick={() => { onPlayground(); onCloseMobile(); }}><Icon name="project" size={16} /><span>{t('Playground', '场景广场')}</span></button>}
      <button onClick={() => runAction(() => { onMemory(); onCloseMobile(); })}><Icon name="spark" size={16} /><span>{t('Memory', '记忆')}</span></button>
      <button onClick={() => runAction(() => { onSkills(); onCloseMobile(); })}><Icon name="cube" size={16} /><span>{t('Skills', '技能')}</span></button>
      {onHelp && <button className={helpActive ? 'active' : ''} aria-current={helpActive ? 'page' : undefined} onClick={() => { onHelp(); onCloseMobile(); }}><Icon name="file" size={16} /><span>{t('Help', '帮助')}</span></button>}
    </nav>
    <div className="sidebar-scroll">
      <nav className="sidebar-projects" aria-label={t('Projects', '项目')}>
        <button type="button" className="sidebar-project-catalog-link" aria-current={projectsActive ? 'page' : undefined} onClick={() => { onProjects(); onCloseMobile(); }}>
          <Icon name="folder" size={18} />
          <span>{t('My projects', '我的项目')}</span>
          <Icon name="arrow-right" size={15} />
        </button>
      </nav>
      <section className="sidebar-recents">
        <div className="sidebar-section-head"><span>{t('Recent tasks', '最近任务')}</span><button onClick={() => runAction(() => { onNew(); onCloseMobile(); })} title={t('Create standalone conversation', '新建独立会话')} aria-label={t('Create standalone conversation', '新建独立会话')}><Icon name="plus" size={15} /></button></div>
        <div className="session-list">
          {recentSessions.length === 0 ? <div className="sidebar-empty">{t('No tasks', '暂无任务')}</div> : recentSessions.map(session => <div className={`session-row ${activeId === session.session_id ? 'selected' : ''}`} key={session.session_id} title={titleFor(session)} onClick={() => { onSelect(session.session_id); onCloseMobile(); }}>
            <Icon name="file" size={15} /><div className="session-copy"><strong><span className="recent-session-name">{titleFor(session)}</span></strong><span>{statusFor(runStatuses[session.session_id]) || (session.has_graph ? t('Graph loaded', '已加载图') : t('Ready to analyze', '待开始分析'))}</span></div><SessionRunIndicator status={runStatuses[session.session_id]} unread={unreadCompletions[session.session_id]} t={t} />{pinButton(session)}<button className="session-delete" onClick={event => { event.stopPropagation(); onDelete(session.session_id); }} title={t('Delete task', '删除任务')} aria-label={t('Delete task', '删除任务')}><Icon name="trash" size={14} /></button>
          </div>)}
        </div>
      </section>
    </div>
    <div className="sidebar-pagination">
      {sessionListError && <span role="alert">{sessionListError}</span>}
      {hasMoreSessions && <button className="text-button" onClick={onLoadMoreSessions} disabled={loadingSessions}>{loadingSessions ? t('Loading…', '加载中…') : sessionListError ? t('Retry loading tasks', '重试加载任务') : t('Load older tasks', '加载更多任务')}</button>}
    </div>
    <div className="sidebar-footer sidebar-account-footer">
      {account && <section className="sidebar-account-card" aria-label={t('My account', '我的账户')}>
        <div className="sidebar-account-identity">
          <span className="sidebar-account-avatar" aria-hidden="true">{account.username.slice(0, 1).toUpperCase()}</span>
          <div className="sidebar-account-info">
            <strong title={account.username}>{account.username}</strong>
            <span>{t('Personal account', '个人账户')}</span>
          </div>
        </div>
        <div className="sidebar-account-actions">
          <button type="button" className="sidebar-account-password" onClick={account.openPassword} title={t('Change password', '修改密码')}><Icon name="key" size={15} /><span>{t('Password', '修改密码')}</span></button>
          <button type="button" className="sidebar-account-logout" onClick={() => void account.logout()}><Icon name="log-out" size={15} /><span>{t('Sign out', '退出登录')}</span></button>
        </div>
      </section>}
      <div className="sidebar-account-preferences">
      <button className="sidebar-theme" onClick={toggleLanguage} aria-label={language === 'en' ? 'Switch to Chinese' : '切换到英文'} title={language === 'en' ? 'Switch to Chinese' : '切换到英文'}><span lang={language === 'en' ? 'zh-CN' : 'en'}>{language === 'en' ? '中' : 'EN'}</span></button>
      <button className="sidebar-theme" onClick={onTheme} aria-label={dark ? t('Switch to light theme', '切换到浅色主题') : t('Switch to dark theme', '切换到深色主题')} title={dark ? t('Switch to light theme', '切换到浅色主题') : t('Switch to dark theme', '切换到深色主题')}><Icon name={dark ? 'sun' : 'moon'} size={17} /></button>
      </div>
    </div>
    </div>
  </aside>
  <button className={`mobile-sidebar-backdrop ${mobileOpen ? 'is-visible' : ''}`} onClick={onCloseMobile} aria-label={t('Close sidebar', '关闭侧栏')} aria-hidden={!mobileOpen} tabIndex={mobileOpen ? 0 : -1} />
  </>;
}

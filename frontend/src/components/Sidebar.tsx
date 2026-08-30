import { useEffect, useState } from 'react';
import type { Project, Session } from '../types';
import { Icon } from './Icon';

function sessionTitle(session: Session) { return session.title || session.preview || session.last_message || '未命名分析'; }

type Props = {
  projects: Project[];
  sessions: Session[];
  activeId?: string;
  activeProjectId?: string;
  userId: string;
  dark: boolean;
  onTheme: () => void;
  onMemory: () => void;
  onSkills: () => void;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onNew: () => void;
  onNewProject: () => void;
  onNewInProject: (id: string) => void;
  onEditProject: (project: Project) => void;
  onDeleteProject: (project: Project) => void;
  onSelectProject: (id: string) => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  collapsed: boolean;
  onCollapse: () => void;
};

export function Sidebar({ projects, sessions, activeId, activeProjectId, userId, dark, onTheme, onMemory, onSkills, mobileOpen, onCloseMobile, onNew, onNewProject, onNewInProject, onEditProject, onDeleteProject, onSelectProject, onSelect, onDelete, collapsed, onCollapse }: Props) {
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(new Set());
  const [menuProjectId, setMenuProjectId] = useState<string | null>(null);
  const activeSessionProjectId = sessions.find(session => session.session_id === activeId)?.project_id;

  useEffect(() => {
    const projectId = activeProjectId || activeSessionProjectId;
    if (!projectId) return;
    setExpandedProjects(current => current.has(projectId) ? current : new Set([...current, projectId]));
  }, [activeProjectId, activeSessionProjectId]);

  function toggleProject(projectId: string) {
    setExpandedProjects(current => {
      const next = new Set(current);
      if (next.has(projectId)) next.delete(projectId); else next.add(projectId);
      return next;
    });
  }

  function openProject(projectId: string) {
    setExpandedProjects(current => current.has(projectId) ? current : new Set([...current, projectId]));
    setMenuProjectId(null);
    onSelectProject(projectId);
    onCloseMobile();
  }

  return <>
  <aside className={`sidebar ${collapsed ? 'is-collapsed' : ''} ${mobileOpen ? 'mobile-open' : ''}`}>
    <div className="sidebar-head"><span className="version">iGraph 1.0</span><div className="sidebar-tools"><button className="sidebar-collapse" onClick={onCollapse} aria-label="收起侧栏"><Icon name="panel" size={17} /></button></div></div>
    <button className="new-task" onClick={() => { onNew(); onCloseMobile(); }}><Icon name="plus" size={18} /> 新建任务</button>
    <nav className="sidebar-utilities" aria-label="知识与能力管理">
      <button onClick={() => { onMemory(); onCloseMobile(); }}><Icon name="spark" size={16} /><span>Memory</span></button>
      <button onClick={() => { onSkills(); onCloseMobile(); }}><Icon name="cube" size={16} /><span>Skills</span></button>
    </nav>
    <div className="sidebar-scroll">
      <section className="sidebar-projects">
        <div className="sidebar-section-head"><span>项目</span><button onClick={() => { onNewProject(); onCloseMobile(); }} title="创建项目" aria-label="创建项目"><Icon name="plus" size={15} /></button></div>
        <div className="project-list">
          {projects.length === 0 ? <div className="sidebar-empty compact">暂无项目</div> : projects.map(project => {
            const expanded = expandedProjects.has(project.project_id);
            const projectSessions = sessions.filter(session => session.project_id === project.project_id);
            return <div className={`project-tree-node ${expanded ? 'expanded' : ''}`} key={project.project_id}>
              <div className={`project-row ${activeProjectId === project.project_id || activeSessionProjectId === project.project_id ? 'selected' : ''}`}>
                <button className="project-disclosure" onClick={() => toggleProject(project.project_id)} aria-label={`${expanded ? '收起' : '展开'} ${project.name}`} aria-expanded={expanded}><Icon name="chevron" size={13} /></button>
                <button className="project-row-main" onClick={() => openProject(project.project_id)} title={project.name}><span className="project-row-icon"><Icon name="project" size={14} /></span><strong>{project.name}</strong><small>{project.conversation_count || 0}</small></button>
                <button className="project-inline-action" onClick={() => { onNewInProject(project.project_id); onCloseMobile(); }} title={`在 ${project.name} 中新建会话`} aria-label={`在 ${project.name} 中新建会话`}><Icon name="plus" size={14} /></button>
                <button className="project-inline-action" onClick={() => setMenuProjectId(current => current === project.project_id ? null : project.project_id)} title="项目操作" aria-label={`${project.name} 项目操作`}><Icon name="more" size={15} /></button>
              </div>
              {menuProjectId === project.project_id && <div className="project-action-menu">
                <button onClick={() => { setMenuProjectId(null); onEditProject(project); }}><Icon name="edit" size={14} /> 编辑项目</button>
                {!project.is_default && <button className="danger" onClick={() => { setMenuProjectId(null); onDeleteProject(project); }}><Icon name="trash" size={14} /> 删除项目</button>}
              </div>}
              {expanded && <div className="project-session-list">
                {projectSessions.length === 0 ? <div className="project-tree-empty">暂无会话</div> : projectSessions.map(session => <div className={`project-session-row ${activeId === session.session_id ? 'selected' : ''}`} key={session.session_id}>
                  <button className="project-session-main" onClick={() => { onSelect(session.session_id); onCloseMobile(); }} title={sessionTitle(session)}><Icon name="file" size={13} /><span>{sessionTitle(session)}</span></button>
                  <button className="project-session-delete" onClick={() => onDelete(session.session_id)} title="删除会话" aria-label="删除会话"><Icon name="trash" size={12} /></button>
                </div>)}
              </div>}
            </div>;
          })}
        </div>
      </section>
      <section className="sidebar-recents">
        <div className="sidebar-section-head"><span>最近任务</span></div>
        <div className="session-list">
          {sessions.length === 0 ? <div className="sidebar-empty">暂无任务<br /><span>点击上方按钮开始新分析</span></div> : sessions.map(session => <div className={`session-row ${activeId === session.session_id ? 'selected' : ''}`} key={session.session_id} onClick={() => { onSelect(session.session_id); onCloseMobile(); }}>
            <Icon name="file" size={15} /><div className="session-copy"><strong>{sessionTitle(session)}</strong><span>{session.has_graph ? '已加载图谱' : '待开始分析'}</span></div><button className="session-delete" onClick={event => { event.stopPropagation(); onDelete(session.session_id); }} title="删除任务" aria-label="删除任务"><Icon name="trash" size={14} /></button>
          </div>)}
        </div>
      </section>
    </div>
    <div className="sidebar-footer"><div className="avatar">{(userId || 'A').slice(0, 1).toUpperCase()}</div><div><strong>{userId}</strong><span>Personal Workspace</span></div><button className="sidebar-theme" onClick={onTheme} aria-label={dark ? '切换到浅色主题' : '切换到深色主题'} title={dark ? '切换到浅色主题' : '切换到深色主题'}><Icon name={dark ? 'sun' : 'moon'} size={17} /></button></div>
  </aside>
  {mobileOpen && <button className="mobile-sidebar-backdrop" onClick={onCloseMobile} aria-label="关闭侧栏" />}
  </>;
}

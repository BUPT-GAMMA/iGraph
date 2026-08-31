import { useState } from 'react';
import type { FormEvent } from 'react';
import type { Project, Session } from '../types';
import { Icon } from './Icon';

type EditorProps = {
  project?: Project | null;
  onClose: () => void;
  onSave: (name: string, description: string) => Promise<void>;
};

export function ProjectDialog({ project, onClose, onSave }: EditorProps) {
  const [name, setName] = useState(project?.name || '');
  const [description, setDescription] = useState(project?.description || '');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) return setError('请输入项目名称');
    setSaving(true); setError('');
    try { await onSave(name.trim(), description.trim()); } catch (cause: any) { setError(cause.message || '保存项目失败'); setSaving(false); }
  }
  return <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <form className="project-dialog" onSubmit={submit}>
      <div className="dialog-head"><div><span>{project ? 'Project settings' : 'New project'}</span><h2>{project ? '编辑项目' : '创建项目'}</h2></div><button type="button" className="dialog-close" title="关闭" aria-label="关闭" onClick={onClose}><Icon name="close" size={17} /></button></div>
      <label>项目名称<input type="text" autoFocus value={name} disabled={project?.is_default} maxLength={80} onChange={event => setName(event.target.value)} placeholder="例如：GraphAgent Research" /></label>
      <label>描述<textarea value={description} maxLength={500} onChange={event => setDescription(event.target.value)} placeholder="这个项目关注的工作内容（可选）" /></label>
      {error && <div className="dialog-error">{error}</div>}
      <div className="dialog-actions"><button type="button" className="secondary-command" onClick={onClose}>取消</button><button className="primary-command" disabled={saving || !name.trim()}>{saving ? '保存中…' : project ? '保存' : '创建'}</button></div>
    </form>
  </div>;
}

export function DeleteProjectDialog({ project, onClose, onConfirm }: { project: Project; onClose: () => void; onConfirm: (action: 'move_to_general' | 'delete') => Promise<void> }) {
  const [action, setAction] = useState<'move_to_general' | 'delete'>('move_to_general');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  async function confirm() {
    setSaving(true); setError('');
    try { await onConfirm(action); } catch (cause: any) { setError(cause.message || '删除项目失败'); setSaving(false); }
  }
  return <div className="modal-backdrop" role="presentation">
    <div className="project-dialog delete-project-dialog">
      <div className="dialog-head"><div><span>Delete project</span><h2>删除“{project.name}”？</h2></div><button className="dialog-close" title="关闭" aria-label="关闭" onClick={onClose}><Icon name="close" size={17} /></button></div>
      <p className="dialog-description">该项目包含 {project.conversation_count || 0} 个会话。请选择这些会话的处理方式。</p>
      <label className={`dialog-choice ${action === 'move_to_general' ? 'selected' : ''}`}><input type="radio" checked={action === 'move_to_general'} onChange={() => setAction('move_to_general')} /><span><strong>移动到 General</strong><small>保留全部会话、消息和图谱状态。</small></span></label>
      <label className={`dialog-choice danger-choice ${action === 'delete' ? 'selected' : ''}`}><input type="radio" checked={action === 'delete'} onChange={() => setAction('delete')} /><span><strong>删除项目和全部会话</strong><small>会话历史与对应图谱状态都会被删除。</small></span></label>
      {error && <div className="dialog-error">{error}</div>}
      <div className="dialog-actions"><button className="secondary-command" onClick={onClose}>取消</button><button className={`primary-command ${action === 'delete' ? 'destructive-command' : ''}`} disabled={saving} onClick={confirm}>{saving ? '处理中…' : '继续'}</button></div>
    </div>
  </div>;
}

export function MoveConversationDialog({ session, projects, currentProjectId, onClose, onConfirm }: { session: Session; projects: Project[]; currentProjectId?: string; onClose: () => void; onConfirm: (projectId: string) => Promise<void> }) {
  const [projectId, setProjectId] = useState(currentProjectId || projects[0]?.project_id || '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  async function confirm() {
    if (!projectId || projectId === currentProjectId) return onClose();
    setSaving(true); setError('');
    try { await onConfirm(projectId); } catch (cause: any) { setError(cause.message || '移动会话失败'); setSaving(false); }
  }
  return <div className="modal-backdrop" role="presentation">
    <div className="project-dialog move-dialog">
      <div className="dialog-head"><div><span>Move conversation</span><h2>移动会话</h2></div><button className="dialog-close" title="关闭" aria-label="关闭" onClick={onClose}><Icon name="close" size={17} /></button></div>
      <p className="dialog-description">{session.title || '未命名分析'}</p>
      <div className="move-project-list">{projects.map(project => <label className={`dialog-choice ${projectId === project.project_id ? 'selected' : ''}`} key={project.project_id}><input type="radio" checked={projectId === project.project_id} onChange={() => setProjectId(project.project_id)} /><span><strong>{project.name}</strong><small>{project.conversation_count || 0} 个会话{project.project_id === currentProjectId ? ' · 当前项目' : ''}</small></span></label>)}</div>
      {error && <div className="dialog-error">{error}</div>}
      <div className="dialog-actions"><button className="secondary-command" onClick={onClose}>取消</button><button className="primary-command" disabled={saving || !projectId || projectId === currentProjectId} onClick={confirm}>{saving ? '移动中…' : '移动'}</button></div>
    </div>
  </div>;
}

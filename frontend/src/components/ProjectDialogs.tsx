import { useState } from 'react';
import type { FormEvent } from 'react';
import type { Project } from '../types';
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
    const normalizedName = name.trim();
    const normalizedDescription = description.trim();
    if (!normalizedName) return setError('请输入项目名称');
    if (Array.from(normalizedName).length > 80) return setError('项目名称不能超过 80 个字符');
    if (Array.from(normalizedDescription).length > 500) return setError('项目描述不能超过 500 个字符');
    setSaving(true); setError('');
    try { await onSave(normalizedName, normalizedDescription); } catch (cause: any) { setError(cause.message || '保存项目失败'); setSaving(false); }
  }
  return <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <form className="project-dialog" onSubmit={submit}>
      <div className="dialog-head"><div><span>{project ? '项目设置' : '新建项目'}</span><h2>{project ? '编辑项目' : '创建项目'}</h2></div><button type="button" className="dialog-close" title="关闭" aria-label="关闭" onClick={onClose}><Icon name="close" size={17} /></button></div>
      <label>项目名称<input type="text" autoFocus value={name} maxLength={80} onChange={event => setName(event.target.value)} placeholder="例如：社交网络分析" /></label>
      <label>描述<textarea value={description} maxLength={500} onChange={event => setDescription(event.target.value)} placeholder="这个项目关注的工作内容（可选）" /></label>
      {error && <div className="dialog-error">{error}</div>}
      <div className="dialog-actions"><button type="button" className="secondary-command" onClick={onClose}>取消</button><button className="primary-command" disabled={saving || !name.trim()}>{saving ? '保存中…' : project ? '保存' : '创建'}</button></div>
    </form>
  </div>;
}

export function DeleteProjectDialog({ project, onClose, onConfirm }: { project: Project; onClose: () => void; onConfirm: () => Promise<void> }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  async function confirm() {
    setSaving(true); setError('');
    try { await onConfirm(); } catch (cause: any) { setError(cause.message || '删除项目失败'); setSaving(false); }
  }
  return <div className="modal-backdrop" role="presentation">
    <div className="project-dialog delete-project-dialog">
      <div className="dialog-head"><div><span>删除项目</span><h2>删除“{project.name}”？</h2></div><button className="dialog-close" title="关闭" aria-label="关闭" onClick={onClose}><Icon name="close" size={17} /></button></div>
      <p className="dialog-description">该项目包含 {project.conversation_count || 0} 个会话、{project.dataset_count || 0} 个数据集。将停止相关任务，删除会话、数据集及训练模型。此操作无法撤销。</p>
      {error && <div className="dialog-error">{error}</div>}
      <div className="dialog-actions"><button className="secondary-command" onClick={onClose}>取消</button><button className="primary-command destructive-command" disabled={saving} onClick={confirm}>{saving ? '处理中…' : '删除项目及全部内容'}</button></div>
    </div>
  </div>;
}

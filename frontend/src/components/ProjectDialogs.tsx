import { Popup, PopupBackdrop } from './PopupMotion';
import { useId, useState } from 'react';
import type { FormEvent } from 'react';
import type { Project } from '../types';
import { Icon } from './Icon';
import { useI18n } from '../i18n';
import './ProjectEditor.css';

type EditorProps = {
  project?: Project | null;
  onClose: () => void;
  onSave: (name: string, description: string) => Promise<void>;
};

export function ProjectDialog({ project, onClose, onSave }: EditorProps) {
  const { t } = useI18n();
  const id = useId();
  const [name, setName] = useState(project?.name || '');
  const [description, setDescription] = useState(project?.description || '');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    const normalizedName = name.trim();
    const normalizedDescription = description.trim();
    if (!normalizedName) return setError(t('Enter a project name', '请输入项目名称'));
    if (Array.from(normalizedName).length > 80) return setError(t('Project name cannot exceed 80 characters', '项目名称不能超过 80 个字符'));
    if (Array.from(normalizedDescription).length > 500) return setError(t('Project description cannot exceed 500 characters', '项目描述不能超过 500 个字符'));
    setSaving(true); setError('');
    try { await onSave(normalizedName, normalizedDescription); } catch (cause: any) { setError(cause.message || t('Failed to save project', '保存项目失败')); setSaving(false); }
  }
  return <PopupBackdrop className="modal-backdrop" role="presentation" onMouseDown={event => { if (!saving && event.target === event.currentTarget) onClose(); }}>
    <Popup.form className="project-dialog project-editor" role="dialog" aria-modal="true"
      aria-label={project ? t('Edit project', '编辑项目') : t('Create project', '创建项目')} onSubmit={submit}
      onKeyDown={event => { if (event.key === 'Escape' && !saving) onClose(); }}>
      <div className="dialog-head">
        <div><span className="project-editor-icon" aria-hidden="true"><Icon name="folder" size={17} /></span>
          <h2>{project ? t('Edit project', '编辑项目') : t('New project', '新建项目')}</h2></div>
        <button type="button" className="dialog-close" disabled={saving} aria-label={t('Close', '关闭')} onClick={onClose}><Icon name="close" size={17} /></button>
      </div>
      <div className="project-editor-fields">
        <div className="project-editor-field">
          <div><label htmlFor={id + '-name'}>{t('Project name', '项目名称')}</label><i aria-hidden="true">*</i></div>
          <div className="project-editor-input">
            <input id={id + '-name'} type="text" autoFocus aria-required="true" disabled={saving} value={name} maxLength={80}
              onChange={event => setName(event.target.value)} placeholder={t('Enter a project name', '请输入项目名称')} />
            <span className="project-editor-count" aria-hidden="true">{name.length} / 80</span>
          </div>
        </div>
        <div className="project-editor-field">
          <div><label htmlFor={id + '-description'}>{t('Description', '描述')}</label><small>{t('Optional', '选填')}</small></div>
          <div className="project-editor-input">
            <textarea id={id + '-description'} disabled={saving} value={description} maxLength={500}
              onChange={event => setDescription(event.target.value)} placeholder={t('What are you working on?', '这个项目关注什么？')} />
            <span className="project-editor-count" aria-hidden="true">{description.length} / 500</span>
          </div>
        </div>
        {error && <div className="dialog-error" role="alert">{error}</div>}
      </div>
      <div className="dialog-actions">
        <button type="button" className="secondary-command" disabled={saving} onClick={onClose}>{t('Cancel', '取消')}</button>
        <button className="primary-command" disabled={saving || !name.trim()}><Icon name="check" size={15} />{saving ? t('Saving…', '保存中…') : project ? t('Save', '保存') : t('Create', '创建')}</button>
      </div>
    </Popup.form>
  </PopupBackdrop>;
}

export function DeleteProjectDialog({ project, onClose, onConfirm }: { project: Project; onClose: () => void; onConfirm: () => Promise<void> }) {
  const { t } = useI18n();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  async function confirm() {
    setSaving(true); setError('');
    try { await onConfirm(); } catch (cause: any) { setError(cause.message || t('Failed to delete project', '删除项目失败')); setSaving(false); }
  }
  const conversationCount = project.conversation_count || 0;
  const datasetCount = project.dataset_count || 0;
  return <PopupBackdrop className="modal-backdrop" role="presentation">
    <Popup.div className="project-dialog delete-project-dialog" role="alertdialog" aria-modal="true" aria-label={t('Delete project', '删除项目')}>
      <div className="dialog-head"><div><span>{t('Delete project', '删除项目')}</span><h2>{t(`Delete “${project.name}”?`, `删除“${project.name}”？`)}</h2></div><button className="dialog-close" title={t('Close', '关闭')} aria-label={t('Close', '关闭')} onClick={onClose}><Icon name="close" size={17} /></button></div>
      <p className="dialog-description">{t(
        `This project contains ${conversationCount} conversation${conversationCount === 1 ? '' : 's'} and ${datasetCount} dataset${datasetCount === 1 ? '' : 's'}. Related tasks will be stopped, and conversations, datasets, and trained models will be deleted. This action cannot be undone.`,
        `该项目包含 ${conversationCount} 个会话、${datasetCount} 个数据集。将停止相关任务，删除会话、数据集及训练模型。此操作无法撤销。`,
      )}</p>
      {error && <div className="dialog-error">{error}</div>}
      <div className="dialog-actions"><button className="secondary-command" onClick={onClose}>{t('Cancel', '取消')}</button><button className="primary-command destructive-command" disabled={saving} onClick={confirm}>{saving ? t('Deleting…', '处理中…') : t('Delete project and all contents', '删除项目及全部内容')}</button></div>
    </Popup.div>
  </PopupBackdrop>;
}

import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { CustomModel } from '../api';
import type { AttachmentRef, DraftAttachment } from '../types';
import { Icon } from './Icon';

export type ComposerAttachment = AttachmentRef | DraftAttachment;

function attachmentId(attachment: ComposerAttachment) {
  return 'localId' in attachment ? attachment.localId : attachment.file_token;
}

function attachmentStatus(attachment: ComposerAttachment) {
  if (!('status' in attachment)) return '';
  if (attachment.status === 'uploading') return '上传中';
  if (attachment.status === 'failed') return '上传失败';
  if (attachment.status === 'needs_reselect') return '需重新选择';
  return '';
}

export type ComposerProps = {
  value: string;
  model: string;
  models: string[];
  customModels?: CustomModel[];
  activeDatasetName?: string;
  onChange: (value: string) => void;
  onModelChange: (model: string) => void;
  onDataset: () => void;
  onSend: () => void;
  onUpload: (file: File) => void;
  onRemoveAttachment: (fileToken: string) => void;
  attachments: ComposerAttachment[];
  loading: boolean;
  uploading: boolean;
  disabled: boolean;
  sendBlocked?: boolean;
  runtimeControlsDisabled?: boolean;
  sendBlockedReason?: string;
  watchPaused?: boolean;
  canPauseWatching?: boolean;
  onPauseWatching?: () => void;
  onResumeWatching?: () => void;
  focusRequest?: number;
  onSaveCustomModel?: (input: { model_id: string; display_name: string; base_url: string; api_key: string }) => Promise<void>;
  onDeleteCustomModel?: (modelId: string) => Promise<void>;
};

export function Composer({ value, model, models, customModels = [], activeDatasetName, onChange, onModelChange, onDataset, onSend, onUpload, onRemoveAttachment, attachments, loading, uploading, disabled, sendBlocked = false, runtimeControlsDisabled = false, sendBlockedReason, watchPaused = false, canPauseWatching = false, onPauseWatching, onResumeWatching, focusRequest, onSaveCustomModel, onDeleteCustomModel }: ComposerProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const modelPickerRef = useRef<HTMLDivElement>(null);
  const [modelOpen, setModelOpen] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);
  const [savingModel, setSavingModel] = useState(false);
  const [modelError, setModelError] = useState('');
  const [customDraft, setCustomDraft] = useState({ model_id: '', base_url: '', api_key: '' });
  const modelOptions = models.length ? models : [model].filter(Boolean);
  const customIds = new Set(customModels.map(item => item.model_id));
  const builtInOptions = modelOptions.filter(option => !customIds.has(option));
  const runControl = watchPaused || (loading && canPauseWatching);
  const runControlLabel = watchPaused ? '继续接收' : '暂停接收（Agent 将继续运行）';
  useEffect(() => { if (!disabled) textareaRef.current?.focus(); }, [focusRequest, disabled]);
  useEffect(() => {
    if (!modelOpen) return;
    function closeOnOutside(event: MouseEvent) {
      if (!modelPickerRef.current?.contains(event.target as Node)) setModelOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) { if (event.key === 'Escape') setModelOpen(false); }
    document.addEventListener('mousedown', closeOnOutside);
    document.addEventListener('keydown', closeOnEscape);
    return () => { document.removeEventListener('mousedown', closeOnOutside); document.removeEventListener('keydown', closeOnEscape); };
  }, [modelOpen]);
  useEffect(() => {
    if (!configOpen) return;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === 'Escape' && !savingModel) setConfigOpen(false);
    }
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [configOpen, savingModel]);
  function openCustomModelDialog() {
    setModelOpen(false);
    setModelError('');
    setConfigOpen(true);
  }
  function closeCustomModelDialog() {
    if (savingModel) return;
    setModelError('');
    setConfigOpen(false);
  }
  async function saveCustom() {
    if (!onSaveCustomModel) return;
    setModelError(''); setSavingModel(true);
    try {
      await onSaveCustomModel({ ...customDraft, display_name: customDraft.model_id });
      setCustomDraft({ model_id: '', base_url: '', api_key: '' });
      setConfigOpen(false);
    } catch (cause: any) { setModelError(cause.message || '自定义模型保存失败'); }
    finally { setSavingModel(false); }
  }
  async function removeCustom(modelId: string) {
    if (!onDeleteCustomModel || !window.confirm('确认删除这个自定义模型配置吗？')) return;
    setModelError('');
    try { await onDeleteCustomModel(modelId); }
    catch (cause: any) { setModelError(cause.message || '自定义模型删除失败'); }
  }
  function chooseModel(next: string) { setModelOpen(false); if (next !== model) onModelChange(next); }
  return <div className="composer-wrap">
    <div className="composer">
      {attachments.length ? <div className="attachment-list">{attachments.map(attachment => {
        const status = attachmentStatus(attachment);
        const id = attachmentId(attachment);
        return <div className={`attachment-chip ${status ? 'has-status' : ''}`} key={id}><Icon name="file" size={14} /><span title={attachment.filename}>{attachment.filename}</span>{status && <em>{status}</em>}<button type="button" onClick={() => onRemoveAttachment(id)} aria-label={`移除 ${attachment.filename}`} title="移除附件" disabled={loading || uploading}><Icon name="close" size={13} /></button></div>;
      })}</div> : null}
      <textarea ref={textareaRef} value={value} disabled={disabled} rows={3} placeholder="今天帮你分析什么？粘贴边列表，或直接用自然语言提问" onChange={event => onChange(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); if (!sendBlocked) onSend(); } }} />
      <div className="composer-toolbar">
        <input ref={inputRef} type="file" hidden accept=".txt,.csv,.edgelist,.dat,.json,.xlsx,.pdf,.pt,.npz" onChange={event => { const file = event.target.files?.[0]; if (file) onUpload(file); event.currentTarget.value = ''; }} />
        <button className="plain-icon composer-upload" onClick={() => inputRef.current?.click()} title="上传图文件" aria-label="上传图文件" disabled={disabled || uploading}>{uploading ? <span className="loader" /> : <Icon name="plus" size={20} />}</button>
        <button className={`composer-dataset ${activeDatasetName ? 'is-loaded' : ''}`} onClick={onDataset} disabled={disabled || uploading || runtimeControlsDisabled} title={activeDatasetName ? `当前数据集：${activeDatasetName}` : '选择项目数据集'}><Icon name="database" size={14} /><span>{activeDatasetName || '数据集'}</span></button>
        <div ref={modelPickerRef} className={`composer-model ${modelOpen ? 'is-open' : ''}`}>
          <button type="button" className="composer-model-trigger" onClick={() => setModelOpen(current => !current)} disabled={disabled || loading || runtimeControlsDisabled || modelOptions.length === 0} aria-label="选择模型" aria-expanded={modelOpen}>
            <span className="composer-model-icon"><Icon name="spark" size={14} /></span>
            <span className="composer-model-value">{model}</span>
            <span className="composer-model-chevron"><Icon name="chevron" size={12} /></span>
          </button>
          {modelOpen ? <div className="composer-model-menu" role="listbox" aria-label="可用模型">
            <div className="composer-model-section">可用模型</div>
            {builtInOptions.map(option => <button type="button" role="option" aria-selected={option === model} className={`composer-model-option ${option === model ? 'is-selected' : ''}`} key={option} onClick={() => chooseModel(option)}><span className="composer-option-mark"><Icon name="spark" size={14} /></span><span>{option}</span>{option === model ? <Icon name="check" size={14} /> : null}</button>)}
            {customModels.length ? <div className="composer-model-section">自定义模型</div> : null}
            {customModels.map(item => <div className={`composer-custom-model-row ${item.model_id === model ? 'is-selected' : ''}`} key={`custom-${item.model_id}`}><button type="button" role="option" aria-selected={item.model_id === model} className="composer-model-option" onClick={() => chooseModel(item.model_id)}><span className="composer-option-mark custom"><Icon name="settings" size={14} /></span><span title={item.model_id}>{item.model_id}</span>{item.model_id === model ? <Icon name="check" size={14} /> : null}</button>{onDeleteCustomModel ? <button type="button" className="composer-model-delete" disabled={item.model_id === model} onClick={() => void removeCustom(item.model_id)} aria-label={`删除自定义模型 ${item.model_id}`} title={item.model_id === model ? '请先切换到其他模型' : '删除自定义模型'}><Icon name="trash" size={14} /></button> : null}</div>)}
            {onSaveCustomModel ? <button type="button" className="composer-model-config" onClick={openCustomModelDialog}><Icon name="edit" size={15} /> 配置自定义模型</button> : null}
          </div> : null}
        </div>
        <div className="composer-spacer" />
        <span className="auto-mode">{watchPaused ? '接收已暂停 · Agent 仍在运行' : loading && canPauseWatching ? '正在接收 · 可暂停观看' : '◉ Auto'}</span>
        <button
          className={`send-button ${loading && !watchPaused ? 'stop' : ''}`}
          onClick={runControl ? (watchPaused ? onResumeWatching : onPauseWatching) : onSend}
          disabled={runControl ? uploading : disabled || uploading || sendBlocked}
          title={runControl ? runControlLabel : sendBlocked ? sendBlockedReason : '发送'}
          aria-label={runControl ? runControlLabel : sendBlocked ? sendBlockedReason || '当前不可发送' : '发送'}
        >{loading && !watchPaused ? <span className="stop-square" /> : <Icon name="send" size={18} />}</button>
      </div>
    </div>
    {configOpen ? <div className="modal-backdrop custom-model-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) closeCustomModelDialog(); }}>
      <form className="project-dialog custom-model-dialog" role="dialog" aria-modal="true" aria-labelledby="custom-model-dialog-title" onSubmit={(event: FormEvent) => { event.preventDefault(); void saveCustom(); }}>
        <div className="dialog-head"><div><span>Custom model</span><h2 id="custom-model-dialog-title">配置自定义模型</h2></div><button type="button" className="dialog-close" title="关闭" aria-label="关闭自定义模型配置" onClick={closeCustomModelDialog} disabled={savingModel}><Icon name="close" size={17} /></button></div>
        <div className="custom-model-fields">
          <label className="custom-model-wide">模型 ID<input type="text" autoFocus autoComplete="off" value={customDraft.model_id} onChange={event => setCustomDraft(current => ({ ...current, model_id: event.target.value }))} placeholder="例如：deepseek-chat 或 llama-3.1-8b" /></label>
          <label className="custom-model-wide">Base URL<input type="url" autoComplete="url" value={customDraft.base_url} onChange={event => setCustomDraft(current => ({ ...current, base_url: event.target.value }))} placeholder="https://api.example.com/v1" /></label>
          <label className="custom-model-wide">API Key<input type="password" autoComplete="new-password" value={customDraft.api_key} onChange={event => setCustomDraft(current => ({ ...current, api_key: event.target.value }))} placeholder="输入 API Key" /></label>
        </div>
        {modelError ? <div className="dialog-error">{modelError}</div> : null}
        <div className="dialog-actions"><button type="button" className="secondary-command" onClick={closeCustomModelDialog} disabled={savingModel}>取消</button><button type="submit" className="primary-command" disabled={savingModel || !customDraft.model_id.trim() || !customDraft.base_url.trim() || !customDraft.api_key.trim()}>{savingModel ? '保存中…' : '保存模型'}</button></div>
      </form>
    </div> : null}
    <div className="composer-footer"><span>{watchPaused ? '仅暂停本页接收；Agent 会在服务器继续运行，点击“继续接收”可补发进度' : sendBlockedReason || '支持粘贴边列表 · 支持上传 .txt / .json / .pt 文件'}</span><span>Shift + Enter 换行</span></div>
  </div>;
}

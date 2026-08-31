import { useRef } from 'react';
import type { AttachmentRef } from '../types';
import { Icon } from './Icon';

export type ComposerProps = {
  value: string;
  model: string;
  models: string[];
  activeDatasetName?: string;
  onChange: (value: string) => void;
  onModelChange: (model: string) => void;
  onDataset: () => void;
  onSend: () => void;
  onUpload: (file: File) => void;
  onRemoveAttachment: (fileToken: string) => void;
  attachments: AttachmentRef[];
  loading: boolean;
  uploading: boolean;
  disabled: boolean;
  sendBlocked?: boolean;
  runtimeControlsDisabled?: boolean;
  sendBlockedReason?: string;
};

export function Composer({ value, model, models, activeDatasetName, onChange, onModelChange, onDataset, onSend, onUpload, onRemoveAttachment, attachments, loading, uploading, disabled, sendBlocked = false, runtimeControlsDisabled = false, sendBlockedReason }: ComposerProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const modelOptions = models.length ? models : [model].filter(Boolean);
  return <div className="composer-wrap">
    <div className="composer">
      {attachments.length ? <div className="attachment-list">{attachments.map(attachment => <div className="attachment-chip" key={attachment.file_token}><Icon name="file" size={14} /><span title={attachment.filename}>{attachment.filename}</span><button type="button" onClick={() => onRemoveAttachment(attachment.file_token)} aria-label={`移除 ${attachment.filename}`} title="移除附件"><Icon name="close" size={13} /></button></div>)}</div> : null}
      <textarea value={value} disabled={disabled} rows={3} placeholder="今天帮你分析什么？粘贴边列表，或直接用自然语言提问" onChange={event => onChange(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); if (!sendBlocked) onSend(); } }} />
      <div className="composer-toolbar">
        <input ref={inputRef} type="file" hidden accept=".txt,.csv,.edgelist,.dat,.json,.xlsx,.pdf,.pt,.npz" onChange={event => { const file = event.target.files?.[0]; if (file) onUpload(file); event.currentTarget.value = ''; }} />
        <button className="plain-icon composer-upload" onClick={() => inputRef.current?.click()} title="上传图文件" aria-label="上传图文件" disabled={disabled || uploading}>{uploading ? <span className="loader" /> : <Icon name="plus" size={20} />}</button>
        <button className={`composer-dataset ${activeDatasetName ? 'is-loaded' : ''}`} onClick={onDataset} disabled={disabled || uploading || runtimeControlsDisabled} title={activeDatasetName ? `当前数据集：${activeDatasetName}` : '选择项目数据集'}><Icon name="database" size={14} /><span>{activeDatasetName || '数据集'}</span></button>
        <label className="composer-model" title="切换当前会话模型"><Icon name="spark" size={14} /><select value={model} onChange={event => onModelChange(event.target.value)} disabled={disabled || loading || runtimeControlsDisabled || modelOptions.length === 0} aria-label="选择模型">{modelOptions.map(option => <option value={option} key={option}>{option}</option>)}</select><Icon name="chevron" size={12} /></label>
        <div className="composer-spacer" />
        <span className="auto-mode">◉ Auto</span>
        <button className={`send-button ${loading ? 'stop' : ''}`} onClick={onSend} disabled={disabled || uploading || sendBlocked} title={sendBlocked ? sendBlockedReason : '发送'} aria-label={sendBlocked ? sendBlockedReason || '当前不可发送' : '发送'}>{loading ? <span className="stop-square" /> : <Icon name="send" size={18} />}</button>
      </div>
    </div>
    <div className="composer-footer"><span>{sendBlockedReason || '支持粘贴边列表 · 支持上传 .txt / .json / .pt 文件'}</span><span>Shift + Enter 换行</span></div>
  </div>;
}

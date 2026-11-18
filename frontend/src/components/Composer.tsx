import { useEffect, useId, useLayoutEffect, useRef } from 'react';
import type { CustomModel } from '../api';
import type { AttachmentRef, DraftAttachment } from '../types';
import { Icon } from './Icon';
import { FileTypeIcon } from './FileTypeIcon';
import { ModelPicker } from './ModelPicker';
import { useI18n, type Translate } from '../i18n';
import { ContextWindow, type ContextModel } from './ContextWindow';
import type { ExampleQuestionSuggestion } from '../userCases';

export type ComposerAttachment = AttachmentRef | DraftAttachment;

function resizeComposerInput(textarea: HTMLTextAreaElement | null) {
  if (!textarea || !textarea.clientWidth) return;
  const style = getComputedStyle(textarea);
  const lineHeight = parseFloat(style.lineHeight);
  const padding = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
  const border = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
  const minimum = 2 * lineHeight + padding + border;
  const maximum = 6 * lineHeight + padding + border;
  const scrollTop = textarea.scrollTop;
  // Measure wrapped content at the current width, including pasted text.
  textarea.style.overflowY = 'hidden';
  textarea.style.height = 'auto';
  const contentHeight = textarea.value || textarea.dataset.exampleSuggestion ? textarea.scrollHeight + border : minimum;
  textarea.style.height = `${Math.min(maximum, Math.max(minimum, contentHeight))}px`;
  textarea.style.overflowY = contentHeight > maximum ? 'auto' : 'hidden';
  textarea.scrollTop = scrollTop;
}

function attachmentId(attachment: ComposerAttachment) {
  return 'localId' in attachment ? attachment.localId : attachment.file_token;
}

function attachmentStatus(attachment: ComposerAttachment, t: Translate) {
  if (!('status' in attachment)) return '';
  if (attachment.status === 'staged') return t('Waiting to upload', '等待上传');
  if (attachment.status === 'uploading') return attachment.progress === 100 ? t('Verifying', '正在确认') : t('Uploading', '上传中');
  if (attachment.status === 'failed') return t('Upload failed', '上传失败');
  if (attachment.status === 'needs_reselect') return t('Reselect required', '需重新选择');
  return '';
}

export type ComposerProps = {
  contextSessionId?: string;
  contextModel?: ContextModel;
  value: string;
  model: string;
  models: string[];
  customModels?: CustomModel[];
  activeDatasetName?: string;
  onRemoteGraph?: () => void;
  exampleSuggestion?: ExampleQuestionSuggestion;
  onChange: (value: string) => void;
  onModelChange: (model: string) => void;
  onDataset: () => void;
  onSend: () => void;
  onUpload: (file: File) => void;
  onRemoveAttachment: (fileToken: string) => void;
  onRetryAttachment?: (localId: string) => void;
  attachments: ComposerAttachment[];
  loading: boolean;
  uploading: boolean;
  disabled: boolean;
  sendBlocked?: boolean;
  runtimeControlsDisabled?: boolean;
  sendBlockedReason?: string;
  canCancel?: boolean;
  cancellation?: 'requested' | 'stopping' | 'unconfirmed' | 'saving' | 'confirmed';
  onCancel?: () => void;
  focusRequest?: number;
  onManageModels?: () => void;
  onSaveCustomModel?: (input: { model_id: string; display_name: string; base_url: string; api_key: string }) => Promise<void>;
  onDeleteCustomModel?: (modelId: string) => Promise<void>;
};

export function Composer({ contextSessionId, contextModel, value, model, models, customModels = [], activeDatasetName, exampleSuggestion, onRemoteGraph, onChange, onModelChange, onDataset, onSend, onUpload, onRemoveAttachment, onRetryAttachment, attachments, loading, uploading, disabled, sendBlocked = false, runtimeControlsDisabled = false, sendBlockedReason, canCancel = false, cancellation, onCancel, focusRequest, onManageModels, onSaveCustomModel, onDeleteCustomModel }: ComposerProps) {
  const { t } = useI18n();
  const inputRef = useRef<HTMLInputElement>(null);
  const composingRef = useRef(false);
  const cannotSend = disabled || uploading || sendBlocked || attachments.some(item => 'status' in item && ['uploading', 'failed', 'needs_reselect'].includes(item.status));
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const runControl = canCancel;
  const stopping = cancellation === 'requested' || cancellation === 'stopping' || cancellation === 'saving';
  const runControlLabel = t('Stop current task', '终止当前任务');
  const cancellationLabel = cancellation === 'unconfirmed' ? t('Stop is not confirmed; click the square to retry', '停止请求尚未确认，可点击方块重试')
    : cancellation === 'saving' ? t('Stopped · saving results', '已停止，正在保存结果') : stopping ? t('Stopping…', '正在停止…') : cancellation === 'confirmed' ? t('Task ended', '任务已结束') : '';
  const suggestionHintId = useId();
  const visibleSuggestion = !value && !loading && !cannotSend && !runControl && !runtimeControlsDisabled && !attachments.length
    ? exampleSuggestion : undefined;
  function acceptExampleSuggestion() {
    if (!visibleSuggestion) return;
    onChange(visibleSuggestion.question);
    textareaRef.current?.focus();
  }
  useLayoutEffect(() => { resizeComposerInput(textareaRef.current); }, [value, visibleSuggestion?.question]);
  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    const resize = () => resizeComposerInput(textarea);
    let lastWidth = 0;
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(entries => {
      const width = entries[0].contentRect.width;
      if (width === lastWidth) return;
      lastWidth = width;
      resize();
    });
    // Only width changes need another measurement; height changes are our output.
    observer?.observe(textarea.parentElement!);
    window.addEventListener('resize', resize);
    return () => { observer?.disconnect(); window.removeEventListener('resize', resize); };
  }, []);
  useEffect(() => { if (!disabled) textareaRef.current?.focus(); }, [focusRequest, disabled]);
  return <div className="composer-wrap" onDragOver={event => {
    if (event.dataTransfer.types.includes('Files')) event.preventDefault();
  }} onDrop={event => {
    if (!event.dataTransfer.files.length) return;
    event.preventDefault();
    if (!disabled && !runtimeControlsDisabled) onUpload(event.dataTransfer.files[0]);
  }}>
      {attachments.length ? <div className="attachment-list">{attachments.map(attachment => {
        const status = attachmentStatus(attachment, t);
        const id = attachmentId(attachment);
        const progress = 'progress' in attachment ? attachment.progress : undefined;
        return <div className={`attachment-chip ${status ? 'has-status' : ''}`} key={id}>
          {'status' in attachment && attachment.status === 'uploading' ? <span className="attachment-progress" role="progressbar" aria-label={t(`${attachment.filename} upload progress`, `${attachment.filename} 上传进度`)} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
            <svg viewBox="0 0 36 36" aria-hidden="true"><circle className="attachment-progress-track" cx="18" cy="18" r="15" /><circle className="attachment-progress-value" cx="18" cy="18" r="15" pathLength="100" strokeDasharray={`${progress ?? 0} 100`} /></svg><small>{progress === undefined ? '…' : `${progress}%`}</small>
          </span> : <FileTypeIcon filename={attachment.filename} />}
          <span title={attachment.filename}>{attachment.filename}</span>{status && <em title={'error' in attachment ? attachment.error : undefined}>{status}</em>}
          {'status' in attachment && attachment.status === 'failed' && attachment.file && onRetryAttachment ? <button type="button" onClick={() => onRetryAttachment(id)} aria-label={t(`Retry ${attachment.filename}`, `重试 ${attachment.filename}`)} disabled={disabled || loading || runtimeControlsDisabled}>{t('Retry', '重试')}</button> : null}
          <button type="button" onClick={() => onRemoveAttachment(id)} aria-label={t(`Remove ${attachment.filename}`, `移除 ${attachment.filename}`)} title={t('Remove attachment', '移除附件')} disabled={disabled || loading || runtimeControlsDisabled}><Icon name="close" size={13} /></button>
        </div>;
      })}</div> : null}
    <div className="composer">
      <textarea ref={textareaRef} value={value} disabled={disabled} rows={2}
        aria-label={t('Message', '消息')}
        aria-describedby={visibleSuggestion ? suggestionHintId : undefined}
        data-example-suggestion={visibleSuggestion ? 'true' : undefined}
        placeholder={visibleSuggestion?.question || t('What would you like to analyze? Paste an edge list or ask in natural language', '今天帮你分析什么？粘贴边列表，或直接用自然语言提问')} onChange={event => onChange(event.target.value)}
        onCompositionStart={() => { composingRef.current = true; }} onCompositionEnd={() => { composingRef.current = false; }}
        onKeyDown={event => {
          if (composingRef.current || event.nativeEvent.isComposing || event.keyCode === 229) return;
          if (event.key === 'Tab' && visibleSuggestion && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey) {
            event.preventDefault();
            acceptExampleSuggestion();
            return;
          }
          if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); if (!cannotSend && !runControl) onSend(); }
        }} />
      {visibleSuggestion && <button type="button" className="composer-example-hint" id={suggestionHintId}
        onClick={acceptExampleSuggestion}
        aria-label={t('Fill in the next example question', '填入下一示例问题')}>
        <span>{t('Next example', '下一示例')} <span className="composer-example-step">{visibleSuggestion.number} / {visibleSuggestion.total}</span></span>
        <span className="composer-example-shortcut"><kbd>Tab</kbd>{t('to fill', '填入')}</span>
      </button>}
      <div className="composer-toolbar">
        <input ref={inputRef} type="file" hidden accept=".sql,.txt,.csv,.edgelist,.dat,.json,.xlsx,.pdf,.pt,.npz,.py,.md,.zip,.yaml,.yml,.js,.tsv" onChange={event => { const file = event.target.files?.[0]; if (file) onUpload(file); event.currentTarget.value = ''; }} />
        <button className="plain-icon composer-upload" onClick={() => inputRef.current?.click()} title={t('Upload graph file', '上传图文件')} aria-label={t('Upload graph file', '上传图文件')} disabled={disabled || runtimeControlsDisabled}><Icon name="plus" size={20} /></button>
        {onRemoteGraph && <button className="composer-dataset" onClick={onRemoteGraph} disabled={disabled || uploading || runtimeControlsDisabled}><Icon name="database" size={12} />{t('Remote databases', '远程数据库')}</button>}
        <button data-testid="local-dataset-selector" className={`composer-dataset ${activeDatasetName ? 'is-loaded' : ''}`} onClick={() => onDataset()} disabled={disabled || uploading || runtimeControlsDisabled} title={activeDatasetName ? t(`Current dataset: ${activeDatasetName}`, `当前数据集：${activeDatasetName}`) : t('Select project dataset', '选择项目数据集')}><Icon name="database" size={12} /><span>{activeDatasetName || t('Dataset', '数据集')}</span></button>
        <ModelPicker model={model} models={models} customModels={customModels} disabled={disabled || loading || runtimeControlsDisabled}
          onModelChange={onModelChange} onManageModels={onManageModels} onSaveCustomModel={onSaveCustomModel} onDeleteCustomModel={onDeleteCustomModel} />
        <div className="composer-spacer" />
        <span className="auto-mode" role="status">{cancellationLabel || t('◉ Auto', '◉ 自动')}</span>
        <button
          className={`send-button ${runControl ? 'stop' : ''}`}
          onClick={runControl ? onCancel : onSend}
          disabled={runControl ? stopping : cannotSend}
          title={runControl ? runControlLabel : sendBlocked ? sendBlockedReason : t('Send', '发送')}
          aria-label={runControl ? runControlLabel : sendBlocked ? sendBlockedReason || t('Cannot send right now', '当前不可发送') : t('Send', '发送')}
        >{runControl ? <span className="stop-square" /> : <Icon name="send" size={18} />}</button>
      </div>
      <ContextWindow sessionId={contextSessionId} model={contextModel} loading={loading} />
    </div>
  </div>;
}

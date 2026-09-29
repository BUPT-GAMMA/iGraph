import { useActionDialog } from './useActionDialog';
import { Popup, PopupBackdrop, PopupPresence } from './PopupMotion';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { modelErrorMessage, type CustomModel } from '../api';
import type { AttachmentRef, DraftAttachment } from '../types';
import { Icon } from './Icon';
import { FileTypeIcon } from './FileTypeIcon';
import { ModelProviderIcon } from './ModelProviderIcon';
import { selectRepresentativeModels } from './modelProviders';
import { useI18n, type Translate } from '../i18n';

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
  const contentHeight = textarea.value ? textarea.scrollHeight + border : minimum;
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
  value: string;
  model: string;
  models: string[];
  customModels?: CustomModel[];
  activeDatasetName?: string;
  onRemoteGraph?: () => void;
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

export function Composer({ value, model, models, customModels = [], activeDatasetName, onRemoteGraph, onChange, onModelChange, onDataset, onSend, onUpload, onRemoveAttachment, onRetryAttachment, attachments, loading, uploading, disabled, sendBlocked = false, runtimeControlsDisabled = false, sendBlockedReason, canCancel = false, cancellation, onCancel, focusRequest, onManageModels, onSaveCustomModel, onDeleteCustomModel }: ComposerProps) {
  const { t } = useI18n();
  const { askConfirm, actionDialog } = useActionDialog();
  const inputRef = useRef<HTMLInputElement>(null);
  const composingRef = useRef(false);
  const cannotSend = disabled || uploading || sendBlocked || attachments.some(item => 'status' in item && ['uploading', 'failed', 'needs_reselect'].includes(item.status));
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);
  const modelMenuRef = useRef<HTMLDivElement>(null);
  const modelPickerRef = useRef<HTMLDivElement>(null);
  const [modelOpen, setModelOpen] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);
  const [savingModel, setSavingModel] = useState(false);
  const [modelError, setModelError] = useState('');
  const [customDraft, setCustomDraft] = useState({ model_id: '', base_url: '', api_key: '' });
  const modelOptions = models.length ? models : [model].filter(Boolean);
  const currentCustom = customModels.find(item => item.model_id === model);
  const modelLabel = (item: CustomModel) => item.upstream_model_id ? item.display_name || item.upstream_model_id : item.model_id;
  const customGroups = new Map<string, CustomModel[]>();
  for (const item of customModels) { const key = item.provider_name || t('Custom models', '自定义模型'); customGroups.set(key, [...(customGroups.get(key) || []), item]); }
  const customIds = new Set(customModels.map(item => item.model_id));
  const builtInOptions = selectRepresentativeModels(modelOptions.filter(option => !customIds.has(option)), model);
  const runControl = canCancel;
  const stopping = cancellation === 'requested' || cancellation === 'stopping' || cancellation === 'saving';
  const runControlLabel = t('Stop current task', '终止当前任务');
  const cancellationLabel = cancellation === 'unconfirmed' ? t('Stop is not confirmed; click the square to retry', '停止请求尚未确认，可点击方块重试')
    : cancellation === 'saving' ? t('Stopped · saving results', '已停止，正在保存结果') : stopping ? t('Stopping…', '正在停止…') : cancellation === 'confirmed' ? t('Task ended', '任务已结束') : '';
  useLayoutEffect(() => { resizeComposerInput(textareaRef.current); }, [value]);
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
  useLayoutEffect(() => {
    if (!modelOpen) return;
    const composer = composerRef.current;
    const picker = modelPickerRef.current;
    const menu = modelMenuRef.current;
    if (!composer || !picker || !menu) return;
    const mainColumn = composer.closest<HTMLElement>('.main-column');
    const positionMenu = () => {
      const bounds = composer.getBoundingClientRect();
      if (bounds.width <= 0) return;
      const previousTranslate = menu.style.translate;
      menu.style.translate = 'none';
      // Short model names shrink the trigger. Keep its menu inside the composer
      // instead of letting right alignment extend into the clipped sidebar.
      menu.style.maxWidth = `${Math.max(0, bounds.width - 16)}px`;
      menu.style.maxHeight = '';
      menu.style.transform = 'none';
      // Keep the upward-opening list below the chat's clipped top edge in short windows.
      const top = Math.max(0, mainColumn?.getBoundingClientRect().top ?? 0);
      const availableHeight = Math.max(0, menu.getBoundingClientRect().bottom - top - 8);
      const cssMaxHeight = getComputedStyle(menu).maxHeight;
      menu.style.maxHeight = cssMaxHeight && cssMaxHeight !== 'none'
        ? `min(${cssMaxHeight}, ${availableHeight}px)` : `${availableHeight}px`;
      const menuBounds = menu.getBoundingClientRect();
      const left = Math.max(bounds.left + 8, Math.min(menuBounds.left, bounds.right - 8 - menuBounds.width));
      menu.style.transform = `translateX(${left - menuBounds.left}px)`;
      menu.style.translate = previousTranslate;
    };
    positionMenu();
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(positionMenu);
    observer?.observe(composer);
    observer?.observe(picker);
    if (mainColumn) observer?.observe(mainColumn);
    window.addEventListener('resize', positionMenu);
    return () => { observer?.disconnect(); window.removeEventListener('resize', positionMenu); };
  }, [modelOpen]);
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
    } catch (cause: any) { setModelError(cause.message || t('Could not save the custom model', '自定义模型保存失败')); }
    finally { setSavingModel(false); }
  }
  async function removeCustom(modelId: string) {
    if (!onDeleteCustomModel || !await askConfirm(t('Delete this custom model configuration?', '确认删除这个自定义模型配置吗？'))) return;
    setModelError('');
    try { await onDeleteCustomModel(modelId); }
    catch (cause: any) { setModelError(cause.message || t('Could not delete the custom model', '自定义模型删除失败')); }
  }
  function chooseModel(next: string) { setModelOpen(false); if (next !== model) onModelChange(next); }
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
    <div ref={composerRef} className="composer">
      <textarea ref={textareaRef} value={value} disabled={disabled} rows={2} placeholder={t('What would you like to analyze? Paste an edge list or ask in natural language', '今天帮你分析什么？粘贴边列表，或直接用自然语言提问')} onChange={event => onChange(event.target.value)}
        onCompositionStart={() => { composingRef.current = true; }} onCompositionEnd={() => { composingRef.current = false; }}
        onKeyDown={event => {
          if (composingRef.current || event.nativeEvent.isComposing || event.keyCode === 229) return;
          if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); if (!cannotSend && !runControl) onSend(); }
        }} />
      <div className="composer-toolbar">
        <input ref={inputRef} type="file" hidden accept=".sql,.txt,.csv,.edgelist,.dat,.json,.xlsx,.pdf,.pt,.npz,.py,.md,.zip,.yaml,.yml,.js,.tsv" onChange={event => { const file = event.target.files?.[0]; if (file) onUpload(file); event.currentTarget.value = ''; }} />
        <button className="plain-icon composer-upload" onClick={() => inputRef.current?.click()} title={t('Upload graph file', '上传图文件')} aria-label={t('Upload graph file', '上传图文件')} disabled={disabled || runtimeControlsDisabled}><Icon name="plus" size={20} /></button>
        {onRemoteGraph && <button className="composer-dataset" onClick={onRemoteGraph} disabled={disabled || uploading || runtimeControlsDisabled}><Icon name="database" size={12} />{t('Remote databases', '远程数据库')}</button>}
        <button data-testid="local-dataset-selector" className={`composer-dataset ${activeDatasetName ? 'is-loaded' : ''}`} onClick={() => onDataset()} disabled={disabled || uploading || runtimeControlsDisabled} title={activeDatasetName ? t(`Current dataset: ${activeDatasetName}`, `当前数据集：${activeDatasetName}`) : t('Select project dataset', '选择项目数据集')}><Icon name="database" size={12} /><span>{activeDatasetName || t('Dataset', '数据集')}</span></button>
        <div ref={modelPickerRef} className={`composer-model ${modelOpen ? 'is-open' : ''}`}>
          <button type="button" className="composer-model-trigger" onClick={() => setModelOpen(current => !current)} disabled={disabled || loading || runtimeControlsDisabled || (modelOptions.length === 0 && !onManageModels)} aria-label={t('Select model', '选择模型')} aria-expanded={modelOpen}>
            <span className="composer-model-icon"><ModelProviderIcon modelId={currentCustom?.upstream_model_id || model} size={13} /></span>
            <span className="composer-model-value">{currentCustom ? modelLabel(currentCustom) : model}</span>
            <span className="composer-model-chevron"><Icon name="chevron" size={10} /></span>
          </button>
          <PopupPresence>{modelOpen ? <Popup.div variant="popover" ref={modelMenuRef} className="composer-model-menu" role="group" aria-label={t('Available models', '可用模型')}>
            {!!builtInOptions.length && <div className="composer-model-section">{t('Models', '模型')}</div>}
            {builtInOptions.map(option => <button type="button" aria-pressed={option === model} className={`composer-model-option ${option === model ? 'is-selected' : ''}`} key={option} onClick={() => chooseModel(option)}><span className="composer-option-mark"><ModelProviderIcon modelId={option} size={15} /></span><span>{option}</span>{option === model ? <Icon name="check" size={14} /> : null}</button>)}
            {Array.from(customGroups, ([name, items]) => <div key={name}><div className="composer-model-section">{name}</div>{items.map(item => <div className={`composer-custom-model-row ${item.model_id === model ? 'is-selected' : ''}`} key={item.model_id}>
              <button type="button" aria-pressed={item.model_id === model} className="composer-model-option" disabled={!!item.unavailable_reason} title={modelErrorMessage(item.unavailable_reason) || item.upstream_model_id || item.model_id} onClick={() => chooseModel(item.model_id)}><span className="composer-option-mark custom"><ModelProviderIcon modelId={item.upstream_model_id || item.model_id} size={15} /></span><span className="composer-model-label">{modelLabel(item)}{item.upstream_model_id && item.display_name !== item.upstream_model_id && <small>{item.upstream_model_id}</small>}</span>{item.model_id === model ? <Icon name="check" size={14} /> : null}</button>
              {!onManageModels && onDeleteCustomModel && <button type="button" className="composer-model-delete" disabled={item.model_id === model} onClick={() => void removeCustom(item.model_id)} aria-label={t(`Delete custom model ${item.model_id}`, `删除自定义模型 ${item.model_id}`)}><Icon name="trash" size={14} /></button>}
            </div>)}</div>)}
            {onManageModels ? <button type="button" className="composer-model-config" onClick={() => { setModelOpen(false); onManageModels(); }}><Icon name="cube" size={15} />{t('Manage model services', '管理模型服务')}</button> : onSaveCustomModel ? <button type="button" className="composer-model-config" onClick={openCustomModelDialog}><Icon name="edit" size={15} /> {t('Configure custom model', '配置自定义模型')}</button> : null}
          </Popup.div> : null}</PopupPresence>
        </div>
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
    </div>
    {actionDialog}
    <PopupPresence>{configOpen ? <PopupBackdrop className="modal-backdrop custom-model-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) closeCustomModelDialog(); }}>
      <Popup.form className="project-dialog custom-model-dialog" role="dialog" aria-modal="true" aria-labelledby="custom-model-dialog-title" onSubmit={(event: FormEvent) => { event.preventDefault(); void saveCustom(); }}>
        <div className="dialog-head"><div><span>{t('Custom model', '自定义模型')}</span><h2 id="custom-model-dialog-title">{t('Configure custom model', '配置自定义模型')}</h2></div><button type="button" className="dialog-close" title={t('Close', '关闭')} aria-label={t('Close custom model settings', '关闭自定义模型配置')} onClick={closeCustomModelDialog} disabled={savingModel}><Icon name="close" size={17} /></button></div>
        <div className="custom-model-fields">
          <label className="custom-model-wide">{t('Model ID', '模型 ID')}<input type="text" autoFocus autoComplete="off" value={customDraft.model_id} onChange={event => setCustomDraft(current => ({ ...current, model_id: event.target.value }))} placeholder={t('For example: deepseek-chat or llama-3.1-8b', '例如：deepseek-chat 或 llama-3.1-8b')} /></label>
          <label className="custom-model-wide">{t('API endpoint', '接口地址')}<input type="url" autoComplete="url" value={customDraft.base_url} onChange={event => setCustomDraft(current => ({ ...current, base_url: event.target.value }))} placeholder="https://api.example.com/v1" /></label>
          <label className="custom-model-wide">{t('API key', '访问密钥')}<input type="password" autoComplete="new-password" value={customDraft.api_key} onChange={event => setCustomDraft(current => ({ ...current, api_key: event.target.value }))} placeholder={t('Enter API key', '输入访问密钥')} /></label>
        </div>
        {modelError ? <div className="dialog-error">{modelError}</div> : null}
        <div className="dialog-actions"><button type="button" className="secondary-command" onClick={closeCustomModelDialog} disabled={savingModel}>{t('Cancel', '取消')}</button><button type="submit" className="primary-command" disabled={savingModel || !customDraft.model_id.trim() || !customDraft.base_url.trim() || !customDraft.api_key.trim()}>{savingModel ? t('Saving…', '保存中…') : t('Save model', '保存模型')}</button></div>
      </Popup.form>
    </PopupBackdrop> : null}</PopupPresence>
  </div>;
}

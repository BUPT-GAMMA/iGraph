import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { modelErrorMessage, type CustomModel } from '../api';
import { useI18n } from '../i18n';
import { Icon } from './Icon';
import { ModelProviderIcon } from './ModelProviderIcon';
import { selectRepresentativeModels } from './modelProviders';
import { Popup, PopupBackdrop, PopupPresence } from './PopupMotion';
import { useActionDialog } from './useActionDialog';

type Props = {
  model: string;
  models: string[];
  customModels?: CustomModel[];
  disabled?: boolean;
  label?: string;
  onModelChange: (model: string) => void;
  onManageModels?: () => void;
  onSaveCustomModel?: (model: { model_id: string; display_name: string; base_url: string; api_key: string }) => Promise<void>;
  onDeleteCustomModel?: (modelId: string) => Promise<void>;
};

export function ModelPicker({ model, models, customModels = [], disabled = false, label, onModelChange,
  onManageModels, onSaveCustomModel, onDeleteCustomModel }: Props) {
  const { t } = useI18n();
  const { askConfirm, actionDialog } = useActionDialog();
  const configTitleId = useId();
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
  useLayoutEffect(() => {
    if (!modelOpen) return;
    const picker = modelPickerRef.current;
    const container = picker?.closest<HTMLElement>('.composer, .construction-panel');
    const menu = modelMenuRef.current;
    if (!container || !picker || !menu) return;
    const clippingPanel = picker.closest<HTMLElement>('.construction-panel, .main-column');
    const positionMenu = () => {
      const bounds = container.getBoundingClientRect();
      if (bounds.width <= 0) return;
      const previousTranslate = menu.style.translate;
      menu.style.translate = 'none';
      // Keep the menu inside the visible panel when the trigger is narrow.
      menu.style.maxWidth = `${Math.max(0, bounds.width - 16)}px`;
      menu.style.maxHeight = '';
      menu.style.transform = 'none';
      // Clamp the upward-opening list to the top of the visible panel.
      const top = Math.max(0, clippingPanel?.getBoundingClientRect().top ?? 0);
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
    observer?.observe(container);
    observer?.observe(picker);
    if (clippingPanel) observer?.observe(clippingPanel);
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
  return <>
    <div ref={modelPickerRef} className={`composer-model ${modelOpen ? 'is-open' : ''}`} onKeyDown={event => {
      if (event.key !== 'Escape' || !modelOpen) return;
      event.stopPropagation();
      setModelOpen(false);
      modelPickerRef.current?.querySelector('button')?.focus();
    }}>
      <button type="button" className="composer-model-trigger" onClick={() => setModelOpen(current => !current)} disabled={disabled || (modelOptions.length === 0 && !onManageModels)} aria-label={label || t('Select model', '选择模型')} aria-expanded={modelOpen}>
        <span className="composer-model-icon"><ModelProviderIcon modelId={currentCustom?.upstream_model_id || model} size={13} /></span>
        <span className="composer-model-value">{currentCustom ? modelLabel(currentCustom) : model || t('Choose a model', '选择模型')}</span>
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
    {actionDialog}
    <PopupPresence>{configOpen ? <PopupBackdrop className="modal-backdrop custom-model-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) closeCustomModelDialog(); }}>
      <Popup.form className="project-dialog custom-model-dialog" role="dialog" aria-modal="true" aria-labelledby={configTitleId} onSubmit={event => { event.preventDefault(); void saveCustom(); }}>
        <div className="dialog-head"><div><span>{t('Custom model', '自定义模型')}</span><h2 id={configTitleId}>{t('Configure custom model', '配置自定义模型')}</h2></div><button type="button" className="dialog-close" title={t('Close', '关闭')} aria-label={t('Close custom model settings', '关闭自定义模型配置')} onClick={closeCustomModelDialog} disabled={savingModel}><Icon name="close" size={17} /></button></div>
        <div className="custom-model-fields">
          <label className="custom-model-wide">{t('Model ID', '模型 ID')}<input type="text" autoFocus autoComplete="off" value={customDraft.model_id} onChange={event => setCustomDraft(current => ({ ...current, model_id: event.target.value }))} placeholder={t('For example: deepseek-chat or llama-3.1-8b', '例如：deepseek-chat 或 llama-3.1-8b')} /></label>
          <label className="custom-model-wide">{t('API endpoint', '接口地址')}<input type="url" autoComplete="url" value={customDraft.base_url} onChange={event => setCustomDraft(current => ({ ...current, base_url: event.target.value }))} placeholder="https://api.example.com/v1" /></label>
          <label className="custom-model-wide">{t('API key', '访问密钥')}<input type="password" autoComplete="new-password" value={customDraft.api_key} onChange={event => setCustomDraft(current => ({ ...current, api_key: event.target.value }))} placeholder={t('Enter API key', '输入访问密钥')} /></label>
        </div>
        {modelError ? <div className="dialog-error">{modelError}</div> : null}
        <div className="dialog-actions"><button type="button" className="secondary-command" onClick={closeCustomModelDialog} disabled={savingModel}>{t('Cancel', '取消')}</button><button type="submit" className="primary-command" disabled={savingModel || !customDraft.model_id.trim() || !customDraft.base_url.trim() || !customDraft.api_key.trim()}>{savingModel ? t('Saving…', '保存中…') : t('Save model', '保存模型')}</button></div>
      </Popup.form>
    </PopupBackdrop> : null}</PopupPresence>
  </>;
}

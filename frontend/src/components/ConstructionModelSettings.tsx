import { useEffect, useState } from 'react';
import { getConstructionModelSettings, saveConstructionModelSettings, type ConstructionModelSettings as Settings } from '../api';
import { useI18n } from '../i18n';

type Props = { model: string; models: string[]; disabled: boolean; onModelChange: (model: string) => void;
  onSavedModel: (model: string) => void; onDirtyChange: (dirty: boolean) => void };

export function ConstructionModelSettings({ model, models, disabled, onModelChange, onSavedModel, onDirtyChange }: Props) {
  const { t } = useI18n();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [url, setUrl] = useState('');
  const [key, setKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [custom, setCustom] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const dirty = !!(url || key || clearKey || custom && !models.includes(model));
  useEffect(() => { onDirtyChange(dirty || saving); }, [dirty, saving, onDirtyChange]);
  useEffect(() => {
    const controller = new AbortController();
    void getConstructionModelSettings(controller.signal).then(result => {
      if (!controller.signal.aborted) setSettings(result);
    }).catch(() => { /* The editable form still allows a complete new connection. */ });
    return () => controller.abort();
  }, []);
  async function save() {
    setSaving(true); setError(''); setSaved(false);
    try {
      const result = await saveConstructionModelSettings({ model: model.trim(), base_url: url.trim(), api_key: key.trim(), clear_api_key: clearKey });
      setSettings(result); onSavedModel(result.model); setUrl(''); setKey(''); setClearKey(false); setCustom(false); setSaved(true);
    } catch (cause) { setError(cause instanceof Error ? cause.message : t('Unable to save model settings', '无法保存模型配置')); }
    finally { setSaving(false); }
  }
  return <section className="construction-model-settings" aria-label={t('Construction model settings', '构图模型配置')}>
    <strong>{t('Construction model settings', '构图模型配置')}</strong>
    <fieldset className="construction-build-options" disabled={disabled || saving}>
      <label>{t('Construction model', '构图大模型')}<select value={custom ? '__custom__' : model} onChange={event => {
        setSaved(false); setCustom(event.target.value === '__custom__');
        if (event.target.value !== '__custom__') onModelChange(event.target.value);
      }}>{models.map(value => <option key={value} value={value}>{value}</option>)}<option value="__custom__">{t('Custom model…', '自定义模型…')}</option></select></label>
      {custom && <label>{t('Construction model ID', '构图模型 ID')}<input maxLength={200} value={model} onChange={event => { onModelChange(event.target.value); setSaved(false); }} /></label>}
      <label>{t('Construction API URL', '构图 API URL')}<input type="url" autoComplete="off" maxLength={2048} value={url} onChange={event => { setUrl(event.target.value); setSaved(false); }} placeholder={t('Enter your API base URL', '请输入 API 基础地址')} /></label>
      <label>{t('Construction API key', '构图 API Key')}<input type="password" autoComplete="new-password" maxLength={8192} value={key} onChange={event => { setKey(event.target.value); setClearKey(false); setSaved(false); }} placeholder={t('Enter your API key', '请输入 API Key')} /></label>
      <small>{t('OpenAI-compatible API. Used for TXT extraction and AI-assisted table construction. Blank fields keep the saved or server connection.', '使用 OpenAI 兼容接口，适用于 TXT 智能抽取和表格智能构图。URL 和 Key 留空时沿用已保存或服务器配置。')}</small>
      <small>{settings?.has_base_url && settings.has_api_key ? t('Connection saved', '连接已配置') : settings?.has_server_config && !settings.has_base_url ? t('Server connection available', '服务器已配置连接') : t('Enter a URL and API key before using AI construction.', '使用智能构图前，请填写 URL 和 API Key。')}</small>
      {settings?.has_api_key && <label className="construction-check"><input type="checkbox" checked={clearKey} onChange={event => { setClearKey(event.target.checked); setKey(''); setSaved(false); }} />{t('Remove saved construction key', '清除已保存的构图 Key')}</label>}
      <button className="secondary-command" type="button" disabled={!model.trim()} onClick={() => void save()}>{saving ? t('Saving…', '保存中…') : t('Save model settings', '保存模型配置')}</button>
    </fieldset>
    {dirty && <small>{t('Save model settings before starting construction.', '请先保存模型配置，再开始构图。')}</small>}
    {error && <small role="alert">{error}</small>}
    {saved && <small role="status">{t('Model settings saved. New builds will use this connection.', '模型配置已保存，新构图将使用此连接。')}</small>}
  </section>;
}

import { useState } from 'react';
import { ApiError, modelErrorMessage, savePlaygroundConnection, type PlaygroundConnection } from '../api';
import { useI18n } from '../i18n';

export function PlaygroundConnectionForm({ connection, onSaved, onCancel }: {
  connection: PlaygroundConnection; onSaved: (next: PlaygroundConnection) => void; onCancel: () => void;
}) {
  const { t } = useI18n();
  const [url, setUrl] = useState(connection.base_url);
  const [model, setModel] = useState(connection.model);
  const [key, setKey] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const needsKey = !connection.configured || url.trim().replace(/\/+$/, '') !== connection.base_url;
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (saving || !url.trim() || !model.trim() || (needsKey && !key.trim())) return;
    setSaving(true); setError('');
    try {
      const saved = await savePlaygroundConnection({ base_url: url.trim(), model: model.trim(), api_key: key, revision: connection.revision });
      setKey(''); onSaved(saved);
    } catch (cause) {
      setError((cause instanceof ApiError && modelErrorMessage(cause.code)) || t('Could not save the connection. Check the URL, model and key.', '保存失败，请检查接口地址、模型和密钥。'));
    } finally { setSaving(false); }
  }
  return <form className="playground-connection" onSubmit={save}>
    <h2>{t('Model connection', '模型连接')}</h2>
    <p>{t('Connect your own OpenAI-compatible service to chat in Playground.', '填写你自己的 OpenAI 兼容接口后，即可在场景广场对话。')}</p>
    <div className="playground-fields">
      <label>{t('API URL', '接口 URL')}<input type="url" value={url} required autoComplete="off" maxLength={2048} onChange={e => setUrl(e.target.value)} /></label>
      <label>{t('Model ID', '模型 ID')}<input value={model} required autoComplete="off" maxLength={256} onChange={e => setModel(e.target.value)} /></label>
      <label>API Key<input type="password" value={key} required={needsKey} autoComplete="new-password" maxLength={8192}
        placeholder={needsKey ? t('Enter your API key', '填写你的 API Key') : t('Leave blank to keep the saved key', '留空保留已保存的密钥')} onChange={e => setKey(e.target.value)} /></label>
    </div>
    {error && <div role="alert">{error}</div>}
    <div className="playground-connection-actions">
      {connection.configured && <button className="secondary-command" type="button" onClick={onCancel}>{t('Cancel', '取消')}</button>}
      <button className="primary-command" type="submit" disabled={saving || !url.trim() || !model.trim() || (needsKey && !key.trim())}>
        {saving ? t('Saving…', '正在保存…') : t('Save connection', '保存连接')}</button>
    </div>
  </form>;
}

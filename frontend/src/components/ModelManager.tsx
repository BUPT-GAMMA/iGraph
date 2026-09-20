import { useEffect, useRef, useState } from 'react';
import { deleteModelConfiguration, listModels, modelErrorMessage, saveModelConfiguration, setDefaultModelConfiguration, setModelEnabled, testModelConfiguration, type ModelCatalog, type ModelConfiguration } from '../api';
import { useI18n } from '../i18n';
import { Icon } from './Icon';

const blank = { display_name: '', upstream_model_id: '', base_url: '', api_key: '', clear_api_key: false,
  provider: 'openai-compatible', tools: true, vision: false, context_window: 32768, max_output_tokens: 4096 };

export function ModelManager({ onClose, onUpdated }: { onClose: () => void; onUpdated: (catalog: ModelCatalog) => void }) {
  const { t } = useI18n();
  const [catalog, setCatalog] = useState<ModelCatalog>({ models: [], default_model: '' });
  const [query, setQuery] = useState('');
  const [editing, setEditing] = useState<ModelConfiguration | 'new' | null>(null);
  const [draft, setDraft] = useState(blank);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<ModelConfiguration | null>(null);
  const [testTarget, setTestTarget] = useState<ModelConfiguration | null>(null);
  const dialog = useRef<HTMLElement>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    const prior = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    void refresh().catch(cause => { if (alive.current) setError(cause.message); });
    return () => { alive.current = false; prior?.focus(); };
  }, []);

  async function refresh() {
    const result = await listModels();
    if (!alive.current) return;
    setCatalog(result); onUpdated(result);
  }
  async function act(action: () => Promise<unknown>, message = '') {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      await action();
      if (!alive.current) return;
      await refresh(); setNotice(message);
    } catch (cause) {
      if (alive.current) {
        setError(cause instanceof Error ? cause.message : t('Operation failed', '操作失败'));
        await refresh().catch(() => undefined);
      }
    } finally { if (alive.current) setBusy(false); }
  }
  function edit(model: ModelConfiguration | 'new') {
    setEditing(model); setError(''); setNotice('');
    setDraft(model === 'new' ? blank : { display_name: model.display_name, upstream_model_id: model.upstream_model_id,
      base_url: model.base_url, api_key: '', clear_api_key: false, provider: model.provider || 'openai-compatible',
      tools: model.tools, vision: model.vision, context_window: model.context_window, max_output_tokens: model.max_output_tokens });
  }
  const rows = catalog.models.filter(item => `${item.display_name} ${item.upstream_model_id} ${item.provider}`.toLowerCase().includes(query.toLowerCase()));
  return <div className="overlay model-manager-overlay">
    <section ref={dialog} tabIndex={-1} className="model-manager" role="dialog" aria-modal="true" aria-label={t('Model management', '模型管理')}
      onKeyDown={event => {
        if (event.key === 'Escape' && !busy) { event.preventDefault(); if (deleteTarget || testTarget) { setDeleteTarget(null); setTestTarget(null); } else if (editing) setEditing(null); else onClose(); }
        if (event.key === 'Tab') {
          const nodes = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]') || []).filter(el => el.getClientRects().length);
          if (!nodes.length) { event.preventDefault(); return; }
          if (event.shiftKey && (document.activeElement === nodes[0] || document.activeElement === dialog.current)) { event.preventDefault(); nodes[nodes.length - 1]?.focus(); }
          else if (!event.shiftKey && (document.activeElement === nodes[nodes.length - 1] || document.activeElement === dialog.current)) { event.preventDefault(); nodes[0].focus(); }
        }
      }}>
      <header><div><h2>{t('Model management', '模型管理')}</h2><p>{t('Manage connections. Each conversation keeps its own model.', '管理接入配置，每个会话独立选择模型。')}</p></div>
        <button type="button" className="plain-icon" disabled={busy} onClick={onClose} aria-label={t('Close model management', '关闭模型管理')}><Icon name="close" /></button></header>
      {error && <p className="model-manager-alert" role="alert">{error}</p>}
      {notice && <p className="model-manager-notice" role="status">{notice}</p>}
      {deleteTarget ? <div className="model-confirm"><h3>{t('Delete connection?', '删除接入配置？')}</h3><p>{deleteTarget.display_name}</p><p>{t('Credentials will be removed. Conversation history stays available; select another model to continue.', '将删除凭据。历史会话仍可查看，选择其他模型后可以继续。')}</p><div className="model-actions"><button disabled={busy} onClick={() => setDeleteTarget(null)}>{t('Cancel', '取消')}</button><button disabled={busy} onClick={() => void act(async () => { await deleteModelConfiguration(deleteTarget); setDeleteTarget(null); }, t('Connection deleted', '接入配置已删除'))}>{t('Delete configuration', '删除配置')}</button></div></div>
      : testTarget ? <div className="model-confirm"><h3>{t('Test connection?', '测试连接？')}</h3><p>{t('Sends a short test message to this endpoint. May incur a small charge. No conversation content is sent.', '将向此接口发送简短测试消息，可能产生少量费用，不会发送会话内容。')}</p><strong>{testTarget.display_name}</strong><div className="model-actions"><button disabled={busy} onClick={() => setTestTarget(null)}>{t('Cancel', '取消')}</button><button disabled={busy} onClick={() => void act(async () => { await testModelConfiguration(testTarget); setTestTarget(null); }, t('Connection and request format accepted. This does not certify analysis quality.', '连接与请求格式验证通过，不代表分析质量已验证。'))}>{busy ? t('Testing…', '测试中…') : t('Run test', '开始测试')}</button></div></div>
      : editing ? <form className="model-editor" onSubmit={event => { event.preventDefault(); void act(async () => {
        await saveModelConfiguration({ ...draft, ...(editing !== 'new' ? { config_id: editing.config_id, revision: editing.revision } : {}) });
        setEditing(null);
      }, t('Saved. The current conversation model was not changed.', '已保存，当前会话使用的模型未改变。')); }}>
        <h3>{editing === 'new' ? t('Add connection', '添加模型接入') : t('Edit connection', '编辑模型接入')}</h3>
        <fieldset disabled={busy}>
          <label>{t('Display name', '显示名称')}<input required value={draft.display_name} onChange={event => setDraft({ ...draft, display_name: event.target.value })} placeholder={t('Qwen · local server', 'Qwen · 本地服务')} /></label>
          <label>{t('Model ID', '模型 ID')}<input required value={draft.upstream_model_id} onChange={event => setDraft({ ...draft, upstream_model_id: event.target.value })} /></label>
          <label>Base URL<input required type="url" value={draft.base_url} onChange={event => setDraft({ ...draft, base_url: event.target.value })} placeholder="http://localhost:1234/v1" /></label>
          <label>API Key<input type="password" autoComplete="new-password" value={draft.api_key} onChange={event => setDraft({ ...draft, api_key: event.target.value })} placeholder={editing === 'new' ? t('Optional for servers without authentication', '无认证的本地服务可留空') : t('Leave blank to keep the saved key', '留空保留已保存的密钥')} /></label>
          {editing !== 'new' && <label className="model-checkbox"><input type="checkbox" checked={draft.clear_api_key} onChange={event => setDraft({ ...draft, clear_api_key: event.target.checked })} />{t('Remove saved API key', '清除已保存的密钥')}</label>}
          <div className="model-editor-grid"><label>{t('Context window (tokens)', '上下文窗口（tokens）')}<input required type="number" min="2048" max="2000000" value={draft.context_window} onChange={event => setDraft({ ...draft, context_window: Number(event.target.value) })} /></label><label>{t('Output limit (tokens)', '输出上限（tokens）')}<input required type="number" min="128" max={draft.context_window - 1} value={draft.max_output_tokens} onChange={event => setDraft({ ...draft, max_output_tokens: Number(event.target.value) })} /></label></div>
          <label className="model-checkbox"><input type="checkbox" checked={draft.tools} onChange={event => setDraft({ ...draft, tools: event.target.checked })} />{t('Supports tool calling (required for graph analysis)', '支持工具调用（图分析必需）')}</label>
          <label className="model-checkbox"><input type="checkbox" checked={draft.vision} onChange={event => setDraft({ ...draft, vision: event.target.checked })} />{t('Supports image input', '支持图片输入')}</label>
          <p className="model-muted">{t('Use the limits and capabilities documented by your endpoint.', '请按接入服务的实际能力和限制填写。')}</p>
        </fieldset><div className="model-actions"><button type="button" disabled={busy} onClick={() => setEditing(null)}>{t('Cancel', '取消')}</button><button type="submit" disabled={busy}>{busy ? t('Saving…', '保存中…') : t('Save configuration', '保存配置')}</button></div>
      </form> : <>
        <div className="model-manager-toolbar"><input aria-label={t('Search models', '搜索模型')} placeholder={t('Search models', '搜索模型')} value={query} onChange={event => setQuery(event.target.value)} /><button disabled={busy} onClick={() => edit('new')}><Icon name="plus" size={15} />{t('Add model', '添加模型')}</button></div>
        <p className="model-muted">{t('The default applies only to new conversations.', '默认模型只影响新建会话。')}</p>
        {(!catalog.default_model || !catalog.models.some(item => item.config_id === catalog.default_model && !item.unavailable_reason)) && <p role="status" className="model-manager-alert">{t('The default model is unavailable. Choose a new default below.', '默认模型不可用，请在下方重新设置。')}</p>}
        <div className="model-manager-list">{rows.map(item => <article className="model-card" key={item.config_id}>
          <div className="model-card-heading"><strong>{item.display_name}</strong>{catalog.default_model === item.config_id && <span className="model-badge">{t('Default', '默认')}</span>}<span className="model-muted">{item.source === 'deployment' ? t('Deployment config', '部署配置') : t('Custom connection', '自定义接入')}</span></div>
          <div className="model-muted model-identifiers">{item.upstream_model_id}<span>{item.base_url}</span></div>
          {item.unavailable_reason && <p className="model-unavailable">{modelErrorMessage(item.unavailable_reason)}</p>}
          <div className="model-actions">
            <button disabled={busy || Boolean(item.unavailable_reason) || item.config_id === catalog.default_model} onClick={() => void act(() => setDefaultModelConfiguration(item.config_id), t('Default updated for new conversations', '已更新新会话默认模型'))}>{t('Set as default', '设为默认')}</button>
            <button disabled={busy || item.unavailable_reason === 'MODEL_NOT_CONFIGURED'} onClick={() => setTestTarget(item)}>{t('Test connection', '测试连接')}</button>
            {item.source === 'custom' && <button disabled={busy} onClick={() => edit(item)}>{t('Edit', '编辑')}</button>}
            <button disabled={busy} onClick={() => void act(() => setModelEnabled(item, !item.enabled))}>{item.enabled ? t('Disable', '停用') : t('Enable', '启用')}</button>
            {item.source === 'custom' && <button disabled={busy} onClick={() => setDeleteTarget(item)}>{t('Delete', '删除')}</button>}
          </div>
        </article>)}</div>
        {!rows.length && <p>{t('No matching models', '没有匹配的模型')}</p>}
      </>}
    </section>
  </div>;
}

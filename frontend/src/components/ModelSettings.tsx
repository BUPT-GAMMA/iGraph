import { useEffect, useRef, useState, type FormEvent } from 'react';
import { createPortal } from 'react-dom';
import {
  deleteModelConfiguration, deleteModelProvider, discoverProviderModels, importProviderModels, listModelProviders, listModels,
  modelErrorMessage, saveModelConfiguration, saveModelProvider, setDefaultModel, setModelEnabled, testModelConfiguration,
  type DiscoveredModels, type ModelCatalog, type ModelConfiguration, type ModelProviderConnection,
  type ModelWrite, type ProviderPreset, type ProviderWrite,
} from '../api';
import { useI18n } from '../i18n';
import { Icon } from './Icon';
import { ModelProviderIcon } from './ModelProviderIcon';
import type { ModelProvider } from './modelProviders';
import './ModelSettings.css';

type Props = { onClose: () => void; onChanged: (catalog: ModelCatalog) => void };
type View = 'connection' | 'discover' | 'model';
const freshConnection = (): ProviderWrite => ({ name: '', preset_id: 'custom', protocol: 'openai-compatible', base_url: '', enabled: true, api_key: '', clear_api_key: false });
const freshModel = (): Omit<ModelWrite, 'provider_id' | 'provider_revision'> => ({ upstream_model_id: '', display_name: '', tools: true, vision: false, context_window: 131072, max_output_tokens: 4096 });
function draftFor(provider: ModelProviderConnection): ProviderWrite {
  return { provider_id: provider.provider_id, revision: provider.revision, name: provider.name, preset_id: provider.preset_id,
    protocol: provider.protocol, base_url: provider.base_url, enabled: provider.enabled, api_key: '', clear_api_key: false };
}

export function ModelSettings({ onClose, onChanged }: Props) {
  const { t } = useI18n();
  const [catalog, setCatalog] = useState<ModelCatalog>({ models: [], default_model: '' });
  const [providers, setProviders] = useState<ModelProviderConnection[]>([]);
  const [presets, setPresets] = useState<ProviderPreset[]>([]);
  const [selected, setSelected] = useState('deployment');
  const [draft, setDraft] = useState<ProviderWrite>(freshConnection);
  const [view, setView] = useState<View>('connection');
  const [modelDraft, setModelDraft] = useState(freshModel);
  const [discovered, setDiscovered] = useState<DiscoveredModels | null>(null);
  const [checked, setChecked] = useState<string[]>([]);
  const [search, setSearch] = useState('');
  const [modelSearch, setModelSearch] = useState('');
  const [filter, setFilter] = useState('all');
  const [editIdentity, setEditIdentity] = useState(false);
  const [reveal, setReveal] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirm, setConfirm] = useState<{ text: string; action: () => void } | null>(null);
  const alive = useRef(true);
  const working = useRef(false);
  const dialog = useRef<HTMLDivElement>(null);
  const closeRef = useRef(() => {});
  const provider = providers.find(p => p.provider_id === selected);
  const isNew = selected === 'new';
  const deployment = selected === 'deployment';
  const dirty = !deployment && JSON.stringify(draft) !== JSON.stringify(provider ? draftFor(provider) : freshConnection());
  const ownModels = catalog.models.filter(m => deployment ? m.source === 'deployment' : m.provider_id === selected).sort((a, b) => a.display_name.localeCompare(b.display_name));
  const presetName = (preset: ProviderPreset) => preset.id === 'custom' ? t('Custom provider', '自定义提供商') : preset.id === 'qwen' ? t('Alibaba Cloud', '阿里云百炼') : preset.name;
  const providerIcon = (id: string) => (presets.find(p => p.id === id)?.icon || 'unknown') as ModelProvider;
  const protect = (action: () => void) => {
    if (working.current) return;
    if (dirty || view === 'model') setConfirm({ text: t('Discard unsaved changes?', '放弃尚未保存的修改？'), action });
    else action();
  };
  closeRef.current = () => protect(onClose);

  useEffect(() => {
    alive.current = true;
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeRef.current(); }
      if (event.key !== 'Tab') return;
      const nodes = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]') || []).filter(e => e.getClientRects().length);
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', keydown, true);
    void reload().catch(cause => { if (alive.current) setError(cause.message); }).finally(() => { if (alive.current) setLoading(false); });
    return () => { alive.current = false; document.body.style.overflow = overflow; document.removeEventListener('keydown', keydown, true); previous?.focus(); };
  }, []);

  async function reload(selectId?: string) {
    const [connections, models] = await Promise.all([listModelProviders(), listModels()]);
    if (!alive.current) return;
    setProviders(connections.providers); setPresets(connections.presets); setCatalog(models); onChanged(models);
    if (selectId) {
      const next = connections.providers.find(p => p.provider_id === selectId);
      setSelected(next ? selectId : 'deployment');
      setDraft(next ? draftFor(next) : freshConnection());
    }
  }
  async function run(label: string, operation: () => Promise<void>) {
    if (working.current) return;
    working.current = true; setBusy(label); setError(''); setNotice('');
    try { await operation(); }
    catch (cause) { if (alive.current) setError(cause instanceof Error ? cause.message : t('Request failed', '请求失败')); }
    finally { working.current = false; if (alive.current) setBusy(''); }
  }
  function choose(id: string) {
    protect(() => {
      const next = providers.find(p => p.provider_id === id);
      setSelected(id); setDraft(next ? draftFor(next) : freshConnection()); setView('connection'); setEditIdentity(false);
      setError(''); setNotice(''); setDiscovered(null); setChecked([]); setModelSearch(''); setFilter('all'); setReveal(false);
    });
  }
  function usePreset(preset: ProviderPreset) {
    setDraft(current => ({ ...current, preset_id: preset.id, name: presetName(preset), base_url: preset.base_url }));
  }
  function saveConnection(event: FormEvent) {
    event.preventDefault();
    void run('save', async () => {
      const result = await saveModelProvider(draft);
      if (!alive.current) return;
      setReveal(false); setEditIdentity(false); setDraft(draftFor(result.provider)); setSelected(result.provider.provider_id);
      await reload(result.provider.provider_id); setNotice(t('Connection saved', '连接配置已保存'));
    });
  }
  function editModel(model?: ModelConfiguration) {
    setModelDraft(model ? { config_id: model.config_id, revision: model.revision, upstream_model_id: model.upstream_model_id,
      display_name: model.display_name, tools: model.tools, vision: model.vision,
      context_window: model.context_window, max_output_tokens: model.max_output_tokens } : freshModel());
    setError(''); setNotice(''); setView('model');
  }
  function saveModel(event: FormEvent) {
    event.preventDefault();
    if (!provider) return;
    void run('model-save', async () => {
      await saveModelConfiguration({ ...modelDraft, provider_id: provider.provider_id, provider_revision: provider.revision });
      await reload(); if (!alive.current) return;
      setView('connection'); setNotice(t('Model saved. Select it in the conversation to use it.', '模型已保存，可在会话中选择使用。'));
    });
  }
  function discover(showList: boolean) {
    if (!provider) return;
    void run('discover', async () => {
      const result = await discoverProviderModels(provider);
      if (!alive.current) return;
      setDiscovered(result); setChecked([]);
      if (showList) { setView('discover'); setModelSearch(''); }
      else setNotice(t(`Connected · ${result.latency_ms} ms · ${result.models.length} models found`, `连接成功 · ${result.latency_ms} ms · 发现 ${result.models.length} 个模型`));
    });
  }
  async function importModels() {
    if (!provider || !discovered) return;
    // Each bounded batch commits atomically and skips duplicates on retry.
    let added = 0;
    try {
      for (let offset = 0; offset < checked.length; offset += 200) {
        const batch = checked.slice(offset, offset + 200);
        const result = await importProviderModels(provider, batch.map(id => discovered.models.find(m => m.id === id)!));
        added += result.added_count;
        if (!alive.current) return;
        setChecked(current => current.filter(value => !batch.includes(value)));
      }
      setView('connection');
    } finally {
      await reload();
      if (alive.current && added) setNotice(t(`${added} models added`, `已添加 ${added} 个模型`));
    }
  }
  const searchMatch = (id: string, name: string) => `${id} ${name}`.toLowerCase().includes(modelSearch.trim().toLowerCase());
  const visibleModels = ownModels.filter(m => searchMatch(m.upstream_model_id, m.display_name)
    && (filter === 'all' || (filter === 'enabled' && !m.unavailable_reason) || (filter === 'vision' && m.vision)));
  const addedIds = new Set(ownModels.map(m => m.upstream_model_id));
  const remoteModels = discovered?.models.filter(m => searchMatch(m.id, m.name)) || [];
  const importable = remoteModels.filter(m => !addedIds.has(m.id));
  const heading = deployment ? t('Deployment models', '系统模型') : isNew ? t('Add provider', '添加提供商') : provider?.name || '';
  const back = () => protect(() => { setView('connection'); setError(''); });

  return createPortal(<div className="provider-settings-overlay" onMouseDown={e => { if (e.target === e.currentTarget) closeRef.current(); }}>
    <div ref={dialog} tabIndex={-1} className="provider-settings" role="dialog" aria-modal="true" aria-labelledby="model-services-title">
      <header className="pm-header"><div className="pm-heading-icon"><Icon name="settings" size={20} /></div><div><h2 id="model-services-title">{t('Model services', '模型服务')}</h2><p>{t('Your providers. Your models. One workspace.', '连接你的模型，融入每一次图分析。')}</p></div><button className="pm-icon-button pm-close" onClick={() => closeRef.current()} disabled={!!busy} aria-label={t('Close model services', '关闭模型服务')}><Icon name="close" /></button></header>
      {confirm && <div className="pm-confirm" role="alert"><span>{confirm.text}</span><button onClick={() => setConfirm(null)}>{t('Keep editing', '取消')}</button><button className="pm-danger" onClick={() => { const action = confirm.action; setConfirm(null); action(); }}>{t('Confirm', '确认')}</button></div>}
      <div className="pm-layout">
        <aside className="pm-sidebar"><label className="pm-search"><Icon name="search" size={16} /><input value={search} onChange={e => setSearch(e.target.value)} placeholder={t('Search providers', '搜索提供商')} aria-label={t('Search providers', '搜索提供商')} /></label>
          <div className="pm-sidebar-label">{t('PROVIDERS', '提供商')}<span>{providers.length}</span></div>
          <nav aria-label={t('Model providers', '模型提供商')}>
            <button className={`pm-provider ${deployment ? 'is-selected' : ''}`} onClick={() => choose('deployment')} disabled={!!busy}><span className="pm-avatar is-system"><Icon name="cube" size={20} /></span><span><strong>{t('Deployment models', '系统模型')}</strong><small>{t('Managed by administrator', '由管理员配置')}</small></span><Icon name="chevron" size={13} /></button>
            {providers.filter(p => p.name.toLowerCase().includes(search.trim().toLowerCase())).map(p => <button key={p.provider_id} className={`pm-provider ${p.provider_id === selected ? 'is-selected' : ''}`} onClick={() => choose(p.provider_id)} disabled={!!busy} aria-pressed={p.provider_id === selected}>
              <span className={`pm-avatar preset-${p.preset_id}`}><ModelProviderIcon provider={providerIcon(p.preset_id)} size={22} /></span><span><strong>{p.name}</strong><small>{t(`${p.model_count} models`, `${p.model_count} 个模型`)}</small></span><i className={`pm-status-dot ${p.enabled ? 'is-enabled' : ''}`} aria-label={p.enabled ? t('Enabled', '已启用') : t('Disabled', '已停用')} /></button>)}
            {!loading && !providers.length && <p className="pm-sidebar-empty">{t('Add a provider to connect your own models.', '添加提供商，接入自己的模型。')}</p>}
          </nav><button className="pm-add-provider" disabled={!!busy} onClick={() => choose('new')}><Icon name="plus" size={17} />{t('Add provider', '添加提供商')}</button>
        </aside>
        <main className="pm-main" aria-busy={loading || !!busy}>
          <div className="pm-title-row"><div className="pm-title"><span className="pm-avatar pm-avatar-large">{deployment ? <Icon name="cube" size={24} /> : <ModelProviderIcon provider={providerIcon(draft.preset_id)} size={27} />}</span><div><h3>{heading}</h3><span>{deployment ? t('Available in your iGraph account', '当前账户可使用的系统预置模型') : 'OpenAI Chat Completions'}</span></div></div><div className="pm-title-actions">
            {provider && view === 'connection' && <button className="pm-icon-button" aria-label={t('Edit provider', '编辑提供商')} title={t('Edit provider', '编辑提供商')} disabled={!!busy} onClick={() => setEditIdentity(!editIdentity)}><Icon name="edit" size={16} /></button>}
            {provider && <button role="switch" aria-checked={provider.enabled} aria-label={t('Enable provider', '启用提供商')} className={`pm-switch ${provider.enabled ? 'is-on' : ''}`} disabled={!!busy || dirty || view !== 'connection'} onClick={() => void run('provider-state', async () => { const result = await saveModelProvider({ ...draftFor(provider), enabled: !provider.enabled }); await reload(result.provider.provider_id); })}><span /></button>}
            <button className="pm-icon-button" disabled={!!busy || loading} aria-label={t('Refresh model services', '刷新模型服务')} title={t('Refresh', '刷新')} onClick={() => protect(() => void run('reload', () => reload(selected)))}><Icon name="refresh" size={17} /></button>
          </div></div>
          {error && <div className="pm-feedback is-error" role="alert"><Icon name="info" size={16} /><span>{error}</span></div>}
          {notice && <div className="pm-feedback is-success" role="status"><Icon name="check" size={16} /><span>{notice}</span></div>}
          {loading ? <div className="pm-empty"><span className="loader" /><p>{t('Loading model services…', '正在加载模型服务…')}</p></div> : <>
            {view === 'connection' && <>
              {!deployment && <form className="pm-connection" onSubmit={saveConnection}>
                {isNew && <div className="pm-preset-grid">{presets.map(p => <button key={p.id} type="button" className={`pm-preset ${draft.preset_id === p.id ? 'is-selected' : ''}`} disabled={!!busy} onClick={() => usePreset(p)}><ModelProviderIcon provider={providerIcon(p.id)} size={20} /><span>{presetName(p)}</span>{draft.preset_id === p.id && <Icon name="check" size={13} />}</button>)}</div>}
                <div className="pm-form-grid">{(isNew || editIdentity) && <><label>{t('Provider name', '提供商名称')}<input required maxLength={120} value={draft.name} disabled={!!busy} onChange={e => setDraft({ ...draft, name: e.target.value })} placeholder={t('e.g. My model gateway', '例如：我的模型网关')} /></label>
                  <label>{t('Provider type', '提供商类型')}<select value={draft.preset_id} disabled={!!busy} onChange={e => { const p = presets.find(p => p.id === e.target.value); if (p) usePreset(p); }}>{presets.map(p => <option key={p.id} value={p.id}>{presetName(p)}</option>)}</select></label></>}
                  <label className="pm-wide">{t('API key', 'API 密钥')}<span className="pm-key-field"><input aria-label={t('API key', 'API 密钥')} type={reveal ? 'text' : 'password'} autoComplete="new-password" spellCheck={false} maxLength={8192} value={draft.api_key || ''} disabled={!!busy || draft.clear_api_key} onChange={e => setDraft({ ...draft, api_key: e.target.value })} placeholder={provider?.has_api_key ? t('Saved · leave blank to keep', '已保存 · 留空保留现有密钥') : t('Enter API key (optional for local services)', '输入 API 密钥，本地服务可留空')} /><button type="button" aria-label={reveal ? t('Hide API key', '隐藏 API 密钥') : t('Show API key', '显示 API 密钥')} onClick={() => setReveal(!reveal)} disabled={!!busy}><Icon name={reveal ? 'eye-off' : 'eye'} size={17} /></button></span></label>
                  {provider?.has_api_key && <label className="pm-check pm-wide"><input type="checkbox" checked={!!draft.clear_api_key} disabled={!!busy} onChange={e => setDraft({ ...draft, clear_api_key: e.target.checked, api_key: '' })} />{t('Clear saved key (for services without authentication)', '清除已保存的密钥（用于无需认证的服务）')}</label>}
                  <label className="pm-wide">{t('API base URL', 'API 地址')}<input type="url" aria-label={t('API base URL', 'API 地址')} aria-describedby="pm-endpoint-preview" required autoComplete="off" spellCheck={false} value={draft.base_url} disabled={!!busy} onChange={e => setDraft({ ...draft, base_url: e.target.value })} placeholder="https://api.example.com/v1" /><small id="pm-endpoint-preview" className="pm-endpoint">{t('Request URL: ', '请求地址：')}{draft.base_url.trim().replace(/\/+$/, '') || 'https://api.example.com/v1'}/chat/completions</small></label>
                </div>
                <p className="pm-hint">{draft.preset_id === 'ollama' ? t('In Docker, use host.docker.internal to reach Ollama on your computer. Enable network access in Ollama.', 'Docker 中通过 host.docker.internal 访问本机 Ollama，需在 Ollama 中允许网络访问。') : t('Use a Chat Completions compatible API base URL, including its version path. All models below share this connection.', '填写兼容 Chat Completions 的 API 基础地址，包含版本路径。下方模型共用此连接。')}</p>
                <div className="pm-form-actions"><span>{dirty ? t('Unsaved changes', '有未保存的修改') : isNew ? t('Save the connection to add models', '保存连接后即可添加模型') : t('Connection saved', '连接已保存')}</span><button type="button" className="pm-button" disabled={!!busy || !provider || dirty} onClick={() => discover(false)}>{busy === 'discover' ? t('Checking…', '检测中…') : t('Test connection', '检测连接')}</button><button type="submit" className="pm-button is-primary" disabled={!!busy || (!dirty && !isNew)}>{busy === 'save' ? t('Saving…', '保存中…') : t('Save connection', '保存连接')}</button></div>
              </form>}
              {!isNew && <section className="pm-model-section"><div className="pm-section-head"><h4>{t('Models', '模型')}<span>{ownModels.length}</span></h4>{!deployment && <div className="pm-actions"><button className="pm-button" disabled={!!busy || dirty || !provider} onClick={() => editModel()}><Icon name="plus" size={15} />{t('Add model', '添加模型')}</button><button className="pm-button is-primary" disabled={!!busy || dirty || !provider} onClick={() => discover(true)}><Icon name="list-check" size={15} />{t('Manage models', '管理模型')}</button></div>}</div>
                {!!ownModels.length && <div className="pm-model-toolbar"><label className="pm-search"><Icon name="search" size={16} /><input value={modelSearch} onChange={e => setModelSearch(e.target.value)} placeholder={t('Search model ID or name', '搜索模型 ID 或名称')} aria-label={t('Search added models', '搜索已添加模型')} /></label><div className="pm-filters" role="group" aria-label={t('Filter models', '筛选模型')}>{[['all', t('All', '全部')], ['enabled', t('Available', '可用')], ['vision', t('Vision', '视觉')]].map(([id, name]) => <button key={id} aria-pressed={filter === id} className={filter === id ? 'is-active' : ''} onClick={() => setFilter(id)}>{name}</button>)}</div></div>}
                <div className="pm-model-list">{visibleModels.map(m => <article key={m.config_id} className={`pm-model-row ${m.unavailable_reason ? 'is-unavailable' : ''}`}>
                  <span className="pm-model-logo"><ModelProviderIcon modelId={m.upstream_model_id} size={22} /></span><div className="pm-model-copy"><strong title={m.display_name}>{m.display_name}</strong><code title={m.upstream_model_id}>{m.upstream_model_id}</code><div className="pm-badges">{m.tools && <span><Icon name="tool" size={11} />{t('Tools', '工具')}</span>}{m.vision && <span className="is-vision"><Icon name="eye" size={11} />{t('Vision', '视觉')}</span>}<span className="is-neutral">{Math.round(m.context_window / 1024)}K {t('context', '上下文')}</span>{catalog.default_model === m.config_id && <span className="is-default">{t('Default', '默认')}</span>}</div>{m.unavailable_reason && <small className="pm-model-reason">{modelErrorMessage(m.unavailable_reason)}</small>}</div>
                  <div className="pm-model-actions"><button className="pm-icon-button" disabled={!!busy || !!m.unavailable_reason || catalog.default_model === m.config_id} title={t('Set as default for new conversations', '设为新会话默认模型')} aria-label={t(`Set ${m.display_name} as default`, `将 ${m.display_name} 设为默认`)} onClick={() => void run(m.config_id, async () => { await setDefaultModel(m.config_id); await reload(); })}><Icon name="star" size={16} /></button><button className="pm-icon-button" disabled={!!busy || dirty} title={t('Test model', '检测模型')} aria-label={t(`Test ${m.display_name}`, `检测 ${m.display_name}`)} onClick={() => void run(m.config_id, async () => { await testModelConfiguration(m); if (alive.current) setNotice(t(`${m.display_name}: model request succeeded`, `${m.display_name}：模型请求成功`)); })}><Icon name="pulse" size={16} /></button>{!deployment && <><button className="pm-icon-button" disabled={!!busy || dirty} title={t('Edit model', '编辑模型')} aria-label={t(`Edit ${m.display_name}`, `编辑 ${m.display_name}`)} onClick={() => editModel(m)}><Icon name="settings" size={16} /></button><button className="pm-icon-button pm-delete" disabled={!!busy || dirty} title={t('Remove model', '移除模型')} aria-label={t(`Remove ${m.display_name}`, `移除 ${m.display_name}`)} onClick={() => setConfirm({ text: t(`Remove ${m.display_name}? Conversations using it will need another model.`, `移除 ${m.display_name}？使用它的会话需要重新选择模型。`), action: () => void run(m.config_id, async () => { await deleteModelConfiguration(m); await reload(); }) })}><Icon name="trash" size={15} /></button></>}<button role="switch" aria-checked={m.enabled} className={`pm-switch pm-switch-small ${m.enabled ? 'is-on' : ''}`} disabled={!!busy || dirty} aria-label={t(`Enable ${m.display_name}`, `启用 ${m.display_name}`)} onClick={() => void run(m.config_id, async () => { await setModelEnabled(m, !m.enabled); await reload(); })}><span /></button></div>
                </article>)}</div>
                {!visibleModels.length && <div className="pm-empty"><span className="pm-empty-icon"><Icon name="cube" size={28} /></span><strong>{ownModels.length ? t('No matching models', '没有匹配的模型') : t('Your models start here', '从添加第一个模型开始')}</strong><p>{ownModels.length ? t('Try a different search or filter.', '试试其他关键词或筛选条件。') : t('Discover models from your provider, or add a model ID manually.', '从提供商获取模型列表，或手动添加模型 ID。')}</p></div>}
                {deployment && <p className="pm-hint">{t('Deployment connections are managed by your administrator. Default models apply to new conversations; existing conversations keep their selection.', '系统连接由管理员维护。默认模型用于新会话，已有会话保留各自选择。')}</p>}
              </section>}
              {provider && <div className="pm-provider-footer"><span>{t('Changes apply to future requests. Active tasks keep their original connection.', '修改用于后续请求，运行中的任务继续使用原连接。')}</span><button disabled={!!busy || dirty} onClick={() => setConfirm({ text: t(`Delete provider ${provider.name}? Remove its models first.`, `删除提供商 ${provider.name}？请先移除其下的所有模型。`), action: () => void run('delete', async () => { await deleteModelProvider(provider); await reload('deployment'); }) })}>{t('Delete provider', '删除提供商')}</button></div>}
            </>}
            {view === 'model' && <form className="pm-model-editor" onSubmit={saveModel}><button type="button" className="pm-back" onClick={back} disabled={!!busy}><Icon name="arrow-left" size={16} />{t('Back to provider', '返回提供商')}</button><h4>{modelDraft.config_id ? t('Edit model', '编辑模型') : t('Add model', '添加模型')}</h4><p className="pm-hint">{t('Use the exact model ID from your provider. Capabilities and token limits should match its documentation.', '模型 ID 需与提供商一致，能力与 Token 上限请按模型文档填写。')}</p><div className="pm-form-grid">
              <label className="pm-wide">{t('Model ID', '模型 ID')}<input autoFocus required maxLength={256} value={modelDraft.upstream_model_id} disabled={!!busy} onChange={e => setModelDraft({ ...modelDraft, upstream_model_id: e.target.value })} placeholder="qwen3.6-plus" /></label>
              <label className="pm-wide">{t('Display name', '显示名称')}<input maxLength={120} value={modelDraft.display_name} disabled={!!busy} onChange={e => setModelDraft({ ...modelDraft, display_name: e.target.value })} placeholder={modelDraft.upstream_model_id || t('Optional alias', '可选，便于识别的名称')} /></label>
              <label>{t('Context window (tokens)', '上下文窗口（Token）')}<input type="number" required min={2048} max={2000000} value={modelDraft.context_window} disabled={!!busy} onChange={e => setModelDraft({ ...modelDraft, context_window: Number(e.target.value) })} /></label>
              <label>{t('Max output (tokens)', '最大输出（Token）')}<input type="number" required min={128} max={modelDraft.context_window - 1} value={modelDraft.max_output_tokens} disabled={!!busy} onChange={e => setModelDraft({ ...modelDraft, max_output_tokens: Number(e.target.value) })} /></label>
              <label className="pm-capability"><input type="checkbox" checked={modelDraft.tools} disabled={!!busy} onChange={e => setModelDraft({ ...modelDraft, tools: e.target.checked })} /><Icon name="tool" size={18} /><span>{t('Tool calling', '工具调用')}<small>{t('Required for graph analysis', '图分析必需')}</small></span></label><label className="pm-capability"><input type="checkbox" checked={modelDraft.vision} disabled={!!busy} onChange={e => setModelDraft({ ...modelDraft, vision: e.target.checked })} /><Icon name="eye" size={18} /><span>{t('Vision', '视觉理解')}<small>{t('Accepts image input', '支持图像输入')}</small></span></label>
            </div>{!modelDraft.tools && <p className="pm-feedback">{t('Models without tool calling can be saved but cannot be selected for graph analysis.', '不支持工具调用的模型可保存，但无法用于图分析会话。')}</p>}<div className="pm-form-actions"><button type="button" className="pm-button" onClick={back} disabled={!!busy}>{t('Cancel', '取消')}</button><button className="pm-button is-primary" type="submit" disabled={!!busy}>{busy ? t('Saving…', '保存中…') : t('Save model', '保存模型')}</button></div></form>}
            {view === 'discover' && <section className="pm-discovery"><button className="pm-back" onClick={() => { setView('connection'); setError(''); }} disabled={!!busy}><Icon name="arrow-left" size={16} />{t('Back to provider', '返回提供商')}</button><div className="pm-section-head"><h4>{t('Manage models', '管理模型')}<span>{discovered?.models.length || 0}</span></h4><button className="pm-button" onClick={() => discover(true)} disabled={!!busy}><Icon name="refresh" size={15} />{t('Refresh', '刷新')}</button></div><p className="pm-hint">{t('Select the chat models you want to add. Check tool support and context limits in model settings after importing.', '选择要添加的对话模型，导入后请在模型设置中核对工具调用能力和上下文上限。')}</p><label className="pm-search"><Icon name="search" size={16} /><input value={modelSearch} onChange={e => setModelSearch(e.target.value)} placeholder={t('Search model ID or name', '搜索模型 ID 或名称')} aria-label={t('Search discovered models', '搜索发现的模型')} /></label><div className="pm-discovery-summary"><span>{t(`${checked.length} selected`, `已选 ${checked.length} 个`)}</span><button className="pm-text-button" disabled={!!busy || !importable.length} onClick={() => setChecked(current => Array.from(new Set([...current, ...importable.map(m => m.id)])))}>{t('Select all results', '选择全部搜索结果')}</button><button className="pm-text-button" disabled={!!busy || !checked.length} onClick={() => setChecked([])}>{t('Clear', '清空')}</button></div><div className="pm-remote-list">{remoteModels.map(m => <label key={m.id} className={`pm-remote-row ${addedIds.has(m.id) ? 'is-added' : ''}`}><input type="checkbox" checked={addedIds.has(m.id) || checked.includes(m.id)} disabled={!!busy || addedIds.has(m.id)} onChange={e => setChecked(current => e.target.checked ? [...current, m.id] : current.filter(id => id !== m.id))} /><ModelProviderIcon modelId={m.id} size={20} /><span><strong>{m.name}</strong><code>{m.id}</code></span>{addedIds.has(m.id) && <small>{t('Added', '已添加')}</small>}</label>)}</div>{!remoteModels.length && <div className="pm-empty"><p>{t('No models found. Try another search or add a model manually.', '未找到模型，可更换关键词或手动添加。')}</p></div>}{discovered?.truncated && <p className="pm-hint">{t('Showing the first 5,000 models. Other IDs can be added manually.', '显示前 5,000 个模型，其他型号可手动添加。')}</p>}<div className="pm-form-actions"><button className="pm-button" disabled={!!busy} onClick={() => editModel()}>{t('Add manually', '手动添加')}</button><button className="pm-button is-primary" disabled={!!busy || !checked.length} onClick={() => void run('import', importModels)}>{busy === 'import' ? t('Adding…', '正在添加…') : t(`Add selected (${checked.length})`, `添加所选（${checked.length}）`)}</button></div></section>}
          </>}
        </main>
      </div>
    </div>
  </div>, document.body);
}

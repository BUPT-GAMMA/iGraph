import { Popup, PopupBackdrop, PopupPresence } from './PopupMotion';
import { useIsPresent } from 'motion/react';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { createPortal } from 'react-dom';
import {
  deleteModelConfiguration, deleteModelProvider, discoverProviderModels, importProviderModels, getProviderCredentials, listModelProviders, listModels,
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
type ModelDraft = Omit<ModelWrite, 'provider_id' | 'provider_revision'>;
const connectionFields = ['name', 'preset_id', 'protocol', 'base_url', 'enabled'] as const;
const modelFields = ['upstream_model_id', 'display_name', 'tools', 'vision', 'context_window', 'max_output_tokens'] as const;
const freshConnection = (): ProviderWrite => ({ name: '', preset_id: 'custom', protocol: 'openai-compatible', base_url: '', enabled: true, api_key: '', clear_api_key: false });
const freshModel = (): ModelDraft => ({ upstream_model_id: '', display_name: '', tools: true, vision: false, context_window: 131072, max_output_tokens: 4096 });
function draftFor(provider: ModelProviderConnection, apiKey = ''): ProviderWrite {
  return { provider_id: provider.provider_id, revision: provider.revision, name: provider.name, preset_id: provider.preset_id,
    protocol: provider.protocol, base_url: provider.base_url, enabled: provider.enabled, api_key: apiKey, clear_api_key: false };
}

export function ModelSettings({ onClose, onChanged }: Props) {
  const { t } = useI18n();
  const present = useIsPresent();
  const presentRef = useRef(present);
  presentRef.current = present;
  const [catalog, setCatalog] = useState<ModelCatalog>({ models: [], default_model: '' });
  const [providers, setProviders] = useState<ModelProviderConnection[]>([]);
  const [presets, setPresets] = useState<ProviderPreset[]>([]);
  const [selected, setSelected] = useState('');
  const [draft, setDraft] = useState<ProviderWrite>(freshConnection);
  const [connectionBaseline, setConnectionBaseline] = useState<ProviderWrite>(freshConnection);
  const [view, setView] = useState<View>('connection');
  const [modelDraft, setModelDraft] = useState(freshModel);
  const [modelBaseline, setModelBaseline] = useState(freshModel);
  const [discovered, setDiscovered] = useState<DiscoveredModels | null>(null);
  const [checked, setChecked] = useState<string[]>([]);
  const [search, setSearch] = useState('');
  const [modelSearch, setModelSearch] = useState('');
  const [filter, setFilter] = useState('all');
  const [editIdentity, setEditIdentity] = useState(false);
  const [reveal, setReveal] = useState(false);
  const [credential, setCredential] = useState<{ provider_id: string; revision: number; api_key: string } | null>(null);
  const [credentialError, setCredentialError] = useState('');
  const [credentialReload, setCredentialReload] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirm, setConfirm] = useState<{ title: string; detail?: string; label: string; action: () => void } | null>(null);
  const alive = useRef(true);
  const working = useRef(false);
  const dialog = useRef<HTMLDivElement>(null);
  const confirmation = useRef<HTMLDivElement>(null);
  const closeRef = useRef(() => {});
  const provider = providers.find(p => p.provider_id === selected);
  const isNew = selected === 'new';
  const editable = provider?.editable !== false;
  const credentialMatches = !!provider && credential?.provider_id === provider.provider_id && credential.revision === provider.revision;
  const keyReady = !provider || !editable || credentialMatches;
  const savedKey = credentialMatches ? credential.api_key : '';
  const dirty = editable && (connectionFields.some(field => draft[field] !== connectionBaseline[field])
    || (draft.clear_api_key ? '' : draft.api_key || '') !== (connectionBaseline.api_key || ''));
  const modelDirty = view === 'model' && modelFields.some(field => modelDraft[field] !== modelBaseline[field]);
  const ownModels = catalog.models.filter(m => m.provider_id === selected).sort((a, b) => a.display_name.localeCompare(b.display_name));
  const presetName = (preset: ProviderPreset) => preset.id === 'custom' ? t('Custom provider', '自定义提供商') : preset.id === 'qwen' ? t('Alibaba Cloud', '阿里云百炼') : preset.name;
  const providerIcon = (id: string) => (presets.find(p => p.id === id)?.icon || 'unknown') as ModelProvider;
  const protect = (action: () => void) => {
    if (working.current) return;
    if (dirty || modelDirty) setConfirm({ title: t('Discard changes?', '放弃修改？'), label: t('Discard', '放弃'), action });
    else action();
  };
  function resetConnection(next: ProviderWrite) {
    setDraft(next); setConnectionBaseline(next);
  }
  function resetModel(model?: ModelConfiguration) {
    const next: ModelDraft = model ? { config_id: model.config_id, revision: model.revision, upstream_model_id: model.upstream_model_id,
      display_name: model.display_name, tools: model.tools, vision: model.vision,
      context_window: model.context_window, max_output_tokens: model.max_output_tokens } : freshModel();
    setModelDraft(next); setModelBaseline(next);
  }
  closeRef.current = () => { if (confirm) setConfirm(null); else protect(onClose); };

  useEffect(() => {
    if (!confirm) return;
    const previous = document.activeElement as HTMLElement | null;
    confirmation.current?.querySelector<HTMLButtonElement>('button')?.focus();
    return () => { if (previous?.isConnected) previous.focus(); };
  }, [confirm]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(''), 3000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    alive.current = true;
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (!presentRef.current) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeRef.current(); }
      if (event.key !== 'Tab') return;
      const scope = confirmation.current?.dataset.popupState === 'open' ? confirmation.current : dialog.current;
      const nodes = Array.from(scope?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]') || []).filter(e => e.getClientRects().length);
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === scope)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === scope)) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener('keydown', keydown, true);
    void reload().catch(cause => { if (alive.current) setError(cause.message); }).finally(() => { if (alive.current) setLoading(false); });
    return () => { alive.current = false; document.body.style.overflow = overflow; document.removeEventListener('keydown', keydown, true); if (!document.activeElement?.closest('[data-popup-state="open"][aria-modal="true"], dialog[open]')) previous?.focus(); };
  }, []);

  useEffect(() => {
    setReveal(false); setCredential(null); setCredentialError('');
    if (!provider || !editable) return;
    const controller = new AbortController();
    let cancelled = false;
    void getProviderCredentials(provider, controller.signal).then(result => {
      if (cancelled) return;
      setCredential(result);
      // Loading saved credentials changes the baseline too, without erasing edits to other fields.
      const withKey = (current: ProviderWrite) => current.provider_id === result.provider_id && current.revision === result.revision
        ? { ...current, api_key: result.api_key, clear_api_key: false } : current;
      setDraft(withKey); setConnectionBaseline(withKey);
    }).catch(cause => {
      if (!cancelled) setCredentialError(cause instanceof Error ? cause.message : t('Could not load API key', '读取密钥失败'));
    });
    return () => { cancelled = true; controller.abort(); };
  }, [provider?.provider_id, provider?.revision, editable, credentialReload]);

  async function reload(selectId?: string) {
    const [connections, models] = await Promise.all([listModelProviders(), listModels()]);
    if (!alive.current) return;
    setProviders(connections.providers); setPresets(connections.presets); setCatalog(models); onChanged(models);
    const target = selectId ?? selected;
    const defaultProvider = models.models.find(m => m.config_id === models.default_model)?.provider_id;
    const next = connections.providers.find(p => p.provider_id === target)
      || (target !== 'new' ? connections.providers.find(p => p.provider_id === defaultProvider) || connections.providers[0] : undefined);
    setSelected(next?.provider_id || 'new');
    if (selectId !== undefined || !dirty) {
      resetConnection(next ? draftFor(next) : freshConnection());
      setCredential(null); setCredentialError(''); setCredentialReload(value => value + 1);
    }
    if (selectId !== undefined && view === 'model') {
      const latest = models.models.find(m => m.config_id === modelDraft.config_id && m.provider_id === next?.provider_id);
      resetModel(latest);
      if (modelDraft.config_id && !latest) setView('connection');
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
    if (id === selected) return;
    protect(() => {
      const next = providers.find(p => p.provider_id === id);
      setSelected(id); resetConnection(next ? draftFor(next) : freshConnection()); setView('connection'); setEditIdentity(false);
      setError(''); setNotice(''); setDiscovered(null); setChecked([]); setModelSearch(''); setFilter('all'); setReveal(false);
    });
  }
  function usePreset(preset: ProviderPreset) {
    setDraft(current => {
      const previous = presets.find(p => p.id === current.preset_id);
      const name = !current.name || (previous && current.name === presetName(previous)) ? presetName(preset) : current.name;
      return { ...current, preset_id: preset.id, name, base_url: preset.base_url };
    });
  }
  function saveConnection(event: FormEvent) {
    event.preventDefault();
    if (!isNew && !keyReady) return;
    void run('save', async () => {
      const result = await saveModelProvider(draft);
      if (!alive.current) return;
      setReveal(false); setEditIdentity(false); resetConnection(draftFor(result.provider)); setSelected(result.provider.provider_id);
      await reload(result.provider.provider_id); if (!isNew) setNotice(t('Saved', '已保存'));
    });
  }
  function editModel(model?: ModelConfiguration) {
    resetModel(model);
    setError(''); setNotice(''); setView('model');
  }
  function saveModel(event: FormEvent) {
    event.preventDefault();
    if (!provider) return;
    void run('model-save', async () => {
      await saveModelConfiguration({ ...modelDraft, provider_id: provider.provider_id, provider_revision: provider.revision });
      await reload(); if (!alive.current) return;
      setView('connection'); setNotice(t('Saved', '已保存'));
    });
  }
  function discover(showList: boolean) {
    if (!provider) return;
    void run('discover', async () => {
      const result = await discoverProviderModels(provider);
      if (!alive.current) return;
      setDiscovered(result); setChecked([]);
      if (showList) { setView('discover'); setModelSearch(''); }
      else setNotice(t(`Connected · ${result.latency_ms} ms`, `连接成功 · ${result.latency_ms} ms`));
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
  const heading = isNew ? t('Add provider', '添加提供商') : provider?.name || '';
  const back = () => protect(() => { setView('connection'); setError(''); });

  const connectionReady = editable && keyReady && !!provider?.base_url && !dirty;
  const endpoint = `${draft.base_url.trim().replace(/\/+$/, '')}/chat/completions`;
  const identityFields = <>
    <label>{t('Provider name', '提供商名称')}
      <input required maxLength={120} value={draft.name} disabled={!!busy} autoFocus={isNew}
        onChange={e => setDraft({ ...draft, name: e.target.value })} />
    </label>
    <label>{t('Provider type', '提供商类型')}
      <select value={draft.preset_id} disabled={!!busy} onChange={e => {
        const preset = presets.find(p => p.id === e.target.value); if (preset) usePreset(preset);
      }}>{presets.map(p => <option key={p.id} value={p.id}>{presetName(p)}</option>)}</select>
    </label>
  </>;
  function removeProvider() {
    if (!provider) return;
    const next = providers.find(p => p.provider_id !== provider.provider_id)?.provider_id || 'new';
    setConfirm({
      title: t(`Delete “${provider.name}”?`, `删除“${provider.name}”？`),
      detail: provider.scope === 'shared' ? t('Removes this provider and its models for all users.', '所有账户将无法使用此提供商及其模型。')
        : ownModels.length ? t(`Also removes ${ownModels.length} models.`, `同时删除 ${ownModels.length} 个模型。`) : undefined,
      label: t('Delete', '删除'),
      action: () => void run('delete', async () => {
        await deleteModelProvider(provider);
        if (!alive.current) return;
        setView('connection'); setEditIdentity(false); setDiscovered(null); setChecked([]); setModelSearch('');
        await reload(next);
      }),
    });
  }

  return createPortal(<PopupBackdrop className="provider-settings-overlay" onMouseDown={e => {
    if (e.target === e.currentTarget && !confirm) closeRef.current();
  }}>
    <Popup.div ref={dialog} tabIndex={-1} className="provider-settings" role="dialog" aria-modal={!confirm} aria-labelledby="model-services-title">
      <header className="pm-header">
        <h2 id="model-services-title">{t('Model services', '模型服务')}</h2>
        <button className="pm-icon-button pm-close" onClick={() => closeRef.current()} disabled={!!busy}
          aria-label={t('Close model services', '关闭模型服务')}><Icon name="close" /></button>
      </header>
      <div className="pm-layout">
        <aside className="pm-sidebar">
          <label className="pm-search"><Icon name="search" size={16} /><input value={search} onChange={e => setSearch(e.target.value)}
            placeholder={t('Search providers', '搜索提供商')} aria-label={t('Search providers', '搜索提供商')} /></label>
          <nav aria-label={t('Model providers', '模型提供商')}>
            {providers.filter(p => p.name.toLowerCase().includes(search.trim().toLowerCase())).map(p =>
              <button key={p.provider_id} className={`pm-provider ${p.provider_id === selected ? 'is-selected' : ''}`}
                onClick={() => choose(p.provider_id)} disabled={!!busy} aria-pressed={p.provider_id === selected}>
                <span className={`pm-avatar preset-${p.preset_id}`}>{p.preset_id === 'zhizengzeng' ? <b>Z</b> : <ModelProviderIcon provider={providerIcon(p.preset_id)} size={22} />}</span>
                <strong title={p.name}>{p.name}</strong>
                <i className={`pm-status-dot ${p.enabled ? 'is-enabled' : ''}`} aria-label={p.enabled ? t('Enabled', '已启用') : t('Disabled', '已停用')} />
              </button>)}
          </nav>
          <button className="pm-add-provider" disabled={!!busy} onClick={() => choose('new')}>
            <Icon name="plus" size={17} />{t('Add provider', '添加提供商')}
          </button>
        </aside>
        <main className="pm-main" aria-busy={loading || !!busy}>
          <div className="pm-title-row">
            <h3 title={!editable ? t('Managed by administrators', '由管理员维护') : undefined}>{heading}</h3>
            <div className="pm-title-actions">
              {provider && editable && view === 'connection' && <button className="pm-icon-button" aria-label={t('Edit provider', '编辑提供商')}
                title={t('Edit provider', '编辑提供商')} disabled={!!busy} onClick={() => setEditIdentity(!editIdentity)}><Icon name="edit" size={16} /></button>}
              {provider && editable && <button className="pm-icon-button pm-delete" aria-label={t('Delete provider', '删除提供商')}
                title={t('Delete provider', '删除提供商')} disabled={!!busy} onClick={removeProvider}><Icon name="trash" size={16} /></button>}
              {provider && editable && <button role="switch" aria-checked={provider.enabled} aria-label={t('Enable provider', '启用提供商')}
                className={`pm-switch ${provider.enabled ? 'is-on' : ''}`} disabled={!!busy || !keyReady || dirty || view !== 'connection'}
                onClick={() => void run('provider-state', async () => {
                  const result = await saveModelProvider({ ...draftFor(provider), enabled: !provider.enabled });
                  await reload(result.provider.provider_id);
                })}><span /></button>}
              <button className="pm-icon-button" disabled={!!busy || loading} aria-label={t('Refresh model services', '刷新模型服务')}
                title={t('Refresh', '刷新')} onClick={() => protect(() => void run('reload', () => reload(selected)))}><Icon name="refresh" size={17} /></button>
            </div>
          </div>
          {(error || credentialError) && <div className="pm-feedback is-error" role="alert"><Icon name="info" size={16} /><span>{error || credentialError}</span></div>}
          {notice && <div className="pm-feedback is-success" role="status"><Icon name="check" size={16} /><span>{notice}</span></div>}
          {loading ? <div className="pm-empty" role="status"><span className="loader" /><span>{t('Loading…', '加载中…')}</span></div> : <>
            {view === 'connection' && <>
              {isNew && <form className="pm-create" onSubmit={saveConnection}>
                <div className="pm-form-grid">{identityFields}</div>
                <div className="pm-form-actions"><button className="pm-button is-primary" type="submit" disabled={!!busy || !draft.name.trim()}>
                  {busy === 'save' ? t('Adding…', '添加中…') : t('Add', '添加')}
                </button></div>
              </form>}
              {provider && <form className="pm-connection" onSubmit={saveConnection}>
                <div className="pm-form-grid">
                  {editIdentity && identityFields}
                  <label className="pm-wide">{t('API key', 'API 密钥')}
                    <span className="pm-key-field">
                      <input aria-label={t('API key', 'API 密钥')} type={reveal ? 'text' : 'password'} autoComplete="new-password"
                        spellCheck={false} maxLength={8192} value={editable && keyReady ? draft.api_key || '' : ''}
                        disabled={!!busy || !editable || !keyReady} aria-busy={editable && !keyReady && !credentialError}
                        onChange={e => setDraft({ ...draft, api_key: e.target.value, clear_api_key: !!savedKey && !e.target.value })}
                        placeholder={!editable ? (provider.has_api_key ? t('Configured', '已配置') : t('Not configured', '未配置'))
                          : !keyReady ? (credentialError ? t('Load failed', '读取失败') : t('Loading…', '加载中…')) : 'API Key'} />
                      <button type="button" aria-label={reveal ? t('Hide API key', '隐藏 API 密钥') : t('Show API key', '显示 API 密钥')}
                        onClick={() => setReveal(!reveal)} disabled={!!busy || !editable || !keyReady}><Icon name={reveal ? 'eye-off' : 'eye'} size={17} /></button>
                      <button type="button" className="pm-key-check" disabled={!!busy || !connectionReady} onClick={() => discover(false)}>
                        {busy === 'discover' ? t('Checking…', '检测中…') : t('Test', '检测')}
                      </button>
                    </span>
                  </label>
                  <label className="pm-wide"><span className="pm-field-label">{t('API base URL', 'API 地址')}
                    <span title={`OpenAI Chat Completions${draft.base_url ? ` · ${endpoint}` : ''}`}><Icon name="info" size={13} /></span>
                  </span><input type="url" aria-label={t('API base URL', 'API 地址')} autoComplete="off" spellCheck={false}
                    value={draft.base_url} disabled={!!busy || !editable} onChange={e => setDraft({ ...draft, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></label>
                </div>
                {editable && <div className="pm-form-actions"><button type="submit" className="pm-button is-primary" disabled={!!busy || !keyReady || !dirty}>
                  {busy === 'save' ? t('Saving…', '保存中…') : t('Save', '保存')}
                </button></div>}
              </form>}
              {!isNew && <section className="pm-model-section">
                <div className="pm-section-head"><h4>{t('Models', '模型')}<span>{ownModels.length}</span></h4>
                  {editable && <div className="pm-actions">
                    <button className="pm-button" disabled={!!busy || !connectionReady} onClick={() => editModel()}><Icon name="plus" size={15} />{t('Add', '添加')}</button>
                    <button className="pm-button is-primary" disabled={!!busy || !connectionReady} onClick={() => discover(true)}><Icon name="list-check" size={15} />{t('Manage', '管理')}</button>
                  </div>}
                </div>
                {!!ownModels.length && <div className="pm-model-toolbar">
                  <label className="pm-search"><Icon name="search" size={16} /><input value={modelSearch} onChange={e => setModelSearch(e.target.value)}
                    placeholder={t('Search models', '搜索模型')} aria-label={t('Search added models', '搜索已添加模型')} /></label>
                  <div className="pm-filters" role="group" aria-label={t('Filter models', '筛选模型')}>
                    {[["all", t('All', '全部')], ["enabled", t('Available', '可用')], ["vision", t('Vision', '视觉')]].map(([id, name]) =>
                      <button key={id} aria-pressed={filter === id} className={filter === id ? 'is-active' : ''} onClick={() => setFilter(id)}>{name}</button>)}
                  </div>
                </div>}
                <div className="pm-model-list">{visibleModels.map(m => <article key={m.config_id} className={`pm-model-row ${m.unavailable_reason ? 'is-unavailable' : ''}`}>
                  <span className="pm-model-logo"><ModelProviderIcon modelId={m.upstream_model_id} size={22} /></span>
                  <div className="pm-model-copy">
                    <div className="pm-model-name"><strong title={m.display_name}>{m.display_name}</strong>
                      {m.display_name !== m.upstream_model_id && <code title={m.upstream_model_id}>{m.upstream_model_id}</code>}
                    </div>
                    <div className="pm-badges">
                      {m.tools && <span title={t('Tool calling', '工具调用')} aria-label={t('Tool calling', '工具调用')}><Icon name="tool" size={13} /></span>}
                      {m.vision && <span className="is-vision" title={t('Vision', '视觉')} aria-label={t('Vision', '视觉')}><Icon name="eye" size={13} /></span>}
                      <span className="is-neutral" title={t('Context window', '上下文窗口')}>{Math.round(m.context_window / 1024)}K</span>
                      {catalog.default_model === m.config_id && <span className="is-default">{t('Default', '默认')}</span>}
                      {m.unavailable_reason && <span className="is-neutral" title={modelErrorMessage(m.unavailable_reason)}><Icon name="info" size={13} /></span>}
                    </div>
                  </div>
                  <div className="pm-model-actions">
                    <button className="pm-icon-button" disabled={!!busy || !!m.unavailable_reason || catalog.default_model === m.config_id}
                      title={t('Set as default', '设为默认')} aria-label={t(`Set ${m.display_name} as default`, `将 ${m.display_name} 设为默认`)}
                      onClick={() => void run(m.config_id, async () => { await setDefaultModel(m.config_id); await reload(); })}><Icon name="star" size={16} /></button>
                    <button className="pm-icon-button" disabled={!!busy || dirty} title={t('Test model', '检测模型')}
                      aria-label={t(`Test ${m.display_name}`, `检测 ${m.display_name}`)} onClick={() => void run(m.config_id, async () => {
                        await testModelConfiguration(m); if (alive.current) setNotice(t('Test passed', '检测通过'));
                      })}><Icon name="pulse" size={16} /></button>
                    {editable && <>
                      <button className="pm-icon-button" disabled={!!busy || dirty} title={t('Edit model', '编辑模型')}
                        aria-label={t(`Edit ${m.display_name}`, `编辑 ${m.display_name}`)} onClick={() => editModel(m)}><Icon name="settings" size={16} /></button>
                      <button className="pm-icon-button pm-delete" disabled={!!busy || dirty} title={t('Remove model', '移除模型')}
                        aria-label={t(`Remove ${m.display_name}`, `移除 ${m.display_name}`)} onClick={() => setConfirm({
                          title: t(`Remove “${m.display_name}”?`, `移除“${m.display_name}”？`), label: t('Remove', '移除'),
                          action: () => void run(m.config_id, async () => { await deleteModelConfiguration(m); await reload(); }),
                        })}><Icon name="trash" size={15} /></button>
                    </>}
                    <button role="switch" aria-checked={m.enabled} className={`pm-switch pm-switch-small ${m.enabled ? 'is-on' : ''}`} disabled={!!busy || dirty}
                      aria-label={t(`Enable ${m.display_name}`, `启用 ${m.display_name}`)} onClick={() => void run(m.config_id, async () => { await setModelEnabled(m, !m.enabled); await reload(); })}><span /></button>
                  </div>
                </article>)}</div>
                {!visibleModels.length && <div className="pm-empty">{ownModels.length ? t('No matching models', '无匹配模型') : t('No models', '暂无模型')}</div>}
              </section>}
            </>}
            {view === 'model' && <form className="pm-model-editor" onSubmit={saveModel}>
              <button type="button" className="pm-back" onClick={back} disabled={!!busy}><Icon name="arrow-left" size={16} />{t('Back', '返回')}</button>
              <h4>{modelDraft.config_id ? t('Edit model', '编辑模型') : t('Add model', '添加模型')}</h4>
              <div className="pm-form-grid">
                <label className="pm-wide">{t('Model ID', '模型 ID')}<input autoFocus required maxLength={256} value={modelDraft.upstream_model_id} disabled={!!busy}
                  onChange={e => setModelDraft({ ...modelDraft, upstream_model_id: e.target.value })} /></label>
                <label className="pm-wide">{t('Display name', '显示名称')}<input maxLength={120} value={modelDraft.display_name} disabled={!!busy}
                  onChange={e => setModelDraft({ ...modelDraft, display_name: e.target.value })} placeholder={modelDraft.upstream_model_id} /></label>
                <label>{t('Context window', '上下文窗口')}<input type="number" required min={2048} max={2000000} value={modelDraft.context_window} disabled={!!busy}
                  onChange={e => setModelDraft({ ...modelDraft, context_window: Number(e.target.value) })} /></label>
                <label>{t('Max output', '最大输出')}<input type="number" required min={128} max={modelDraft.context_window - 1} value={modelDraft.max_output_tokens} disabled={!!busy}
                  onChange={e => setModelDraft({ ...modelDraft, max_output_tokens: Number(e.target.value) })} /></label>
                <label className="pm-capability"><input type="checkbox" checked={modelDraft.tools} disabled={!!busy}
                  onChange={e => setModelDraft({ ...modelDraft, tools: e.target.checked })} /><Icon name="tool" size={18} />{t('Tool calling', '工具调用')}</label>
                <label className="pm-capability"><input type="checkbox" checked={modelDraft.vision} disabled={!!busy}
                  onChange={e => setModelDraft({ ...modelDraft, vision: e.target.checked })} /><Icon name="eye" size={18} />{t('Vision', '视觉')}</label>
              </div>
              {!modelDraft.tools && <p className="pm-feedback">{t('Graph analysis requires tool calling.', '图分析需要工具调用。')}</p>}
              <div className="pm-form-actions"><button type="button" className="pm-button" onClick={back} disabled={!!busy}>{t('Cancel', '取消')}</button>
                <button className="pm-button is-primary" type="submit" disabled={!!busy}>{busy ? t('Saving…', '保存中…') : t('Save', '保存')}</button></div>
            </form>}
            {view === 'discover' && <section className="pm-discovery">
              <button className="pm-back" onClick={() => { setView('connection'); setError(''); }} disabled={!!busy}><Icon name="arrow-left" size={16} />{t('Back', '返回')}</button>
              <div className="pm-section-head"><h4>{t('Manage models', '管理模型')}<span>{discovered?.models.length || 0}</span></h4>
                <button className="pm-icon-button" aria-label={t('Refresh discovered models', '刷新发现的模型')} title={t('Refresh', '刷新')}
                  onClick={() => discover(true)} disabled={!!busy}><Icon name="refresh" size={16} /></button></div>
              <label className="pm-search"><Icon name="search" size={16} /><input value={modelSearch} onChange={e => setModelSearch(e.target.value)}
                placeholder={t('Search models', '搜索模型')} aria-label={t('Search discovered models', '搜索发现的模型')} /></label>
              <div className="pm-discovery-summary"><span>{t(`${checked.length} selected`, `已选 ${checked.length} 个`)}</span>
                <button className="pm-text-button" disabled={!!busy || !importable.length} onClick={() => setChecked(current => Array.from(new Set([...current, ...importable.map(m => m.id)])))}>{t('Select all', '全选')}</button>
                <button className="pm-text-button" disabled={!!busy || !checked.length} onClick={() => setChecked([])}>{t('Clear', '清空')}</button></div>
              <div className="pm-remote-list">{remoteModels.map(m => <label key={m.id} className={`pm-remote-row ${addedIds.has(m.id) ? 'is-added' : ''}`}>
                <input type="checkbox" checked={addedIds.has(m.id) || checked.includes(m.id)} disabled={!!busy || addedIds.has(m.id)}
                  onChange={e => setChecked(current => e.target.checked ? [...current, m.id] : current.filter(id => id !== m.id))} />
                <ModelProviderIcon modelId={m.id} size={20} /><span><strong>{m.name}</strong>{m.name !== m.id && <code>{m.id}</code>}</span>
                {addedIds.has(m.id) && <small>{t('Added', '已添加')}</small>}
              </label>)}</div>
              {!remoteModels.length && <div className="pm-empty">{t('No matching models', '无匹配模型')}</div>}
              {discovered?.truncated && <p className="pm-feedback">{t('Showing 5,000 models', '已显示 5,000 个模型')}</p>}
              <div className="pm-form-actions"><button className="pm-button" disabled={!!busy} onClick={() => editModel()}>{t('Add manually', '手动添加')}</button>
                <button className="pm-button is-primary" disabled={!!busy || !checked.length} onClick={() => void run('import', importModels)}>
                  {busy === 'import' ? t('Adding…', '添加中…') : t(`Add selected (${checked.length})`, `添加所选（${checked.length}）`)}
                </button></div>
            </section>}
          </>}
        </main>
      </div>
      <PopupPresence>{confirm && <PopupBackdrop className="pm-confirm-overlay" onMouseDown={e => { if (e.target === e.currentTarget) setConfirm(null); }}>
        <Popup.div ref={confirmation} className="pm-confirm" role="alertdialog" aria-modal="true" aria-labelledby="pm-confirm-title" aria-describedby={confirm.detail ? 'pm-confirm-detail' : undefined}>
          <h4 id="pm-confirm-title">{confirm.title}</h4>
          {confirm.detail && <p id="pm-confirm-detail">{confirm.detail}</p>}
          <div className="pm-form-actions"><button className="pm-button" onClick={() => setConfirm(null)}>{t('Cancel', '取消')}</button>
            <button className="pm-button is-danger" onClick={() => { const action = confirm.action; setConfirm(null); action(); }}>{confirm.label}</button></div>
        </Popup.div>
      </PopupBackdrop>}</PopupPresence>
    </Popup.div>
  </PopupBackdrop>, document.body);
}

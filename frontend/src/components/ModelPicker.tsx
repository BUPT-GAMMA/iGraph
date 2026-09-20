import { createContext, useContext, useEffect, useRef, useState } from 'react';
import { modelErrorMessage, type ModelCatalog } from '../api';
import { useI18n } from '../i18n';
import { Icon } from './Icon';
import { ModelProviderIcon } from './ModelProviderIcon';
import { getModelProviderMeta } from './modelProviders';

export const ModelCatalogContext = createContext<{
  catalog: ModelCatalog | null; userId: string; unavailable?: string | null;
  manage: () => void; refresh: () => void;
} | null>(null);

export function ModelPicker({ model, disabled, onChange }: { model: string; disabled: boolean; onChange: (id: string) => void }) {
  const context = useContext(ModelCatalogContext)!;
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const recentKey = `igraph:recent-models:${context.userId}`;
  const [recent, setRecent] = useState<string[]>(() => {
    try { const value = JSON.parse(localStorage.getItem(recentKey) || '[]'); return Array.isArray(value) ? value.filter(item => typeof item === 'string').slice(0, 3) : []; } catch { return []; }
  });
  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    const outside = (event: MouseEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('mousedown', outside);
    return () => document.removeEventListener('mousedown', outside);
  }, [open]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  const current = context.catalog?.models.find(item => item.config_id === model);
  const options = (context.catalog?.models || []).filter(item => `${item.display_name} ${item.upstream_model_id} ${item.base_url}`.toLowerCase().includes(query.toLowerCase()));
  const groups = new Map<string, typeof options>();
  for (const item of options) {
    const label = item.source === 'custom' ? t('Custom connections', '自定义接入') : getModelProviderMeta(item.upstream_model_id).label;
    groups.set(label, [...(groups.get(label) || []), item]);
  }
  const choose = (id: string) => {
    setOpen(false); trigger.current?.focus(); onChange(id);
    const next = [id, ...recent.filter(value => value !== id)].slice(0, 3);
    setRecent(next); try { localStorage.setItem(recentKey, JSON.stringify(next)); } catch { /* Optional preference. */ }
  };
  const option = (item: typeof options[number], prefix = '') => <button key={prefix + item.config_id} type="button" className="model-picker-option" aria-pressed={item.config_id === model} disabled={Boolean(item.unavailable_reason)} onClick={() => choose(item.config_id)}>
    <ModelProviderIcon modelId={item.upstream_model_id} size={16} /><span><strong>{item.display_name}</strong><small>{item.source === 'custom' ? `${item.upstream_model_id} · ${item.base_url}` : item.upstream_model_id}{item.unavailable_reason && ` · ${modelErrorMessage(item.unavailable_reason)}`}</small></span>{item.config_id === model && <Icon name="check" size={14} />}
  </button>;
  return <div className="model-picker" ref={root} onKeyDown={event => {
    if (event.key === 'Escape') { setOpen(false); trigger.current?.focus(); }
    if (open && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault(); const nodes = Array.from(root.current?.querySelectorAll<HTMLElement>('.model-picker-menu input, .model-picker-menu button:not(:disabled)') || []);
      const index = nodes.indexOf(document.activeElement as HTMLElement);
      nodes[(index + (event.key === 'ArrowDown' ? 1 : nodes.length - 1)) % nodes.length]?.focus();
    }
  }}>
    <button ref={trigger} type="button" className="composer-model-trigger" disabled={disabled} aria-label={t('Select model', '选择模型')} aria-expanded={open} onClick={() => { setQuery(''); setOpen(!open); if (!open) context.refresh(); }} title={current?.display_name || model}>
      <ModelProviderIcon modelId={current?.upstream_model_id || model} size={14} /><span className="composer-model-value">{current?.display_name || (model.startsWith('custom-') ? t('Unavailable model', '不可用模型') : model)}</span><Icon name="chevron" size={10} />
    </button>
    {open && <div className="model-picker-menu" role="group" aria-label={t('Available models', '可用模型')}>
      <input ref={search} aria-label={t('Search models', '搜索模型')} placeholder={t('Search models or endpoints', '搜索模型或接入地址')} value={query} onChange={event => setQuery(event.target.value)} />
      <div className="model-picker-scroll">
        {!query && recent.some(id => options.some(item => item.config_id === id && !item.unavailable_reason)) && <><div className="composer-model-section">{t('Recently selected', '最近选择')}</div>{recent.map(id => options.find(item => item.config_id === id && !item.unavailable_reason)).filter(Boolean).map(item => option(item!, 'recent-'))}</>}
        {[...groups].map(([label, items]) => <section key={label}><div className="composer-model-section">{label}</div>{items.map(item => option(item))}</section>)}
        {!options.length && <p className="model-muted">{context.catalog ? t('No matching models', '没有匹配的模型') : t('Model list unavailable. Open model management to retry.', '模型列表暂不可用，可打开模型管理重试。')}</p>}
      </div>
      <button className="composer-model-config" type="button" onClick={() => { setOpen(false); context.manage(); }}><Icon name="settings" size={15} />{t('Manage models', '管理模型')}</button>
    </div>}
  </div>;
}

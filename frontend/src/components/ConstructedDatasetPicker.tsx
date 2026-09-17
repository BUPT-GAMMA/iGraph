import { useEffect, useRef, useState } from 'react';
import { listConstructedDatasets } from '../api';
import type { Dataset } from '../types';
import { useI18n } from '../i18n';
import { Icon } from './Icon';
import './ConstructedDatasetPicker.css';

export function ConstructedDatasetPicker({ projectId, projectName, onImport, onClose }: {
  projectId: string; projectName: string; onImport: (dataset: Dataset) => Promise<void>; onClose: () => void;
}) {
  const { t } = useI18n();
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [pending, setPending] = useState('');
  const [query, setQuery] = useState('');
  const [attempt, setAttempt] = useState(0);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setDatasets([]);
    listConstructedDatasets(projectId, controller.signal).then(result => {
      if (!controller.signal.aborted) setDatasets(result.datasets || []);
    }).catch(cause => { if (!controller.signal.aborted) setError(cause.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [projectId, attempt]);
  async function select(dataset: Dataset) {
    if (pending) return;
    setPending(dataset.dataset_id); setError('');
    try { await onImport(dataset); if (mounted.current) onClose(); }
    catch (cause) { if (mounted.current) setError(cause instanceof Error ? cause.message : t('Import failed', '导入失败')); }
    finally { if (mounted.current) setPending(''); }
  }
  const matches = datasets.filter(dataset => `${dataset.name} ${dataset.description || ''}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  return <div className="modal-backdrop"><section className="project-dialog constructed-picker" role="dialog" aria-modal="true" aria-labelledby="constructed-picker-title">
    <div className="dialog-head"><h2 id="constructed-picker-title">{t('Load a constructed graph', '加载已构好的图')}</h2><button className="dialog-close" disabled={!!pending} onClick={onClose} aria-label={t('Close', '关闭')}><Icon name="close" size={17} /></button></div>
    <p>{t(`Import an independent copy into ${projectName}; the source is unchanged.`, `导入到「${projectName}」的独立副本，原数据不变。`)}</p>
    <input type="text" aria-label={t('Search constructed datasets', '搜索已构图数据集')} placeholder={t('Search name or description', '搜索名称或描述')} value={query} onChange={e => setQuery(e.target.value)} />
    {loading && <p role="status">{t('Loading…', '加载中…')}</p>}
    {error && <div className="dialog-error" role="alert">{error}<button type="button" disabled={!!pending} onClick={() => setAttempt(value => value + 1)}>{t('Retry', '重试')}</button></div>}
    {!loading && !error && !matches.length && <p>{datasets.length ? t('No matching datasets', '没有匹配的数据集') : t('No completed graphs yet. Build a personal dataset in “Create your graph” first.', '暂无已完成的图。请先在“创建你的图”中完成个人数据集构图。')}</p>}
    <div className="constructed-picker-list">{matches.map(dataset => <article key={dataset.dataset_id}>
      <div><strong>{dataset.name}</strong><p>{dataset.description || t('No description', '暂无描述')}</p><small>{t(`${Number(dataset.metadata?.num_nodes || 0)} nodes · ${Number(dataset.metadata?.num_edges || 0)} edges`, `${Number(dataset.metadata?.num_nodes || 0)} 个节点 · ${Number(dataset.metadata?.num_edges || 0)} 条边`)}</small></div>
      <button className="secondary-command" disabled={!!pending} onClick={() => void select(dataset)}>{pending === dataset.dataset_id ? t('Importing…', '导入中…') : t('Load into project', '加载到项目')}</button>
    </article>)}</div>
  </section></div>;
}

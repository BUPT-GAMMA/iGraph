import { PopupPresence } from './PopupMotion';
import { useEffect, useRef, useState } from 'react';
import { getConstructionState } from '../api';
import { useI18n } from '../i18n';
import type { Translate } from '../i18n';
import type { ConstructionState, CsvBundleSchema, Dataset } from '../types';
import { constructionStatus, supportsConstruction } from '../construction';
import { Icon } from './Icon';
import { CsvBundleDialog } from './CsvBundleDialog';

type Props = {
  datasets: Dataset[];
  busy?: boolean;
  onUpload: (file: File | File[], schema?: CsvBundleSchema, name?: string) => void;
  onConstruct: (dataset: Dataset) => void;
  onDelete?: (dataset: Dataset) => void;
  onStateChange?: (dataset: Dataset, state: ConstructionState) => void;
};

function localizedConstructionLabel(dataset: Dataset, t: Translate) {
  const construction = dataset.metadata?.construction as { revision?: number; status?: string } | undefined;
  const status = constructionStatus(dataset);
  if (status === 'building' || status === 'processing') return t('View progress', '查看进度');
  if (status === 'failed' || status === 'error') return t('Rebuild graph', '重新构图');
  if (status === 'algorithm_unavailable') return t('Algorithm unavailable', '算法待接入');
  return status === 'ready' || (!construction?.status && Number(construction?.revision) > 0)
    ? t('View graph build', '查看构图')
    : t('Build graph', '构图');
}

function sourceSummary(dataset: Dataset, t: Translate, formatNumber: (value: number) => string) {
  const construction = dataset.metadata?.construction as { revision?: number; status?: string } | undefined;
  const status = constructionStatus(dataset);
  const nodes = Number(dataset.metadata?.num_nodes);
  const edges = Number(dataset.metadata?.num_edges);
  if (status === 'building' || status === 'processing') return t('Graph construction is in progress. Reopen it to view progress.', '构图处理中，可重新进入查看进度');
  if (status === 'failed' || status === 'error') return t('The previous graph construction failed. The source file is still available.', '上次构图失败，源文件仍已保留');
  if (status === 'algorithm_unavailable') return t('The graph construction algorithm is not available yet. The source file is preserved.', '构图算法暂不可用，源文件已保留');
  if (status === 'ready' || (!construction?.status && Number(construction?.revision) > 0)) {
    if (Number.isFinite(nodes) && Number.isFinite(edges)) return t(`${formatNumber(nodes)} nodes · ${formatNumber(edges)} edges`, `${formatNumber(nodes)} 个节点 · ${formatNumber(edges)} 条边`);
    return t(`Graph built · Version ${construction?.revision || 1}`, `已构图 · 版本 ${construction?.revision || 1}`);
  }
  return t(`${dataset.original_filename} · Ready to build`, `${dataset.original_filename} · 已就绪，等待构图`);
}

function sourceType(dataset: Dataset) {
  return (dataset.file_type || dataset.original_filename.split('.').pop() || 'FILE').replace(/^\./, '').toUpperCase();
}

export function CreateGraphWorkspace({ datasets, busy = false, onUpload, onConstruct, onDelete, onStateChange }: Props) {
  const { language, t } = useI18n();
  const formatNumber = (value: number) => value.toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US');
  const inputRef = useRef<HTMLInputElement>(null);
  const [csvFiles, setCsvFiles] = useState<File[] | null>(null);
  const [selectionError, setSelectionError] = useState('');
  const stateCallback = useRef(onStateChange);
  stateCallback.current = onStateChange;
  const constructible = datasets.filter(supportsConstruction);
  const monitored = constructible.filter(dataset => ['building', 'processing'].includes(constructionStatus(dataset)));
  const monitorSignature = monitored.map(dataset => `${dataset.project_id || 'personal'}:${dataset.dataset_id}`).sort().join('|');

  useEffect(() => {
    if (!monitorSignature || !stateCallback.current) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const refresh = async () => {
      const updates = await Promise.allSettled(monitored.map(async dataset => ({ dataset, state: await getConstructionState(dataset, controller.signal) })));
      if (disposed) return;
      updates.forEach(update => { if (update.status === 'fulfilled') stateCallback.current?.(update.value.dataset, update.value.state); });
      if (!disposed) timer = setTimeout(() => void refresh(), 3000);
    };
    timer = setTimeout(() => void refresh(), 3000);
    return () => { disposed = true; controller.abort(); clearTimeout(timer); };
  }, [monitorSignature]);

  function chooseFile() { inputRef.current?.click(); }
  function selected(files: FileList | null) {
    const chosen = Array.from(files || []);
    if (!chosen.length) return;
    if (chosen.length === 1 && !chosen[0].name.toLowerCase().endsWith('.csv')) {
      setSelectionError(''); onUpload(chosen[0]); return;
    }
    if (chosen.every(file => file.name.toLowerCase().endsWith('.csv'))) {
      setSelectionError(''); setCsvFiles(chosen); return;
    }
    setSelectionError(t('Multiple-file graph construction accepts CSV files only.', '多文件构图只能同时选择 CSV 文件。'));
  }

  return <div className="create-graph-workspace">
    <header className="create-graph-header">
      <div className="create-graph-title">
        <span className="context-icon"><Icon name="graph" size={23} /></span>
        <div><h1>{t('Graph Construction Workspace', '构图工作台')}</h1></div>
      </div>
      <input ref={inputRef} type="file" hidden multiple accept=".sql,.csv,.txt" onChange={event => { selected(event.currentTarget.files); event.currentTarget.value = ''; }} />
      <button className="primary-command create-graph-upload-button" onClick={chooseFile} disabled={busy}><Icon name="upload" size={16} /> {t('Upload source', '上传数据源')}</button>
    </header>

    <div className="create-graph-content">
      <section className="create-graph-intro" aria-labelledby="create-graph-intro-title">
        <div className="create-graph-intro-copy">
          <h2 id="create-graph-intro-title">{t('Turn a data source into an analysis-ready graph', '把数据源变成可以分析的图')}</h2>
          {selectionError && <div className="csv-bundle-error" role="alert">{selectionError}</div>}
          <button className="primary-command" onClick={chooseFile} disabled={busy}><Icon name="plus" size={16} /> {t('Choose source file', '选择源文件')}</button>
        </div>
        <div className="create-graph-flow" aria-label={t('Graph construction workflow', '构图流程')}>
          <div className="create-graph-step">
            <div className="create-graph-step-head"><span>01</span><span className="create-graph-step-icon"><Icon name="file" size={20} /></span></div>
            <div className="create-graph-step-copy"><strong>{t('Add a data source', '添加数据源')}</strong><small>SQL / CSV / TXT</small></div>
          </div>
          <span className="create-graph-flow-arrow" aria-hidden="true"><Icon name="arrow-right" size={16} /></span>
          <div className="create-graph-step">
            <div className="create-graph-step-head"><span>02</span><span className="create-graph-step-icon"><Icon name="graph" size={20} /></span></div>
            <div className="create-graph-step-copy"><strong>{t('Generate and validate', '生成并验证')}</strong></div>
          </div>
          <span className="create-graph-flow-arrow" aria-hidden="true"><Icon name="arrow-right" size={16} /></span>
          <div className="create-graph-step">
            <div className="create-graph-step-head"><span>03</span><span className="create-graph-step-icon"><Icon name="check" size={20} /></span></div>
            <div className="create-graph-step-copy"><strong>{t('Save and optimize', '保存与优化')}</strong></div>
          </div>
        </div>
      </section>

      <section className="create-graph-sources" aria-labelledby="create-graph-sources-title">
        <div className="create-graph-section-head"><div><h2 id="create-graph-sources-title">{t('My graph sources', '我的构图数据源')}</h2></div><span>{t(`${formatNumber(constructible.length)} available`, `${formatNumber(constructible.length)} 个可用`)}</span></div>
        {constructible.length ? <div className="create-graph-source-grid">{constructible.map(dataset => <article className="create-graph-source-card" key={dataset.dataset_id}>
          <div className="create-graph-source-icon"><Icon name="database" size={20} /></div>
          <div className="create-graph-source-copy"><div><strong title={dataset.name}>{dataset.name}</strong><span>{sourceType(dataset)}</span></div><p>{sourceSummary(dataset, t, formatNumber)}</p>{dataset.description && <p title={dataset.description}>{dataset.description}</p>}</div>
          <div className="create-graph-source-actions">
            <button className="secondary-command" onClick={() => onConstruct(dataset)} disabled={busy} aria-label={`${localizedConstructionLabel(dataset, t)} ${dataset.name}`}>{localizedConstructionLabel(dataset, t)} <Icon name="arrow-right" size={14} /></button>
            {onDelete && <button className="plain-icon create-graph-delete" onClick={() => onDelete(dataset)} disabled={busy} title={t('Delete dataset', '删除数据集')} aria-label={t(`Delete dataset ${dataset.name}`, `删除数据集 ${dataset.name}`)}><Icon name="trash" size={16} /></button>}
          </div>
        </article>)}</div> : <div className="create-graph-empty">
          <span><Icon name="graph" size={34} /></span><strong>{t('No graph-ready data sources yet', '还没有可构图的数据源')}</strong><button className="secondary-command" onClick={chooseFile} disabled={busy}>{t('Upload your first source', '上传第一个数据源')}</button>
        </div>}
      </section>
    </div>
    <PopupPresence>{csvFiles && <CsvBundleDialog files={csvFiles} onCancel={() => setCsvFiles(null)} onSubmit={(files, schema, name) => { setCsvFiles(null); onUpload(files, schema, name); }} />}</PopupPresence>
  </div>;
}

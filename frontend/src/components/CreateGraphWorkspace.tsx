import { useExampleLabels } from '../userCases';
import { PopupPresence } from './PopupMotion';
import { useEffect, useRef, useState } from 'react';
import { getConstructionState } from '../api';
import { useI18n } from '../i18n';
import type { Translate } from '../i18n';
import type { ConstructionState, CsvBundleSchema, Dataset } from '../types';
import { canViewSavedGraph, constructionStatus, isMolecularGraph, isPrebuiltGraph, supportsConstruction } from '../construction';
import { Icon } from './Icon';
import { CsvBundleDialog } from './CsvBundleDialog';
import { SourceDataDialog } from './SourceDataPreview';
import './WorkspaceCatalog.css';
import './GraphSourceCatalog.css';

type Props = {
  datasets: Dataset[];
  busy?: boolean;
  onUpload: (file: File | File[], schema?: CsvBundleSchema, name?: string) => void;
  onConstruct: (dataset: Dataset) => void;
  onAnalyze?: (dataset: Dataset) => void;
  onDelete?: (dataset: Dataset) => void;
  onStateChange?: (dataset: Dataset, state: ConstructionState) => void;
};

function localizedConstructionLabel(dataset: Dataset, t: Translate) {
  const construction = dataset.metadata?.construction as { revision?: number; status?: string } | undefined;
  const status = constructionStatus(dataset);
  if (status === 'building' || status === 'processing') return t('View progress', '查看进度');
  if (status === 'draft') return t('Save graph result', '保存构图结果');
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
  if (isPrebuiltGraph(dataset)) {
    if (sourceType(dataset) === 'NPZ') {
      if (dataset.status === 'failed' || dataset.status === 'error') return t('Unable to load this graph.', '图加载失败。');
      if (dataset.status !== 'ready') return t('The graph is being prepared.', '图正在准备中。');
      return Number.isFinite(nodes) && Number.isFinite(edges)
        ? t(`Saved graph · ${formatNumber(nodes)} nodes · ${formatNumber(edges)} edges`, `已保存的图 · ${formatNumber(nodes)} 个节点 · ${formatNumber(edges)} 条边`)
        : t('Saved graph · Ready for analysis', '已保存的图 · 可直接开始分析');
    }
    if (dataset.status === 'failed' || dataset.status === 'error') return t('Unable to load this PT graph. Check the file format and upload it again.', 'PT 图加载失败，请检查文件格式后重新上传。');
    if (dataset.status !== 'ready') return t('The PT graph is being prepared.', 'PT 图正在准备中。');
    return Number.isFinite(nodes) && Number.isFinite(edges)
      ? t(`PT graph · ${formatNumber(nodes)} nodes · ${formatNumber(edges)} edges`, `PT 图 · ${formatNumber(nodes)} 个节点 · ${formatNumber(edges)} 条边`)
      : t('PT file uploaded · Graph structure is checked when loaded for analysis.', 'PT 文件已上传 · 开始分析时加载并校验图结构。');
  }
  if (status === 'building' || status === 'processing') return t('Graph construction is in progress. Reopen it to view progress.', '构图处理中，可重新进入查看进度');
  if (status === 'draft') return t('Graph draft ready. Save it before starting analysis.', '构图草稿已生成，保存后即可分析');
  if (status === 'failed' || status === 'error') return t('The previous graph construction failed. The source file is still available.', '上次构图失败，源文件仍已保留');
  if (status === 'algorithm_unavailable') return t('The graph construction algorithm is not available yet. The source file is preserved.', '构图算法暂不可用，源文件已保留');
  if (status === 'ready' || (!construction?.status && Number(construction?.revision) > 0)) {
    if (Number.isFinite(nodes) && Number.isFinite(edges)) return t(`${formatNumber(nodes)} nodes · ${formatNumber(edges)} edges`, `${formatNumber(nodes)} 个节点 · ${formatNumber(edges)} 条边`);
    return t(`Graph built · Version ${construction?.revision || 1}`, `已构图 · 版本 ${construction?.revision || 1}`);
  }
  return t(`${dataset.original_filename} · Ready to build`, `${dataset.original_filename} · 已就绪，等待构图`);
}

const sourceFormats = [
  { extension: 'csv', icon: 'table', label: ['CSV tables', 'CSV 表格'] },
  { extension: 'sql', icon: 'database', label: ['SQL dump', 'SQL 脚本'] },
  { extension: 'txt', icon: 'file', label: ['Text', '文本'] },
  { extension: 'pt', icon: 'graph', label: ['Saved graph', '已有图'] },
] as const;
const acceptedFiles = '.sql,.csv,.txt,.pt';

function sourceBadge(dataset: Dataset, t: Translate) {
  const status = constructionStatus(dataset);
  if (status === 'failed' || status === 'error') return { tone: 'failed', label: t('Failed', '处理失败') };
  if (status === 'building' || status === 'processing' || status === 'preparing') return { tone: 'running', label: t('Processing', '处理中') };
  if (status === 'draft') return { tone: 'warning', label: t('Unsaved draft', '待保存') };
  if (status === 'algorithm_unavailable') return { tone: 'warning', label: t('Unavailable', '算法待接入') };
  if (status === 'ready') return { tone: 'ready', label: t('Ready', '已就绪') };
  return { tone: 'neutral', label: t('Ready to build', '待构图') };
}

function sourceType(dataset: Dataset) {
  return (dataset.file_type || dataset.original_filename.split('.').pop() || 'FILE').replace(/^\./, '').toUpperCase();
}

export function CreateGraphWorkspace({ datasets, busy = false, onUpload, onConstruct, onAnalyze, onDelete, onStateChange }: Props) {
  const { language, t } = useI18n();
  const labels = useExampleLabels();
  const formatNumber = (value: number) => value.toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US');
  const inputRef = useRef<HTMLInputElement>(null);
  const [csvFiles, setCsvFiles] = useState<File[] | null>(null);
  const [selectionError, setSelectionError] = useState('');
  const [sourceFilter, setSourceFilter] = useState<'all' | 'examples' | 'own'>('all');
  const [query, setQuery] = useState('');
  const [sourceSample, setSourceSample] = useState<Dataset | null>(null);
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const stateCallback = useRef(onStateChange);
  stateCallback.current = onStateChange;
  const sources = datasets.filter(dataset => supportsConstruction(dataset) || isPrebuiltGraph(dataset));
  const text = query.trim().toLowerCase();
  const visibleSources = sources.filter(dataset => {
    const example = Boolean(dataset.metadata?.user_case_id);
    if ((sourceFilter === 'examples' && !example) || (sourceFilter === 'own' && example)) return false;
    const keywords = [dataset.name, labels.name(dataset.name), dataset.original_filename, sourceType(dataset)];
    return !text || keywords.some(value => value.toLowerCase().includes(text));
  });
  const monitored = sources.filter(dataset => !isPrebuiltGraph(dataset) && ['building', 'processing'].includes(constructionStatus(dataset)));
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

  function chooseFile(accept = acceptedFiles) {
    if (busy || !inputRef.current) return;
    inputRef.current.accept = accept;
    inputRef.current.click();
  }
  function selected(files: FileList | null) {
    if (busy) return;
    const chosen = Array.from(files || []);
    if (!chosen.length) return;
    if (chosen.some(file => !/\.(sql|csv|txt|pt)$/i.test(file.name))) {
      setSelectionError(t('Choose a SQL, CSV, TXT, or PT file.', '请选择 SQL、CSV、TXT 或 PT 文件。')); return;
    }
    if (chosen.length === 1 && !chosen[0].name.toLowerCase().endsWith('.csv')) {
      setSelectionError(''); onUpload(chosen[0]); return;
    }
    if (chosen.every(file => file.name.toLowerCase().endsWith('.csv'))) {
      setSelectionError(''); setCsvFiles(chosen); return;
    }
    setSelectionError(t('Multiple-file graph construction accepts CSV files only.', '多文件构图只能同时选择 CSV 文件。'));
  }

  return <div className="workspace-catalog graph-source-catalog">
    <header className="catalog-page-head">
      <span className="context-icon" aria-hidden="true"><Icon name="graph" size={23} /></span>
      <h1>{t('Graph Construction Workspace', '构图工作台')}</h1>
      <input ref={inputRef} type="file" hidden multiple accept={acceptedFiles} onChange={event => { selected(event.currentTarget.files); event.currentTarget.value = ''; }} />
      <button type="button" className="primary-command" onClick={() => chooseFile()} disabled={busy}>
        <Icon name="upload" size={16} />{t('Upload source', '上传数据源')}
      </button>
    </header>
    <div className="catalog-page-content">
      <section className="graph-source-upload" aria-labelledby="graph-source-upload-title">
        <h2 className="source-catalog-heading" id="graph-source-upload-title">{t('Upload or load data', '上传或加载数据')}</h2>
        <div className="graph-source-formats">
          {sourceFormats.map(format => <button key={format.extension} type="button" className="graph-source-format"
            disabled={busy} onClick={() => chooseFile('.' + format.extension)}>
            <Icon name={format.icon} size={19} /><span><b>{t(format.label[0], format.label[1])}</b><small>{'.' + format.extension}</small></span>
          </button>)}
        </div>
        <button type="button" className="graph-source-drop" data-dragging={dragging} disabled={busy} onClick={() => chooseFile()}
          onDragEnter={event => {
            event.preventDefault();
            if (event.dataTransfer.types.includes('Files') && ++dragDepth.current === 1) setDragging(true);
          }}
          onDragOver={event => { event.preventDefault(); event.dataTransfer.dropEffect = busy ? 'none' : 'copy'; }}
          onDragLeave={event => { event.preventDefault(); if (--dragDepth.current <= 0) { dragDepth.current = 0; setDragging(false); } }}
          onDrop={event => { event.preventDefault(); dragDepth.current = 0; setDragging(false); selected(event.dataTransfer.files); }}>
          <Icon name="upload" size={23} />
          <span><b>{t('Drop source files here', '拖拽源文件到这里')}</b>
            <small>SQL / CSV / TXT / PT · {t('CSV supports multiple files', 'CSV 支持多文件')}</small></span>
          <span className="graph-source-choose">{t('Choose source file', '选择源文件')}</span>
        </button>
        {selectionError && <div className="csv-bundle-error" role="alert">{selectionError}</div>}
      </section>

      <section className="graph-source-list" aria-labelledby="create-graph-sources-title">
        <div className="source-catalog-head">
          <h2 className="source-catalog-heading" id="create-graph-sources-title">{t('My graph sources', '我的构图数据源')}</h2>
          <span className="source-catalog-count">{t(`${formatNumber(visibleSources.length)} available`, `${formatNumber(visibleSources.length)} 个可用`)}</span>
        </div>
        <div className="source-catalog-filters">
          <div className="source-catalog-tabs" role="group" aria-label={t('Data source categories', '数据源分类')}>
            {([
              { id: 'all', label: t('All', '全部') },
              { id: 'examples', label: t('Examples', '样例数据') },
              { id: 'own', label: t('My uploads', '我的上传') },
            ] as const).map(filter => <button key={filter.id} type="button" aria-pressed={sourceFilter === filter.id}
              onClick={() => setSourceFilter(filter.id)}>{filter.label}</button>)}
          </div>
          <div className="source-catalog-search"><Icon name="search" size={15} />
            <input type="search" aria-label={t('Search sources', '搜索数据源')} placeholder={t('Search sources', '搜索数据源')}
              value={query} onChange={event => setQuery(event.target.value)} />
          </div>
        </div>
        {visibleSources.length ? <div className="catalog-card-grid">{visibleSources.map(dataset => {
          const name = labels.name(dataset.name);
          const badge = sourceBadge(dataset, t);
          const graphReady = isPrebuiltGraph(dataset) ? dataset.status === 'ready' : constructionStatus(dataset) === 'ready';
          const buildLabel = localizedConstructionLabel(dataset, t);
          return <article className="catalog-card graph-source-card" key={dataset.dataset_id} aria-label={name}>
            <div className="catalog-card-head">
              <span className="catalog-card-icon"><Icon name={isPrebuiltGraph(dataset) ? 'graph' : sourceType(dataset) === 'CSV' ? 'table' : 'file'} size={20} /></span>
              <div><h3 title={name}>{name}</h3>
                <div className="graph-source-type"><span>{sourceType(dataset)}</span>
                  {Boolean(dataset.metadata?.user_case_id) && <span className="is-example">{t('Example', '示例')}</span>}</div>
              </div>
            </div>
            {dataset.description && <p className="catalog-card-description" title={labels.description(dataset.description)}>{labels.description(dataset.description)}</p>}
            <div className="catalog-card-meta">
              <span title={sourceSummary(dataset, t, formatNumber)}>{sourceSummary(dataset, t, formatNumber)}</span>
              <span className="catalog-status" data-tone={badge.tone}>{badge.label}</span>
            </div>
            <div className="catalog-card-actions">
              {canViewSavedGraph(dataset) && <button type="button" className="secondary-command" onClick={() => onConstruct(dataset)} disabled={busy}
                aria-label={(isMolecularGraph(dataset) ? t('View graph build', '查看构图') : t('View construction result', '查看构图结果')) + ' ' + name}>
                <Icon name="graph" size={14} />{isMolecularGraph(dataset) ? t('View graph build', '查看构图') : t('View construction result', '查看构图结果')}
              </button>}
              {isPrebuiltGraph(dataset)
                ? <button type="button" className="secondary-command" onClick={() => onAnalyze?.(dataset)} disabled={busy || !graphReady || !onAnalyze}
                    aria-label={t(`Start analysis ${name}`, `开始分析 ${name}`)}>{t('Start analysis', '开始分析')}<Icon name="arrow-right" size={14} /></button>
                : <button type="button" className={graphReady ? 'secondary-command' : 'primary-command'} onClick={() => onConstruct(dataset)}
                    disabled={busy} aria-label={buildLabel + ' ' + name}>{buildLabel}<Icon name="arrow-right" size={14} /></button>}
              {graphReady && supportsConstruction(dataset) && onAnalyze && <button type="button" className="secondary-command"
                onClick={() => onAnalyze(dataset)} disabled={busy} aria-label={t(`Start analysis ${name}`, `开始分析 ${name}`)}>
                {t('Start analysis', '开始分析')}<Icon name="arrow-right" size={14} /></button>}
              {supportsConstruction(dataset) && <button type="button" className="secondary-command" onClick={() => setSourceSample(dataset)} disabled={busy}
                aria-label={t(`Source data preview for ${name}`, `源数据示例 ${name}`)}><Icon name="table" size={14} />{t('Source data preview', '源数据示例')}</button>}
              {onDelete && <button type="button" className="plain-icon create-graph-delete" onClick={() => onDelete(dataset)} disabled={busy} title={t('Delete dataset', '删除数据集')}
                aria-label={t(`Delete dataset ${name}`, `删除数据集 ${name}`)}><Icon name="trash" size={16} /></button>}
            </div>
          </article>;
        })}</div> : <div className="catalog-empty">
          <span className="catalog-card-icon"><Icon name="graph" size={23} /></span>
          <strong>{busy && sources.length === 0 ? t('Loading data sources…', '正在加载数据源…')
            : sources.length ? t('No matching data sources', '没有匹配的数据源') : t('No graph-ready data sources yet', '还没有可构图的数据源')}</strong>
          {!busy && !sources.length && <button type="button" className="secondary-command" onClick={() => chooseFile()}>{t('Upload your first source', '上传第一个数据源')}</button>}
        </div>}
      </section>
    </div>
    <PopupPresence>{csvFiles && <CsvBundleDialog files={csvFiles} onCancel={() => setCsvFiles(null)} onSubmit={(files, schema, name) => {
      setCsvFiles(null); onUpload(files, schema, name);
    }} />}</PopupPresence>
    <PopupPresence>{sourceSample && <SourceDataDialog key={sourceSample.dataset_id} dataset={sourceSample} onClose={() => setSourceSample(null)} />}</PopupPresence>
  </div>;
}

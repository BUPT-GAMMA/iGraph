import { useExampleLabels } from '../userCases';
import { PopupPresence } from './PopupMotion';
import { useEffect, useRef, useState } from 'react';
import { getConstructionState } from '../api';
import { useI18n } from '../i18n';
import type { Translate } from '../i18n';
import type { ConstructionState, CsvBundleSchema, Dataset } from '../types';
import { constructionStatus, isMolecularGraph, isPrebuiltGraph, supportsConstruction } from '../construction';
import { Icon } from './Icon';
import { CsvBundleDialog } from './CsvBundleDialog';

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
  const stateCallback = useRef(onStateChange);
  stateCallback.current = onStateChange;
  const sources = datasets.filter(dataset => supportsConstruction(dataset) || isPrebuiltGraph(dataset));
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

  function chooseFile() { inputRef.current?.click(); }
  function selected(files: FileList | null) {
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

  return <div className="create-graph-workspace">
    <header className="create-graph-header">
      <div className="create-graph-title">
        <span className="context-icon"><Icon name="graph" size={23} /></span>
        <div><h1>{t('Graph Construction Workspace', '构图工作台')}</h1></div>
      </div>
      <input ref={inputRef} type="file" hidden multiple accept=".sql,.csv,.txt,.pt" onChange={event => { selected(event.currentTarget.files); event.currentTarget.value = ''; }} />
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
            <div className="create-graph-step-copy"><strong>{t('Add a data source', '添加数据源')}</strong><small>SQL / CSV / TXT / PT</small></div>
          </div>
          <span className="create-graph-flow-arrow" aria-hidden="true"><Icon name="arrow-right" size={16} /></span>
          <div className="create-graph-step">
            <div className="create-graph-step-head"><span>02</span><span className="create-graph-step-icon"><Icon name="graph" size={20} /></span></div>
            <div className="create-graph-step-copy"><strong>{t('Build or load a graph', '构图或载入现成图')}</strong></div>
          </div>
          <span className="create-graph-flow-arrow" aria-hidden="true"><Icon name="arrow-right" size={16} /></span>
          <div className="create-graph-step">
            <div className="create-graph-step-head"><span>03</span><span className="create-graph-step-icon"><Icon name="check" size={20} /></span></div>
            <div className="create-graph-step-copy"><strong>{t('Select and analyze', '选择图并分析')}</strong></div>
          </div>
        </div>
      </section>

      <section className="create-graph-sources" aria-labelledby="create-graph-sources-title">
        <div className="create-graph-section-head"><div><h2 id="create-graph-sources-title">{t('My graph sources', '我的构图数据源')}</h2></div><span>{t(`${formatNumber(sources.length)} available`, `${formatNumber(sources.length)} 个可用`)}</span></div>
        {sources.length ? <div className="create-graph-source-grid">{sources.map(dataset => <article className="create-graph-source-card" key={dataset.dataset_id}>
          <div className="create-graph-source-icon"><Icon name="database" size={20} /></div>
          <div className="create-graph-source-copy"><div><strong title={labels.name(dataset.name)}>{labels.name(dataset.name)}</strong><span>{sourceType(dataset)}</span>{Boolean(dataset.metadata?.user_case_id) && <span>{t("Example", "示例")}</span>}</div><p>{sourceSummary(dataset, t, formatNumber)}</p>{dataset.description && <p title={labels.description(dataset.description)}>{labels.description(dataset.description)}</p>}</div>
          <div className="create-graph-source-actions">
            {isMolecularGraph(dataset) && <button className="secondary-command" onClick={() => onConstruct(dataset)} disabled={busy || dataset.status !== 'ready'} aria-label={t(`View graph build ${labels.name(dataset.name)}`, `查看构图 ${labels.name(dataset.name)}`)}>{t('View graph build', '查看构图')} <Icon name="graph" size={14} /></button>}
            {isPrebuiltGraph(dataset)
              ? <button className="secondary-command" onClick={() => onAnalyze?.(dataset)} disabled={busy || dataset.status !== 'ready' || !onAnalyze} aria-label={t(`Start analysis ${labels.name(dataset.name)}`, `开始分析 ${labels.name(dataset.name)}`)}>{t('Start analysis', '开始分析')} <Icon name="arrow-right" size={14} /></button>
              : <button className="secondary-command" onClick={() => onConstruct(dataset)} disabled={busy} aria-label={`${localizedConstructionLabel(dataset, t)} ${labels.name(dataset.name)}`}>{localizedConstructionLabel(dataset, t)} <Icon name="arrow-right" size={14} /></button>}
            {onDelete && <button className="plain-icon create-graph-delete" onClick={() => onDelete(dataset)} disabled={busy} title={t('Delete dataset', '删除数据集')} aria-label={t(`Delete dataset ${labels.name(dataset.name)}`, `删除数据集 ${labels.name(dataset.name)}`)}><Icon name="trash" size={16} /></button>}
          </div>
        </article>)}</div> : <div className="create-graph-empty">
          <span><Icon name="graph" size={34} /></span><strong>{t('No graph-ready data sources yet', '还没有可构图的数据源')}</strong><button className="secondary-command" onClick={chooseFile} disabled={busy}>{t('Upload your first source', '上传第一个数据源')}</button>
        </div>}
      </section>
    </div>
    <PopupPresence>{csvFiles && <CsvBundleDialog files={csvFiles} onCancel={() => setCsvFiles(null)} onSubmit={(files, schema, name) => { setCsvFiles(null); onUpload(files, schema, name); }} />}</PopupPresence>
  </div>;
}

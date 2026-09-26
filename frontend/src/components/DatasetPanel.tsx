import { useRef } from 'react';
import type { Dataset, ServerDataset } from '../types';
import { constructionLabel, supportsConstruction } from '../construction';
import { serverDatasetActionLabel, serverDatasetPreparationLabel, serverDatasetReady } from '../serverDatasets';
import { DrawerShell } from './DrawerShell';
import { Icon } from './Icon';
import { DatasetDescriptionEditor } from './DatasetDescriptionEditor';
import { useI18n, type Language, type Translate } from '../i18n';

type Props = {
  datasets: Dataset[];
  projectName?: string;
  activeDatasetId?: string | null;
  busy?: boolean;
  onSelect: (dataset: Dataset) => void;
  onUpload: (file: File) => void;
  serverDatasets?: ServerDataset[];
  onUseServer?: (dataset: ServerDataset) => void;
  onRename: (dataset: Dataset) => void;
  onDelete: (dataset: Dataset) => void;
  onConstruct?: (dataset: Dataset) => void;
  onLoadConstructed?: () => void;
  onDatasetUpdated?: (dataset: Dataset) => void;
  onClose: () => void;
  onRemoteGraph?: () => void;
};

function datasetSummary(dataset: Dataset, language: Language, t: Translate) {
  if (dataset.status === 'source_ready') return t(`${dataset.original_filename} · Source file ready to build`, `${dataset.original_filename} · 源文件已就绪，待构图`);
  const nodes = Number(dataset.metadata?.num_nodes);
  const edges = Number(dataset.metadata?.num_edges);
  const locale = language === 'zh' ? 'zh-CN' : 'en';
  if (Number.isFinite(nodes) && Number.isFinite(edges)) return t(`${nodes.toLocaleString(locale)} node${nodes === 1 ? '' : 's'} · ${edges.toLocaleString(locale)} edge${edges === 1 ? '' : 's'}`, `${nodes.toLocaleString(locale)} 个节点 · ${edges.toLocaleString(locale)} 条边`);
  const status = dataset.status === 'ready' ? t('Ready', '就绪')
    : dataset.status === 'processing' || dataset.status === 'uploading' ? t('Processing', '处理中')
      : dataset.status === 'failed' || dataset.status === 'error' ? t('Failed', '失败')
        : dataset.status;
  return `${dataset.original_filename} · ${status}`;
}

function displayConstructionLabel(dataset: Dataset, t: Translate) {
  const label = constructionLabel(dataset);
  const english: Record<string, string> = {
    '查看进度': 'View progress',
    '重新构图': 'Rebuild graph',
    '算法待接入': 'Algorithm unavailable',
    '查看构图': 'View graph',
    '构图': 'Build graph',
  };
  return t(english[label] || label, label);
}

function fileSize(size: number) {
  if (!Number.isFinite(size) || size < 0) return '-';
  if (size < 1024) return `${size} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = size;
  let index = -1;
  do { value /= 1024; index += 1; } while (value >= 1024 && index < units.length - 1);
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${units[index]}`;
}

export function DatasetPanel({ datasets, serverDatasets = [], projectName, activeDatasetId, busy, onSelect, onUpload, onUseServer, onRename, onDelete, onConstruct, onLoadConstructed, onDatasetUpdated, onClose, onRemoteGraph }: Props) {
  const { language, t } = useI18n();
  const title = projectName ? t('Project datasets', '项目数据集') : t('Personal datasets', '个人数据集');
  const inputRef = useRef<HTMLInputElement>(null);
  const importedServerIds = new Set(datasets.map(dataset => dataset.source_id).filter(Boolean));
  const availableServerDatasets = serverDatasets.filter(dataset => !importedServerIds.has(dataset.server_dataset_id));
  return <DrawerShell ariaLabel={title} className="dataset-drawer" onClose={onClose}>{requestClose => <>
      <div className="drawer-head"><div><strong>{title}</strong>{projectName && <span>{projectName}</span>}</div><button className="plain-icon" onClick={requestClose} aria-label={t('Close', '关闭')} title={t('Close', '关闭')}><Icon name="close" size={17} /></button></div>
      <div className="dataset-drawer-actions">
        {onRemoteGraph && <button className="secondary-command" onClick={onRemoteGraph}>{t("Remote databases", "远程数据库")}</button>}
        <input ref={inputRef} type="file" hidden accept=".sql,.txt,.csv,.tsv,.edgelist,.dat,.json,.pt,.npz" onChange={event => { const file = event.target.files?.[0]; if (file) onUpload(file); event.currentTarget.value = ''; }} />
        <button className="primary-command" onClick={() => inputRef.current?.click()} disabled={busy}><Icon name="upload" size={15} /> {t('Add dataset', '添加数据集')}</button>
        {onLoadConstructed && <button className="secondary-command" disabled={busy} onClick={onLoadConstructed}><Icon name="graph" size={15} />{t('Load constructed graph', '加载已构图数据')}</button>}
      </div>
      <div className="dataset-list">
        {datasets.length ? datasets.map(dataset => <div key={dataset.dataset_id}><div className={`dataset-row ${activeDatasetId === dataset.dataset_id ? 'active' : ''}`}>
          <button className="dataset-row-main" onClick={() => onSelect(dataset)} disabled={busy || dataset.status !== 'ready'} aria-current={activeDatasetId === dataset.dataset_id ? 'true' : undefined}>
            <span className="dataset-state"><Icon name={activeDatasetId === dataset.dataset_id ? 'check' : 'database'} size={15} /></span>
            <span className="dataset-copy"><span className="dataset-title-line"><strong>{dataset.name}</strong>{activeDatasetId === dataset.dataset_id && <span className="dataset-badges"><b>{t('Current', '当前')}</b></span>}</span><small>{datasetSummary(dataset, language, t)}</small></span>
          </button>
          <div className="dataset-row-actions">
            {onConstruct && supportsConstruction(dataset) && <button className="dataset-construction-button" onClick={() => onConstruct(dataset)} disabled={busy} aria-label={`${displayConstructionLabel(dataset, t)} ${dataset.name}`}><Icon name="graph" size={14} />{displayConstructionLabel(dataset, t)}</button>}
            <button onClick={() => onRename(dataset)} disabled={busy} title={t('Rename', '重命名')} aria-label={t(`Rename ${dataset.name}`, `重命名 ${dataset.name}`)}><Icon name="edit" size={14} /></button>
            <button className="danger" onClick={() => onDelete(dataset)} disabled={busy} title={t('Delete', '删除')} aria-label={t(`Delete ${dataset.name}`, `删除 ${dataset.name}`)}><Icon name="trash" size={14} /></button>
          </div>
        </div><details className="dataset-description"><summary>{t('Dataset description', '数据集描述')}</summary><DatasetDescriptionEditor dataset={dataset} disabled={busy} onSaved={onDatasetUpdated} /></details></div>) : <div className="drawer-empty"><Icon name="database" size={24} /><strong>{t('No datasets', '暂无数据集')}</strong></div>}
      </div>
      <div className="server-dataset-section">
        <div className="server-dataset-heading"><strong>{t('Server datasets', '服务器数据源')}</strong><span>{onUseServer ? t('Prepared once on the server; each session uses an independent logical version.', '服务器端只预处理一次；使用时为当前会话绑定独立逻辑版本') : t('Create or select a project to add a server dataset.', '请先创建或选择项目，再将服务器数据源添加到项目')}</span></div>
        <div className="server-dataset-list">
          {availableServerDatasets.length ? availableServerDatasets.map(dataset => <div className="server-dataset-row" key={dataset.server_dataset_id}>
            <span className="project-dataset-icon"><Icon name="database" size={15} /></span>
            <span className="server-dataset-copy"><strong>{dataset.name}</strong><small>{dataset.relative_path} · {fileSize(dataset.size_bytes)} · {dataset.directed === false ? t('Undirected', '无向图') : t('Directed', '有向图')}</small>{serverDatasetPreparationLabel(dataset, t) && <em className={`server-preparation ${dataset.preparation?.status || ''}`} title={dataset.preparation?.error || dataset.metadata_error || undefined}>{serverDatasetPreparationLabel(dataset, t)}</em>}</span>
            <button className="text-button" onClick={() => onUseServer?.(dataset)} disabled={busy || !onUseServer || !serverDatasetReady(dataset)}>{onUseServer ? serverDatasetActionLabel(dataset, t('Use', '使用'), t) : t('Select a project first', '选择项目后使用')}</button>
          </div>) : <div className="server-dataset-empty">{onUseServer ? t('No server datasets available to add', '暂无可添加的服务器数据源') : t('No server datasets', '暂无服务器数据源')}</div>}
        </div>
      </div>
  </>}</DrawerShell>;
}

import { useRef } from 'react';
import type { Dataset } from '../types';
import { constructionLabel, supportsConstruction } from '../construction';
import { DrawerShell } from './DrawerShell';
import { Icon } from './Icon';
import { useI18n, type Language, type Translate } from '../i18n';

type Props = {
  datasets: Dataset[];
  projectName?: string;
  activeDatasetId?: string | null;
  busy?: boolean;
  onSelect: (dataset: Dataset) => void;
  onUpload: (file: File) => void;
  onRename: (dataset: Dataset) => void;
  onDelete: (dataset: Dataset) => void;
  onConstruct?: (dataset: Dataset) => void;
  onClose: () => void;
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

export function DatasetPanel({ datasets, projectName, activeDatasetId, busy, onSelect, onUpload, onRename, onDelete, onConstruct, onClose }: Props) {
  const { language, t } = useI18n();
  const title = projectName ? t('Project datasets', '项目数据集') : t('Personal datasets', '个人数据集');
  const inputRef = useRef<HTMLInputElement>(null);
  return <DrawerShell ariaLabel={title} className="dataset-drawer" onClose={onClose}>{requestClose => <>
      <div className="drawer-head"><div><strong>{title}</strong><span>{projectName || t('Reusable across this account; select one to use in the current conversation', '同账号可复用，选择后应用于当前对话')}</span></div><button className="plain-icon" onClick={requestClose} aria-label={t('Close', '关闭')} title={t('Close', '关闭')}><Icon name="close" size={17} /></button></div>
      <div className="dataset-drawer-actions">
        <input ref={inputRef} type="file" hidden accept=".sql,.txt,.csv,.tsv,.edgelist,.dat,.json,.pt,.npz" onChange={event => { const file = event.target.files?.[0]; if (file) onUpload(file); event.currentTarget.value = ''; }} />
        <button className="primary-command" onClick={() => inputRef.current?.click()} disabled={busy}><Icon name="upload" size={15} /> {t('Add dataset', '添加数据集')}</button>
      </div>
      <div className="dataset-list">
        {datasets.length ? datasets.map(dataset => <div className={`dataset-row ${activeDatasetId === dataset.dataset_id ? 'active' : ''}`} key={dataset.dataset_id}>
          <button className="dataset-row-main" onClick={() => onSelect(dataset)} disabled={busy || dataset.status !== 'ready'} aria-current={activeDatasetId === dataset.dataset_id ? 'true' : undefined}>
            <span className="dataset-state"><Icon name={activeDatasetId === dataset.dataset_id ? 'check' : 'database'} size={15} /></span>
            <span className="dataset-copy"><span className="dataset-title-line"><strong>{dataset.name}</strong>{activeDatasetId === dataset.dataset_id && <span className="dataset-badges"><b>{t('Current', '当前')}</b></span>}</span><small>{datasetSummary(dataset, language, t)}</small></span>
          </button>
          <div className="dataset-row-actions">
            {onConstruct && supportsConstruction(dataset) && <button className="dataset-construction-button" onClick={() => onConstruct(dataset)} disabled={busy} aria-label={`${displayConstructionLabel(dataset, t)} ${dataset.name}`}><Icon name="graph" size={14} />{displayConstructionLabel(dataset, t)}</button>}
            <button onClick={() => onRename(dataset)} disabled={busy} title={t('Rename', '重命名')} aria-label={t(`Rename ${dataset.name}`, `重命名 ${dataset.name}`)}><Icon name="edit" size={14} /></button>
            <button className="danger" onClick={() => onDelete(dataset)} disabled={busy} title={t('Delete', '删除')} aria-label={t(`Delete ${dataset.name}`, `删除 ${dataset.name}`)}><Icon name="trash" size={14} /></button>
          </div>
        </div>) : <div className="drawer-empty"><Icon name="database" size={24} /><strong>{t('No datasets', '暂无数据集')}</strong><span>{projectName ? t('Once added, new conversations in this project can use it directly.', '添加后，同项目中的新会话可直接使用。') : t('Upload or select a dataset to use it in the current conversation.', '上传或选择数据集后，在当前对话中使用。')}</span></div>}
      </div>
  </>}</DrawerShell>;
}

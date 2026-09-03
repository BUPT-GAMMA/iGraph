import { useRef } from 'react';
import type { Dataset } from '../types';
import { DrawerShell } from './DrawerShell';
import { Icon } from './Icon';

type Props = {
  datasets: Dataset[];
  projectName?: string;
  activeDatasetId?: string | null;
  busy?: boolean;
  onSelect: (dataset: Dataset) => void;
  onUpload: (file: File) => void;
  onRename: (dataset: Dataset) => void;
  onDelete: (dataset: Dataset) => void;
  onClose: () => void;
};

function datasetSummary(dataset: Dataset) {
  const nodes = Number(dataset.metadata?.num_nodes);
  const edges = Number(dataset.metadata?.num_edges);
  if (Number.isFinite(nodes) && Number.isFinite(edges)) return `${nodes.toLocaleString()} nodes · ${edges.toLocaleString()} edges`;
  return `${dataset.original_filename} · ${dataset.status}`;
}

export function DatasetPanel({ datasets, projectName, activeDatasetId, busy, onSelect, onUpload, onRename, onDelete, onClose }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  return <DrawerShell ariaLabel="项目数据集" className="dataset-drawer" onClose={onClose}>{requestClose => <>
      <div className="drawer-head"><div><strong>项目数据集</strong><span>{projectName || '当前项目'}</span></div><button className="plain-icon" onClick={requestClose} aria-label="关闭"><Icon name="close" size={17} /></button></div>
      <div className="dataset-drawer-actions">
        <input ref={inputRef} type="file" hidden accept=".txt,.csv,.tsv,.edgelist,.dat,.json,.pt,.npz" onChange={event => { const file = event.target.files?.[0]; if (file) onUpload(file); event.currentTarget.value = ''; }} />
        <button className="primary-command" onClick={() => inputRef.current?.click()} disabled={busy}><Icon name="upload" size={15} /> 添加数据集</button>
      </div>
      <div className="dataset-list">
        {datasets.length ? datasets.map(dataset => <div className={`dataset-row ${activeDatasetId === dataset.dataset_id ? 'active' : ''}`} key={dataset.dataset_id}>
          <button className="dataset-row-main" onClick={() => onSelect(dataset)} disabled={busy || dataset.status !== 'ready'} aria-current={activeDatasetId === dataset.dataset_id ? 'true' : undefined}>
            <span className="dataset-state"><Icon name={activeDatasetId === dataset.dataset_id ? 'check' : 'database'} size={15} /></span>
            <span className="dataset-copy"><span className="dataset-title-line"><strong>{dataset.name}</strong>{activeDatasetId === dataset.dataset_id && <span className="dataset-badges"><b>当前</b></span>}</span><small>{datasetSummary(dataset)}</small></span>
          </button>
          <div className="dataset-row-actions">
            <button onClick={() => onRename(dataset)} disabled={busy} title="重命名" aria-label={`重命名 ${dataset.name}`}><Icon name="edit" size={14} /></button>
            <button className="danger" onClick={() => onDelete(dataset)} disabled={busy} title="删除" aria-label={`删除 ${dataset.name}`}><Icon name="trash" size={14} /></button>
          </div>
        </div>) : <div className="drawer-empty"><Icon name="database" size={24} /><strong>暂无项目数据集</strong><span>添加后，同项目中的新会话可直接使用。</span></div>}
      </div>
  </>}</DrawerShell>;
}

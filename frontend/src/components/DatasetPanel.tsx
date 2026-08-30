import { useRef } from 'react';
import type { Dataset } from '../types';
import { Icon } from './Icon';

type Props = {
  datasets: Dataset[];
  projectName?: string;
  activeDatasetId?: string | null;
  busy?: boolean;
  onSelect: (dataset: Dataset) => void;
  onUpload: (file: File) => void;
  onSetDefault: (dataset: Dataset) => void;
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

export function DatasetPanel({ datasets, projectName, activeDatasetId, busy, onSelect, onUpload, onSetDefault, onRename, onDelete, onClose }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  return <div className="overlay" onClick={onClose}>
    <section className="drawer dataset-drawer" onClick={event => event.stopPropagation()}>
      <div className="drawer-head"><div><strong>项目数据集</strong><span>{projectName || '当前项目'}</span></div><button className="plain-icon" onClick={onClose} aria-label="关闭"><Icon name="close" size={17} /></button></div>
      <div className="dataset-drawer-actions">
        <input ref={inputRef} type="file" hidden accept=".txt,.csv,.tsv,.edgelist,.dat,.json,.pt,.npz" onChange={event => { const file = event.target.files?.[0]; if (file) onUpload(file); event.currentTarget.value = ''; }} />
        <button className="primary-command" onClick={() => inputRef.current?.click()} disabled={busy}><Icon name="upload" size={15} /> 添加数据集</button>
      </div>
      <p className="drawer-description">会话共享项目数据源，但各自维护独立的图运行状态。</p>
      <div className="dataset-list">
        {datasets.length ? datasets.map(dataset => <div className={`dataset-row ${activeDatasetId === dataset.dataset_id ? 'active' : ''}`} key={dataset.dataset_id}>
          <button className="dataset-row-main" onClick={() => onSelect(dataset)} disabled={busy || dataset.status !== 'ready'}>
            <span className="dataset-state"><Icon name={activeDatasetId === dataset.dataset_id ? 'check' : 'database'} size={15} /></span>
            <span className="dataset-copy"><strong>{dataset.name}</strong><small>{datasetSummary(dataset)}</small></span>
          </button>
          <div className="dataset-badges">{dataset.is_default && <span>默认</span>}{activeDatasetId === dataset.dataset_id && <b>当前</b>}</div>
          <div className="dataset-row-actions">
            {!dataset.is_default && <button onClick={() => onSetDefault(dataset)} disabled={busy} title="设为项目默认"><Icon name="check" size={14} /></button>}
            <button onClick={() => onRename(dataset)} disabled={busy} title="重命名"><Icon name="edit" size={14} /></button>
            <button className="danger" onClick={() => onDelete(dataset)} disabled={busy} title="删除"><Icon name="trash" size={14} /></button>
          </div>
        </div>) : <div className="drawer-empty"><Icon name="database" size={24} /><strong>暂无项目数据集</strong><span>添加后，同项目中的新会话可直接使用。</span></div>}
      </div>
    </section>
  </div>;
}

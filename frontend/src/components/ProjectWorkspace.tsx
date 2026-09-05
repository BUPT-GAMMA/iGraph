import { useRef } from 'react';
import type { Dataset, Project } from '../types';
import { Icon } from './Icon';

type Props = {
  project: Project;
  datasets: Dataset[];
  busy?: boolean;
  onUpload: (file: File) => void;
  onSetDefault: (dataset: Dataset) => void;
  onRename: (dataset: Dataset) => void;
  onDelete: (dataset: Dataset) => void;
};

function metric(dataset: Dataset) {
  const nodes = Number(dataset.metadata?.num_nodes);
  const edges = Number(dataset.metadata?.num_edges);
  return Number.isFinite(nodes) && Number.isFinite(edges)
    ? `${nodes.toLocaleString()} nodes · ${edges.toLocaleString()} edges`
    : dataset.original_filename;
}

export function ProjectWorkspace({ project, datasets, busy, onUpload, onSetDefault, onRename, onDelete }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  return <section className="project-workspace">
    <header className="project-workspace-head">
      <div className="project-workspace-title">
        <div className="context-icon"><Icon name="folder-open" size={21} /></div>
        <div><span>Project</span><h1>{project.name}</h1><p>{project.description || '集中管理这个项目中的图分析会话。'}</p></div>
      </div>
    </header>
    <div className="project-stat-row">
      <div><span>Conversations</span><strong>{project.conversation_count || 0}</strong><small>{project.is_default ? 'General workspace' : 'Project workspace'}</small></div>
      <div><span>Datasets</span><strong>{datasets.length}</strong><small>{datasets.some(item => item.is_default) ? 'Default ready' : 'No default selected'}</small></div>
    </div>
    <section className="project-datasets">
      <div className="project-section-head"><div><h2>Data Sources</h2><p>项目中的会话可选择这些数据源，并保持各自的运行时状态。</p></div><input ref={inputRef} type="file" hidden accept=".txt,.csv,.tsv,.edgelist,.dat,.json,.pt,.npz" onChange={event => { const file = event.target.files?.[0]; if (file) onUpload(file); event.currentTarget.value = ''; }} /><button className="primary-command" onClick={() => inputRef.current?.click()} disabled={busy}><Icon name="upload" size={15} /> 添加数据集</button></div>
      <div className="project-dataset-table">
        {datasets.length ? datasets.map(dataset => <div className="project-dataset-row" key={dataset.dataset_id}>
          <span className="project-dataset-icon"><Icon name="database" size={16} /></span>
          <div><strong>{dataset.name}</strong><span>{metric(dataset)}</span></div>
          <span className={`dataset-status ${dataset.status}`}>{dataset.status}</span>
          {dataset.is_default ? <b className="default-badge">默认</b> : <button className="text-button" onClick={() => onSetDefault(dataset)} disabled={busy}>设为默认</button>}
          <div className="project-dataset-actions"><button onClick={() => onRename(dataset)} disabled={busy} title="重命名"><Icon name="edit" size={14} /></button><button className="danger" onClick={() => onDelete(dataset)} disabled={busy} title="删除"><Icon name="trash" size={14} /></button></div>
        </div>) : <div className="project-dataset-empty"><Icon name="database" size={25} /><strong>还没有数据集</strong><span>上传第一个数据集后，它会自动成为项目默认数据集。</span></div>}
      </div>
    </section>
  </section>;
}

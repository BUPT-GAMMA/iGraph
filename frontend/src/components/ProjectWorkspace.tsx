import { useRef } from 'react';
import type { Dataset, Project } from '../types';
import { constructionLabel, supportsConstruction } from '../construction';
import { Icon } from './Icon';

type Props = {
  project: Project;
  datasets: Dataset[];
  busy?: boolean;
  onUpload: (file: File) => void;
  onSetDefault: (dataset: Dataset) => void;
  onRename: (dataset: Dataset) => void;
  onDelete: (dataset: Dataset) => void;
  onConstruct?: (dataset: Dataset) => void;
};

function metric(dataset: Dataset) {
  const nodes = Number(dataset.metadata?.num_nodes);
  const edges = Number(dataset.metadata?.num_edges);
  return Number.isFinite(nodes) && Number.isFinite(edges)
    ? `${nodes.toLocaleString()} 个节点 · ${edges.toLocaleString()} 条边`
    : dataset.original_filename;
}

function statusLabel(status: string) {
  if (status === 'source_ready') return '待构图';
  if (status === 'ready') return '就绪';
  if (status === 'processing' || status === 'uploading') return '处理中';
  if (status === 'failed' || status === 'error') return '失败';
  return status;
}

export function ProjectWorkspace({ project, datasets, busy, onUpload, onSetDefault, onRename, onDelete, onConstruct }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  return <section className="project-workspace">
    <header className="project-workspace-head">
      <div className="project-workspace-title">
        <div className="context-icon"><Icon name="folder-open" size={21} /></div>
        <div><span>项目</span><h1>{project.name}</h1>{project.description ? <p>{project.description}</p> : null}</div>
      </div>
    </header>
    <div className="project-stat-row">
      <div><span>会话</span><strong>{project.conversation_count || 0}</strong></div>
      <div><span>数据集</span><strong>{datasets.length}</strong></div>
    </div>
    <section className="project-datasets">
      <div className="project-section-head"><div><h2>数据源</h2></div><input ref={inputRef} type="file" hidden accept=".sql,.txt,.csv,.tsv,.edgelist,.dat,.json,.pt,.npz" onChange={event => { const file = event.target.files?.[0]; if (file) onUpload(file); event.currentTarget.value = ''; }} /><button className="primary-command" onClick={() => inputRef.current?.click()} disabled={busy}><Icon name="upload" size={15} /> 添加数据集</button></div>
      <div className="project-dataset-table">
        {datasets.length ? datasets.map(dataset => <div className="project-dataset-row" key={dataset.dataset_id}>
          <span className="project-dataset-icon"><Icon name="database" size={16} /></span>
          <div><strong>{dataset.name}</strong><span>{metric(dataset)}</span></div>
          <span className={`dataset-status ${dataset.status}`}>{statusLabel(dataset.status)}</span>
          {dataset.is_default ? <b className="default-badge">默认</b> : <button className="text-button" onClick={() => onSetDefault(dataset)} disabled={busy || dataset.status === 'source_ready'}>设为默认</button>}
          <div className="project-dataset-actions">{onConstruct && supportsConstruction(dataset) && <button className="dataset-construction-button" onClick={() => onConstruct(dataset)} disabled={busy} aria-label={`${constructionLabel(dataset)} ${dataset.name}`}><Icon name="graph" size={14} />{constructionLabel(dataset)}</button>}<button onClick={() => onRename(dataset)} disabled={busy} title="重命名"><Icon name="edit" size={14} /></button><button className="danger" onClick={() => onDelete(dataset)} disabled={busy} title="删除"><Icon name="trash" size={14} /></button></div>
        </div>) : <div className="project-dataset-empty"><Icon name="database" size={25} /><strong>还没有数据集</strong></div>}
      </div>
    </section>
  </section>;
}

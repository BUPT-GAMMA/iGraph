import { useEffect, useRef } from 'react';
import { getConstructionState } from '../api';
import type { ConstructionState, Dataset } from '../types';
import { constructionLabel, constructionStatus, supportsConstruction } from '../construction';
import { Icon } from './Icon';

type Props = {
  datasets: Dataset[];
  busy?: boolean;
  onUpload: (file: File) => void;
  onConstruct: (dataset: Dataset) => void;
  onStateChange?: (dataset: Dataset, state: ConstructionState) => void;
};

function sourceSummary(dataset: Dataset) {
  const construction = dataset.metadata?.construction as { revision?: number; status?: string } | undefined;
  const status = constructionStatus(dataset);
  const nodes = Number(dataset.metadata?.num_nodes);
  const edges = Number(dataset.metadata?.num_edges);
  if (status === 'building' || status === 'processing') return '构图处理中，可重新进入查看进度';
  if (status === 'failed' || status === 'error') return '上次构图失败，源文件仍已保留';
  if (status === 'algorithm_unavailable') return '构图算法暂不可用，源文件已保留';
  if (status === 'ready' || (!construction?.status && Number(construction?.revision) > 0)) {
    if (Number.isFinite(nodes) && Number.isFinite(edges)) return `${nodes.toLocaleString()} 个节点 · ${edges.toLocaleString()} 条边`;
    return `已构图 · 版本 ${construction?.revision || 1}`;
  }
  return `${dataset.original_filename} · 已就绪，等待构图`;
}

function sourceType(dataset: Dataset) {
  return (dataset.file_type || dataset.original_filename.split('.').pop() || 'FILE').replace(/^\./, '').toUpperCase();
}

export function CreateGraphWorkspace({ datasets, busy = false, onUpload, onConstruct, onStateChange }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const stateCallback = useRef(onStateChange);
  stateCallback.current = onStateChange;
  const constructible = datasets.filter(supportsConstruction);
  const otherDatasets = datasets.filter(dataset => !supportsConstruction(dataset));
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

  return <div className="create-graph-workspace">
    <header className="create-graph-header">
      <div className="create-graph-title">
        <span className="context-icon"><Icon name="graph" size={23} /></span>
        <div><span>CREATE YOUR GRAPH</span><h1>构图工作台</h1><p>上传源文件，构建、检查并优化可复用的图数据。</p></div>
      </div>
      <input ref={inputRef} type="file" hidden accept=".sql,.csv,.txt" onChange={event => { const file = event.currentTarget.files?.[0]; if (file) onUpload(file); event.currentTarget.value = ''; }} />
      <button className="primary-command create-graph-upload-button" onClick={chooseFile} disabled={busy}><Icon name="upload" size={16} /> 上传数据源</button>
    </header>

    <div className="create-graph-content">
      <section className="create-graph-intro" aria-labelledby="create-graph-intro-title">
        <div className="create-graph-intro-copy">
          <span className="create-graph-kicker">从结构化数据开始</span>
          <h2 id="create-graph-intro-title">把数据源变成可以分析的图</h2>
          <p>选择已有源文件，或上传新的 SQL、CSV、TXT。构图结果保存在个人数据集中，不依赖当前会话，可随时回来继续优化。</p>
          <button className="primary-command" onClick={chooseFile} disabled={busy}><Icon name="plus" size={16} /> 选择源文件</button>
        </div>
        <div className="create-graph-flow" aria-label="构图流程">
          <div><span>01</span><Icon name="file" size={20} /><strong>添加数据源</strong><small>SQL / CSV / TXT</small></div>
          <Icon name="arrow-right" size={18} />
          <div><span>02</span><Icon name="graph" size={20} /><strong>生成并验证</strong><small>节点、关系与属性</small></div>
          <Icon name="arrow-right" size={18} />
          <div><span>03</span><Icon name="check" size={20} /><strong>保存与优化</strong><small>形成可复用图数据</small></div>
        </div>
      </section>

      <section className="create-graph-sources" aria-labelledby="create-graph-sources-title">
        <div className="create-graph-section-head"><div><h2 id="create-graph-sources-title">我的构图数据源</h2><p>点击一个数据源进入构图编辑器。</p></div><span>{constructible.length} 个可用</span></div>
        {constructible.length ? <div className="create-graph-source-grid">{constructible.map(dataset => <article className="create-graph-source-card" key={dataset.dataset_id}>
          <div className="create-graph-source-icon"><Icon name="database" size={20} /></div>
          <div className="create-graph-source-copy"><div><strong title={dataset.name}>{dataset.name}</strong><span>{sourceType(dataset)}</span></div><p>{sourceSummary(dataset)}</p></div>
          <button className="secondary-command" onClick={() => onConstruct(dataset)} disabled={busy} aria-label={`${constructionLabel(dataset)} ${dataset.name}`}>{constructionLabel(dataset)} <Icon name="arrow-right" size={14} /></button>
        </article>)}</div> : <div className="create-graph-empty">
          <span><Icon name="graph" size={34} /></span><strong>还没有可构图的数据源</strong><p>上传 SQL、CSV 或 TXT 文件后，会直接进入构图编辑器。</p><button className="secondary-command" onClick={chooseFile} disabled={busy}>上传第一个数据源</button>
        </div>}
        {otherDatasets.length > 0 && <div className="create-graph-other-sources"><Icon name="database" size={15} /><span>另有 {otherDatasets.length} 个现成图数据集无需构图，可在分析任务中直接使用。</span></div>}
      </section>
    </div>
  </div>;
}

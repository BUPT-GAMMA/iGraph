import { Component, useEffect, useMemo, useRef, useState } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import * as d3 from 'd3';
import type { GraphCollectionSummary, GraphData, GraphViewState, Message, ToolCall } from '../types';
import { Icon } from './Icon';
import { SlidingTabs } from './SlidingTabs';

type Tab = 'Overview' | 'Graph' | 'Data' | 'ML' | 'Artifacts';
type LayoutMode = 'force' | 'radial' | 'circle';

function nodeId(node: Record<string, unknown>, index: number) { return String(node.id ?? node.label ?? index); }

type GraphWorkspaceProps = { view: GraphViewState; datasetName?: string; messages?: Message[]; liveTools?: ToolCall[]; tab: Tab; onTab: (tab: Tab) => void; selected: Record<string, unknown> | null; onSelect: (node: Record<string, unknown>) => void; collapsed: boolean; onToggle: () => void; onExpand?: (node: Record<string, unknown>) => void; layoutVersion?: number; resetKey?: string };

class GraphWorkspaceErrorBoundary extends Component<{ children: ReactNode; resetKey?: string; collapsed: boolean; onToggle: () => void }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('Graph workspace render failed', { error, info }); }
  componentDidUpdate(previous: Readonly<{ resetKey?: string }>) {
    if (this.state.failed && previous.resetKey !== this.props.resetKey) this.setState({ failed: false });
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return <aside className={`graph-workspace ${this.props.collapsed ? 'is-collapsed' : ''}`}><div className="workspace-top"><strong className="workspace-title">图谱工作台</strong><button className="plain-icon workspace-panel-toggle" onClick={this.props.onToggle} aria-label={this.props.collapsed ? '展开右侧栏' : '收起右侧栏'}><Icon name={this.props.collapsed ? 'panel-right-open' : 'panel-right-close'} size={18} /></button></div><div className="workspace-panel-content" aria-hidden={this.props.collapsed}><div className="graph-empty"><strong>工作台暂时无法显示</strong><span>切换会话或数据集后将自动恢复。</span></div></div></aside>;
  }
}

export function GraphWorkspace(props: GraphWorkspaceProps) {
  return <GraphWorkspaceErrorBoundary resetKey={props.resetKey} collapsed={props.collapsed} onToggle={props.onToggle}><GraphWorkspaceContent {...props} /></GraphWorkspaceErrorBoundary>;
}

function GraphWorkspaceContent({ view, datasetName, messages = [], liveTools = [], tab, onTab, selected, onSelect, collapsed, onToggle, onExpand, layoutVersion = 0 }: GraphWorkspaceProps) {
  const data = view.status === 'ready' ? view.data : null;
  const canvas = useRef<SVGSVGElement>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [layout, setLayout] = useState<LayoutMode>('force');
  const [connectedOnly, setConnectedOnly] = useState(false);
  const layoutLabel = layout === 'force' ? '力导向' : layout === 'radial' ? '径向' : '环形';
  const degreeById = useMemo(() => {
    const degrees = new Map<string, number>();
    (data?.edges || []).forEach(edge => {
      const source = String(edge.src ?? edge.source ?? '');
      const target = String(edge.dst ?? edge.target ?? '');
      degrees.set(source, (degrees.get(source) || 0) + 1);
      degrees.set(target, (degrees.get(target) || 0) + 1);
    });
    return degrees;
  }, [data]);
  const visibleNodes = useMemo(() => {
    if (!data?.nodes) return [];
    const text = query.trim().toLowerCase();
    return data.nodes.map((node, index) => ({ node, id: nodeId(node, index) })).filter(item => {
      const matches = !text || item.id.toLowerCase().includes(text) || String(item.node.label ?? '').toLowerCase().includes(text);
      const connected = !connectedOnly || (degreeById.get(item.id) || 0) > 0;
      return matches && connected;
    });
  }, [data, query, connectedOnly, degreeById]);

  useEffect(() => {
    if (collapsed || !canvas.current || !data?.nodes?.length || tab !== 'Graph') return;
    const svg = d3.select(canvas.current); svg.selectAll('*').remove();
    const host = canvas.current.parentElement; const width = host?.clientWidth || 360; const height = host?.clientHeight || 420;
    const nodes = visibleNodes.slice(0, 180).map(item => ({ ...item.node, id: item.id }));
    const nodeIds = new Set(nodes.map(node => node.id));
    const edges = (data.edges || []).map(edge => ({ source: String(edge.src ?? edge.source), target: String(edge.dst ?? edge.target) })).filter(edge => nodeIds.has(edge.source) && nodeIds.has(edge.target)).slice(0, 400);
    const simulation = d3.forceSimulation(nodes as any).force('link', d3.forceLink(edges as any).id((node: any) => node.id).distance(layout === 'radial' ? 72 : 58)).force('charge', d3.forceManyBody().strength(-150)).force('center', d3.forceCenter(width / 2, height / 2)).force('collision', d3.forceCollide(15));
    if (layout === 'radial') simulation.force('radial', d3.forceRadial(Math.min(width, height) * .3, width / 2, height / 2).strength(.45));
    const link = svg.append('g').selectAll('line').data(edges).join('line').attr('class', 'graph-link');
    const node = svg.append('g').selectAll('g').data(nodes).join('g').attr('class', 'graph-node').on('click', (_, item) => onSelect(item as Record<string, unknown>)).call(d3.drag<any, any>().on('start', (event, item) => { if (!event.active) simulation.alphaTarget(.25).restart(); item.fx = item.x; item.fy = item.y; }).on('drag', (event, item) => { item.fx = event.x; item.fy = event.y; }).on('end', (event, item) => { if (!event.active) simulation.alphaTarget(0); item.fx = null; item.fy = null; }));
    if (layout === 'circle') {
      const radius = Math.max(35, Math.min(width, height) * .34);
      nodes.forEach((item: any, index) => { const angle = (index / Math.max(nodes.length, 1)) * Math.PI * 2; item.x = width / 2 + Math.cos(angle) * radius; item.y = height / 2 + Math.sin(angle) * radius; });
      simulation.stop();
    }
    node.append('circle').attr('r', (item: any) => item.id === String(selected?.id ?? '') ? 13 : 8).attr('fill', (item: any) => item.id === String(selected?.id ?? '') ? 'var(--graph-node-selected-fill)' : 'var(--graph-node-fill)');
    node.append('text').attr('dy', 20).text((item: any) => String(item.label ?? item.id).slice(0, 12));
    simulation.on('tick', () => { link.attr('x1', (edge: any) => edge.source.x).attr('y1', (edge: any) => edge.source.y).attr('x2', (edge: any) => edge.target.x).attr('y2', (edge: any) => edge.target.y); node.attr('transform', (item: any) => `translate(${item.x},${item.y})`); });
    simulation.tick();
    return () => { simulation.stop(); };
  }, [collapsed, data, tab, selected, onSelect, layoutVersion, layout, visibleNodes]);
  const tabs: Tab[] = ['Overview', 'Graph', 'Data', 'ML', 'Artifacts'];
  return <aside className={`graph-workspace ${collapsed ? 'is-collapsed' : ''}`}><div className="workspace-top"><strong className="workspace-title">图谱工作台</strong><button className="plain-icon workspace-panel-toggle" onClick={onToggle} aria-label={collapsed ? '展开右侧栏' : '收起右侧栏'} title={collapsed ? '展开右侧栏' : '收起右侧栏'}><Icon name={collapsed ? 'panel-right-open' : 'panel-right-close'} size={18} /></button></div><div className="workspace-panel-content" aria-hidden={collapsed}><SlidingTabs ariaLabel="图谱工作台视图" className="workspace-tabs" indicatorClassName="workspace-tab-indicator" options={tabs.map(item => ({ value: item, label: item }))} value={tab} onChange={onTab} />
    {tab === 'ML' ? <MLTab messages={messages} liveTools={liveTools} /> : tab === 'Artifacts' ? <ArtifactsTab messages={messages} /> : view.status === 'unsupported' ? (tab === 'Graph' ? <div className="graph-empty graph-unsupported"><div className="empty-icon small"><Icon name="graph" size={22} /></div><strong>非独立图，无法直接可视化</strong></div> : <CollectionTab tab={tab} datasetName={datasetName} summary={view.summary} />) : view.status === 'loading' ? <div className="graph-empty"><span className="loader" /><strong>正在加载图谱</strong></div> : view.status === 'failed' ? <div className="graph-empty"><div className="empty-icon small"><Icon name="close" size={22} /></div><strong>图谱加载失败</strong><span>{view.error}</span></div> : !data ? <div className="graph-empty"><div className="empty-icon small"><Icon name="graph" size={22} /></div><strong>暂无图谱</strong><span>加载数据或发送建图请求后，这里会显示图谱分析结果。</span></div> : tab === 'Graph' ? <><div className="graph-toolbar"><button onClick={() => setSearchOpen(current => !current)} aria-pressed={searchOpen}><Icon name="search" size={14} /> 搜索节点</button><button onClick={() => setLayout(current => current === 'force' ? 'radial' : current === 'radial' ? 'circle' : 'force')} title="切换图谱布局">布局：{layoutLabel} <Icon name="chevron" size={12} /></button><button onClick={() => setConnectedOnly(current => !current)} aria-pressed={connectedOnly}>过滤{connectedOnly ? '：有连接' : ''}</button></div>{searchOpen ? <div className="graph-search"><Icon name="search" size={14} /><input autoFocus value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索节点 ID 或名称" /><span>{visibleNodes.length} 个结果</span></div> : null}<div className="graph-canvas"><svg ref={canvas} /></div><div className="node-inspector">{selected ? <><div className="inspector-title"><strong>{String(selected.label ?? selected.id ?? 'Selected node')}</strong><span>selected</span></div><div className="inspector-row"><span>Node ID</span><strong>{String(selected.id ?? '-')}</strong></div><div className="inspector-row"><span>Type</span><strong>{String(selected.type ?? selected.category ?? 'node')}</strong></div><div className="inspector-row"><span>Degree</span><strong>{degreeById.get(String(selected.id ?? '')) ?? '-'}</strong></div><div className="inspector-actions"><button onClick={() => onExpand?.(selected)} disabled={!onExpand}><Icon name="plus" size={13} /> 扩展邻居</button><button onClick={() => navigator.clipboard?.writeText(String(selected.id ?? ''))}><Icon name="copy" size={13} /> 复制 ID</button></div></> : <span>点击图中节点查看属性</span>}</div></> : <WorkspaceTab tab={tab} data={data} messages={messages} liveTools={liveTools} />}
  </div></aside>;
}

function displayCount(value: unknown) {
  const count = Number(value);
  return Number.isFinite(count) ? count.toLocaleString() : '-';
}

function CollectionTab({ tab, datasetName, summary }: { tab: Tab; datasetName?: string; summary: GraphCollectionSummary }) {
  const splits = summary.split_sizes || {};
  const validationSize = splits.val ?? splits.valid;
  if (tab === 'Overview') {
    return <div className="overview-content"><div className="overview-hero"><span className="eyebrow">GRAPH COLLECTION</span><h2>{datasetName || '当前数据集'}</h2><strong>多图分类数据集已就绪</strong></div><div className="stat-grid"><div><span>Graphs</span><strong>{displayCount(summary.num_graphs)}</strong></div><div><span>Tasks</span><strong>{displayCount(summary.num_tasks)}</strong></div><div><span>Metric</span><strong>{summary.metric || '-'}</strong></div><div><span>Splits</span><strong>{Object.keys(splits).length || '-'}</strong></div></div><div className="workspace-note"><Icon name="spark" size={15} /> 该数据集包含多张独立样本图，可继续用于图分类训练和推理。</div></div>;
  }
  const missingRate = Number(summary.missing_label_rate);
  return <div className="tab-content collection-data"><div className="tab-title"><strong>数据集划分与特征</strong><span>{displayCount(summary.num_graphs)} graphs</span></div><div className="collection-section"><strong>数据划分</strong><div className="stat-grid"><div><span>Train</span><strong>{displayCount(splits.train)}</strong></div><div><span>Validation</span><strong>{displayCount(validationSize)}</strong></div><div><span>Test</span><strong>{displayCount(splits.test)}</strong></div><div><span>Edgeless</span><strong>{displayCount(summary.num_edgeless_graphs)}</strong></div></div></div><div className="collection-section"><strong>词表与标签</strong><div className="stat-grid"><div><span>Node vocab</span><strong>{displayCount(summary.node_vocab_size)}</strong></div><div><span>Edge vocab</span><strong>{displayCount(summary.edge_vocab_size)}</strong></div><div><span>Missing labels</span><strong>{Number.isFinite(missingRate) ? `${(missingRate * 100).toFixed(2)}%` : '-'}</strong></div><div><span>Edge features</span><strong>{summary.uses_edge_features == null ? '-' : summary.uses_edge_features ? 'Yes' : 'No'}</strong></div></div></div></div>;
}

function WorkspaceTab({ tab, data, messages, liveTools }: { tab: Tab; data: GraphData; messages: Message[]; liveTools: ToolCall[] }) {
  if (tab === 'Overview') {
    const totalNodes = data.total_nodes ?? data.num_nodes ?? data.nodes.length;
    const totalEdges = data.total_edges ?? data.num_edges ?? data.edges.length;
    const renderedNodes = data.truncated_to ?? data.nodes.length;
    return <div className="overview-content"><div className="overview-hero"><span className="eyebrow">CURRENT GRAPH</span><h2>当前数据集</h2><strong>图谱分析已就绪</strong></div><div className="stat-grid"><div><span>Nodes</span><strong>{totalNodes.toLocaleString()}</strong></div><div><span>Edges</span><strong>{totalEdges.toLocaleString()}</strong></div><div><span>Direction</span><strong>{data.directed ? 'Directed' : 'Undirected'}</strong></div><div><span>Canvas</span><strong>{renderedNodes.toLocaleString()}</strong></div></div>{data.truncated ? <div className="workspace-note"><Icon name="spark" size={15} /> 画布当前展示 {renderedNodes.toLocaleString()} 个节点，数据集总量统计保持完整。</div> : <div className="workspace-note"><Icon name="spark" size={15} /> 从中央对话发起图算法，结果会同步到这里。</div>}</div>;
  }
  if (tab === 'Data') return <div className="tab-content"><div className="tab-title"><strong>节点与边数据</strong><span>{(data.total_nodes ?? data.num_nodes ?? data.nodes.length).toLocaleString()} nodes</span></div><div className="data-list">{data.nodes.slice(0, 20).map((node, index) => <div key={index}><Icon name="project" size={14} /><span>{String(node.label ?? node.id ?? `Node ${index + 1}`)}</span><small>{String(node.type ?? 'node')}</small></div>)}</div>{data.nodes.length < (data.total_nodes ?? data.num_nodes ?? data.nodes.length) ? <div className="workspace-note">当前为可视化预览，已加载 {data.nodes.length.toLocaleString()} 个节点样本。</div> : null}</div>;
  if (tab === 'ML') return <MLTab messages={messages} liveTools={liveTools} />;
  return <ArtifactsTab messages={messages} />;
}

const ML_TOOLS = new Set(['train', 'infer', 'load_graph_classification_dataset']);
const METRIC_LABELS: Record<string, string> = {
  loss: 'Loss', train_loss: 'Train Loss', val_loss: 'Val Loss', test_loss: 'Test Loss',
  train_acc: 'Train Acc', val_acc: 'Val Acc', test_acc: 'Test Acc', accuracy: 'Accuracy',
  val_auc: 'Val AUC', test_auc: 'Test AUC', auc: 'AUC', f1: 'F1', precision: 'Precision', recall: 'Recall',
  best_epoch: 'Best Epoch', trained_epochs: 'Epochs', num_classes: 'Classes',
};
const CHART_COLORS = ['#109c90', '#d97745', '#4476b8', '#bd4f6c', '#7b6cb7'];

function recordValue(value: unknown): Record<string, any> {
  if (typeof value === 'string') {
    try { const decoded = JSON.parse(value); return decoded && typeof decoded === 'object' && !Array.isArray(decoded) ? decoded : {}; } catch { return {}; }
  }
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : {};
}

function metricNumber(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatMetric(key: string, value: unknown) {
  const number = metricNumber(value);
  if (number === null) return String(value ?? '-');
  if (/(acc|accuracy|auc|f1|precision|recall)$/i.test(key) && Math.abs(number) <= 1) return `${(number * 100).toFixed(1)}%`;
  if (Number.isInteger(number)) return number.toLocaleString();
  return number.toFixed(Math.abs(number) < 0.01 ? 5 : 4).replace(/0+$/, '').replace(/\.$/, '');
}

function metricLabel(key: string) {
  return METRIC_LABELS[key] || key.replace(/_/g, ' ').replace(/\b\w/g, (value: string) => value.toUpperCase());
}

function runHistory(run: ToolCall) {
  const result = recordValue(run.result);
  const candidates = [run.history, result.history, recordValue(result.metrics).history];
  const rows = candidates.find(Array.isArray) || [];
  const byEpoch = new Map<number, Record<string, unknown>>();
  rows.forEach(row => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) return;
    const epoch = metricNumber((row as Record<string, unknown>).epoch);
    if (epoch !== null) byEpoch.set(epoch, row as Record<string, unknown>);
  });
  return [...byEpoch.values()].sort((left, right) => Number(left.epoch) - Number(right.epoch));
}

function legacyTrainingMetrics(content: string) {
  const metrics: Record<string, number> = {};
  const splitKeys: Array<[RegExp, string]> = [[/训练集|train/i, 'train'], [/验证集|validation|\bval\b/i, 'val'], [/测试集|test/i, 'test']];
  content.split(/\r?\n/).filter(line => line.trim().startsWith('|')).forEach(line => {
    const cells = line.split('|').slice(1, -1).map(cell => cell.replace(/\*\*/g, '').trim());
    const split = splitKeys.find(([pattern]) => pattern.test(cells[0] || ''))?.[1];
    if (!split || cells.length < 2) return;
    const accuracyMatch = cells[1].match(/-?\d+(?:\.\d+)?/);
    const lossMatch = cells[2]?.match(/-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/i);
    const countMatch = cells[3]?.match(/\d+/);
    if (accuracyMatch) metrics[`${split}_acc`] = Number(accuracyMatch[0]) / (cells[1].includes('%') ? 100 : 1);
    if (lossMatch) metrics[`${split}_loss`] = Number(lossMatch[0]);
    if (countMatch) metrics[`num_${split}`] = Number(countMatch[0]);
  });
  const bestEpoch = content.match(/最佳轮次\*{0,2}\s*[:：]\s*(\d+)/);
  if (bestEpoch) metrics.best_epoch = Number(bestEpoch[1]);
  return Object.keys(metrics).length ? metrics : null;
}

function failedTrainingRun(tool: ToolCall) {
  if (String(tool.tool || '') !== 'train' || tool.result == null) return false;
  const result = recordValue(tool.result);
  if (Object.keys(result).length) return result.success === false || Boolean(result.error && !result.async);
  return typeof tool.result === 'string' && /(validation error|error executing tool|invalid (?:argument|input)|tool.*failed)/i.test(tool.result);
}

function mergeMlRuns(messages: Message[], liveTools: ToolCall[]) {
  const saved = messages.flatMap(message => (message.tools || []).filter(tool => ML_TOOLS.has(String(tool.tool || '')) && !failedTrainingRun(tool)).map(tool => {
    if (String(tool.tool || '') !== 'train' || Object.keys(recordValue(recordValue(tool.result).metrics)).length) return tool;
    const metrics = legacyTrainingMetrics(message.content || '');
    return metrics ? { ...tool, result: { ...recordValue(tool.result), metrics } } : tool;
  }));
  const runs = [...saved];
  liveTools.filter(tool => ML_TOOLS.has(String(tool.tool || '')) && !failedTrainingRun(tool)).forEach(live => {
    const index = live.call_id ? runs.findIndex(run => run.call_id === live.call_id) : -1;
    if (index < 0) runs.push(live);
    else runs[index] = { ...runs[index], ...live, result: live.result ?? runs[index].result, history: live.history?.length ? live.history : runs[index].history };
  });
  return runs;
}

function availableMetricKeys(rows: Array<Record<string, unknown>>) {
  const excluded = new Set(['epoch', 'epochs', 'progress', 'best_epoch', 'bad_epochs', 'early_stop_patience', 'threshold']);
  const keys = new Set<string>();
  rows.forEach(row => Object.entries(row).forEach(([key, value]) => { if (!excluded.has(key) && metricNumber(value) !== null) keys.add(key); }));
  return [...keys];
}

function MetricChart({ rows, keys, title }: { rows: Array<Record<string, unknown>>; keys: string[]; title: string }) {
  const width = 560; const height = 190; const left = 46; const right = 12; const top = 18; const bottom = 28;
  const series = keys.map(key => ({ key, values: rows.map(row => ({ epoch: Number(row.epoch), value: metricNumber(row[key]) })).filter(point => point.value !== null) as Array<{ epoch: number; value: number }> })).filter(item => item.values.length);
  if (!series.length) return null;
  const values = series.flatMap(item => item.values.map(point => point.value));
  const epochs = series.flatMap(item => item.values.map(point => point.epoch));
  let minY = Math.min(...values); let maxY = Math.max(...values);
  if (keys.every(key => /(acc|accuracy|auc|f1|precision|recall)$/i.test(key)) && minY >= 0 && maxY <= 1) { minY = 0; maxY = 1; }
  else { const padding = (maxY - minY || Math.abs(maxY) || 1) * .12; minY = Math.max(0, minY - padding); maxY += padding; }
  const minX = Math.min(...epochs); const maxX = Math.max(...epochs);
  const x = (epoch: number) => left + ((epoch - minX) / (maxX - minX || 1)) * (width - left - right);
  const y = (value: number) => top + (1 - (value - minY) / (maxY - minY || 1)) * (height - top - bottom);
  return <div className="ml-chart-panel"><div className="ml-chart-heading"><strong>{title}</strong><div>{series.map((item, index) => <span key={item.key}><i style={{ backgroundColor: CHART_COLORS[index % CHART_COLORS.length] }} />{metricLabel(item.key)}</span>)}</div></div><svg className="ml-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${title}训练曲线`}>
    {[0, .25, .5, .75, 1].map(fraction => { const value = maxY - (maxY - minY) * fraction; const py = top + (height - top - bottom) * fraction; return <g key={fraction}><line x1={left} x2={width - right} y1={py} y2={py} /><text x={left - 7} y={py + 4} textAnchor="end">{formatMetric(keys[0], value)}</text></g>; })}
    <text x={left} y={height - 7}>E{minX}</text><text x={width - right} y={height - 7} textAnchor="end">E{maxX}</text>
    {series.map((item, index) => { const points = item.values.map(point => `${x(point.epoch)},${y(point.value)}`).join(' '); return <g key={item.key}><polyline points={points} style={{ stroke: CHART_COLORS[index % CHART_COLORS.length] }} />{item.values.map(point => <circle key={point.epoch} cx={x(point.epoch)} cy={y(point.value)} r={item.values.length < 25 ? 2.4 : 1.4} style={{ fill: CHART_COLORS[index % CHART_COLORS.length] }} />)}</g>; })}
  </svg></div>;
}

function MetricsTable({ rows }: { rows: Array<Record<string, unknown>> }) {
  const keys = availableMetricKeys(rows).slice(0, 5);
  if (!rows.length) return <div className="ml-no-history">该运行没有可用的逐 epoch 指标。</div>;
  return <div className="ml-table-wrap"><table className="ml-table"><thead><tr><th>Epoch</th>{keys.map(key => <th key={key}>{metricLabel(key)}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={`${row.epoch}-${index}`}><td>{String(row.epoch)}</td>{keys.map(key => <td key={key}>{formatMetric(key, row[key])}</td>)}</tr>)}</tbody></table></div>;
}

function FinalMetricsTable({ metrics }: { metrics: Record<string, unknown> }) {
  const rows = [
    { label: '训练集', prefix: 'train' },
    { label: '验证集', prefix: 'val' },
    { label: '测试集', prefix: 'test' },
  ].filter(item => ['acc', 'loss'].some(key => metricNumber(metrics[`${item.prefix}_${key}`]) !== null) || metricNumber(metrics[`num_${item.prefix}`]) !== null);
  if (!rows.length) return <div className="ml-no-history">该运行没有可用的逐 epoch 指标。</div>;
  return <div className="ml-table-wrap"><table className="ml-table"><thead><tr><th>数据划分</th><th>Accuracy</th><th>Loss</th><th>样本数</th></tr></thead><tbody>{rows.map(row => <tr key={row.prefix}><td>{row.label}</td><td>{formatMetric(`${row.prefix}_acc`, metrics[`${row.prefix}_acc`])}</td><td>{formatMetric(`${row.prefix}_loss`, metrics[`${row.prefix}_loss`])}</td><td>{formatMetric(`num_${row.prefix}`, metrics[`num_${row.prefix}`])}</td></tr>)}</tbody></table></div>;
}

function MLRunCard({ run, index }: { run: ToolCall; index: number }) {
  const history = runHistory(run);
  const [view, setView] = useState<'chart' | 'table'>(history.length ? 'chart' : 'table');
  const result = recordValue(run.result);
  const metrics = recordValue(result.metrics);
  const args = recordValue(run.args);
  const progress = recordValue(run.progress);
  const context = { ...args, ...result, ...progress };
  const lossKeys = availableMetricKeys(history).filter(key => /loss/i.test(key));
  const scoreKeys = availableMetricKeys(history).filter(key => /(acc|accuracy|auc|f1|precision|recall)/i.test(key));
  const preferred = ['test_acc', 'val_acc', 'train_acc', 'test_auc', 'val_auc', 'accuracy', 'f1', 'train_loss', 'loss', 'best_epoch', 'trained_epochs'];
  const summaryKeys = preferred.filter(key => metricNumber(metrics[key]) !== null).slice(0, 4);
  if (!summaryKeys.length) Object.keys(metrics).filter(key => metricNumber(metrics[key]) !== null).slice(0, 4).forEach(key => summaryKeys.push(key));
  const taskNames: Record<string, string> = { node_classification: '节点分类', link_prediction: '链接预测', graph_classification: '图分类', pretrain: '图预训练' };
  const task = String(context.task || 'Graph ML');
  const model = String(context.display_model || context.model || context.method || '模型训练');
  const completedEpochs = Number(metrics.trained_epochs || progress.epoch || history[history.length - 1]?.epoch || 0);
  const totalEpochs = Number(progress.epochs || recordValue(args.train_config).epochs || recordValue(args.algo_config).epochs || completedEpochs || 0);
  return <article className="ml-run-card"><header className="ml-run-header"><div className="workspace-list-icon"><Icon name="cube" size={16} /></div><div><strong>{taskNames[task] || task}</strong><span>{model}{totalEpochs ? ` · ${completedEpochs}/${totalEpochs} epochs` : ''}</span></div><b className={run.done ? 'done' : 'running'}>{run.done ? (metrics.stopped_early ? '提前停止' : '已完成') : '训练中'}</b></header>
    {summaryKeys.length ? <div className="ml-metric-grid">{summaryKeys.map(key => <div key={key}><span>{metricLabel(key)}</span><strong>{formatMetric(key, metrics[key])}</strong></div>)}</div> : null}
    <div className="ml-view-bar"><span>{history.length ? `${history.length} 个训练点` : `运行 ${index + 1}`}</span><SlidingTabs ariaLabel="训练结果视图" className="ml-view-toggle" indicatorClassName="ml-view-indicator" options={[{ value: 'chart', label: '曲线', disabled: !history.length }, { value: 'table', label: '表格' }]} value={view} onChange={setView} /></div>
    {view === 'chart' ? <div className="ml-charts"><MetricChart rows={history} keys={lossKeys} title="损失" /><MetricChart rows={history} keys={scoreKeys} title="评估指标" />{!lossKeys.length && !scoreKeys.length ? <FinalMetricsTable metrics={metrics} /> : null}</div> : history.length ? <MetricsTable rows={history} /> : <FinalMetricsTable metrics={metrics} />}
  </article>;
}

function MLTab({ messages, liveTools }: { messages: Message[]; liveTools: ToolCall[] }) {
  const runs = useMemo(() => mergeMlRuns(messages, liveTools), [messages, liveTools]);
  if (!runs.length) return <div className="tab-placeholder"><Icon name="cube" size={22} /><strong>Graph ML</strong><span>当前会话还没有训练或推理记录。</span></div>;
  const trainingRuns = runs.filter(run => String(run.tool || '') === 'train');
  const otherRuns = runs.filter(run => String(run.tool || '') !== 'train');
  return <div className="tab-content ml-tab"><div className="tab-title"><strong>Graph ML 训练结果</strong><span>{trainingRuns.length} 次训练</span></div>{trainingRuns.map((run, index) => <MLRunCard key={`${run.call_id || 'train'}-${index}`} run={run} index={index} />)}{otherRuns.length ? <div className="ml-activity"><strong>数据与推理记录</strong><div className="workspace-list">{otherRuns.map((run, index) => <div className="workspace-list-row" key={`${run.call_id || run.tool}-${index}`}><div className="workspace-list-icon"><Icon name="cube" size={15} /></div><div><strong>{String(run.tool)}</strong><span>{run.done ? '已完成' : '运行中'}</span></div><b>{run.done ? '完成' : '运行中'}</b></div>)}</div></div> : null}</div>;
}

function ArtifactsTab({ messages }: { messages: Message[] }) {
  const reports = messages.flatMap(message => message.reports || []);
  if (!reports.length) return <div className="tab-placeholder"><Icon name="file" size={22} /><strong>Artifacts</strong><span>当前会话还没有分析报告或导出产物。</span></div>;
  return <div className="tab-content"><div className="tab-title"><strong>分析产物</strong><span>{reports.length} 份报告</span></div><div className="workspace-list">{reports.map((report, index) => <div className="workspace-list-row" key={`${String(report.id || report.title || 'report')}-${index}`}><div className="workspace-list-icon"><Icon name="file" size={15} /></div><div><strong>{String(report.title || report.name || `分析报告 ${index + 1}`)}</strong><span>{String(report.intent || report.type || 'analysis')}</span></div></div>)}</div></div>;
}

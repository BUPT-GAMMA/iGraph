import { memo, useEffect, useId, useMemo, useRef, useState } from 'react';
import * as d3 from 'd3';
import { listProjectTrainingRuns } from '../api';
import type { GraphCollectionSummary, GraphData, GraphViewState, Highlight, Message, ProjectTrainingRun, ToolCall } from '../types';
import { BrandLogo, Icon } from './Icon';
import { SlidingTabs } from './SlidingTabs';
import { WorkspaceFiles } from './WorkspaceFiles';

type Tab = 'Overview' | 'Graph' | 'Data' | 'ML' | 'Artifacts';
type LayoutMode = 'force' | 'radial' | 'circle';
type StopJob = (sessionId: string, jobId: string, notify: (status: string) => void) => void;

const TAB_LABELS: Record<Tab, string> = {
  Overview: '概览',
  Graph: '图谱',
  Data: '数据',
  ML: '机器学习',
  Artifacts: '分析产物',
};

function nodeId(node: Record<string, unknown>, index: number) { return String(node.id ?? node.label ?? index); }

const COMMUNITY_COLORS = ['#3b82f6', '#f97316', '#10b981', '#a855f7', '#eab308', '#ef4444', '#06b6d4', '#ec4899'];
const DEFAULT_HIGHLIGHT_COLORS: Record<string, string> = { node: 'var(--accent, #2563eb)', path: '#d97706', subgraph: '#7c3aed' };

function normalizeHighlights(value: GraphData['highlights']): Highlight[] {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== 'object') return [];
  const output: Highlight[] = [];
  (value.nodes || []).forEach((item, index) => output.push({ id: `node-${index}`, type: 'node', nodes: item.ids || [], color: item.color, label: item.label }));
  (value.paths || []).forEach((item, index) => output.push({ id: `path-${index}`, type: 'path', nodes: item.node_sequence || [], edges: item.edges as Highlight['edges'], color: item.color, label: item.label }));
  (value.subgraphs || []).forEach((item, index) => output.push({ id: `subgraph-${index}`, type: 'subgraph', nodes: item.nodes || [], edges: item.edges as Highlight['edges'], color: item.color, label: item.label }));
  return output;
}

function edgeKey(source: unknown, target: unknown) { return `${String(source)}\u0000${String(target)}`; }

function endpointId(value: unknown) {
  return String(value && typeof value === 'object' && 'id' in value ? (value as { id: unknown }).id : value);
}

function highlightedEdgeColor(mapping: Map<string, string>, source: unknown, target: unknown) {
  const sourceId = endpointId(source);
  const targetId = endpointId(target);
  return mapping.get(edgeKey(sourceId, targetId)) || mapping.get(edgeKey(targetId, sourceId));
}

function communityColor(community: string | undefined) {
  if (community === undefined) return 'var(--graph-node-fill)';
  const numeric = Number(community);
  const index = Number.isFinite(numeric)
    ? Math.abs(Math.trunc(numeric))
    : [...community].reduce((hash, character) => ((hash * 31) + character.charCodeAt(0)) >>> 0, 0);
  return COMMUNITY_COLORS[index % COMMUNITY_COLORS.length];
}

function paddedHullPath(points: Array<[number, number]>, padding = 15) {
  if (!points.length) return null;
  const expanded = points.flatMap(([x, y]) => Array.from({ length: 8 }, (_, index) => {
    const angle = index * Math.PI / 4;
    return [x + Math.cos(angle) * padding, y + Math.sin(angle) * padding] as [number, number];
  }));
  const hull = d3.polygonHull(expanded);
  return hull ? `M${hull.map(point => point.join(',')).join('L')}Z` : null;
}

function highlightEdgePairs(highlight: Highlight) {
  const pairs = new Set<string>();
  const sequence = (highlight.nodes || []).map(String);
  for (let index = 1; index < sequence.length; index += 1) pairs.add(edgeKey(sequence[index - 1], sequence[index]));
  (highlight.edges || []).forEach(edge => {
    if (Array.isArray(edge)) pairs.add(edgeKey(edge[0], edge[1]));
    else pairs.add(edgeKey(edge.src ?? edge.source, edge.dst ?? edge.target));
  });
  return pairs;
}

type GraphWorkspaceProps = {
  data: GraphData | null;
  view?: GraphViewState;
  datasetName?: string;
  messages?: Message[];
  liveTools?: ToolCall[];
  projectId?: string | null;
  sessionId?: string;
  tab: Tab;
  onTab: (tab: Tab) => void;
  selected: Record<string, unknown> | null;
  onSelect: (node: Record<string, unknown>) => void;
  collapsed: boolean;
  onToggle: () => void;
  onExpand?: (node: Record<string, unknown>) => void;
  layoutVersion?: number;
  onStopJob?: StopJob;
  embedded?: boolean;
  onConstruct?: () => void;
  constructionLabel?: string;
};

function graphWorkspacePropsEqual(previous: GraphWorkspaceProps, next: GraphWorkspaceProps) {
  const coreEqual = previous.data === next.data
    && previous.view === next.view
    && previous.tab === next.tab
    && previous.selected === next.selected
    && previous.collapsed === next.collapsed
    && previous.layoutVersion === next.layoutVersion
    && previous.projectId === next.projectId
    && previous.sessionId === next.sessionId
    && previous.onStopJob === next.onStopJob;
  if (previous.embedded !== next.embedded || previous.onConstruct !== next.onConstruct || previous.constructionLabel !== next.constructionLabel || previous.datasetName !== next.datasetName) return false;
  if (!coreEqual) return false;
  if (next.tab === 'ML' || next.tab === 'Artifacts') return previous.messages === next.messages && previous.liveTools === next.liveTools;
  return true;
}

export const GraphWorkspace = memo(function GraphWorkspace({ data, view, datasetName, messages = [], liveTools = [], projectId = '', sessionId = '', tab, onTab, selected, onSelect, collapsed, onToggle, onExpand, layoutVersion = 0, onStopJob, embedded, onConstruct, constructionLabel = '构图' }: GraphWorkspaceProps) {
  const arrowId = `graph-arrow-${useId().replace(/:/g, '')}`;
  const incomingDataSignature = useMemo(() => data ? JSON.stringify(data) : 'empty', [data]);
  const stableDataRef = useRef<{ signature: string; data: GraphData | null }>({ signature: incomingDataSignature, data });
  if (stableDataRef.current.signature !== incomingDataSignature) stableDataRef.current = { signature: incomingDataSignature, data };
  const renderedData = stableDataRef.current.data;
  const canvas = useRef<SVGSVGElement>(null);
  const onSelectRef = useRef(onSelect);
  const zoomTransformRef = useRef(d3.zoomIdentity);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [layout, setLayout] = useState<LayoutMode>('force');
  const [connectedOnly, setConnectedOnly] = useState(false);
  const [canvasBounds, setCanvasBounds] = useState('');
  const [fitVersion, setFitVersion] = useState(0);
  const [projectTrainingRuns, setProjectTrainingRuns] = useState<ProjectTrainingRun[]>([]);
  const [projectTrainingLoading, setProjectTrainingLoading] = useState(false);
  const [projectTrainingError, setProjectTrainingError] = useState('');
  const hasPendingLiveTraining = liveTools.some(tool => String(tool.tool || '') === 'train' && !tool.done);
  useEffect(() => { onSelectRef.current = onSelect; }, [onSelect]);
  useEffect(() => {
    const host = canvas.current?.parentElement;
    if (!embedded || collapsed || tab !== 'Graph' || !host || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(entries => {
      const size = entries[0]?.contentRect;
      if (size) setCanvasBounds(`${Math.round(size.width)}:${Math.round(size.height)}`);
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, [embedded, collapsed, tab, renderedData]);
  const layoutLabel = layout === 'force' ? '力导向' : layout === 'radial' ? '径向' : '环形';
  const communityById = useMemo(() => {
    const raw = renderedData?.community || renderedData?.communities;
    const mapping = raw?.node_community;
    return mapping && typeof mapping === 'object' ? new Map(Object.entries(mapping).map(([id, value]) => [String(id), String(value)])) : new Map<string, string>();
  }, [renderedData]);
  const highlights = useMemo(() => normalizeHighlights(renderedData?.highlights), [renderedData]);
  const highlightByNode = useMemo(() => {
    const mapping = new Map<string, Highlight[]>();
    highlights.forEach(highlight => (highlight.nodes || []).forEach(id => {
      const key = String(id); mapping.set(key, [...(mapping.get(key) || []), highlight]);
    }));
    return mapping;
  }, [highlights]);
  const degreeById = useMemo(() => {
    const degrees = new Map<string, number>();
    (renderedData?.edges || []).forEach(edge => {
      const source = String(edge.src ?? edge.source ?? '');
      const target = String(edge.dst ?? edge.target ?? '');
      degrees.set(source, (degrees.get(source) || 0) + 1);
      degrees.set(target, (degrees.get(target) || 0) + 1);
    });
    return degrees;
  }, [renderedData]);
  const visibleNodes = useMemo(() => {
    if (!renderedData?.nodes) return [];
    const text = query.trim().toLowerCase();
    return renderedData.nodes.map((node, index) => ({ node, id: nodeId(node, index) })).filter(item => {
      const matches = !text || item.id.toLowerCase().includes(text) || String(item.node.label ?? '').toLowerCase().includes(text);
      const connected = !connectedOnly || (degreeById.get(item.id) || 0) > 0;
      return matches && connected;
    });
  }, [renderedData, query, connectedOnly, degreeById]);

  useEffect(() => {
    if (tab !== 'ML' || !projectId) {
      setProjectTrainingRuns([]);
      setProjectTrainingLoading(false);
      setProjectTrainingError('');
      return;
    }
    let disposed = false;
    let timer = 0;
    let controller: AbortController | null = null;
    const refreshTrainingRuns = async () => {
      controller = new AbortController();
      try {
        const response = await listProjectTrainingRuns(projectId, controller.signal);
        if (disposed) return;
        setProjectTrainingRuns(response.training_runs || []);
        setProjectTrainingError('');
        setProjectTrainingLoading(false);
        if (response.has_running) timer = window.setTimeout(refreshTrainingRuns, 1500);
      } catch (cause) {
        if (disposed || (cause && typeof cause === 'object' && 'name' in cause && cause.name === 'AbortError')) return;
        setProjectTrainingError(cause instanceof Error ? cause.message : '训练结果同步失败');
        setProjectTrainingLoading(false);
        timer = window.setTimeout(refreshTrainingRuns, 4000);
      }
    };
    setProjectTrainingRuns([]);
    setProjectTrainingError('');
    setProjectTrainingLoading(true);
    void refreshTrainingRuns();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      controller?.abort();
    };
  }, [projectId, tab, hasPendingLiveTraining]);

  useEffect(() => {
    if (collapsed || !canvas.current || !renderedData?.nodes?.length || tab !== 'Graph') return;
    const svg = d3.select(canvas.current); svg.selectAll('*').remove();
    if (renderedData.directed) svg.append('defs').append('marker').attr('id', arrowId).attr('viewBox', '0 -5 10 10').attr('refX', 19).attr('refY', 0).attr('markerWidth', 6).attr('markerHeight', 6).attr('orient', 'auto').append('path').attr('d', 'M0,-5L10,0L0,5').attr('fill', 'var(--graph-edge-stroke, #94a3b8)');
    const host = canvas.current.parentElement; const width = host?.clientWidth || 360; const height = host?.clientHeight || 420;
    const viewport = svg.append('g').attr('class', 'graph-viewport').attr('transform', zoomTransformRef.current.toString());
    const zoom = d3.zoom<SVGSVGElement, unknown>().extent([[0, 0], [width, height]]).scaleExtent([.25, 6]).on('zoom', event => { zoomTransformRef.current = event.transform; viewport.attr('transform', event.transform.toString()); });
    svg.call(zoom).call(zoom.transform, zoomTransformRef.current).on('dblclick.zoom', null);
    const nodes = visibleNodes.slice(0, 180).map(item => ({ ...item.node, id: item.id }));
    const nodeIds = new Set(nodes.map(node => node.id));
    const edges = (renderedData.edges || []).map(edge => ({ source: String(edge.src ?? edge.source), target: String(edge.dst ?? edge.target), weight: edge.weight })).filter(edge => nodeIds.has(edge.source) && nodeIds.has(edge.target)).slice(0, 400);
    const visibleHighlights = highlights.filter(highlight => (highlight.nodes || []).some(id => nodeIds.has(String(id))));
    const pathPairs = new Map<string, string>();
    visibleHighlights.filter(highlight => highlight.type === 'path').forEach(highlight => highlightEdgePairs(highlight).forEach(key => pathPairs.set(key, highlight.color || DEFAULT_HIGHLIGHT_COLORS.path)));
    const subgraphPairs = new Map<string, string>();
    visibleHighlights.filter(highlight => highlight.type === 'subgraph').forEach(highlight => highlightEdgePairs(highlight).forEach(key => subgraphPairs.set(key, highlight.color || DEFAULT_HIGHLIGHT_COLORS.subgraph)));
    const simulation = d3.forceSimulation(nodes as any).force('link', d3.forceLink(edges as any).id((node: any) => node.id).distance(layout === 'radial' ? 72 : 58)).force('charge', d3.forceManyBody().strength(-150)).force('center', d3.forceCenter(width / 2, height / 2)).force('collision', d3.forceCollide(15));
    if (layout === 'radial') simulation.force('radial', d3.forceRadial(Math.min(width, height) * .3, width / 2, height / 2).strength(.45));
    const hullLayer = viewport.append('g').attr('class', 'graph-subgraph-hulls');
    const subgraphHulls = visibleHighlights.filter(highlight => highlight.type === 'subgraph').map((highlight, index) => ({ highlight, path: hullLayer.append('path').attr('class', 'graph-subgraph-hull').attr('fill', highlight.color || DEFAULT_HIGHLIGHT_COLORS.subgraph).attr('stroke', highlight.color || DEFAULT_HIGHLIGHT_COLORS.subgraph).attr('aria-label', highlight.label || `子图 ${index + 1}`) }));
    const link = viewport.append('g').selectAll('line').data(edges).join('line').attr('class', 'graph-link')
      .attr('marker-end', renderedData.directed ? `url(#${arrowId})` : null)
      .attr('stroke', edge => highlightedEdgeColor(pathPairs, edge.source, edge.target) || highlightedEdgeColor(subgraphPairs, edge.source, edge.target) || null)
      .classed('is-path-highlight', edge => Boolean(highlightedEdgeColor(pathPairs, edge.source, edge.target)))
      .classed('is-subgraph-highlight', edge => !highlightedEdgeColor(pathPairs, edge.source, edge.target) && Boolean(highlightedEdgeColor(subgraphPairs, edge.source, edge.target)));
    const node = viewport.append('g').selectAll('g').data(nodes).join('g').attr('class', 'graph-node').on('click', (_, item) => onSelectRef.current(item as Record<string, unknown>)).call(d3.drag<any, any>().on('start', (event, item) => { if (!event.active) simulation.alphaTarget(.25).restart(); item.fx = item.x; item.fy = item.y; }).on('drag', (event, item) => { item.fx = event.x; item.fy = event.y; }).on('end', (event, item) => { if (!event.active) simulation.alphaTarget(0); item.fx = null; item.fy = null; }));
    if (layout === 'circle') {
      const radius = Math.max(35, Math.min(width, height) * .34);
      nodes.forEach((item: any, index) => { const angle = (index / Math.max(nodes.length, 1)) * Math.PI * 2; item.x = width / 2 + Math.cos(angle) * radius; item.y = height / 2 + Math.sin(angle) * radius; });
      simulation.stop();
    }
    node.append('circle').attr('r', (item: any) => item.id === String(selected?.id ?? '') ? 13 : 8).attr('fill', (item: any) => item.id === String(selected?.id ?? '') ? 'var(--graph-node-selected-fill)' : communityColor(communityById.get(item.id))).attr('stroke', item => highlightByNode.get(item.id)?.[0]?.color || null).classed('is-highlighted', item => highlightByNode.has(item.id));
    node.append('circle').attr('class', 'graph-node-halo').attr('r', (item: any) => highlightByNode.has(item.id) ? 13 : 0).attr('fill', item => highlightByNode.get(item.id)?.[0]?.color || DEFAULT_HIGHLIGHT_COLORS.node).attr('opacity', .2);
    node.append('text').attr('dy', 20).text((item: any) => String(item.label ?? item.id).slice(0, 12));
    const renderFrame = () => {
      if (embedded) nodes.forEach((item: any) => {
        item.x = Math.max(24, Math.min(width - 24, item.x));
        item.y = Math.max(24, Math.min(height - 30, item.y));
      });
      link.attr('x1', (edge: any) => edge.source.x).attr('y1', (edge: any) => edge.source.y).attr('x2', (edge: any) => edge.target.x).attr('y2', (edge: any) => edge.target.y);
      node.attr('transform', (item: any) => `translate(${item.x},${item.y})`);
      subgraphHulls.forEach(({ highlight, path }) => {
        const points = (highlight.nodes || []).map(id => nodes.find((item: any) => item.id === String(id))).filter(Boolean).map((item: any) => [item.x, item.y] as [number, number]);
        path.attr('d', paddedHullPath(points));
      });
    };
    simulation.on('tick', renderFrame);
    simulation.tick();
    renderFrame();
    return () => { simulation.stop(); svg.on('.zoom', null); };
  }, [collapsed, renderedData, tab, layoutVersion, layout, visibleNodes, highlights, communityById, highlightByNode, embedded, canvasBounds, fitVersion]);
  useEffect(() => {
    if (!canvas.current || collapsed || tab !== 'Graph') return;
    const selectedId = String(selected?.id ?? '');
    d3.select(canvas.current).selectAll<SVGCircleElement, Record<string, unknown>>('.graph-node > circle:not(.graph-node-halo)')
      .attr('r', item => String(item.id ?? '') === selectedId ? 13 : 8)
      .attr('fill', item => String(item.id ?? '') === selectedId ? 'var(--graph-node-selected-fill)' : communityColor(communityById.get(String(item.id ?? ''))));
  }, [selected, communityById, collapsed, tab]);
  const tabs: Tab[] = embedded ? ['Graph', 'Overview', 'Data'] : ['Overview', 'Graph', 'Data', 'ML', 'Artifacts'];
  return <aside className={`graph-workspace ${collapsed ? 'is-collapsed' : ''}`}><div className="workspace-top"><strong className="workspace-title">图工作台</strong>{onConstruct && !collapsed && <button className="workspace-construction-button" onClick={onConstruct} title={datasetName ? `${constructionLabel}：${datasetName}` : '选择数据集并构图'}><Icon name="graph" size={14} />{constructionLabel}</button>}<button className="plain-icon workspace-panel-toggle" onClick={onToggle} aria-label={collapsed ? '展开右侧栏' : '收起右侧栏'} title={collapsed ? '展开右侧栏' : '收起右侧栏'}><Icon name={collapsed ? 'panel-right-open' : 'panel-right-close'} size={21} /></button></div><div className="workspace-panel-content" aria-hidden={collapsed}><SlidingTabs ariaLabel="图工作台视图" className="workspace-tabs" indicatorClassName="workspace-tab-indicator" options={tabs.map(item => ({ value: item, label: TAB_LABELS[item] }))} value={tab} onChange={onTab} />
    {tab === 'ML' ? <MLTab hasProject={Boolean(projectId)} onStopJob={onStopJob} messages={messages} liveTools={liveTools} projectRuns={projectTrainingRuns} sessionId={sessionId} loading={projectTrainingLoading} error={projectTrainingError} /> : tab === 'Artifacts' ? <><WorkspaceFiles key={sessionId} sessionId={sessionId} revision={JSON.stringify([messages.length, ...[...messages.flatMap(message => message.tools || []), ...liveTools].filter(tool => tool.tool?.startsWith('workspace_')).map(tool => [tool.call_id, tool.done, tool.result])])} /><ArtifactsTab messages={messages} /></> : view?.status === 'unsupported' ? (tab === 'Graph' ? <div className="graph-empty graph-unsupported"><div className="empty-icon small"><Icon name="graph" size={22} /></div><strong>非独立图，无法直接可视化</strong></div> : <CollectionTab tab={tab} datasetName={datasetName} summary={view.summary} />) : view?.status === 'loading' ? <div className="graph-empty"><span className="loader" /><strong>正在加载图</strong></div> : view?.status === 'failed' ? <div className="graph-empty"><strong>图加载失败</strong><span>{view.error}</span></div> : !data ? <div className="graph-empty"><div className="empty-icon small"><BrandLogo /></div><strong>暂无图</strong><span>加载数据或发送建图请求后，这里会显示图分析结果。</span></div> : tab === 'Graph' ? <><div className="graph-toolbar"><button onClick={() => setSearchOpen(current => !current)} aria-pressed={searchOpen}><Icon name="search" size={14} /> 搜索节点</button><button onClick={() => setLayout(current => current === 'force' ? 'radial' : current === 'radial' ? 'circle' : 'force')} title="切换图布局">布局：{layoutLabel} <Icon name="chevron" size={12} /></button><button onClick={() => setConnectedOnly(current => !current)} aria-pressed={connectedOnly}>过滤{connectedOnly ? '：有连接' : ''}</button>{embedded && <button onClick={() => { zoomTransformRef.current = d3.zoomIdentity; setFitVersion(current => current + 1); }}>适应画布</button>}</div>{searchOpen ? <div className="graph-search"><Icon name="search" size={14} /><input autoFocus value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索节点 ID 或名称" /><span>{visibleNodes.length} 个结果</span></div> : null}<div className="graph-canvas"><svg ref={canvas} aria-label="图谱画布，可拖动平移并使用滚轮缩放" /></div>{(communityById.size || highlights.length) ? <div className="graph-legend"><span className="graph-legend-title">图例</span>{communityById.size ? <span><i className="legend-dot community" />社区着色</span> : null}{highlights.filter(item => item.type === 'node').length ? <span><i className="legend-dot node-highlight" />节点高亮</span> : null}{highlights.filter(item => item.type === 'path').length ? <span><i className="legend-line path-highlight" />路径高亮</span> : null}{highlights.filter(item => item.type === 'subgraph').length ? <span><i className="legend-line subgraph-highlight" />子图高亮</span> : null}</div> : null}<div className="node-inspector">{selected ? <><div className="inspector-title"><strong>{String(selected.label ?? selected.id ?? '已选节点')}</strong><span>已选择</span></div><div className="inspector-row"><span>节点 ID</span><strong>{String(selected.id ?? '-')}</strong></div><div className="inspector-row"><span>类型</span><strong>{String(selected.type ?? selected.category ?? '节点')}</strong></div><div className="inspector-row"><span>度数</span><strong>{degreeById.get(String(selected.id ?? '')) ?? '-'}</strong></div><div className="inspector-actions"><button onClick={() => onExpand?.(selected)} disabled={!onExpand}><Icon name="plus" size={13} /> 扩展邻居</button><button onClick={() => navigator.clipboard?.writeText(String(selected.id ?? ''))}><Icon name="copy" size={13} /> 复制 ID</button></div></> : <span>点击图中节点查看属性</span>}</div></> : <WorkspaceTab tab={tab} data={data} messages={messages} />}
  </div></aside>;
}, graphWorkspacePropsEqual);

function displayCount(value: unknown) {
  const count = Number(value);
  return Number.isFinite(count) ? count.toLocaleString() : '-';
}

function CollectionTab({ tab, datasetName, summary }: { tab: Tab; datasetName?: string; summary: GraphCollectionSummary }) {
  const splits = summary.split_sizes || {};
  const validationSize = splits.val ?? splits.valid;
  if (tab === 'Overview') return <div className="overview-content"><div className="overview-hero"><span className="eyebrow">图集合</span><h2>{datasetName || '当前数据集'}</h2><strong>多图分类数据集已就绪</strong></div><div className="stat-grid"><div><span>图数量</span><strong>{displayCount(summary.num_graphs)}</strong></div><div><span>任务数</span><strong>{displayCount(summary.num_tasks)}</strong></div><div><span>评估指标</span><strong>{summary.metric || '-'}</strong></div><div><span>数据划分</span><strong>{Object.keys(splits).length || '-'}</strong></div></div><div className="workspace-note"><Icon name="spark" size={15} /> 该数据集包含多张独立样本图，可继续用于图分类训练和推理。</div></div>;
  const missingRate = Number(summary.missing_label_rate);
  return <div className="tab-content collection-data"><div className="tab-title"><strong>数据集划分与特征</strong><span>{displayCount(summary.num_graphs)} 张图</span></div><div className="collection-section"><strong>数据划分</strong><div className="stat-grid"><div><span>训练集</span><strong>{displayCount(splits.train)}</strong></div><div><span>验证集</span><strong>{displayCount(validationSize)}</strong></div><div><span>测试集</span><strong>{displayCount(splits.test)}</strong></div><div><span>无边图</span><strong>{displayCount(summary.num_edgeless_graphs)}</strong></div></div></div><div className="collection-section"><strong>词表与标签</strong><div className="stat-grid"><div><span>节点词表</span><strong>{displayCount(summary.node_vocab_size)}</strong></div><div><span>边词表</span><strong>{displayCount(summary.edge_vocab_size)}</strong></div><div><span>缺失标签</span><strong>{Number.isFinite(missingRate) ? `${(missingRate * 100).toFixed(2)}%` : '-'}</strong></div><div><span>边特征</span><strong>{summary.uses_edge_features == null ? '-' : summary.uses_edge_features ? '是' : '否'}</strong></div></div></div></div>;
}

function WorkspaceTab({ tab, data, messages }: { tab: Tab; data: GraphData; messages: Message[] }) {
  if (tab === 'Overview') {
    const totalNodes = data.total_nodes ?? data.num_nodes ?? data.nodes.length;
    const totalEdges = data.total_edges ?? data.num_edges ?? data.edges.length;
    const renderedNodes = data.truncated_to ?? data.nodes.length;
    return <div className="overview-content"><div className="overview-hero"><span className="eyebrow">当前图</span><h2>当前数据集</h2><strong>图分析已就绪</strong></div><div className="stat-grid"><div><span>节点数</span><strong>{totalNodes.toLocaleString()}</strong></div><div><span>边数</span><strong>{totalEdges.toLocaleString()}</strong></div><div><span>方向</span><strong>{data.directed ? '有向图' : '无向图'}</strong></div><div><span>画布节点</span><strong>{renderedNodes.toLocaleString()}</strong></div></div>{data.truncated ? <div className="workspace-note"><Icon name="spark" size={15} /> 画布当前展示 {renderedNodes.toLocaleString()} 个节点，数据集总量统计保持完整。</div> : <div className="workspace-note"><Icon name="spark" size={15} /> 从中央对话发起图算法，结果会同步到这里。</div>}</div>;
  }
  if (tab === 'Data') return <div className="tab-content"><div className="tab-title"><strong>节点与边数据</strong><span>{(data.total_nodes ?? data.num_nodes ?? data.nodes.length).toLocaleString()} 个节点</span></div><div className="data-list">{data.nodes.slice(0, 20).map((node, index) => <div key={index}><Icon name="project" size={14} /><span>{String(node.label ?? node.id ?? `节点 ${index + 1}`)}</span><small>{String(node.type ?? '节点')}</small></div>)}</div>{data.nodes.length < (data.total_nodes ?? data.num_nodes ?? data.nodes.length) ? <div className="workspace-note">当前为可视化预览，已加载 {data.nodes.length.toLocaleString()} 个节点样本。</div> : null}</div>;
  return <ArtifactsTab messages={messages} />;
}

const ML_TOOLS = new Set(['train', 'infer', 'load_graph_classification_dataset']);
const METRIC_LABELS: Record<string, string> = {
  loss: '损失', train_loss: '训练损失', val_loss: '验证损失', test_loss: '测试损失',
  train_acc: '训练准确率', val_acc: '验证准确率', test_acc: '测试准确率', accuracy: '准确率',
  train_auc: '训练 AUC', val_auc: '验证 AUC', test_auc: '测试 AUC', auc: 'AUC',
  train_f1: '训练 F1', val_f1: '验证 F1', test_f1: '测试 F1', f1: 'F1', macro_f1: '宏平均 F1', micro_f1: '微平均 F1',
  precision: '精确率', recall: '召回率', learning_rate: '学习率',
  best_epoch: '最佳轮次', trained_epochs: '训练轮次', num_classes: '类别数',
  num_train: '训练样本数', num_val: '验证样本数', num_test: '测试样本数',
};
const CHART_COLORS = ['var(--accent, #2563eb)', '#d97745', '#0891b2', '#bd4f6c', '#7b6cb7'];

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

function trainingRunIdentity(run: ToolCall) {
  const result = recordValue(run.result);
  return String((run as ProjectTrainingRun).training_run_id || run.call_id || result.job_id || '');
}

function trainingRunEpoch(run: ToolCall) {
  const result = recordValue(run.result);
  const metrics = recordValue(result.metrics);
  const progress = recordValue(run.progress);
  const history = runHistory(run);
  return Number(metrics.trained_epochs || progress.epoch || history[history.length - 1]?.epoch || 0);
}

function mergeTrainingRuns(localRuns: ToolCall[], projectRuns: ProjectTrainingRun[]) {
  const merged = [...localRuns];
  projectRuns.forEach(projectRun => {
    const identity = trainingRunIdentity(projectRun);
    const index = identity ? merged.findIndex(run => trainingRunIdentity(run) === identity) : -1;
    if (index < 0) {
      merged.push(projectRun);
      return;
    }
    const local = merged[index];
    const preferred = projectRun.done || trainingRunEpoch(projectRun) >= trainingRunEpoch(local) ? projectRun : local;
    const fallback = preferred === projectRun ? local : projectRun;
    merged[index] = {
      ...fallback,
      ...preferred,
      done: Boolean(local.done || projectRun.done),
      result: { ...recordValue(fallback.result), ...recordValue(preferred.result) },
      progress: { ...recordValue(fallback.progress), ...recordValue(preferred.progress) },
      history: runHistory({ history: [...runHistory(local), ...runHistory(projectRun)] }),
    };
  });
  return merged;
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
    <text x={left} y={height - 7}>第 {minX} 轮</text><text x={width - right} y={height - 7} textAnchor="end">第 {maxX} 轮</text>
    {series.map((item, index) => { const points = item.values.map(point => `${x(point.epoch)},${y(point.value)}`).join(' '); return <g key={item.key}><polyline points={points} style={{ stroke: CHART_COLORS[index % CHART_COLORS.length] }} />{item.values.map(point => <circle key={point.epoch} cx={x(point.epoch)} cy={y(point.value)} r={item.values.length < 25 ? 2.4 : 1.4} style={{ fill: CHART_COLORS[index % CHART_COLORS.length] }} />)}</g>; })}
  </svg></div>;
}

function MetricsTable({ rows }: { rows: Array<Record<string, unknown>> }) {
  const keys = availableMetricKeys(rows).slice(0, 5);
  if (!rows.length) return <div className="ml-no-history">该运行没有可用的逐轮指标。</div>;
  return <div className="ml-table-wrap"><table className="ml-table"><thead><tr><th>轮次</th>{keys.map(key => <th key={key}>{metricLabel(key)}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={`${row.epoch}-${index}`}><td>{String(row.epoch)}</td>{keys.map(key => <td key={key}>{formatMetric(key, row[key])}</td>)}</tr>)}</tbody></table></div>;
}

function FinalMetricsTable({ metrics }: { metrics: Record<string, unknown> }) {
  const rows = [
    { label: '训练集', prefix: 'train' },
    { label: '验证集', prefix: 'val' },
    { label: '测试集', prefix: 'test' },
  ].filter(item => ['acc', 'loss'].some(key => metricNumber(metrics[`${item.prefix}_${key}`]) !== null) || metricNumber(metrics[`num_${item.prefix}`]) !== null);
  if (!rows.length) return <div className="ml-no-history">该运行没有可用的逐轮指标。</div>;
  return <div className="ml-table-wrap"><table className="ml-table"><thead><tr><th>数据划分</th><th>准确率</th><th>损失</th><th>样本数</th></tr></thead><tbody>{rows.map(row => <tr key={row.prefix}><td>{row.label}</td><td>{formatMetric(`${row.prefix}_acc`, metrics[`${row.prefix}_acc`])}</td><td>{formatMetric(`${row.prefix}_loss`, metrics[`${row.prefix}_loss`])}</td><td>{formatMetric(`num_${row.prefix}`, metrics[`num_${row.prefix}`])}</td></tr>)}</tbody></table></div>;
}

function JobStopButton({ run, sessionId, onStopJob }: { run: ToolCall; sessionId: string; onStopJob?: StopJob }) {
  const [status, setStatus] = useState('');
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const result = recordValue(run.result);
  const jobId = String(result.job_id || recordValue(run.progress).job_id || '');
  const terminal = ['done', 'completed', 'cancelled', 'failed'].includes(String(result.job_status || result.status || ''));
  if (!onStopJob || !jobId || !sessionId || terminal || status === 'confirmed') return status === 'confirmed' ? <span role="status">作业已结束</span> : null;
  const pending = status === 'stopping';
  return <button type="button" disabled={pending} aria-label="终止作业" onClick={() => onStopJob(sessionId, jobId, next => { if (mounted.current) setStatus(next); })}>
    {pending ? '正在停止…' : status === 'unconfirmed' ? '停止尚未确认，重试' : '终止作业'}
  </button>;
}

function MLRunCard({ run, index, sessionId, onStopJob }: { run: ToolCall; index: number; sessionId: string; onStopJob?: StopJob }) {
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
  const task = String(context.task || '图机器学习');
  const model = String(context.display_model || context.model || context.method || '模型训练');
  const completedEpochs = Number(metrics.trained_epochs || progress.epoch || history[history.length - 1]?.epoch || 0);
  const totalEpochs = Number(progress.epochs || recordValue(args.train_config).epochs || recordValue(args.algo_config).epochs || completedEpochs || 0);
  const projectRun = run as ProjectTrainingRun;
  const source = [projectRun.session_title, projectRun.dataset_name].filter(Boolean).join(' · ');
  const jobStatus = String(result.job_status || result.status || progress.status || '');
  const statusLabel = projectRun.status_unavailable ? '状态不可用' : jobStatus === 'cancelled' ? '已终止' : jobStatus === 'cancelling' ? '正在停止' : jobStatus === 'failed' ? '失败' : run.done ? (metrics.stopped_early ? '提前停止' : '已完成') : '训练中';
  return <article className="ml-run-card"><header className="ml-run-header"><div className="workspace-list-icon"><Icon name="cube" size={16} /></div><div><strong>{taskNames[task] || task}</strong><span>{model}{totalEpochs ? ` · ${completedEpochs}/${totalEpochs} 轮` : ''}{source ? ` · ${source}` : ''}</span></div><b className={projectRun.status_unavailable ? 'unavailable' : run.done ? 'done' : 'running'}>{statusLabel}</b></header>
    <JobStopButton run={run} sessionId={projectRun.session_id || sessionId} onStopJob={onStopJob} />
    {summaryKeys.length ? <div className="ml-metric-grid">{summaryKeys.map(key => <div key={key}><span>{metricLabel(key)}</span><strong>{formatMetric(key, metrics[key])}</strong></div>)}</div> : null}
    <div className="ml-view-bar"><span>{history.length ? `${history.length} 个训练点` : `运行 ${index + 1}`}</span><SlidingTabs ariaLabel="训练结果视图" className="ml-view-toggle" indicatorClassName="ml-view-indicator" options={[{ value: 'chart', label: '曲线', disabled: !history.length }, { value: 'table', label: '表格' }]} value={view} onChange={setView} /></div>
    {view === 'chart' ? <div className="ml-charts"><MetricChart rows={history} keys={lossKeys} title="损失" /><MetricChart rows={history} keys={scoreKeys} title="评估指标" />{!lossKeys.length && !scoreKeys.length ? <FinalMetricsTable metrics={metrics} /> : null}</div> : history.length ? <MetricsTable rows={history} /> : <FinalMetricsTable metrics={metrics} />}
  </article>;
}

function ProjectTrainingComparison({ runs }: { runs: ProjectTrainingRun[] }) {
  return <div className="ml-comparison-wrap"><table className="ml-comparison"><thead><tr><th>模型</th><th>会话</th><th>数据集</th><th>状态</th><th>轮次</th><th>验证准确率</th><th>测试准确率</th><th>最佳轮次</th></tr></thead><tbody>{runs.map((run, index) => {
    const context = { ...recordValue(run.args), ...recordValue(run.result), ...recordValue(run.progress) };
    const metrics = recordValue(recordValue(run.result).metrics);
    const model = String(context.display_model || context.model || context.method || '模型训练');
    const statusLabel = run.status_unavailable ? '状态不可用' : run.done ? '已完成' : '训练中';
    return <tr key={run.training_run_id || `${run.session_id}-${index}`}><td>{model}</td><td title={run.session_title}>{run.session_title || '新会话'}</td><td>{run.dataset_name || '-'}</td><td><span className={run.status_unavailable ? 'ml-status-unavailable' : run.done ? 'ml-status-done' : 'ml-status-running'}>{statusLabel}</span></td><td>{trainingRunEpoch(run) || '-'}</td><td>{formatMetric('val_acc', metrics.val_acc)}</td><td>{formatMetric('test_acc', metrics.test_acc)}</td><td>{formatMetric('best_epoch', metrics.best_epoch)}</td></tr>;
  })}</tbody></table></div>;
}

function MLTab({ hasProject, messages, liveTools, projectRuns, sessionId, loading, error, onStopJob }: { hasProject: boolean; messages: Message[]; liveTools: ToolCall[]; projectRuns: ProjectTrainingRun[]; sessionId: string; loading: boolean; error: string; onStopJob?: StopJob }) {
  const [scope, setScope] = useState<'current' | 'project'>('current');
  const runs = useMemo(() => mergeMlRuns(messages, liveTools), [messages, liveTools]);
  const localTrainingRuns = runs.filter(run => String(run.tool || '') === 'train');
  const currentProjectRuns = projectRuns.filter(run => run.session_id === sessionId);
  const currentTrainingRuns = useMemo(() => mergeTrainingRuns(localTrainingRuns, currentProjectRuns), [localTrainingRuns, currentProjectRuns]);
  const otherRuns = runs.filter(run => String(run.tool || '') !== 'train');
  const effectiveScope = !hasProject ? 'current' : scope === 'current' && !currentTrainingRuns.length && projectRuns.length ? 'project' : scope;
  const visibleTrainingRuns = effectiveScope === 'project' ? projectRuns : currentTrainingRuns;
  if (!runs.length && !projectRuns.length && !loading) return <div className="tab-placeholder"><Icon name="cube" size={22} /><strong>图机器学习</strong><span>{hasProject ? '当前项目还没有训练或推理记录。' : '当前会话还没有训练或推理记录。'}</span></div>;
  return <div className="tab-content ml-tab"><div className="ml-tab-heading"><div className="tab-title"><strong>图机器学习训练结果</strong><span>{effectiveScope === 'project' ? `${projectRuns.length} 次项目训练` : `${currentTrainingRuns.length} 次当前训练`}</span></div>{hasProject && <SlidingTabs ariaLabel="训练结果范围" className="ml-scope-toggle" indicatorClassName="ml-scope-indicator" options={[{ value: 'current', label: '当前会话' }, { value: 'project', label: '项目对比' }]} value={effectiveScope} onChange={setScope} />}</div>
    {loading ? <div className="ml-sync-status"><span className="loader" /> 正在同步项目训练结果</div> : null}
    {error ? <div className="ml-sync-error">{error}</div> : null}
    {effectiveScope === 'project' && visibleTrainingRuns.length ? <ProjectTrainingComparison runs={projectRuns} /> : null}
    {!visibleTrainingRuns.length && !loading ? <div className="ml-no-runs">{hasProject ? '该会话还没有训练记录，可切换到“项目对比”查看其他会话。' : '该会话还没有训练记录。'}</div> : visibleTrainingRuns.map((run, index) => <MLRunCard onStopJob={onStopJob} sessionId={sessionId} key={`${trainingRunIdentity(run) || 'train'}-${index}`} run={run} index={index} />)}
    {effectiveScope === 'current' && otherRuns.length ? <div className="ml-activity"><strong>数据与推理记录</strong><div className="workspace-list">{otherRuns.map((run, index) => <div className="workspace-list-row" key={`${run.call_id || run.tool}-${index}`}><div className="workspace-list-icon"><Icon name="cube" size={15} /></div><div><strong>{String(run.tool) === 'infer' ? '推理' : String(run.tool) === 'load_graph_classification_dataset' ? '加载图分类数据集' : String(run.tool)}</strong><span>{run.done ? '已完成' : '运行中'}</span></div><b>{run.done ? '完成' : '运行中'}</b><JobStopButton run={run} sessionId={sessionId} onStopJob={onStopJob} /></div>)}</div></div> : null}</div>;
}

function ArtifactsTab({ messages }: { messages: Message[] }) {
  const reports = messages.flatMap(message => message.reports || []);
  if (!reports.length) return <div className="tab-placeholder"><Icon name="file" size={22} /><strong>分析产物</strong><span>当前会话还没有分析报告或导出产物。</span></div>;
  return <div className="tab-content"><div className="tab-title"><strong>分析产物</strong><span>{reports.length} 份报告</span></div><div className="workspace-list">{reports.map((report, index) => { const type = String(report.intent || report.type || '分析'); return <div className="workspace-list-row" key={`${String(report.id || report.title || 'report')}-${index}`}><div className="workspace-list-icon"><Icon name="file" size={15} /></div><div><strong>{String(report.title || report.name || `分析报告 ${index + 1}`)}</strong><span>{type === 'analysis' ? '分析' : type}</span></div></div>; })}</div></div>;
}

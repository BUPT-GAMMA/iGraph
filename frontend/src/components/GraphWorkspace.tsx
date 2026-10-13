import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { createGraphCanvasState, GraphCanvas } from './GraphCanvas';
import { MoleculeCanvas } from './MoleculeCanvas';
import { GraphCoverage } from './GraphCoverage';
import { graphIndex, previewGraph } from '../graphPreview';
import { graphNodeLabel, financeNodeTypeLabel } from '../financeNodeLabels';
import { normalizeHighlights } from '../graphAppearance';
import { executionError, executionLabel, executionPending, executionStatus } from '../toolStatus';
import { metricNumber, metricSegments } from '../mlMetrics';
import { reportLabel } from '../reportLabels';
import { listProjectTrainingRuns, visualizeGraph } from '../api';
import type { GraphCollectionSummary, GraphData, GraphViewState, Highlight, Message, PartitionedGraphSummary, ProjectTrainingRun, ToolCall } from '../types';
import { BrandLogo, Icon } from './Icon';
import { SlidingTabs } from './SlidingTabs';
import { WorkspaceFiles } from './WorkspaceFiles';
import { GraphExplorePanel } from './GraphExplorePanel';
import { NodeProperties, nodePropertyType } from './NodeProperties';
import { useI18n } from '../i18n';
import type { Language, Translate } from '../i18n';

type Tab = 'Overview' | 'Graph' | 'Data' | 'ML' | 'Artifacts';
type LayoutMode = 'force' | 'radial' | 'circle';
type StopJob = (sessionId: string, jobId: string, notify: (status: string) => void) => void;

const TAB_LABELS: Record<Tab, readonly [string, string]> = {
  Overview: ['Overview', '概览'],
  Graph: ['Graph', '图谱'],
  Data: ['Data', '数据'],
  ML: ['Machine learning', '机器学习'],
  Artifacts: ['Artifacts', '分析产物'],
};
const PROJECT_TRAINING_SYNC_ERROR = '__project_training_sync_failed__';
const localeFor = (language: Language) => language === 'zh' ? 'zh-CN' : 'en-US';

type GraphWorkspaceProps = {
  data: GraphData | null;
  view?: GraphViewState;
  datasetName?: string;
  datasetId?: string;
  messages?: Message[];
  liveTools?: ToolCall[];
  projectId?: string | null;
  sessionId?: string;
  tab: Tab;
  onTab: (tab: Tab) => void;
  selected: Record<string, unknown> | null;
  onSelect: (node: Record<string, unknown> | null) => void;
  collapsed: boolean;
  onToggle: () => void;
  onExpand?: (node: Record<string, unknown>) => void;
  onStopJob?: StopJob;
  embedded?: boolean;
  onConstruct?: () => void;
  constructionLabel?: string;
  onGraphQuery?: (prompt: string) => void;
  onGraphReset?: () => void;
  graphQueryPending?: boolean;
  molecularCase?: boolean;
};

function graphWorkspacePropsEqual(previous: GraphWorkspaceProps, next: GraphWorkspaceProps) {
  const coreEqual = previous.data === next.data
    && previous.view === next.view
    && previous.tab === next.tab
    && previous.selected === next.selected
    && previous.collapsed === next.collapsed
    && previous.projectId === next.projectId
    && previous.onSelect === next.onSelect
    && previous.onExpand === next.onExpand
    && previous.onTab === next.onTab
    && previous.onToggle === next.onToggle
    && previous.sessionId === next.sessionId
    && previous.datasetId === next.datasetId
    && previous.onGraphQuery === next.onGraphQuery
    && previous.onGraphReset === next.onGraphReset
    && previous.graphQueryPending === next.graphQueryPending
    && previous.molecularCase === next.molecularCase
    && previous.onStopJob === next.onStopJob;
  if (previous.embedded !== next.embedded || previous.onConstruct !== next.onConstruct || previous.constructionLabel !== next.constructionLabel || previous.datasetName !== next.datasetName) return false;
  if (!coreEqual) return false;
  if (next.tab === 'ML' || next.tab === 'Artifacts') return previous.messages === next.messages && previous.liveTools === next.liveTools;
  return true;
}

function sameRecord(left: Record<string, unknown>, right: Record<string, unknown>) {
  if (left === right) return true;
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  return leftKeys.length === rightKeys.length
    && leftKeys.every(key => Object.prototype.hasOwnProperty.call(right, key) && left[key] === right[key]);
}

function sameWorkspaceGraph(left: GraphData | null, right: GraphData | null) {
  if (left === right) return true;
  if (!left || !right || left.nodes.length !== right.nodes.length || left.edges.length !== right.edges.length) return false;
  const leftMetadata = Object.keys(left).filter(key => key !== 'nodes' && key !== 'edges');
  const rightMetadata = Object.keys(right).filter(key => key !== 'nodes' && key !== 'edges');
  return leftMetadata.length === rightMetadata.length
    && leftMetadata.every(key => Object.prototype.hasOwnProperty.call(right, key)
      && left[key as keyof GraphData] === right[key as keyof GraphData])
    && left.nodes.every((node, index) => sameRecord(node, right.nodes[index]))
    && left.edges.every((edge, index) => sameRecord(edge, right.edges[index]));
}

export const GraphWorkspace = memo(function GraphWorkspace({ data, view, datasetName, datasetId, messages = [], liveTools = [], projectId = '', sessionId = '', tab, onTab, selected, onSelect, collapsed, onToggle, onExpand, onStopJob, embedded, onConstruct, constructionLabel, onGraphQuery, onGraphReset, graphQueryPending = false, molecularCase = false }: GraphWorkspaceProps) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
  const resolvedConstructionLabel = constructionLabel || t('Build graph', '构图');
  const stableDataRef = useRef<GraphData | null>(data);
  if (!sameWorkspaceGraph(stableDataRef.current, data)) stableDataRef.current = data;
  const renderedData = stableDataRef.current;
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [layout, setLayout] = useState<LayoutMode>('force');
  const [connectedOnly, setConnectedOnly] = useState(false);
  const [displayOpen, setDisplayOpen] = useState(false);
  const [sizeMode, setSizeMode] = useState<'degree' | 'uniform'>('degree');
  const [highlightShape, setHighlightShape] = useState<'star' | 'triangle'>('star');
  const [labels, setLabels] = useState<'auto' | 'all' | 'hidden'>('auto');
  const [edgeWidth, setEdgeWidth] = useState(1);
  const [filterMatches, setFilterMatches] = useState(false);
  const [focusId, setFocusId] = useState('');
  const [depth, setDepth] = useState(0);
  const [fitVersion, setFitVersion] = useState(0);
  const [exploreOpen, setExploreOpen] = useState(false);
  const [resultsOnly, setResultsOnly] = useState(false);
  const [resultIndex, setResultIndex] = useState(-1);
  const [selectedDetail, setSelectedDetail] = useState<Record<string, unknown> | null>(null);
  const [projectTrainingRuns, setProjectTrainingRuns] = useState<ProjectTrainingRun[]>([]);
  const [projectTrainingLoading, setProjectTrainingLoading] = useState(false);
  const [projectTrainingError, setProjectTrainingError] = useState('');
  const hasPendingLiveTraining = liveTools.some(tool => String(tool.tool || '') === 'train' && !tool.done);
  const datasetKey = datasetId || datasetName || '';
  const canvasIdentity = `${sessionId}\u0000${datasetKey}`;
  const canvasMemory = useRef({ identity: canvasIdentity, state: createGraphCanvasState() });
  if (canvasMemory.current.identity !== canvasIdentity) canvasMemory.current = { identity: canvasIdentity, state: createGraphCanvasState() };
  const layoutLabel = layout === 'force' ? t('Force-directed', '力导向') : layout === 'radial' ? t('Radial', '径向') : t('Circular', '环形');
  const communityById = useMemo(() => {
    const raw = renderedData?.community || renderedData?.communities;
    const mapping = raw?.node_community;
    return mapping && typeof mapping === 'object' ? new Map(Object.entries(mapping).map(([id, value]) => [String(id), String(value)])) : new Map<string, string>();
  }, [renderedData]);
  const allHighlights = useMemo(() => normalizeHighlights(renderedData?.highlights, renderedData?.edges), [renderedData]);
  const highlights = useMemo(() => resultIndex >= 0 && allHighlights[resultIndex] ? [allHighlights[resultIndex]] : allHighlights, [allHighlights, resultIndex]);
  const index = useMemo(() => graphIndex(renderedData || { nodes: [], edges: [] }, t), [renderedData, t]);
  const preview = useMemo(() => previewGraph(index, highlights, query, filterMatches, connectedOnly, focusId, depth, resultsOnly),
    [index, highlights, query, filterMatches, connectedOnly, focusId, depth, resultsOnly]);
  useEffect(() => {
    const id = String(selected?.id ?? '');
    if (id && index.nodes.has(id) && !preview.nodes.some(node => node.id === id) && !filterMatches && !depth) setFocusId(id);
  }, [selected, index, preview.nodes, filterMatches, depth]);
  const schemaMode = renderedData?.visualization_kind === 'schema';
  const showMolecule = molecularCase && !schemaMode && renderedData?.schema_kind !== 'tables';
  const selectedId = String(selected?.id ?? '');
  const selectedInfo = selected ? index.nodes.get(String(selected.id)) : undefined;
  const detailedSelected = selectedDetail && String(selectedDetail.id ?? selectedDetail.label ?? '') === selectedId
    ? selectedDetail
    : undefined;
  useEffect(() => {
    setSelectedDetail(null);
    if (!selectedId || !sessionId || schemaMode || showMolecule) return;
    const controller = new AbortController();
    void visualizeGraph(sessionId, { node_names: [selectedId], include_community: false, max_nodes: 1 }, controller.signal)
      .then(result => {
        const detail = result.nodes.find(node => String(node.id ?? node.label ?? '') === selectedId);
        if (detail) setSelectedDetail(detail);
      })
      .catch(cause => {
        if (!(cause && typeof cause === 'object' && 'name' in cause && cause.name === 'AbortError')) setSelectedDetail(null);
      });
    return () => controller.abort();
  }, [datasetKey, schemaMode, selectedId, sessionId, showMolecule]);
  const locate = (id: string) => { setFocusId(id); const node = index.nodes.get(id); if (node) onSelect(node); };
  const changeTab = (nextTab: Tab) => {
    if (tab === 'Graph' && nextTab !== 'Graph') {
      setFocusId('');
      setDepth(0);
      onSelect(null);
    }
    onTab(nextTab);
  };
  useEffect(() => { setQuery(''); setFocusId(''); setDepth(0); setFilterMatches(false); setResultsOnly(false); setResultIndex(-1); setExploreOpen(false); }, [sessionId, datasetKey]);
  useEffect(() => { setResultIndex(-1); if (!allHighlights.length) setResultsOnly(false); }, [allHighlights]);
  const resetGraphFilters = () => { setQuery(''); setFocusId(''); setDepth(0); setFilterMatches(false); setConnectedOnly(false); setResultsOnly(false); setResultIndex(-1); onSelect(null); setFitVersion(current => current + 1); };

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
        setProjectTrainingError(cause instanceof Error ? cause.message : PROJECT_TRAINING_SYNC_ERROR);
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

  const tablesMode = renderedData?.schema_kind === 'tables';
  const tabs: Tab[] = tablesMode ? ['Graph'] : embedded ? ['Graph', 'Overview', 'Data'] : ['Overview', 'Graph', 'Data', 'ML', 'Artifacts'];
  const trainingError = projectTrainingError === PROJECT_TRAINING_SYNC_ERROR
    ? t('Failed to sync training results', '训练结果同步失败')
    : projectTrainingError;
  const panelToggleLabel = collapsed ? t('Expand right sidebar', '展开右侧栏') : t('Collapse right sidebar', '收起右侧栏');
  return <aside className={`graph-workspace ${collapsed ? 'is-collapsed' : ''}`}>
    <div className="workspace-top">
      <strong className="workspace-title">{t('Graph workspace', '图工作台')}</strong>
      {onConstruct && !collapsed && <button className="workspace-construction-button" onClick={onConstruct} title={datasetName
        ? t(`${resolvedConstructionLabel}: ${datasetName}`, `${resolvedConstructionLabel}：${datasetName}`)
        : t('Select a dataset and build a graph', '选择数据集并构图')}><Icon name="graph" size={14} />{resolvedConstructionLabel}</button>}
      <button className="plain-icon workspace-panel-toggle" onClick={onToggle} aria-label={panelToggleLabel} title={panelToggleLabel}><Icon name={collapsed ? 'panel-right-open' : 'panel-right-close'} size={21} /></button>
    </div>
    <div className="workspace-panel-content" aria-hidden={collapsed}>
      {!tablesMode && <SlidingTabs ariaLabel={t('Graph workspace views', '图工作台视图')} className="workspace-tabs" indicatorClassName="workspace-tab-indicator" options={tabs.map(item => ({ value: item, label: t(TAB_LABELS[item][0], TAB_LABELS[item][1]) }))} value={tab} onChange={changeTab} />}
      {tab === 'ML' ? <MLTab hasProject={Boolean(projectId)} onStopJob={onStopJob} messages={messages} liveTools={liveTools} projectRuns={projectTrainingRuns} sessionId={sessionId} loading={projectTrainingLoading} error={trainingError} />
        : tab === 'Artifacts' ? <><WorkspaceFiles key={sessionId} sessionId={sessionId} revision={JSON.stringify([messages.length, ...[...messages.flatMap(message => message.tools || []), ...liveTools].filter(tool => tool.tool?.startsWith('workspace_')).map(tool => [tool.call_id, tool.done, tool.result])])} /><ArtifactsTab messages={messages} /></>
          : view?.status === 'unsupported' ? (tab === 'Graph'
            ? <div className="graph-empty graph-unsupported"><div className="empty-icon small"><Icon name="graph" size={22} /></div><strong>{view.code === 'PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE' ? t('Partitioned large graphs cannot be rendered on the full canvas', '大图分区快照不支持全量画布渲染') : t('This graph collection cannot be visualized directly', '非独立图，无法直接可视化')}</strong>{view.code === 'PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE' ? <span>{t('Run a large-graph algorithm from the conversation and inspect bounded results under Artifacts.', '请从中央对话运行大图算法，并在分析产物中查看有界结果。')}</span> : null}</div>
            : view.code === 'PARTITIONED_GRAPH_NOT_DIRECTLY_VISUALIZABLE'
              ? <PartitionedGraphTab tab={tab} datasetName={datasetName} summary={view.summary} />
              : <CollectionTab tab={tab} datasetName={datasetName} summary={view.summary} />)
            : view?.status === 'loading' ? <div className="graph-empty"><span className="loader" /><strong>{t('Loading graph', '正在加载图')}</strong></div>
              : view?.status === 'failed' ? <div className="graph-empty"><strong>{t('Failed to load graph', '图加载失败')}</strong><span>{view.error}</span></div>
                : !data ? <div className="graph-empty"><div className="empty-icon small"><BrandLogo /></div><strong>{t('No graph yet', '暂无图')}</strong><span>{t('Load data or request a graph to see analysis results here.', '加载数据或发送建图请求后，这里会显示图分析结果。')}</span></div>
                  : tab === 'Graph' ? <>
                    {showMolecule ? <MoleculeCanvas sessionId={sessionId} datasetKey={datasetKey} highlights={allHighlights} selected={selected} onSelect={onSelect} /> : <>
                    <div className="graph-toolbar">
                      <button onClick={() => setSearchOpen(current => !current)} aria-pressed={searchOpen}><Icon name="search" size={14} /> {t('Search nodes', '搜索节点')}</button>
                      {onGraphQuery && <button onClick={() => setExploreOpen(current => !current)} aria-expanded={exploreOpen}><Icon name="spark" size={14} />{t('Explore graph', '图探索')}</button>}
                      <button onClick={() => setLayout(current => current === 'force' ? 'radial' : current === 'radial' ? 'circle' : 'force')} title={t('Change graph layout', '切换图布局')}>{t(`Layout: ${layoutLabel}`, `布局：${layoutLabel}`)} <Icon name="chevron" size={12} /></button>
                      <button onClick={() => setConnectedOnly(current => !current)} aria-pressed={connectedOnly}>{connectedOnly ? t('Filter: connected', '过滤：有连接') : t('Filter', '过滤')}</button>
                      <button onClick={() => setFitVersion(current => current + 1)}>{t('Fit to canvas', '适应画布')}</button>
                      <button aria-expanded={displayOpen} onClick={() => setDisplayOpen(value => !value)}>{t('Display settings', '显示设置')}</button>
                      {onGraphReset && <button disabled={graphQueryPending} onClick={() => { resetGraphFilters(); onGraphReset(); }}>{t('Restore full graph', '恢复全图')}</button>}
                    </div>
                    {exploreOpen && onGraphQuery && <GraphExplorePanel key={`${sessionId}:${datasetName || ''}`} schema={schemaMode} selectedId={String(selected?.id ?? '')} pending={graphQueryPending} onQuery={prompt => { resetGraphFilters(); onGraphQuery(prompt); }} />}
                    {displayOpen && <div className="graph-display-settings">
                      <label>{t('Node size', '节点大小')}<select value={sizeMode} onChange={e => setSizeMode(e.target.value as typeof sizeMode)}><option value="degree">{t('By degree', '按度数')}</option><option value="uniform">{t('Uniform', '统一大小')}</option></select></label>
                      <label>{t('Highlight shape', '高亮形状')}<select value={highlightShape} onChange={e => setHighlightShape(e.target.value as typeof highlightShape)}><option value="star">{t('Star', '星形')}</option><option value="triangle">{t('Triangle', '三角形')}</option></select></label>
                      <label>{t('Node labels', '节点标签')}<select value={labels} onChange={e => setLabels(e.target.value as typeof labels)}><option value="auto">{t('Automatic', '自动')}</option><option value="all">{t('All', '全部')}</option><option value="hidden">{t('Hidden', '隐藏')}</option></select></label>
                      <label>{t('Edge width', '边粗细')}<input aria-label={t('Edge width', '边粗细')} type="range" min="0.6" max="2" step="0.2" value={edgeWidth} onChange={e => setEdgeWidth(Number(e.target.value))} /></label>
                      {!schemaMode && allHighlights.length > 1 && <label>{t('Result layer', '结果图层')}<select aria-label={t('Result layer', '结果图层')} value={resultIndex} onChange={event => { setResultIndex(Number(event.target.value)); setFitVersion(current => current + 1); }}><option value={-1}>{t('All results', '全部结果')}</option>{allHighlights.map((item, index) => <option key={index} value={index}>{item.label || t(`Result ${index + 1}`, `结果 ${index + 1}`)}</option>)}</select></label>}
                      {!schemaMode && allHighlights.length > 0 && <label><input type="checkbox" checked={resultsOnly} onChange={() => { setResultsOnly(current => !current); setDepth(0); setFilterMatches(false); setConnectedOnly(false); setFocusId(''); onSelect(null); setFitVersion(current => current + 1); }} />{t('Result subgraph only', '仅看结果子图')}</label>}
                      <button onClick={() => { setSizeMode('degree'); setLabels('auto'); setEdgeWidth(1); setHighlightShape('star'); }}>{t('Restore defaults', '恢复默认')}</button>
                    </div>}
                    {searchOpen ? <div className="graph-search-group"><div className="graph-search"><Icon name="search" size={14} /><input autoFocus value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { const id = preview.matches.values().next().value; if (id) locate(id); } }} placeholder={t('Search by node ID or name', '搜索节点 ID 或名称')} /><span>{t(`${preview.matches.size.toLocaleString(locale)} results`, `${preview.matches.size.toLocaleString(locale)} 个结果`)}</span></div>
                      <label className="graph-filter-match"><input type="checkbox" checked={filterMatches} onChange={event => setFilterMatches(event.target.checked)} />{t('Show matches only', '仅显示匹配项')}</label>
                      {query.trim() && <div className="graph-search-results">{[...preview.matches].slice(0, 8).map(id => <button key={id} onClick={() => locate(id)}>{String(index.nodes.get(id)?.label ?? id)}{!id.startsWith('row_') && <small>{id}</small>}</button>)}{!preview.matches.size && <span>{t('No matching nodes in the loaded data', '当前已加载数据中没有匹配节点')}</span>}</div>}
                    </div> : null}
                    <div className={`graph-canvas ${schemaMode ? 'is-schema' : ''}`}><GraphCanvas state={canvasMemory.current.state} schema={schemaMode} active={!collapsed} key={`${sessionId}:${datasetName || ''}`} preview={preview} directed={renderedData?.directed} communities={communityById} highlights={highlights} highlightShape={highlightShape} selected={selected} onSelect={node => { onSelect(node); if (!node) { setFocusId(''); setDepth(0); } }} layout={layout} sizeMode={sizeMode} labels={labels} edgeWidth={edgeWidth} fitVersion={fitVersion} focusId={focusId} />{!preview.nodes.length && <div className="graph-preview-empty">{t('No nodes match the current filters', '没有符合条件的节点')}</div>}</div>
                    <GraphCoverage nodes={preview.nodes.length} edges={preview.edges.length}
                      totalNodes={schemaMode || tablesMode ? index.nodes.size : renderedData?.total_nodes ?? renderedData?.num_nodes ?? index.nodes.size}
                      totalEdges={schemaMode || tablesMode ? index.edges.length : renderedData?.total_edges ?? renderedData?.num_edges ?? index.edges.length}
                      kind={tablesMode ? 'tables' : schemaMode ? 'schema' : 'instances'} />
                    {(communityById.size || highlights.length) ? <div className="graph-legend"><span className="graph-legend-title">{t('Legend', '图例')}</span>{communityById.size ? <span><i className="legend-dot community" />{t('Community colors', '社区着色')}</span> : null}{highlights.filter(item => item.type === 'node').length ? <span><i className={`legend-dot node-highlight is-${highlightShape}`} />{t('Highlighted nodes', '节点高亮')}</span> : null}{highlights.filter(item => item.type === 'path').length ? <span><i className="legend-line path-highlight" />{t('Highlighted paths', '路径高亮')}</span> : null}{highlights.filter(item => item.type === 'subgraph').length ? <span><i className="legend-line subgraph-highlight" />{t('Highlighted subgraphs', '子图高亮')}</span> : null}</div> : null}
                    <div className="node-inspector">{selected ? <><div className="inspector-title"><strong>{(schemaMode ? String(selected.label ?? selected.id ?? '') : graphNodeLabel(detailedSelected || selectedInfo || selected, t)) || t('Selected node', '已选节点')}</strong><span>{t('Selected', '已选择')}</span></div>{!(String(selected.id ?? '').startsWith('row_') && selected.label && String(selected.label) !== String(selected.id)) && <div className="inspector-row"><span>{t('Node ID', '节点 ID')}</span><strong>{String(selected.id ?? '-')}</strong></div>}<div className="inspector-row"><span>{t('Type', '类型')}</span><strong>{String((!schemaMode && financeNodeTypeLabel(detailedSelected || selectedInfo || selected, t)) || nodePropertyType(detailedSelected || selectedInfo || selected) || t('Node', '节点'))}</strong></div><div className="inspector-row"><span>{selectedInfo?.degreeLabel || t('Degree', '度数')}</span><strong>{selectedInfo?.degree ?? '-'}</strong></div>{!schemaMode && <NodeProperties node={detailedSelected || selectedInfo || selected} />}<div className="graph-neighborhood"><label>{t('View range', '查看范围')}<select aria-label={t('Neighborhood range', '查看邻域范围')} value={depth} onChange={event => { setFocusId(String(selected.id)); setDepth(Number(event.target.value)); }}><option value="0">{t('Full preview', '全部预览')}</option><option value="1">{t('1-hop neighborhood', '一跳邻域')}</option><option value="2">{t('2-hop neighborhood', '两跳邻域')}</option></select></label><span>{t('Neighborhood uses loaded data', '邻域基于已加载数据')}</span></div><div className="inspector-actions"><button onClick={() => onExpand?.(selected)} disabled={!onExpand}><Icon name="plus" size={13} /> {t('Expand neighbors', '扩展邻居')}</button><button onClick={() => navigator.clipboard?.writeText(String(selected.id ?? ''))}><Icon name="copy" size={13} /> {t('Copy ID', '复制 ID')}</button></div></> : <span>{t('Select a node to view its properties', '点击图中节点查看属性')}</span>}</div>
                    </>}
                  </>
                    : <WorkspaceTab tab={tab} data={data} datasetName={datasetName} messages={messages} />}
    </div>
  </aside>;
}, graphWorkspacePropsEqual);

function displayCount(value: unknown, locale: string) {
  const count = Number(value);
  return Number.isFinite(count) ? count.toLocaleString(locale) : '-';
}

function displayBytes(value: unknown, locale: string) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return '-';
  if (bytes < 1024) return `${bytes.toLocaleString(locale)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let scaled = bytes;
  let unit = -1;
  do {
    scaled /= 1024;
    unit += 1;
  } while (scaled >= 1024 && unit < units.length - 1);
  return `${scaled.toFixed(scaled >= 10 ? 1 : 2)} ${units[unit]}`;
}

function PartitionedGraphTab({ tab, datasetName, summary }: { tab: Tab; datasetName?: string; summary: PartitionedGraphSummary }) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
  const waiting = summary.preparation_status === 'not_started';
  const preparing = Boolean(summary.preparation_status) && !waiting;
  const statusLabel = waiting
    ? t('Waiting to prepare this session snapshot', '等待准备当前会话的分区快照')
    : preparing
      ? t('Preparing partitioned compute snapshot', '正在准备分区计算快照')
      : t('Partitioned compute snapshot ready', '大图分区计算快照已就绪');
  if (tab === 'Overview') return <div className="overview-content"><div className="overview-hero"><span className="eyebrow">{t('Large graph snapshot', '大图快照')}</span><h2>{datasetName || t('Current dataset', '当前数据集')}</h2><strong>{statusLabel}</strong></div><div className="stat-grid"><div><span>{t('Nodes', '节点数')}</span><strong>{displayCount(summary.num_nodes, locale)}</strong></div><div><span>{t('Edges', '边数')}</span><strong>{displayCount(summary.num_edges, locale)}</strong></div><div><span>{t('Partitions', '分区数')}</span><strong>{displayCount(summary.partitions, locale)}</strong></div><div><span>{t('Version', '版本')}</span><strong>{displayCount(summary.graph_version, locale)}</strong></div></div><div className="workspace-note"><Icon name="spark" size={15} /> {waiting ? t('Activating this dataset creates an independent partitioned snapshot for the current session.', '激活该数据集后会为当前会话创建独立的分区快照。') : preparing ? t('Once preparation finishes, submit large-graph algorithms from the conversation.', '准备完成后，可从中央对话提交大图算法。') : t('The snapshot remains partitioned on disk; run algorithms from the conversation without loading it into the regular canvas.', '快照保留在磁盘分区中；请从中央对话运行大图算法，无需加载到普通图画布。')}</div></div>;
  return <div className="tab-content collection-data"><div className="tab-title"><strong>{t('Partition snapshot metadata', '分区快照元数据')}</strong><span>{t(`${displayCount(summary.num_nodes, locale)} nodes`, `${displayCount(summary.num_nodes, locale)} 个节点`)}</span></div><div className="collection-section"><strong>{t('Compute storage', '计算存储')}</strong><div className="stat-grid"><div><span>{t('Representation', '表示')}</span><strong>{summary.representation || '-'}</strong></div><div><span>{t('Storage', '存储')}</span><strong>{displayBytes(summary.storage_bytes, locale)}</strong></div><div><span>{t('Direction', '方向')}</span><strong>{summary.directed == null ? '-' : summary.directed ? t('Directed', '有向') : t('Undirected', '无向')}</strong></div><div><span>{t('Weights', '权重')}</span><strong>{summary.has_weights == null ? '-' : summary.has_weights ? t('Yes', '有') : t('No', '无')}</strong></div></div></div><div className="workspace-note">{t('Nodes and edges are not expanded in full in the browser; algorithms return bounded summaries or Artifacts.', '节点和边不会在浏览器中全量展开；算法结果通过有界摘要或分析产物返回。')}</div></div>;
}

function CollectionTab({ tab, datasetName, summary }: { tab: Tab; datasetName?: string; summary: GraphCollectionSummary }) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
  const splits = summary.split_sizes || {};
  const validationSize = splits.val ?? splits.valid;
  if (tab === 'Overview') return <div className="overview-content"><div className="overview-hero"><span className="eyebrow">{t('Graph collection', '图集合')}</span><h2>{datasetName || t('Current dataset', '当前数据集')}</h2><strong>{t('Graph classification dataset ready', '多图分类数据集已就绪')}</strong></div><div className="stat-grid"><div><span>{t('Graphs', '图数量')}</span><strong>{displayCount(summary.num_graphs, locale)}</strong></div><div><span>{t('Tasks', '任务数')}</span><strong>{displayCount(summary.num_tasks, locale)}</strong></div><div><span>{t('Metric', '评估指标')}</span><strong>{summary.metric || '-'}</strong></div><div><span>{t('Data splits', '数据划分')}</span><strong>{Object.keys(splits).length || '-'}</strong></div></div></div>;
  const missingRate = Number(summary.missing_label_rate);
  return <div className="tab-content collection-data"><div className="tab-title"><strong>{t('Dataset splits and features', '数据集划分与特征')}</strong><span>{t(`${displayCount(summary.num_graphs, locale)} graphs`, `${displayCount(summary.num_graphs, locale)} 张图`)}</span></div><div className="collection-section"><strong>{t('Data splits', '数据划分')}</strong><div className="stat-grid"><div><span>{t('Training', '训练集')}</span><strong>{displayCount(splits.train, locale)}</strong></div><div><span>{t('Validation', '验证集')}</span><strong>{displayCount(validationSize, locale)}</strong></div><div><span>{t('Test', '测试集')}</span><strong>{displayCount(splits.test, locale)}</strong></div><div><span>{t('Edgeless graphs', '无边图')}</span><strong>{displayCount(summary.num_edgeless_graphs, locale)}</strong></div></div></div><div className="collection-section"><strong>{t('Vocabularies and labels', '词表与标签')}</strong><div className="stat-grid"><div><span>{t('Node vocabulary', '节点词表')}</span><strong>{displayCount(summary.node_vocab_size, locale)}</strong></div><div><span>{t('Edge vocabulary', '边词表')}</span><strong>{displayCount(summary.edge_vocab_size, locale)}</strong></div><div><span>{t('Missing labels', '缺失标签')}</span><strong>{Number.isFinite(missingRate) ? `${(missingRate * 100).toFixed(2)}%` : '-'}</strong></div><div><span>{t('Edge features', '边特征')}</span><strong>{summary.uses_edge_features == null ? '-' : summary.uses_edge_features ? t('Yes', '是') : t('No', '否')}</strong></div></div></div></div>;
}

function WorkspaceTab({ tab, data, datasetName, messages }: { tab: Tab; data: GraphData; datasetName?: string; messages: Message[] }) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
  if (tab === 'Overview') {
    const totalNodes = data.total_nodes ?? data.num_nodes ?? data.nodes.length;
    const totalEdges = data.total_edges ?? data.num_edges ?? data.edges.length;
    const averageDegree = totalNodes > 0 ? totalEdges * 2 / totalNodes : 0;
    const possibleEdges = totalNodes > 1 && data.directed != null ? data.directed ? totalNodes * (totalNodes - 1) : totalNodes * (totalNodes - 1) / 2 : 0;
    const density = possibleEdges > 0 ? totalEdges / possibleEdges : null;
    const densityPercent = new Intl.NumberFormat(locale, {
      style: 'percent', maximumFractionDigits: density != null && density > 0 && density < .0001 ? 4 : 2,
    });
    const decimal = new Intl.NumberFormat(locale, { maximumFractionDigits: 2 });
    const communityCount = data.community?.num_communities ?? data.communities?.num_communities;
    const facts = [
      { label: t('Direction', '方向'), value: data.directed == null ? t('Unknown', '未知') : data.directed ? t('Directed graph', '有向图') : t('Undirected graph', '无向图') },
      { label: data.directed ? t('Average total degree', '平均总度') : t('Average degree', '平均度'), value: decimal.format(averageDegree) },
      density == null ? null : { label: t('Link density', '连接密度'), value: densityPercent.format(density) },
      communityCount == null ? null : { label: t('Communities', '社区数'), value: Number(communityCount).toLocaleString(locale) },
    ].filter((item): item is { label: string; value: string } => Boolean(item));
    return <div className="overview-content graph-overview">
      <section className="graph-overview-summary" aria-labelledby="graph-overview-title">
        <div className="graph-overview-heading"><div><span className="eyebrow">{t('Graph overview', '图概览')}</span><h2 id="graph-overview-title">{datasetName || t('Current dataset', '当前数据集')}</h2></div><span className="graph-overview-ready"><i />{t('Ready', '分析就绪')}</span></div>
        <dl className="graph-overview-totals" aria-label={t('Graph scale', '图规模')}>
          <div><dt>{t('Nodes', '节点')}</dt><dd>{totalNodes.toLocaleString(locale)}</dd></div>
          <div><dt>{t('Edges', '边')}</dt><dd>{totalEdges.toLocaleString(locale)}</dd></div>
        </dl>
      </section>
      <section className="graph-overview-section" aria-labelledby="graph-structure-title">
        <header><h3 id="graph-structure-title">{t('Structure', '结构概况')}</h3></header>
        <dl className="graph-overview-facts">{facts.map(fact => <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}</dl>
      </section>
    </div>;
  }
  if (tab === 'Data') return <div className="tab-content"><div className="tab-title"><strong>{t('Node and edge data', '节点与边数据')}</strong><span>{t(`${(data.total_nodes ?? data.num_nodes ?? data.nodes.length).toLocaleString(locale)} nodes`, `${(data.total_nodes ?? data.num_nodes ?? data.nodes.length).toLocaleString(locale)} 个节点`)}</span></div><div className="data-list">{data.nodes.slice(0, 20).map((node, index) => <div key={index}><Icon name="project" size={14} /><span>{graphNodeLabel(node, t) || t(`Node ${index + 1}`, `节点 ${index + 1}`)}</span><small>{financeNodeTypeLabel(node, t) || String(nodePropertyType(node) ?? t('Node', '节点'))}</small></div>)}</div>{data.nodes.length < (data.total_nodes ?? data.num_nodes ?? data.nodes.length) ? <div className="workspace-note">{t(`This is a visualization preview with ${data.nodes.length.toLocaleString(locale)} loaded node samples.`, `当前为可视化预览，已加载 ${data.nodes.length.toLocaleString(locale)} 个节点样本。`)}</div> : null}</div>;
  return <ArtifactsTab messages={messages} />;
}

const ML_TOOLS = new Set(['train', 'infer', 'load_graph_classification_dataset']);
const METRIC_LABELS: Record<string, readonly [string, string]> = {
  loss: ['Loss', '损失'], train_loss: ['Training loss', '训练损失'], val_loss: ['Validation loss', '验证损失'], test_loss: ['Test loss', '测试损失'],
  train_acc: ['Training accuracy', '训练准确率'], val_acc: ['Validation accuracy', '验证准确率'], test_acc: ['Test accuracy', '测试准确率'], accuracy: ['Accuracy', '准确率'],
  train_auc: ['Training AUC', '训练 AUC'], val_auc: ['Validation AUC', '验证 AUC'], test_auc: ['Test AUC', '测试 AUC'], auc: ['AUC', 'AUC'],
  train_f1: ['Training F1', '训练 F1'], val_f1: ['Validation F1', '验证 F1'], test_f1: ['Test F1', '测试 F1'], f1: ['F1', 'F1'], macro_f1: ['Macro F1', '宏平均 F1'], micro_f1: ['Micro F1', '微平均 F1'],
  precision: ['Precision', '精确率'], recall: ['Recall', '召回率'], learning_rate: ['Learning rate', '学习率'],
  best_epoch: ['Best epoch', '最佳轮次'], trained_epochs: ['Epochs trained', '训练轮次'], num_classes: ['Classes', '类别数'],
  num_train: ['Training samples', '训练样本数'], num_val: ['Validation samples', '验证样本数'], num_test: ['Test samples', '测试样本数'],
};
const CHART_COLORS = ['var(--accent, #2563eb)', '#d97745', '#0891b2', '#bd4f6c', '#7b6cb7'];

function recordValue(value: unknown): Record<string, any> {
  if (typeof value === 'string') {
    try { const decoded = JSON.parse(value); return decoded && typeof decoded === 'object' && !Array.isArray(decoded) ? decoded : {}; } catch { return {}; }
  }
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, any> : {};
}

function formatMetric(key: string, value: unknown, locale: string) {
  const number = metricNumber(value);
  if (number === null) return '—';
  if (/(acc|accuracy|auc|f1|precision|recall)$/i.test(key) && Math.abs(number) <= 1) return `${(number * 100).toFixed(1)}%`;
  if (Number.isInteger(number)) return number.toLocaleString(locale);
  return number.toFixed(Math.abs(number) < 0.01 ? 5 : 4).replace(/0+$/, '').replace(/\.$/, '');
}

function metricLabel(key: string, t: Translate) {
  const labels = METRIC_LABELS[key];
  return labels ? t(labels[0], labels[1]) : key.replace(/_/g, ' ').replace(/\b\w/g, (value: string) => value.toUpperCase());
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

function mergeMlRuns(messages: Message[], liveTools: ToolCall[]) {
  const saved = messages.flatMap(message => (message.tools || []).filter(tool => ML_TOOLS.has(String(tool.tool || ''))).map(tool => {
    if (String(tool.tool || '') !== 'train' || executionStatus(tool) !== 'completed' || Object.keys(recordValue(recordValue(tool.result).metrics)).length) return tool;
    const metrics = legacyTrainingMetrics(message.content || '');
    return metrics ? { ...tool, result: { ...recordValue(tool.result), metrics } } : tool;
  }));
  const runs = [...saved];
  liveTools.filter(tool => ML_TOOLS.has(String(tool.tool || ''))).forEach(live => {
    const index = live.call_id ? runs.findIndex(run => run.call_id === live.call_id) : -1;
    if (index < 0) runs.push(live);
    else runs[index] = { ...runs[index], ...live, result: live.result ?? runs[index].result, history: live.history?.length ? live.history : runs[index].history };
  });
  return runs;
}

function trainingRunIdentity(run: ToolCall) {
  const result = recordValue(run.result);
  return String((run as ProjectTrainingRun).training_run_id || run.job_id || result.job_id || run.call_id || '');
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
  const { language, t } = useI18n();
  const locale = localeFor(language);
  const width = 560; const height = 190; const left = 46; const right = 12; const top = 18; const bottom = 28;
  const series = keys.map(key => { const segments = metricSegments(rows, key); return { key, segments, values: segments.flat() }; }).filter(item => item.values.length);
  if (!series.length) return null;
  const values = series.flatMap(item => item.values.map(point => point.value));
  const epochs = series.flatMap(item => item.values.map(point => point.epoch));
  let minY = Math.min(...values); let maxY = Math.max(...values);
  if (keys.every(key => /(acc|accuracy|auc|f1|precision|recall)$/i.test(key)) && minY >= 0 && maxY <= 1) { minY = 0; maxY = 1; }
  else { const padding = (maxY - minY || Math.abs(maxY) || 1) * .12; minY = Math.max(0, minY - padding); maxY += padding; }
  const minX = Math.min(...epochs); const maxX = Math.max(...epochs);
  const x = (epoch: number) => left + ((epoch - minX) / (maxX - minX || 1)) * (width - left - right);
  const y = (value: number) => top + (1 - (value - minY) / (maxY - minY || 1)) * (height - top - bottom);
  return <div className="ml-chart-panel"><div className="ml-chart-heading"><strong>{title}</strong><div>{series.map((item, index) => <span key={item.key}><i style={{ backgroundColor: CHART_COLORS[index % CHART_COLORS.length] }} />{metricLabel(item.key, t)}</span>)}</div></div><svg className="ml-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={t(`${title} training curve`, `${title}训练曲线`)}>
    {[0, .25, .5, .75, 1].map(fraction => { const value = maxY - (maxY - minY) * fraction; const py = top + (height - top - bottom) * fraction; return <g key={fraction}><line x1={left} x2={width - right} y1={py} y2={py} /><text x={left - 7} y={py + 4} textAnchor="end">{formatMetric(keys[0], value, locale)}</text></g>; })}
    <text x={left} y={height - 7}>{t(`Epoch ${minX}`, `第 ${minX} 轮`)}</text><text x={width - right} y={height - 7} textAnchor="end">{t(`Epoch ${maxX}`, `第 ${maxX} 轮`)}</text>
    {series.map((item, index) => <g key={item.key} data-metric={item.key}>{item.segments.filter(segment => segment.length > 1).map((segment, segmentIndex) => <polyline key={segmentIndex} points={segment.map(point => `${x(point.epoch)},${y(point.value)}`).join(' ')} style={{ stroke: CHART_COLORS[index % CHART_COLORS.length] }} />)}{item.values.map(point => <circle key={point.epoch} cx={x(point.epoch)} cy={y(point.value)} r={item.values.length < 25 ? 2.4 : 1.4} style={{ fill: CHART_COLORS[index % CHART_COLORS.length] }} />)}</g>)}
  </svg></div>;
}

function MetricsTable({ rows }: { rows: Array<Record<string, unknown>> }) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
  const keys = availableMetricKeys(rows).slice(0, 5);
  if (!rows.length) return <div className="ml-no-history">{t('No per-epoch metrics are available for this run.', '该运行没有可用的逐轮指标。')}</div>;
  return <div className="ml-table-wrap"><table className="ml-table"><thead><tr><th>{t('Epoch', '轮次')}</th>{keys.map(key => <th key={key}>{metricLabel(key, t)}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={`${row.epoch}-${index}`}><td>{String(row.epoch)}</td>{keys.map(key => <td key={key}>{formatMetric(key, row[key], locale)}</td>)}</tr>)}</tbody></table></div>;
}

function FinalMetricsTable({ metrics }: { metrics: Record<string, unknown> }) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
  const rows = [
    { label: t('Training', '训练集'), prefix: 'train' },
    { label: t('Validation', '验证集'), prefix: 'val' },
    { label: t('Test', '测试集'), prefix: 'test' },
  ].filter(item => ['acc', 'loss'].some(key => metricNumber(metrics[`${item.prefix}_${key}`]) !== null) || metricNumber(metrics[`num_${item.prefix}`]) !== null);
  if (!rows.length) return <div className="ml-no-history">{t('No per-epoch metrics are available for this run.', '该运行没有可用的逐轮指标。')}</div>;
  return <div className="ml-table-wrap"><table className="ml-table"><thead><tr><th>{t('Data split', '数据划分')}</th><th>{t('Accuracy', '准确率')}</th><th>{t('Loss', '损失')}</th><th>{t('Samples', '样本数')}</th></tr></thead><tbody>{rows.map(row => <tr key={row.prefix}><td>{row.label}</td><td>{formatMetric(`${row.prefix}_acc`, metrics[`${row.prefix}_acc`], locale)}</td><td>{formatMetric(`${row.prefix}_loss`, metrics[`${row.prefix}_loss`], locale)}</td><td>{formatMetric(`num_${row.prefix}`, metrics[`num_${row.prefix}`], locale)}</td></tr>)}</tbody></table></div>;
}

function JobStopButton({ run, sessionId, onStopJob }: { run: ToolCall; sessionId: string; onStopJob?: StopJob }) {
  const { t } = useI18n();
  const [status, setStatus] = useState('');
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const result = recordValue(run.result);
  const jobId = String(run.job_id || result.job_id || recordValue(run.progress).job_id || '');
  const terminal = !executionPending(run);
  if (!onStopJob || !jobId || !sessionId || terminal || status === 'confirmed') return status === 'confirmed' ? <span role="status">{t('Job ended', '作业已结束')}</span> : null;
  const pending = status === 'stopping' || executionStatus(run) === 'cancelling';
  return <button type="button" disabled={pending} aria-label={t('Stop job', '终止作业')} onClick={() => onStopJob(sessionId, jobId, next => { if (mounted.current) setStatus(next); })}>
    {pending ? t('Stopping…', '正在停止…') : status === 'unconfirmed' ? t('Stop not confirmed; retry', '停止尚未确认，重试') : t('Stop job', '终止作业')}
  </button>;
}

function MLRunCard({ run, index, sessionId, onStopJob }: { run: ToolCall; index: number; sessionId: string; onStopJob?: StopJob }) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
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
  const taskNames: Record<string, string> = { node_classification: t('Node classification', '节点分类'), link_prediction: t('Link prediction', '链接预测'), graph_classification: t('Graph classification', '图分类'), pretrain: t('Graph pretraining', '图预训练') };
  const task = String(context.task || 'graph_machine_learning');
  const taskLabel = taskNames[task] || (task === 'graph_machine_learning' ? t('Graph machine learning', '图机器学习') : task);
  const model = String(context.display_model || context.model || context.method || t('Model training', '模型训练'));
  const completedEpochs = Number(metrics.trained_epochs || progress.epoch || history[history.length - 1]?.epoch || 0);
  const totalEpochs = Number(progress.epochs || recordValue(args.train_config).epochs || recordValue(args.algo_config).epochs || completedEpochs || 0);
  const projectRun = run as ProjectTrainingRun;
  const source = [projectRun.session_title, projectRun.dataset_name].filter(Boolean).join(' · ');
  const status = executionStatus(run);
  const statusLabel = status === 'completed' && metrics.stopped_early ? t('Stopped early', '提前停止') : executionLabel(run, t);
  const error = executionError(run);
  return <article className="ml-run-card"><header className="ml-run-header"><div className="workspace-list-icon"><Icon name="cube" size={16} /></div><div><strong>{taskLabel}</strong><span>{model}{totalEpochs ? t(` · ${completedEpochs}/${totalEpochs} epochs`, ` · ${completedEpochs}/${totalEpochs} 轮`) : ''}{source ? ` · ${source}` : ''}</span></div><b className={status}>{statusLabel}</b></header>
    {status === 'failed' ? <div className="ml-run-error" role="alert">{error || t('Training failed. See the conversation for details.', '训练失败，请查看对话中的具体原因。')}</div> : null}
    <JobStopButton run={run} sessionId={projectRun.session_id || sessionId} onStopJob={onStopJob} />
    {summaryKeys.length ? <div className="ml-metric-grid">{summaryKeys.map(key => <div key={key}><span>{metricLabel(key, t)}</span><strong>{formatMetric(key, metrics[key], locale)}</strong></div>)}</div> : null}
    <div className="ml-view-bar"><span>{history.length ? t(`${history.length.toLocaleString(locale)} training points`, `${history.length.toLocaleString(locale)} 个训练点`) : t(`Run ${index + 1}`, `运行 ${index + 1}`)}</span><SlidingTabs ariaLabel={t('Training result view', '训练结果视图')} className="ml-view-toggle" indicatorClassName="ml-view-indicator" options={[{ value: 'chart', label: t('Chart', '曲线'), disabled: !history.length }, { value: 'table', label: t('Table', '表格') }]} value={view} onChange={setView} /></div>
    {view === 'chart' ? <div className="ml-charts"><MetricChart rows={history} keys={lossKeys} title={t('Loss', '损失')} /><MetricChart rows={history} keys={scoreKeys} title={t('Evaluation metrics', '评估指标')} />{!lossKeys.length && !scoreKeys.length ? <FinalMetricsTable metrics={metrics} /> : null}</div> : history.length ? <MetricsTable rows={history} /> : <FinalMetricsTable metrics={metrics} />}
  </article>;
}

function ProjectTrainingComparison({ runs }: { runs: ProjectTrainingRun[] }) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
  return <div className="ml-comparison-wrap"><table className="ml-comparison"><thead><tr><th>{t('Model', '模型')}</th><th>{t('Conversation', '会话')}</th><th>{t('Dataset', '数据集')}</th><th>{t('Status', '状态')}</th><th>{t('Epochs', '轮次')}</th><th>{t('Validation accuracy', '验证准确率')}</th><th>{t('Test accuracy', '测试准确率')}</th><th>{t('Best epoch', '最佳轮次')}</th></tr></thead><tbody>{runs.map((run, index) => {
    const context = { ...recordValue(run.args), ...recordValue(run.result), ...recordValue(run.progress) };
    const metrics = recordValue(recordValue(run.result).metrics);
    const model = String(context.display_model || context.model || context.method || t('Model training', '模型训练'));
    return <tr key={run.training_run_id || `${run.session_id}-${index}`}><td>{model}</td><td title={run.session_title}>{run.session_title || t('New conversation', '新会话')}</td><td>{run.dataset_name || '-'}</td><td><span className={`ml-status-${executionStatus(run)}`}>{executionLabel(run, t)}</span></td><td>{trainingRunEpoch(run) || '-'}</td><td>{formatMetric('val_acc', metrics.val_acc, locale)}</td><td>{formatMetric('test_acc', metrics.test_acc, locale)}</td><td>{formatMetric('best_epoch', metrics.best_epoch, locale)}</td></tr>;
  })}</tbody></table></div>;
}

function MLTab({ hasProject, messages, liveTools, projectRuns, sessionId, loading, error, onStopJob }: { hasProject: boolean; messages: Message[]; liveTools: ToolCall[]; projectRuns: ProjectTrainingRun[]; sessionId: string; loading: boolean; error: string; onStopJob?: StopJob }) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
  const [scope, setScope] = useState<'current' | 'project'>('current');
  const runs = useMemo(() => mergeMlRuns(messages, liveTools), [messages, liveTools]);
  const localTrainingRuns = runs.filter(run => String(run.tool || '') === 'train');
  const currentProjectRuns = projectRuns.filter(run => run.session_id === sessionId);
  const currentTrainingRuns = useMemo(() => mergeTrainingRuns(localTrainingRuns, currentProjectRuns), [localTrainingRuns, currentProjectRuns]);
  const otherRuns = runs.filter(run => String(run.tool || '') !== 'train');
  const effectiveScope = !hasProject ? 'current' : scope === 'current' && !currentTrainingRuns.length && projectRuns.length ? 'project' : scope;
  const visibleTrainingRuns = effectiveScope === 'project' ? projectRuns : currentTrainingRuns;
  if (!runs.length && !projectRuns.length && !loading) return <div className="tab-placeholder"><Icon name="cube" size={22} /><strong>{t('Graph machine learning', '图机器学习')}</strong><span>{hasProject ? t('This project has no training or inference records yet.', '当前项目还没有训练或推理记录。') : t('This conversation has no training or inference records yet.', '当前会话还没有训练或推理记录。')}</span></div>;
  return <div className="tab-content ml-tab"><div className="ml-tab-heading"><div className="tab-title"><strong>{t('Graph machine learning results', '图机器学习训练结果')}</strong><span>{effectiveScope === 'project' ? t(`${projectRuns.length.toLocaleString(locale)} project runs`, `${projectRuns.length.toLocaleString(locale)} 次项目训练`) : t(`${currentTrainingRuns.length.toLocaleString(locale)} current runs`, `${currentTrainingRuns.length.toLocaleString(locale)} 次当前训练`)}</span></div>{hasProject && <SlidingTabs ariaLabel={t('Training result scope', '训练结果范围')} className="ml-scope-toggle" indicatorClassName="ml-scope-indicator" options={[{ value: 'current', label: t('Current conversation', '当前会话') }, { value: 'project', label: t('Project comparison', '项目对比') }]} value={effectiveScope} onChange={setScope} />}</div>
    {loading ? <div className="ml-sync-status"><span className="loader" /> {t('Syncing project training results', '正在同步项目训练结果')}</div> : null}
    {error ? <div className="ml-sync-error">{error}</div> : null}
    {effectiveScope === 'project' && visibleTrainingRuns.length ? <ProjectTrainingComparison runs={projectRuns} /> : null}
    {!visibleTrainingRuns.length && !loading ? <div className="ml-no-runs">{hasProject ? t('This conversation has no training records. Switch to “Project comparison” to view other conversations.', '该会话还没有训练记录，可切换到“项目对比”查看其他会话。') : t('This conversation has no training records.', '该会话还没有训练记录。')}</div> : visibleTrainingRuns.map((run, index) => <MLRunCard onStopJob={onStopJob} sessionId={sessionId} key={`${trainingRunIdentity(run) || 'train'}-${index}`} run={run} index={index} />)}
    {effectiveScope === 'current' && otherRuns.length ? <div className="ml-activity"><strong>{t('Data and inference activity', '数据与推理记录')}</strong><div className="workspace-list">{otherRuns.map((run, index) => <div className="workspace-list-row" key={`${run.call_id || run.tool}-${index}`}><div className="workspace-list-icon"><Icon name="cube" size={15} /></div><div><strong>{String(run.tool) === 'infer' ? t('Inference', '推理') : String(run.tool) === 'load_graph_classification_dataset' ? t('Load graph classification dataset', '加载图分类数据集') : String(run.tool)}</strong><span>{executionLabel(run, t)}</span></div><b>{executionLabel(run, t)}</b><JobStopButton run={run} sessionId={sessionId} onStopJob={onStopJob} /></div>)}</div></div> : null}</div>;
}

function reportRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item)) : [];
}

function reportText(value: unknown, locale: string): string {
  if (value == null || value === '') return '-';
  if (typeof value === 'number') return value.toLocaleString(locale, { maximumFractionDigits: 6 });
  if (typeof value === 'boolean') return locale.startsWith('zh') ? (value ? '是' : '否') : (value ? 'Yes' : 'No');
  if (Array.isArray(value)) return value.map(item => reportText(item, locale)).join(locale.startsWith('zh') ? '、' : ', ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function reportToolResult(tools: ToolCall[], name: string): Record<string, unknown> {
  const tool = [...tools].reverse().find(item => String(item.tool || '') === name && item.result != null);
  const result = recordValue(tool?.result);
  const summary = recordValue(result.summary);
  return Object.keys(summary).length ? summary : result;
}

function reportMetric(label: string, value: unknown, unit?: string) {
  if (value == null || value === '') return null;
  return unit ? { label, value, unit } : { label, value };
}

function reportNumber(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function centralityTitle(value: unknown) {
  const name = String(value || '').toLowerCase();
  return ({ pagerank: 'PageRank', degree: '度中心性', betweenness: '中介中心性', closeness: '接近中心性', eigenvector: '特征向量中心性' } as Record<string, string>)[name] || String(value || '中心性');
}

function legacyConnectionSection(tools: ToolCall[], oldSections: Record<string, unknown>[]) {
  const degree = reportToolResult(tools, 'degree_analysis');
  const centrality = reportToolResult(tools, 'node_centrality');
  const oldDegree = oldSections.find(section => section.id === 'degree-distribution');
  const oldCentrality = oldSections.find(section => section.id === 'centrality-ranking');
  if (!Object.keys(degree).length && !Object.keys(centrality).length && !oldDegree && !oldCentrality) return null;

  const degreeRows = reportRecords(degree.top_degree_nodes);
  const centralityRows = reportRecords(centrality.top_nodes);
  const directed = degree.directed === true || degree.avg_in_degree != null || degree.avg_out_degree != null
    || degreeRows.some(row => row.total != null || row.in != null || row.out != null);
  const stats = recordValue(directed ? degree.total_degree_stats : degree.degree_stats);
  let average = reportNumber(degree.avg_degree ?? degree.total_degree_mean ?? stats.mean);
  if (average == null) {
    const averageIn = reportNumber(degree.avg_in_degree);
    const averageOut = reportNumber(degree.avg_out_degree);
    if (averageIn != null && averageOut != null) average = averageIn + averageOut;
  }
  const metrics = [
    reportMetric(directed ? '平均总度' : '平均度', average),
    reportMetric(directed ? '中位总度' : '中位度', stats.median),
    reportMetric(directed ? '最大总度' : '最大度', stats.max),
    reportMetric('平均入度', directed ? degree.avg_in_degree : null),
    reportMetric('平均出度', directed ? degree.avg_out_degree : null),
  ].filter(Boolean);
  const centralityName = centralityTitle(centrality.centrality_type);
  const scores = new Map(centralityRows.map(row => [String(row.node), reportNumber(row.value)]));
  let data = degreeRows.slice(0, 10).flatMap(row => {
    const value = reportNumber(directed ? row.total ?? row.degree : row.degree ?? row.total);
    if (row.node == null || value == null) return [];
    const score = scores.get(String(row.node));
    return [{ label: String(row.node), value, ...(score == null ? {} : { detail: `${centralityName} ${score}` }) }];
  });
  let chartTitle = scores.size ? '关键节点：连接规模与全局影响力' : '高连接节点';
  let unit = directed ? '总度' : '度';
  if (!data.length && centralityRows.length) {
    data = centralityRows.slice(0, 10).flatMap(row => {
      const value = reportNumber(row.value);
      return row.node == null || value == null ? [] : [{ label: String(row.node), value }];
    });
    chartTitle = `${centralityName} 排名`;
    unit = centralityName;
  }
  if (!data.length) {
    const fallback = reportRecords(reportRecords(oldDegree?.charts)[0]?.data || reportRecords(oldCentrality?.charts)[0]?.data);
    data = fallback.flatMap(row => {
      const value = reportNumber(row.value);
      return value == null ? [] : [{ label: String(row.label ?? '-'), value }];
    });
  }
  return {
    id: 'connections-and-key-nodes',
    title: '连接与关键节点',
    summary: degreeRows.length && centralityRows.length
      ? `条形长度表示${directed ? '总度' : '度'}，右侧同时给出 ${centralityName}。`
      : degreeRows.length ? `用${directed ? '总度' : '度'}对比节点的连接规模。` : `按 ${centralityName} 对关键节点排序。`,
    metrics,
    charts: data.length ? [{ type: 'bar', title: chartTitle, data, unit }] : [],
    tables: [], insights: [], limitations: [],
  };
}

function legacyConnectivitySection(tools: ToolCall[]) {
  const result = reportToolResult(tools, 'graph_connectivity');
  if (!Object.keys(result).length) return null;
  const directed = result.directed === true || 'is_weakly_connected' in result || 'is_strongly_connected' in result;
  const strong = recordValue(result.strongly_connected_components);
  const weak = recordValue(result.weakly_connected_components);
  const connected = recordValue(result.connected_components);
  const metrics = [
    reportMetric('弱连通', directed && result.is_weakly_connected != null ? result.is_weakly_connected ? '是' : '否' : null),
    reportMetric('强连通', directed && result.is_strongly_connected != null ? result.is_strongly_connected ? '是' : '否' : null),
    reportMetric('连通', !directed && result.is_connected != null ? result.is_connected ? '是' : '否' : null),
    reportMetric('强连通分量', strong.count), reportMetric('最大强连通分量', strong.largest_size, '节点'),
    reportMetric('弱连通分量', weak.count), reportMetric('最大弱连通分量', weak.largest_size, '节点'),
    reportMetric('连通分量', connected.count), reportMetric('最大连通分量', connected.largest_size, '节点'),
  ].filter(Boolean);
  const component = directed ? Object.keys(weak).length ? weak : strong : connected;
  const kind = directed ? Object.keys(weak).length ? '弱连通' : '强连通' : '连通';
  return {
    id: 'connectivity', title: '连通性',
    summary: component.count != null ? `图包含 ${component.count} 个${kind}分量，最大分量有 ${component.largest_size ?? 0} 个节点。` : '图的连通状态和分量规模。',
    metrics, charts: [], tables: [], insights: [], limitations: [],
  };
}

function conciseLegacySummary(sections: Record<string, unknown>[]) {
  const metricMap = (id: string) => new Map(reportRecords(sections.find(section => section.id === id)?.metrics).map(metric => [String(metric.label), metric.value]));
  const overview = metricMap('structure-overview');
  const connection = metricMap('connections-and-key-nodes');
  const community = metricMap('structure-patterns');
  const parts: string[] = [];
  if (reportNumber(overview.get('节点数')) != null && reportNumber(overview.get('边数')) != null) parts.push(`${Number(overview.get('节点数')).toLocaleString('zh-CN')} 个节点 · ${Number(overview.get('边数')).toLocaleString('zh-CN')} 条边`);
  const average = connection.get('平均总度') ?? connection.get('平均度');
  if (average != null) parts.push(`平均度 ${average}`);
  const communities = community.get('社区数') ?? community.get('分组数量');
  if (communities != null) parts.push(`${communities} 个社区`);
  return parts.length ? `${parts.join('；')}。` : '';
}

function modernizeLegacyReport(report: Record<string, unknown>, tools: ToolCall[]) {
  if (report.version === 'analysis_report.v2') return report;
  const oldSections = reportRecords(report.sections);
  const hasLegacyStructure = oldSections.some(section => ['degree-distribution', 'centrality-ranking', 'global-metrics', 'structure-patterns', 'supplemental-results'].includes(String(section.id)));
  if (!hasLegacyStructure) return report;
  const connection = legacyConnectionSection(tools, oldSections);
  const connectivity = legacyConnectivitySection(tools);
  const sections: Record<string, unknown>[] = [];
  let connectionInserted = false;
  oldSections.forEach(section => {
    const id = String(section.id || '');
    if (id === 'degree-distribution' || id === 'centrality-ranking') {
      if (!connectionInserted && connection) { sections.push(connection); connectionInserted = true; }
      return;
    }
    if (id === 'supplemental-results' && connectivity) return;
    if (id === 'structure-overview') sections.push({ ...section, insights: [] });
    else if (id === 'global-metrics') sections.push({ ...section, title: '整体结构指标', summary: '图的稀疏程度、路径距离、聚集性与连接倾向。', tables: [], insights: [] });
    else if (id === 'structure-patterns') {
      const metrics = reportRecords(section.metrics).map(metric => metric.label === '分组数量' ? { ...metric, label: '社区数' } : metric);
      const values = new Map(metrics.map(metric => [String(metric.label), metric.value]));
      const summary = values.get('社区数') != null
        ? `识别到 ${values.get('社区数')} 个社区${values.get('模块度') == null ? '。' : `，模块度为 ${values.get('模块度')}。`}`
        : section.summary;
      sections.push({ ...section, title: '社区结构', summary, metrics, insights: [] });
    }
    else sections.push(section);
  });
  if (connection && !connectionInserted) sections.splice(Math.min(1, sections.length), 0, connection);
  if (connectivity) {
    const connectionIndex = sections.findIndex(section => section.id === 'connections-and-key-nodes');
    sections.splice(connectionIndex < 0 ? Math.min(1, sections.length) : connectionIndex + 1, 0, connectivity);
  }
  return {
    ...report,
    subtitle: '规模、连接、关键节点与结构特征',
    summary: conciseLegacySummary(sections) || report.summary,
    sections,
    limitations: Array.isArray(report.limitations)
      ? report.limitations.filter(item => String(item) !== '连接分布缺少统计摘要。')
      : [],
  };
}

function ReportChart({ chart, locale }: { chart: Record<string, unknown>; locale: string }) {
  const rows = reportRecords(chart.data).map(row => ({ label: reportText(row.label, locale), value: Number(row.value), detail: row.detail == null ? '' : reportText(reportLabel(row, 'detail', locale), locale) })).filter(row => Number.isFinite(row.value));
  if (!rows.length) return null;
  const max = Math.max(...rows.map(row => Math.max(0, row.value)), 0);
  return <div className="analysis-report-chart" aria-label={reportText(reportLabel(chart, 'title', locale), locale)}>
    <strong>{reportText(reportLabel(chart, 'title', locale), locale)}</strong>
    <div>{rows.map((row, index) => <div className="analysis-report-bar" key={`${row.label}-${index}`}>
      <span title={row.label}>{row.label}</span>
      <i aria-hidden="true"><b style={{ width: `${max > 0 ? Math.max(2, row.value / max * 100) : 0}%` }} /></i>
      <em><strong>{reportText(row.value, locale)}{chart.unit ? ` ${String(reportLabel(chart, 'unit', locale))}` : ''}</strong>{row.detail ? <small>{row.detail}</small> : null}</em>
    </div>)}</div>
  </div>;
}

function ReportTable({ table, locale }: { table: Record<string, unknown>; locale: string }) {
  const rows = reportRecords(table.rows);
  const declaredColumns = reportRecords(table.columns);
  const columns = declaredColumns.length ? declaredColumns : rows.length ? Object.keys(rows[0]).map(key => ({ key, label: key })) : [];
  if (!rows.length || !columns.length) return null;
  return <div className="analysis-report-table-block">
    <strong>{reportText(reportLabel(table, 'title', locale), locale)}</strong>
    <div className="analysis-report-table-scroll"><table><thead><tr>{columns.map(column => <th key={String(column.key)}>{reportText(reportLabel(column, 'label', locale) || column.key, locale)}</th>)}</tr></thead>
      <tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{columns.map(column => <td key={String(column.key)}>{reportText(row[String(column.key)], locale)}</td>)}</tr>)}</tbody></table></div>
  </div>;
}

function AnalysisReport({ report, index, onBack }: { report: Record<string, unknown>; index: number; onBack: () => void }) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
  const title = String(recordValue(report.title_i18n)[language] || report.title || report.name || t(`Analysis report ${index + 1}`, `分析报告 ${index + 1}`));
  const sections = reportRecords(report.sections);
  const limitations = Array.isArray(report.limitations) ? report.limitations : [];
  return <article className="analysis-report" aria-label={t(`${title} report content`, `${title}报告内容`)}>
    <button type="button" className="analysis-report-back" onClick={onBack}><Icon name="arrow-left" size={14} /> {t('Back to reports', '返回报告列表')}</button>
    <header>
      <span>{t('Analysis report', '分析报告')}</span>
      <h2>{title}</h2>
      {report.subtitle ? <p>{String(reportLabel(report, 'subtitle', locale))}</p> : null}
      {report.summary ? <div className="analysis-report-summary">{String(report.summary)}</div> : null}
    </header>
    {sections.length ? <div className="analysis-report-sections">{sections.map((section, sectionIndex) => {
      const metrics = reportRecords(section.metrics);
      const charts = reportRecords(section.charts);
      const tables = reportRecords(section.tables);
      const insights = Array.isArray(section.insights) ? section.insights : [];
      const sectionLimitations = Array.isArray(section.limitations) ? section.limitations : [];
      return <section key={String(section.id || section.title || sectionIndex)}>
        <h3>{String(reportLabel(section, 'title', locale) || t(`Section ${sectionIndex + 1}`, `第 ${sectionIndex + 1} 部分`))}</h3>
        {section.summary ? <p>{String(section.summary)}</p> : null}
        {metrics.length ? <dl className="analysis-report-metrics">{metrics.map((metric, metricIndex) => <div key={`${String(metric.label)}-${metricIndex}`}><dt>{reportText(reportLabel(metric, 'label', locale), locale)}</dt><dd>{reportText(metric.value_i18n || ['图方向', '边权重', '节点特征', '弱连通', '强连通', '连通'].includes(String(metric.label)) ? reportLabel(metric, 'value', locale) : metric.value, locale)}{metric.unit ? <small>{String(reportLabel(metric, 'unit', locale))}</small> : null}</dd></div>)}</dl> : null}
        {charts.map((chart, chartIndex) => <ReportChart chart={chart} locale={locale} key={`${String(chart.title)}-${chartIndex}`} />)}
        {tables.map((table, tableIndex) => <ReportTable table={table} locale={locale} key={`${String(table.title)}-${tableIndex}`} />)}
        {insights.length ? <div className="analysis-report-notes"><strong>{t('Findings', '发现')}</strong><ul>{insights.map((insight, insightIndex) => <li key={insightIndex}>{reportText(insight, locale)}</li>)}</ul></div> : null}
        {sectionLimitations.length ? <div className="analysis-report-limit"><strong>{t('Notes', '说明')}</strong><ul>{sectionLimitations.map((item, itemIndex) => <li key={itemIndex}>{reportText(item, locale)}</li>)}</ul></div> : null}
      </section>;
    })}</div> : <div className="analysis-report-empty">{t('This report has no readable sections.', '这份报告没有可读的内容。')}</div>}
    {limitations.length ? <div className="analysis-report-limit analysis-report-root-limit"><strong>{t('Report notes', '报告说明')}</strong><ul>{limitations.map((item, itemIndex) => <li key={itemIndex}>{reportText(item, locale)}</li>)}</ul></div> : null}
  </article>;
}

function ArtifactsTab({ messages }: { messages: Message[] }) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
  const reports = messages.flatMap(message => (message.reports || []).map(report => ({ report: modernizeLegacyReport(report, message.tools || []), tools: message.tools || [] })));
  const [selectedReport, setSelectedReport] = useState<number | null>(null);
  if (!reports.length) return null;
  if (selectedReport != null && reports[selectedReport]) return <div className="tab-content analysis-report-content"><AnalysisReport report={reports[selectedReport].report} index={selectedReport} onBack={() => setSelectedReport(null)} /></div>;
  return <div className="tab-content"><div className="tab-title"><strong>{t('Artifacts', '分析产物')}</strong><span>{t(`${reports.length.toLocaleString(locale)} reports`, `${reports.length.toLocaleString(locale)} 份报告`)}</span></div><div className="workspace-list">{reports.map(({ report }, index) => {
    const type = String(report.intent || report.type || 'analysis');
    const title = String(recordValue(report.title_i18n)[language] || report.title || report.name || t(`Analysis report ${index + 1}`, `分析报告 ${index + 1}`));
    return <button type="button" className="workspace-list-row artifact-report-row" aria-label={t(`Open report: ${title}`, `查看报告：${title}`)} onClick={() => setSelectedReport(index)} key={`${String(report.id || report.title || 'report')}-${index}`}><div className="workspace-list-icon"><Icon name="file" size={15} /></div><div><strong>{title}</strong><span>{type === 'analysis' ? t('Analysis', '分析') : type}</span></div><b>{t('View', '查看')} <Icon name="arrow-right" size={13} /></b></button>;
  })}</div></div>;
}

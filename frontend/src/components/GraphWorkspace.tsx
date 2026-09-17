import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { GraphCanvas } from './GraphCanvas';
import { graphIndex, previewGraph } from '../graphPreview';
import { normalizeHighlights } from '../graphAppearance';
import { listProjectTrainingRuns } from '../api';
import type { GraphCollectionSummary, GraphData, GraphViewState, Highlight, Message, ProjectTrainingRun, ToolCall } from '../types';
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
  layoutVersion?: number;
  onStopJob?: StopJob;
  embedded?: boolean;
  onConstruct?: () => void;
  constructionLabel?: string;
  onGraphQuery?: (prompt: string) => void;
  onGraphReset?: () => void;
  graphQueryPending?: boolean;
};

function graphWorkspacePropsEqual(previous: GraphWorkspaceProps, next: GraphWorkspaceProps) {
  const coreEqual = previous.data === next.data
    && previous.view === next.view
    && previous.tab === next.tab
    && previous.selected === next.selected
    && previous.collapsed === next.collapsed
    && previous.layoutVersion === next.layoutVersion
    && previous.projectId === next.projectId
    && previous.onSelect === next.onSelect
    && previous.onExpand === next.onExpand
    && previous.onTab === next.onTab
    && previous.onToggle === next.onToggle
    && previous.sessionId === next.sessionId
    && previous.onGraphQuery === next.onGraphQuery
    && previous.onGraphReset === next.onGraphReset
    && previous.graphQueryPending === next.graphQueryPending
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

export const GraphWorkspace = memo(function GraphWorkspace({ data, view, datasetName, messages = [], liveTools = [], projectId = '', sessionId = '', tab, onTab, selected, onSelect, collapsed, onToggle, onExpand, layoutVersion = 0, onStopJob, embedded, onConstruct, constructionLabel, onGraphQuery, onGraphReset, graphQueryPending = false }: GraphWorkspaceProps) {
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
  const [labels, setLabels] = useState<'auto' | 'all' | 'hidden'>('auto');
  const [edgeWidth, setEdgeWidth] = useState(1);
  const [filterMatches, setFilterMatches] = useState(false);
  const [focusId, setFocusId] = useState('');
  const [depth, setDepth] = useState(0);
  const [fitVersion, setFitVersion] = useState(0);
  const [exploreOpen, setExploreOpen] = useState(false);
  const [resultsOnly, setResultsOnly] = useState(false);
  const [resultIndex, setResultIndex] = useState(-1);
  const [projectTrainingRuns, setProjectTrainingRuns] = useState<ProjectTrainingRun[]>([]);
  const [projectTrainingLoading, setProjectTrainingLoading] = useState(false);
  const [projectTrainingError, setProjectTrainingError] = useState('');
  const hasPendingLiveTraining = liveTools.some(tool => String(tool.tool || '') === 'train' && !tool.done);
  const layoutLabel = layout === 'force' ? t('Force-directed', '力导向') : layout === 'radial' ? t('Radial', '径向') : t('Circular', '环形');
  const communityById = useMemo(() => {
    const raw = renderedData?.community || renderedData?.communities;
    const mapping = raw?.node_community;
    return mapping && typeof mapping === 'object' ? new Map(Object.entries(mapping).map(([id, value]) => [String(id), String(value)])) : new Map<string, string>();
  }, [renderedData]);
  const allHighlights = useMemo(() => normalizeHighlights(renderedData?.highlights), [renderedData]);
  const highlights = useMemo(() => resultIndex >= 0 && allHighlights[resultIndex] ? [allHighlights[resultIndex]] : allHighlights, [allHighlights, resultIndex]);
  const index = useMemo(() => graphIndex(renderedData || { nodes: [], edges: [] }, t), [renderedData, t]);
  const preview = useMemo(() => previewGraph(index, highlights, query, filterMatches, connectedOnly, focusId, depth, resultsOnly),
    [index, highlights, query, filterMatches, connectedOnly, focusId, depth, resultsOnly]);
  useEffect(() => {
    const id = String(selected?.id ?? '');
    if (id && index.nodes.has(id) && !preview.nodes.some(node => node.id === id) && !filterMatches && !depth) setFocusId(id);
  }, [selected, index, preview.nodes, filterMatches, depth]);
  const schemaMode = renderedData?.visualization_kind === 'schema';
  const sourcePartial = !!renderedData && (renderedData.truncated === true || (renderedData.total_nodes ?? 0) > index.nodes.size || (renderedData.total_edges ?? 0) > index.edges.length);
  const selectedInfo = selected ? index.nodes.get(String(selected.id)) : undefined;
  const locate = (id: string) => { setFocusId(id); const node = index.nodes.get(id); if (node) onSelect(node); };
  useEffect(() => { setQuery(''); setFocusId(''); setDepth(0); setFilterMatches(false); setResultsOnly(false); setResultIndex(-1); setExploreOpen(false); }, [sessionId, datasetName]);
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
      {!tablesMode && <SlidingTabs ariaLabel={t('Graph workspace views', '图工作台视图')} className="workspace-tabs" indicatorClassName="workspace-tab-indicator" options={tabs.map(item => ({ value: item, label: t(TAB_LABELS[item][0], TAB_LABELS[item][1]) }))} value={tab} onChange={onTab} />}
      {tab === 'ML' ? <MLTab hasProject={Boolean(projectId)} onStopJob={onStopJob} messages={messages} liveTools={liveTools} projectRuns={projectTrainingRuns} sessionId={sessionId} loading={projectTrainingLoading} error={trainingError} />
        : tab === 'Artifacts' ? <><WorkspaceFiles key={sessionId} sessionId={sessionId} revision={JSON.stringify([messages.length, ...[...messages.flatMap(message => message.tools || []), ...liveTools].filter(tool => tool.tool?.startsWith('workspace_')).map(tool => [tool.call_id, tool.done, tool.result])])} /><ArtifactsTab messages={messages} /></>
          : view?.status === 'unsupported' ? (tab === 'Graph' ? <div className="graph-empty graph-unsupported"><div className="empty-icon small"><Icon name="graph" size={22} /></div><strong>{t('This graph collection cannot be visualized directly', '非独立图，无法直接可视化')}</strong></div> : <CollectionTab tab={tab} datasetName={datasetName} summary={view.summary} />)
            : view?.status === 'loading' ? <div className="graph-empty"><span className="loader" /><strong>{t('Loading graph', '正在加载图')}</strong></div>
              : view?.status === 'failed' ? <div className="graph-empty"><strong>{t('Failed to load graph', '图加载失败')}</strong><span>{view.error}</span></div>
                : !data ? <div className="graph-empty"><div className="empty-icon small"><BrandLogo /></div><strong>{t('No graph yet', '暂无图')}</strong><span>{t('Load data or request a graph to see analysis results here.', '加载数据或发送建图请求后，这里会显示图分析结果。')}</span></div>
                  : tab === 'Graph' ? <>
                    <div className="graph-toolbar">
                      <button onClick={() => setSearchOpen(current => !current)} aria-pressed={searchOpen}><Icon name="search" size={14} /> {t('Search nodes', '搜索节点')}</button>
                      {onGraphQuery && <button onClick={() => setExploreOpen(current => !current)} aria-expanded={exploreOpen}><Icon name="spark" size={14} />{t('Explore graph', '图探索')}</button>}
                      <button onClick={() => setLayout(current => current === 'force' ? 'radial' : current === 'radial' ? 'circle' : 'force')} title={t('Change graph layout', '切换图布局')}>{t(`Layout: ${layoutLabel}`, `布局：${layoutLabel}`)} <Icon name="chevron" size={12} /></button>
                      <button onClick={() => setConnectedOnly(current => !current)} aria-pressed={connectedOnly}>{connectedOnly ? t('Filter: connected', '过滤：有连接') : t('Filter', '过滤')}</button>
                      <button onClick={() => setFitVersion(current => current + 1)}>{t('Fit to canvas', '适应画布')}</button>
                      <button aria-expanded={displayOpen} onClick={() => setDisplayOpen(value => !value)}>{t('Display settings', '显示设置')}</button>
                    </div>
                    {exploreOpen && onGraphQuery && <GraphExplorePanel key={`${sessionId}:${datasetName || ''}`} schema={schemaMode} selectedId={String(selected?.id ?? '')} pending={graphQueryPending} onQuery={prompt => { resetGraphFilters(); onGraphQuery(prompt); }} />}
                    {!schemaMode && (allHighlights.length > 0 || onGraphReset) && <div className="graph-result-controls">
                      {allHighlights.length > 0 && <><span>{t('Analysis results', '分析结果')}</span>{allHighlights.length > 1 && <select aria-label={t('Result layer', '结果图层')} value={resultIndex} onChange={event => { setResultIndex(Number(event.target.value)); setFitVersion(current => current + 1); }}><option value={-1}>{t('All results', '全部结果')}</option>{allHighlights.map((item, index) => <option key={index} value={index}>{item.label || t(`Result ${index + 1}`, `结果 ${index + 1}`)}</option>)}</select>}<button aria-pressed={resultsOnly} onClick={() => { setResultsOnly(current => !current); setDepth(0); setFilterMatches(false); setConnectedOnly(false); setFocusId(''); onSelect(null); setFitVersion(current => current + 1); }}>{t('Result subgraph only', '仅看结果子图')}</button></>}
                      {onGraphReset && <button disabled={graphQueryPending} onClick={() => { resetGraphFilters(); onGraphReset(); }}>{t('Restore full graph', '恢复全图')}</button>}
                    </div>}
                    {displayOpen && <div className="graph-display-settings">
                      <label>{t('Node size', '节点大小')}<select value={sizeMode} onChange={e => setSizeMode(e.target.value as typeof sizeMode)}><option value="degree">{t('By degree', '按度数')}</option><option value="uniform">{t('Uniform', '统一大小')}</option></select></label>
                      <label>{t('Node labels', '节点标签')}<select value={labels} onChange={e => setLabels(e.target.value as typeof labels)}><option value="auto">{t('Automatic', '自动')}</option><option value="all">{t('All', '全部')}</option><option value="hidden">{t('Hidden', '隐藏')}</option></select></label>
                      <label>{t('Edge width', '边粗细')}<input aria-label={t('Edge width', '边粗细')} type="range" min="0.6" max="2" step="0.2" value={edgeWidth} onChange={e => setEdgeWidth(Number(e.target.value))} /></label>
                      <button onClick={() => { setSizeMode('degree'); setLabels('auto'); setEdgeWidth(1); }}>{t('Restore defaults', '恢复默认')}</button>
                    </div>}
                    {searchOpen ? <div className="graph-search-group"><div className="graph-search"><Icon name="search" size={14} /><input autoFocus value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { const id = preview.matches.values().next().value; if (id) locate(id); } }} placeholder={t('Search by node ID or name', '搜索节点 ID 或名称')} /><span>{t(`${preview.matches.size.toLocaleString(locale)} results`, `${preview.matches.size.toLocaleString(locale)} 个结果`)}</span></div>
                      <label className="graph-filter-match"><input type="checkbox" checked={filterMatches} onChange={event => setFilterMatches(event.target.checked)} />{t('Show matches only', '仅显示匹配项')}</label>
                      {query.trim() && <div className="graph-search-results">{[...preview.matches].slice(0, 8).map(id => <button key={id} onClick={() => locate(id)}>{String(index.nodes.get(id)?.label ?? id)}<small>{id}</small></button>)}{!preview.matches.size && <span>{t('No matching nodes in the loaded data', '当前已加载数据中没有匹配节点')}</span>}</div>}
                    </div> : null}
                    <div className={`graph-canvas ${schemaMode ? 'is-schema' : ''}`}><GraphCanvas schema={schemaMode} active={!collapsed} key={`${sessionId}:${datasetName || ''}`} preview={preview} directed={renderedData?.directed} communities={communityById} highlights={highlights} selected={selected} onSelect={node => { onSelect(node); if (!node) { setFocusId(''); setDepth(0); } }} layout={layout} layoutVersion={layoutVersion} sizeMode={sizeMode} labels={labels} edgeWidth={edgeWidth} fitVersion={fitVersion} focusId={focusId} />{!preview.nodes.length && <div className="graph-preview-empty">{t('No nodes match the current filters', '没有符合条件的节点')}</div>}</div>
                    <div className="graph-preview-note graph-overview-status" role="status">
                      {tablesMode
                        ? t(`Table relationships · ${preview.nodes.length.toLocaleString(locale)} / ${index.nodes.size.toLocaleString(locale)} tables · ${preview.edges.length.toLocaleString(locale)} / ${index.edges.length.toLocaleString(locale)} foreign keys`, `表关联模式 · ${preview.nodes.length.toLocaleString(locale)} / ${index.nodes.size.toLocaleString(locale)} 张表 · ${preview.edges.length.toLocaleString(locale)} / ${index.edges.length.toLocaleString(locale)} 条外键关系`)
                        : schemaMode
                        ? t(`Graph Schema · ${preview.nodes.length.toLocaleString(locale)} node types · ${preview.edges.length.toLocaleString(locale)} relationships`, `Graph Schema · ${preview.nodes.length.toLocaleString(locale)} 种节点类型 · ${preview.edges.length.toLocaleString(locale)} 种关系`)
                        : <>{resultsOnly && t('Result subgraph · ', '结果子图 · ')}{sourcePartial
                          ? t(`Partial upstream graph · Loaded ${index.nodes.size.toLocaleString(locale)} / ${(renderedData?.total_nodes ?? renderedData?.num_nodes ?? index.nodes.size).toLocaleString(locale)} nodes · `, `上游部分图 · 已载入 ${index.nodes.size.toLocaleString(locale)} / ${(renderedData?.total_nodes ?? renderedData?.num_nodes ?? index.nodes.size).toLocaleString(locale)} 个节点 · `)
                          : preview.nodes.length < index.nodes.size || preview.edges.length < index.edges.length ? t('Smart overview · ', '智能概览 · ') : !resultsOnly ? t('Current graph view · ', '当前图视图 · ') : ''}
                          {t(`Showing ${preview.nodes.length.toLocaleString(locale)} / ${(renderedData?.total_nodes ?? renderedData?.num_nodes ?? index.nodes.size).toLocaleString(locale)} nodes · ${preview.edges.length.toLocaleString(locale)} / ${(renderedData?.total_edges ?? renderedData?.num_edges ?? index.edges.length).toLocaleString(locale)} edges`, `显示 ${preview.nodes.length.toLocaleString(locale)} / ${(renderedData?.total_nodes ?? renderedData?.num_nodes ?? index.nodes.size).toLocaleString(locale)} 个节点 · ${preview.edges.length.toLocaleString(locale)} / ${(renderedData?.total_edges ?? renderedData?.num_edges ?? index.edges.length).toLocaleString(locale)} 条边`)}
                          {(preview.nodes.length < index.nodes.size || preview.edges.length < index.edges.length) && t(' · Local smart overview (bounded preview)', ' · 本地智能概览（有界预览）')}
                          {depth > 0 && t(` · ${depth} hop neighborhood`, ` · ${depth} 跳邻域`)}
                          {sizeMode === 'degree' && t(` · Size: ${preview.nodes[0]?.degreeLabel || 'Degree'}`, ` · 大小：${preview.nodes[0]?.degreeLabel || '度数'}`)}</>}
                      {preview.omittedHighlights > 0 && t(` · ${preview.omittedHighlights.toLocaleString(locale)} highlighted nodes are outside the preview`, ` · ${preview.omittedHighlights.toLocaleString(locale)} 个高亮节点超出预览范围`)}
                      {schemaMode && !tablesMode && (preview.nodes.length < index.nodes.size || preview.edges.length < index.edges.length) && t(` · Bounded preview of ${index.nodes.size.toLocaleString(locale)} node types and ${index.edges.length.toLocaleString(locale)} relationships`, ` · 有界预览，共 ${index.nodes.size.toLocaleString(locale)} 种节点类型、${index.edges.length.toLocaleString(locale)} 种关系`)}
                    </div>
                    {(communityById.size || highlights.length) ? <div className="graph-legend"><span className="graph-legend-title">{t('Legend', '图例')}</span>{communityById.size ? <span><i className="legend-dot community" />{t('Community colors', '社区着色')}</span> : null}{highlights.filter(item => item.type === 'node').length ? <span><i className="legend-dot node-highlight" />{t('Highlighted nodes', '节点高亮')}</span> : null}{highlights.filter(item => item.type === 'path').length ? <span><i className="legend-line path-highlight" />{t('Highlighted paths', '路径高亮')}</span> : null}{highlights.filter(item => item.type === 'subgraph').length ? <span><i className="legend-line subgraph-highlight" />{t('Highlighted subgraphs', '子图高亮')}</span> : null}</div> : null}
                    <div className="node-inspector">{selected ? <><div className="inspector-title"><strong>{String(selected.label ?? selected.id ?? t('Selected node', '已选节点'))}</strong><span>{t('Selected', '已选择')}</span></div><div className="inspector-row"><span>{t('Node ID', '节点 ID')}</span><strong>{String(selected.id ?? '-')}</strong></div><div className="inspector-row"><span>{t('Type', '类型')}</span><strong>{String(nodePropertyType(selectedInfo || selected) ?? t('Node', '节点'))}</strong></div><div className="inspector-row"><span>{selectedInfo?.degreeLabel || t('Degree', '度数')}</span><strong>{selectedInfo?.degree ?? '-'}</strong></div>{!schemaMode && <NodeProperties node={selectedInfo || selected} />}<div className="graph-neighborhood"><label>{t('View range', '查看范围')}<select aria-label={t('Neighborhood range', '查看邻域范围')} value={depth} onChange={event => { setFocusId(String(selected.id)); setDepth(Number(event.target.value)); }}><option value="0">{t('Full preview', '全部预览')}</option><option value="1">{t('1-hop neighborhood', '一跳邻域')}</option><option value="2">{t('2-hop neighborhood', '两跳邻域')}</option></select></label><span>{t('Neighborhood uses loaded data', '邻域基于已加载数据')}</span></div><div className="inspector-actions"><button onClick={() => onExpand?.(selected)} disabled={!onExpand}><Icon name="plus" size={13} /> {t('Expand neighbors', '扩展邻居')}</button><button onClick={() => navigator.clipboard?.writeText(String(selected.id ?? ''))}><Icon name="copy" size={13} /> {t('Copy ID', '复制 ID')}</button></div></> : <span>{t('Select a node to view its properties', '点击图中节点查看属性')}</span>}</div>
                  </>
                    : <WorkspaceTab tab={tab} data={data} messages={messages} previewNodes={preview.nodes.length} previewEdges={preview.edges.length} />}
    </div>
  </aside>;
}, graphWorkspacePropsEqual);

function displayCount(value: unknown, locale: string) {
  const count = Number(value);
  return Number.isFinite(count) ? count.toLocaleString(locale) : '-';
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

function WorkspaceTab({ tab, data, messages, previewNodes, previewEdges }: { tab: Tab; data: GraphData; messages: Message[]; previewNodes: number; previewEdges: number }) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
  if (tab === 'Overview') {
    const totalNodes = data.total_nodes ?? data.num_nodes ?? data.nodes.length;
    const totalEdges = data.total_edges ?? data.num_edges ?? data.edges.length;
    const renderedNodes = previewNodes;
    return <div className="overview-content"><div className="overview-hero"><span className="eyebrow">{t('Current graph', '当前图')}</span><h2>{t('Current dataset', '当前数据集')}</h2><strong>{t('Graph analysis ready', '图分析已就绪')}</strong></div><div className="stat-grid"><div><span>{t('Nodes', '节点数')}</span><strong>{totalNodes.toLocaleString(locale)}</strong></div><div><span>{t('Edges', '边数')}</span><strong>{totalEdges.toLocaleString(locale)}</strong></div><div><span>{t('Direction', '方向')}</span><strong>{data.directed ? t('Directed', '有向图') : t('Undirected', '无向图')}</strong></div><div><span>{t('Canvas nodes', '画布节点')}</span><strong>{renderedNodes.toLocaleString(locale)}</strong></div></div>{renderedNodes < totalNodes || previewEdges < totalEdges ? <div className="workspace-note"><Icon name="spark" size={15} /> {t(`The canvas shows ${renderedNodes.toLocaleString(locale)} nodes and ${previewEdges.toLocaleString(locale)} edges; dataset totals remain complete.`, `画布当前展示 ${renderedNodes.toLocaleString(locale)} 个节点、${previewEdges.toLocaleString(locale)} 条边，数据集总量统计保持完整。`)}</div> : null}</div>;
  }
  if (tab === 'Data') return <div className="tab-content"><div className="tab-title"><strong>{t('Node and edge data', '节点与边数据')}</strong><span>{t(`${(data.total_nodes ?? data.num_nodes ?? data.nodes.length).toLocaleString(locale)} nodes`, `${(data.total_nodes ?? data.num_nodes ?? data.nodes.length).toLocaleString(locale)} 个节点`)}</span></div><div className="data-list">{data.nodes.slice(0, 20).map((node, index) => <div key={index}><Icon name="project" size={14} /><span>{String(node.label ?? node.id ?? t(`Node ${index + 1}`, `节点 ${index + 1}`))}</span><small>{String(node.type ?? t('Node', '节点'))}</small></div>)}</div>{data.nodes.length < (data.total_nodes ?? data.num_nodes ?? data.nodes.length) ? <div className="workspace-note">{t(`This is a visualization preview with ${data.nodes.length.toLocaleString(locale)} loaded node samples.`, `当前为可视化预览，已加载 ${data.nodes.length.toLocaleString(locale)} 个节点样本。`)}</div> : null}</div>;
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

function metricNumber(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatMetric(key: string, value: unknown, locale: string) {
  const number = metricNumber(value);
  if (number === null) return String(value ?? '-');
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
  const { language, t } = useI18n();
  const locale = localeFor(language);
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
  return <div className="ml-chart-panel"><div className="ml-chart-heading"><strong>{title}</strong><div>{series.map((item, index) => <span key={item.key}><i style={{ backgroundColor: CHART_COLORS[index % CHART_COLORS.length] }} />{metricLabel(item.key, t)}</span>)}</div></div><svg className="ml-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={t(`${title} training curve`, `${title}训练曲线`)}>
    {[0, .25, .5, .75, 1].map(fraction => { const value = maxY - (maxY - minY) * fraction; const py = top + (height - top - bottom) * fraction; return <g key={fraction}><line x1={left} x2={width - right} y1={py} y2={py} /><text x={left - 7} y={py + 4} textAnchor="end">{formatMetric(keys[0], value, locale)}</text></g>; })}
    <text x={left} y={height - 7}>{t(`Epoch ${minX}`, `第 ${minX} 轮`)}</text><text x={width - right} y={height - 7} textAnchor="end">{t(`Epoch ${maxX}`, `第 ${maxX} 轮`)}</text>
    {series.map((item, index) => { const points = item.values.map(point => `${x(point.epoch)},${y(point.value)}`).join(' '); return <g key={item.key}><polyline points={points} style={{ stroke: CHART_COLORS[index % CHART_COLORS.length] }} />{item.values.map(point => <circle key={point.epoch} cx={x(point.epoch)} cy={y(point.value)} r={item.values.length < 25 ? 2.4 : 1.4} style={{ fill: CHART_COLORS[index % CHART_COLORS.length] }} />)}</g>; })}
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
  const jobId = String(result.job_id || recordValue(run.progress).job_id || '');
  const terminal = ['done', 'completed', 'cancelled', 'failed'].includes(String(result.job_status || result.status || ''));
  if (!onStopJob || !jobId || !sessionId || terminal || status === 'confirmed') return status === 'confirmed' ? <span role="status">{t('Job ended', '作业已结束')}</span> : null;
  const pending = status === 'stopping';
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
  const jobStatus = String(result.job_status || result.status || progress.status || '');
  const statusLabel = projectRun.status_unavailable ? t('Status unavailable', '状态不可用') : jobStatus === 'cancelled' ? t('Stopped', '已终止') : jobStatus === 'cancelling' ? t('Stopping', '正在停止') : jobStatus === 'failed' ? t('Failed', '失败') : run.done ? (metrics.stopped_early ? t('Stopped early', '提前停止') : t('Completed', '已完成')) : t('Training', '训练中');
  return <article className="ml-run-card"><header className="ml-run-header"><div className="workspace-list-icon"><Icon name="cube" size={16} /></div><div><strong>{taskLabel}</strong><span>{model}{totalEpochs ? t(` · ${completedEpochs}/${totalEpochs} epochs`, ` · ${completedEpochs}/${totalEpochs} 轮`) : ''}{source ? ` · ${source}` : ''}</span></div><b className={projectRun.status_unavailable ? 'unavailable' : run.done ? 'done' : 'running'}>{statusLabel}</b></header>
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
    const statusLabel = run.status_unavailable ? t('Status unavailable', '状态不可用') : run.done ? t('Completed', '已完成') : t('Training', '训练中');
    return <tr key={run.training_run_id || `${run.session_id}-${index}`}><td>{model}</td><td title={run.session_title}>{run.session_title || t('New conversation', '新会话')}</td><td>{run.dataset_name || '-'}</td><td><span className={run.status_unavailable ? 'ml-status-unavailable' : run.done ? 'ml-status-done' : 'ml-status-running'}>{statusLabel}</span></td><td>{trainingRunEpoch(run) || '-'}</td><td>{formatMetric('val_acc', metrics.val_acc, locale)}</td><td>{formatMetric('test_acc', metrics.test_acc, locale)}</td><td>{formatMetric('best_epoch', metrics.best_epoch, locale)}</td></tr>;
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
    {effectiveScope === 'current' && otherRuns.length ? <div className="ml-activity"><strong>{t('Data and inference activity', '数据与推理记录')}</strong><div className="workspace-list">{otherRuns.map((run, index) => <div className="workspace-list-row" key={`${run.call_id || run.tool}-${index}`}><div className="workspace-list-icon"><Icon name="cube" size={15} /></div><div><strong>{String(run.tool) === 'infer' ? t('Inference', '推理') : String(run.tool) === 'load_graph_classification_dataset' ? t('Load graph classification dataset', '加载图分类数据集') : String(run.tool)}</strong><span>{run.done ? t('Completed', '已完成') : t('Running', '运行中')}</span></div><b>{run.done ? t('Done', '完成') : t('Running', '运行中')}</b><JobStopButton run={run} sessionId={sessionId} onStopJob={onStopJob} /></div>)}</div></div> : null}</div>;
}

function ArtifactsTab({ messages }: { messages: Message[] }) {
  const { language, t } = useI18n();
  const locale = localeFor(language);
  const reports = messages.flatMap(message => message.reports || []);
  if (!reports.length) return null;
  return <div className="tab-content"><div className="tab-title"><strong>{t('Artifacts', '分析产物')}</strong><span>{t(`${reports.length.toLocaleString(locale)} reports`, `${reports.length.toLocaleString(locale)} 份报告`)}</span></div><div className="workspace-list">{reports.map((report, index) => { const type = String(report.intent || report.type || 'analysis'); return <div className="workspace-list-row" key={`${String(report.id || report.title || 'report')}-${index}`}><div className="workspace-list-icon"><Icon name="file" size={15} /></div><div><strong>{String(report.title || report.name || t(`Analysis report ${index + 1}`, `分析报告 ${index + 1}`))}</strong><span>{type === 'analysis' ? t('Analysis', '分析') : type}</span></div></div>; })}</div></div>;
}

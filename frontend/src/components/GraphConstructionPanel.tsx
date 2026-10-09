import { useExampleLabels } from '../userCases';
import { Popup, PopupBackdrop } from './PopupMotion';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, getConstructionModels, renameProjectDataset, buildDatasetGraph, commitConstruction, getConstructionGraph, getConstructionSchema, getConstructionState, previewConstruction } from '../api';
import { useI18n } from '../i18n';
import type { Translate } from '../i18n';
import { isGraphData } from '../types';
import type { ConstructionBuildOptions, ConstructionDomainCategory, ConstructionGraph, ConstructionOperation, ConstructionResult, ConstructionSchemaPropertyPath, ConstructionSchemaResponse, ConstructionState, Dataset, GraphData } from '../types';
import { GraphWorkspace } from './GraphWorkspace';
import { Icon } from './Icon';
import { DatasetDescriptionEditor } from './DatasetDescriptionEditor';
import { ConstructionModelSettings } from './ConstructionModelSettings';
import { ConstructionSchemaFields } from './ConstructionSchemaFields';
import { TableSchemaPreview } from './TableSchemaPreview';
import { SourceDataPreview } from './SourceDataPreview';
import { constructionSourceTables, tableRelationGraph } from '../sourceSchema';
import { constructionProperties as properties } from '../constructionProperties';
import './GraphConstructionPanel.css';

type Rule = ConstructionOperation['op'];
type LocalizedText = { en: string; zh: string };
type SchemaWarning = '' | 'version_mismatch' | 'unavailable';
const DOMAIN_CATEGORIES: Array<{ value: ConstructionDomainCategory; label: LocalizedText }> = [
  { value: 'auto', label: { en: 'Auto-detect', zh: '自动识别' } },
  { value: 'finance', label: { en: 'Finance', zh: '金融' } },
  { value: 'academic', label: { en: 'Academic', zh: '学术' } },
  { value: 'commerce', label: { en: 'E-commerce', zh: '电商' } },
  { value: 'healthcare', label: { en: 'Healthcare', zh: '医疗' } },
  { value: 'social', label: { en: 'Social', zh: '社区' } },
  { value: 'general', label: { en: 'General', zh: '通用' } },
];
const RULES: Array<{ id: Rule; label: LocalizedText; description: LocalizedText }> = [
  { id: 'collapse_node', label: { en: 'Convert node to edge', zh: '节点转成边' }, description: { en: 'Compress an intermediate node used only for connections into a direct relationship to reduce traversal hops.', zh: '把只承担连接作用的中间节点压缩成直接关系，减少遍历跳数。' } },
  { id: 'node_property_to_edge', label: { en: 'Move node property to edge', zh: '节点属性转成边属性' }, description: { en: 'Move a node property needed during traversal to the selected related edges.', zh: '把遍历时需要读取的节点属性移动到选中的关联边。' } },
  { id: 'extract_property_node', label: { en: 'Extract node property as node', zh: '节点属性提取为独立节点' }, description: { en: 'Model a shared property, such as a phone number, as a separate node to make related entities easier to find.', zh: '将电话等共享属性建模为独立节点，便于查找关联实体。' } },
  { id: 'delete', label: { en: 'Delete redundant nodes, edges, or properties', zh: '删除冗余节点、边、属性' }, description: { en: 'Select the content to delete, preview the impact, and then save.', zh: '明确选择需要删除的内容，先预览影响再保存。' } },
  { id: 'reverse_edges', label: { en: 'Reverse edge direction', zh: '调整边方向' }, description: { en: 'Reverse the selected edges so their direction matches common query traversals.', zh: '反转所选边，使方向适配常用查询的遍历方式。' } },
  { id: 'rename_type', label: { en: 'Rename node or edge type', zh: '重命名节点、边类型' }, description: { en: 'Replace the selected type name throughout the graph so it matches your domain language.', zh: '统一替换选中类型的名称，让图数据符合业务语义。' } },
];

function rawProperties(item: Record<string, unknown> | undefined) {
  const value = item?.properties;
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function display(value: unknown) { return typeof value === 'object' ? JSON.stringify(value) : String(value ?? ''); }
function instanceNodeName(node: Record<string, unknown>, t: Translate) {
  const id = String(node.id ?? '');
  const data = rawProperties(node).data;
  const name = properties(node).name ?? (data && typeof data === 'object' && !Array.isArray(data) ? (data as Record<string, unknown>).name : undefined);
  if (id.startsWith('entity_') && typeof name === 'string' && name.trim()) return name.trim();
  if (!id.startsWith('row_')) return id;
  const type = String(node.type || t('Node', '节点'));
  const rowIndex = rawProperties(node).row_index;
  return Number.isInteger(rowIndex) && Number(rowIndex) >= 0
    ? t(`${type} · row ${Number(rowIndex) + 1}`, `${type} · 第 ${Number(rowIndex) + 1} 行`)
    : id;
}
function failure(cause: unknown, t: Translate) { return cause instanceof Error ? cause.message : t('The operation failed. Please try again.', '操作失败，请重试'); }
function operationLabel(operation: ConstructionOperation, t: Translate) {
  const label = RULES.find(rule => rule.id === operation.op)?.label;
  return label ? t(label.en, label.zh) : operation.op;
}
function localizedSchemaLabel(value: unknown, t: Translate) {
  const label = String(value ?? '');
  if (label === '未分类节点') return t('Unclassified node', '未分类节点');
  if (label === '未知节点类型') return t('Unknown node type', '未知节点类型');
  if (label === '关系') return t('Relationship', '关系');
  return label;
}
function idsFromSelect(select: HTMLSelectElement) { return Array.from(select.selectedOptions, option => option.value); }
function elapsedLabel(seconds: number | undefined, t: Translate) {
  if (!Number.isFinite(seconds)) return '';
  const value = Math.max(0, Math.floor(seconds || 0));
  return value < 60
    ? t(`${value} sec`, `${value} 秒`)
    : t(`${Math.floor(value / 60)} min ${value % 60} sec`, `${Math.floor(value / 60)} 分 ${value % 60} 秒`);
}
function progressQueryCount(state: ConstructionState) {
  const value = state.progress?.metrics?.query_count;
  return Number.isInteger(value) && Number(value) >= 1 && Number(value) <= 100 ? String(value) : null;
}

function endpointId(value: unknown) {
  if (value && typeof value === 'object' && !Array.isArray(value) && 'id' in value) return String((value as { id: unknown }).id);
  return String(value ?? '');
}
const SCHEMA_PROPERTY_TYPES = ['null', 'boolean', 'integer', 'number', 'string', 'object', 'array'] as const;
function compareSchemaText(left: string, right: string) { return left < right ? -1 : left > right ? 1 : 0; }
function schemaValueType(value: unknown) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  if (typeof value === 'number') return Number.isInteger(value) ? 'integer' : 'number';
  return typeof value;
}
function schemaPropertyPath(parent: string, key: string) {
  if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) return parent ? `${parent}.${key}` : key;
  const quoted = JSON.stringify(key);
  return parent ? `${parent}[${quoted}]` : `[${quoted}]`;
}
function summarizePropertyPaths(records: Array<Record<string, unknown>>): ConstructionSchemaPropertyPath[] {
  const summary = new Map<string, { types: Map<string, number>; present: number }>();
  for (const record of records) {
    const perRecord = new Map<string, Map<string, number>>();
    const visit = (value: unknown, path: string) => {
      const item = perRecord.get(path) || new Map<string, number>();
      const type = schemaValueType(value);
      item.set(type, (item.get(type) || 0) + 1);
      perRecord.set(path, item);
      if (Array.isArray(value)) value.forEach(child => visit(child, `${path}[]`));
      else if (value && typeof value === 'object') {
        Object.keys(value as Record<string, unknown>).sort(compareSchemaText).forEach(key =>
          visit((value as Record<string, unknown>)[key], schemaPropertyPath(path, key)));
      }
    };
    const recordProperties = rawProperties(record);
    Object.keys(recordProperties).sort(compareSchemaText).forEach(key => visit(recordProperties[key], schemaPropertyPath('', key)));
    perRecord.forEach((types, path) => {
      const aggregate = summary.get(path) || { types: new Map<string, number>(), present: 0 };
      aggregate.present += 1;
      types.forEach((count, type) => aggregate.types.set(type, (aggregate.types.get(type) || 0) + count));
      summary.set(path, aggregate);
    });
  }
  return Array.from(summary, ([path, item]) => ({
    path,
    types: [...SCHEMA_PROPERTY_TYPES.filter(type => item.types.has(type)), ...Array.from(item.types.keys()).filter(type => !SCHEMA_PROPERTY_TYPES.includes(type as typeof SCHEMA_PROPERTY_TYPES[number])).sort(compareSchemaText)],
    type_counts: Object.fromEntries([...SCHEMA_PROPERTY_TYPES.filter(type => item.types.has(type)), ...Array.from(item.types.keys()).filter(type => !SCHEMA_PROPERTY_TYPES.includes(type as typeof SCHEMA_PROPERTY_TYPES[number])).sort(compareSchemaText)].map(type => [type, item.types.get(type)!])),
    present_count: item.present,
    value_count: Array.from(item.types.values()).reduce((total, count) => total + count, 0),
  })).sort((left, right) => compareSchemaText(left.path, right.path));
}
function deriveConstructionSchema(dataset: Dataset, revision: number, graph: GraphData): ConstructionSchemaResponse {
  const nodeGroups = new Map<string, Array<Record<string, unknown>>>();
  const nodeTypeById = new Map<string, string>();
  graph.nodes.forEach(node => {
    const typeName = String(node.type || '未分类节点');
    nodeTypeById.set(String(node.id), typeName);
    const group = nodeGroups.get(typeName) || [];
    group.push(node); nodeGroups.set(typeName, group);
  });
  const edgeGroups = new Map<string, { typeName: string; sourceType: string; targetType: string; records: Array<Record<string, unknown>> }>();
  graph.edges.forEach(edge => {
    const typeName = String(edge.type || '关系');
    let sourceType = nodeTypeById.get(endpointId(edge.source)) || '未知节点类型';
    let targetType = nodeTypeById.get(endpointId(edge.target)) || '未知节点类型';
    if (graph.directed === false && compareSchemaText(targetType, sourceType) < 0) [sourceType, targetType] = [targetType, sourceType];
    const key = JSON.stringify([sourceType, typeName, targetType]);
    const group = edgeGroups.get(key) || { typeName, sourceType, targetType, records: [] };
    group.records.push(edge); edgeGroups.set(key, group);
    if (!nodeGroups.has(sourceType)) nodeGroups.set(sourceType, []);
    if (!nodeGroups.has(targetType)) nodeGroups.set(targetType, []);
  });
  const nodeTypes = Array.from(nodeGroups, ([typeName, records]) => ({
    id: typeName, type: typeName, type_name: typeName, instance_count: records.length, property_paths: summarizePropertyPaths(records),
  })).sort((left, right) => compareSchemaText(left.type, right.type));
  const edgeTypes = Array.from(edgeGroups.values(), group => ({
    id: `schema_edge:${encodeURIComponent(group.sourceType)}:${encodeURIComponent(group.typeName)}:${encodeURIComponent(group.targetType)}`,
    type: group.typeName, type_name: group.typeName, source_type: group.sourceType, target_type: group.targetType,
    relationship_count: group.records.length, property_paths: summarizePropertyPaths(group.records),
  })).sort((left, right) => compareSchemaText(`${left.source_type}\u0000${left.type}\u0000${left.target_type}`, `${right.source_type}\u0000${right.type}\u0000${right.target_type}`));
  return {
    dataset_id: dataset.dataset_id,
    revision,
    schema: {
      directed: graph.directed,
      node_count: graph.nodes.length,
      edge_count: graph.edges.length,
      node_type_count: nodeTypes.length,
      edge_type_count: edgeTypes.length,
      node_types: nodeTypes,
      edge_types: edgeTypes,
      graph: {
        directed: graph.directed,
        visualization_kind: 'schema',
        nodes: nodeTypes.map(nodeType => ({
          id: nodeType.id,
          type: 'schema_node',
          label: nodeType.type,
          properties: { schema_kind: 'node_type', ...nodeType },
        })),
        edges: edgeTypes.map(edgeType => ({
          id: edgeType.id,
          source: edgeType.source_type,
          target: edgeType.target_type,
          type: edgeType.type,
          label: edgeType.type,
          properties: { schema_kind: 'edge_type', ...edgeType },
        })),
      },
    },
  };
}
function validSchemaResponse(value: ConstructionSchemaResponse, dataset: Dataset, revision: number) {
  return value?.dataset_id === dataset.dataset_id && value.revision === revision && isGraphData(value.schema?.graph)
    && value.schema.graph.visualization_kind === 'schema'
    && Array.isArray(value.schema?.node_types) && Array.isArray(value.schema?.edge_types);
}
async function resolveConstructionSchema(dataset: Dataset, revision: number, graph: GraphData, signal?: AbortSignal): Promise<{ response: ConstructionSchemaResponse; warning: SchemaWarning }> {
  try {
    const response = await getConstructionSchema(dataset, signal);
    if (!validSchemaResponse(response, dataset, revision)) {
      return { response: deriveConstructionSchema(dataset, revision, graph), warning: 'version_mismatch' as SchemaWarning };
    }
    return {
      response: { ...response, schema: { ...response.schema, graph: { ...response.schema.graph, visualization_kind: 'schema' } } },
      warning: '',
    };
  } catch (cause) {
    if (signal?.aborted) throw cause;
    return { response: deriveConstructionSchema(dataset, revision, graph), warning: 'unavailable' as SchemaWarning };
  }
}
async function loadReadyAssets(dataset: Dataset, signal?: AbortSignal): Promise<{ snapshot: ConstructionGraph; schema: ConstructionSchemaResponse; warning: SchemaWarning }> {
  const graphPromise = getConstructionGraph(dataset, signal);
  const schemaPromise = getConstructionSchema(dataset, signal);
  const [graphResult, schemaResult] = await Promise.allSettled([graphPromise, schemaPromise]);
  if (graphResult.status === 'rejected') throw graphResult.reason;
  const snapshot = graphResult.value;
  if (schemaResult.status === 'fulfilled' && validSchemaResponse(schemaResult.value, dataset, snapshot.revision)) {
    return {
      snapshot,
      schema: { ...schemaResult.value, schema: { ...schemaResult.value.schema, graph: { ...schemaResult.value.schema.graph, visualization_kind: 'schema' } } },
      warning: '',
    };
  }
  if (signal?.aborted) throw (schemaResult.status === 'rejected' ? schemaResult.reason : new DOMException('Aborted', 'AbortError'));
  return {
    snapshot,
    schema: deriveConstructionSchema(dataset, snapshot.revision, snapshot.graph),
    warning: schemaResult.status === 'fulfilled'
      ? 'version_mismatch'
      : 'unavailable',
  };
}

function ConstructionAgentStream({ state, mode }: { state: ConstructionState | null; mode: 'rules' | 'llm' }) {
  const { t } = useI18n();
  const progress = state?.progress;
  const events = (progress?.events || []).slice(-8);
  const metrics = progress?.metrics || {};
  const currentStageEvent = [...events].reverse().find(event => event.kind === 'stage' && event.stage === progress?.stage)?.id;
  const elapsed = elapsedLabel(progress?.elapsed_seconds, t);
  return <section className="construction-agent-stream" aria-live="polite" aria-label={t('Graph construction progress', '构图进度')}>
    <div className="construction-agent-stream-head"><div><span className="construction-agent-live" /><strong>{t('Graph construction progress', '构图进度')}</strong><small>{t('Running', '运行中')}</small></div>{elapsed && <time>{t(`Running for ${elapsed}`, `已运行 ${elapsed}`)}</time>}</div>
    <p className="construction-agent-current">{progress?.current_message || state?.message || t('Graph construction has started.', '构图任务已启动。')}</p>
    <ol>{events.length ? events.map(event => <li key={event.id} className={`${event.outcome || event.kind} ${event.id === currentStageEvent ? 'active' : ''}`}><span>{event.id === currentStageEvent ? <i className="construction-agent-pulse" /> : event.outcome === 'rejected' ? '—' : <Icon name="check" size={12} />}</span><p>{event.message}</p></li>) : <li className="stage active"><span><i className="construction-agent-pulse" /></span><p>{t('Graph construction has started.', '构图任务已启动。')}</p></li>}</ol>
    {(metrics.evaluated_candidates !== undefined || metrics.iterations !== undefined || metrics.chunks_completed !== undefined || metrics.chunks_total !== undefined || metrics.extracted_nodes !== undefined || metrics.extracted_edges !== undefined) && <div className="construction-agent-metrics">
      {metrics.iterations !== undefined && <span>{t('Iterations', '迭代')} <b>{metrics.iterations}{metrics.max_iterations ? ` / ${metrics.max_iterations}` : ''}</b></span>}
      {metrics.evaluated_candidates !== undefined && <span>{t('Candidates', '候选')} <b>{metrics.evaluated_candidates}</b></span>}
      {metrics.accepted_candidates !== undefined && <span>{t('Accepted', '已采用')} <b>{metrics.accepted_candidates}</b></span>}
      {metrics.tool_calls !== undefined && <span>{t('Tool calls', '工具调用')} <b>{metrics.tool_calls}</b></span>}
      {(metrics.chunks_completed !== undefined || metrics.chunks_total !== undefined) && <span>{t('Text chunks', '文本分块')} <b>{metrics.chunks_completed ?? 0}{metrics.chunks_total !== undefined ? ` / ${metrics.chunks_total}` : ''}</b></span>}
      {metrics.extracted_nodes !== undefined && <span>{t('Entities found', '已识别实体')} <b>{metrics.extracted_nodes}</b></span>}
      {metrics.extracted_edges !== undefined && <span>{t('Relationships found', '已识别关系')} <b>{metrics.extracted_edges}</b></span>}
      {metrics.queries_generated !== undefined && <span>{t('Text queries', '原文查询')} <b>{metrics.queries_generated}</b></span>}
      {metrics.queries_passed !== undefined && <span>{t('Queries passed', '查询通过')} <b>{metrics.queries_passed}</b></span>}
      {metrics.queries_repaired !== undefined && <span>{t('Facts repaired', '已修正事实')} <b>{metrics.queries_repaired}</b></span>}
    </div>}
    <small className="construction-agent-disclosure">{(progress?.mode || mode) === 'rules' ? t('Building directly from primary and foreign keys, without automatic optimization.', '直接按主键和外键构图，不进行自动优化。') : t('Only progress and validation results are shown. Agent reasoning is never displayed.', '这里只展示运行进度与验收结果，不展示 Agent 内部推理。')}</small>
  </section>;
}

type Props = {
  dataset: Dataset;
  onClose: () => void;
  onSaved: (dataset: Dataset, graph: GraphData, revision: number) => void;
  onStateChange?: (dataset: Dataset, state: ConstructionState) => void;
  onUse?: (dataset: Dataset) => Promise<void>;
  onDatasetUpdated?: (dataset: Dataset) => void;
  embedded?: boolean;
};

export function GraphConstructionPanel({ dataset, onClose, onSaved, onStateChange, onUse, onDatasetUpdated, embedded = false }: Props) {
  const { language, t } = useI18n();
  const labels = useExampleLabels();
  const translation = useRef(t);
  translation.current = t;
  const formatNumber = (value: number) => value.toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US');
  const [datasetName, setDatasetName] = useState(dataset.name);
  const [nameInput, setNameInput] = useState(dataset.name);
  const [renaming, setRenaming] = useState(false);
  const [constructionModel, setConstructionModel] = useState('deepseek-v4-pro');
  const [constructionModels, setConstructionModels] = useState(['deepseek-v4-pro', 'claude-opus-5-5']);
  const [modelSettingsDirty, setModelSettingsDirty] = useState(false);
  const [state, setState] = useState<ConstructionState | null>(null);
  const [savedGraph, setSavedGraph] = useState<GraphData | null>(null);
  const [savedSchema, setSavedSchema] = useState<ConstructionSchemaResponse | null>(null);
  const [schemaWarning, setSchemaWarning] = useState<SchemaWarning>('');
  const [preview, setPreview] = useState<ConstructionResult | null>(null);
  const [operations, setOperations] = useState<ConstructionOperation[]>([]);
  const [busy, setBusy] = useState('loading');
  const [monitoringBuild, setMonitoringBuild] = useState(false);
  const [queryCount, setQueryCount] = useState('15');
  const [domain, setDomain] = useState('');
  const [domainCategory, setDomainCategory] = useState<ConstructionDomainCategory>('auto');
  const [constructionMode, setConstructionMode] = useState<'rules' | 'llm'>('rules');
  const [experienceEnabled, setExperienceEnabled] = useState(true);
  const [delimiter, setDelimiter] = useState('');
  const [entityTypes, setEntityTypes] = useState('');
  const [textRefineEnabled, setTextRefineEnabled] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const [tab, setTab] = useState<'Overview' | 'Graph' | 'Data' | 'ML' | 'Artifacts'>('Graph');
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
  const [schemaSelected, setSchemaSelected] = useState<Record<string, unknown> | null>(null);
  const [schemaView, setSchemaView] = useState<'tables' | 'optimized' | 'instances'>('tables');
  const [rule, setRule] = useState<Rule>('collapse_node');
  const [nodeIds, setNodeIds] = useState<string[]>([]);
  const [edgeIds, setEdgeIds] = useState<string[]>([]);
  const [property, setProperty] = useState('');
  const [edgeType, setEdgeType] = useState('');
  const [nodeType, setNodeType] = useState('');
  const [targetProperty, setTargetProperty] = useState('');
  const [removeProperty, setRemoveProperty] = useState(true);
  const [target, setTarget] = useState<'node' | 'edge'>('node');
  const [deleteMode, setDeleteMode] = useState<'entity' | 'property'>('entity');
  const [cascade, setCascade] = useState(false);
  const [oldType, setOldType] = useState('');
  const [newType, setNewType] = useState('');
  const [search, setSearch] = useState('');
  const dialog = useRef<HTMLElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const alive = useRef(true);
  const lifecycle = useRef(0);
  const postPending = useRef(false);
  const buildAttempt = useRef(0);
  const buildResolved = useRef(false);
  const currentBuildId = useRef<string | null>(null);
  const notifiedRevision = useRef<number | null>(null);
  const callbacks = useRef({ dataset, onSaved, onStateChange });
  callbacks.current = { dataset, onSaved, onStateChange };
  const isBuilding = state?.status === 'building' || monitoringBuild || busy === 'build';
  const sourceType = (state?.source_type || dataset.file_type).replace('.', '').toLowerCase();
  const supportsDelimiter = sourceType === 'csv' || sourceType === 'txt';
  const semanticTextMode = sourceType === 'txt' && delimiter === '__entities__';
  const editingBusy = Boolean(busy) || isBuilding;
  const graph = preview?.graph || savedGraph;
  const previewSchema = useMemo(() => preview
    ? deriveConstructionSchema(dataset, preview.revision, preview.graph)
    : null, [dataset.dataset_id, preview]);
  const activeSchema = previewSchema || savedSchema;
  const sourceTables = useMemo(() => ['sql', 'csv'].includes(sourceType)
    ? constructionSourceTables(dataset, savedSchema) : [], [sourceType, dataset, savedSchema]);
  const tableGraph = useMemo(() => sourceTables.length ? tableRelationGraph(sourceTables) : null, [sourceTables]);
  const showTableSchema = Boolean(tableGraph) && schemaView === 'tables';
  const showInstanceGraph = sourceType === 'txt' && schemaView !== 'optimized';
  const instanceGraph = useMemo<GraphData | null>(() => graph ? {
    directed: graph.directed,
    nodes: graph.nodes.map(node => ({ ...node, label: instanceNodeName(node, t) })),
    edges: graph.edges.map(edge => ({ ...edge, label: String(edge.type || '') })),
    num_nodes: graph.nodes.length, num_edges: graph.edges.length,
  } : null, [graph, t]);
  const canvasGraph = showInstanceGraph ? instanceGraph : activeSchema?.schema.graph || null;
  const localizedCanvasGraph = useMemo<GraphData | null>(() => canvasGraph ? {
    ...canvasGraph,
    nodes: canvasGraph.nodes.map(node => ({ ...node, label: localizedSchemaLabel(node.label, t) })),
    edges: canvasGraph.edges.map(edge => ({ ...edge, label: localizedSchemaLabel(edge.label, t) })),
  } : null, [canvasGraph, t]);
  const nodes = useMemo(() => graph?.nodes || [], [graph]);
  const edges = useMemo(() => graph?.edges || [], [graph]);
  const nodeById = useMemo(() => new Map(nodes.map(node => [String(node.id), node])), [nodes]);
  const endpointName = useCallback((id: unknown) => {
    const node = nodeById.get(String(id));
    return node ? instanceNodeName(node, t) : String(id);
  }, [nodeById, t]);
  const selectedNode = nodeById.get(nodeIds[0]);
  const activeRule = RULES.find(item => item.id === rule)!;
  const availableProperties = useMemo(() => {
    const records = rule === 'delete' && target === 'edge' ? edges.filter(edge => edgeIds.includes(String(edge.id))) : nodes.filter(node => nodeIds.includes(String(node.id)));
    return Array.from(new Set(records.flatMap(record => Object.keys(properties(record))))).sort();
  }, [rule, target, nodes, edges, nodeIds, edgeIds]);
  const types = useMemo(() => Array.from(new Set((target === 'node' ? nodes : edges).map(item => String(item.type || '')))).filter(Boolean).sort(), [target, nodes, edges]);
  const currentSelection = selected ? nodeById.get(String(selected.id)) : undefined;

  const load = useCallback(async (signal?: AbortSignal, quiet = false, publish = false) => {
    const version = lifecycle.current;
    const attempt = buildAttempt.current;
    if (!quiet) { setBusy('loading'); setError(''); }
    try {
      const nextState = await getConstructionState(dataset, signal);
      const assets = ['ready', 'draft'].includes(nextState.status) ? await loadReadyAssets(dataset, signal) : null;
      const snapshot = assets?.snapshot || null;
      if (signal?.aborted || !alive.current || version !== lifecycle.current || attempt !== buildAttempt.current) return null;
      // A later status response must not replace a completed graph or its edits.
      if (quiet && publish && buildResolved.current) return nextState;
      if (quiet && publish) {
        if (currentBuildId.current && nextState.build_id && currentBuildId.current !== nextState.build_id) return null;
        // Until the POST or a building snapshot identifies this attempt, an
        // earlier failure cannot resolve it or discard its eventual success.
        if (postPending.current && nextState.status !== 'building' && !['ready', 'draft'].includes(nextState.status) &&
            (!currentBuildId.current || nextState.build_id !== currentBuildId.current)) return null;
        if (nextState.status === 'building' && nextState.build_id) currentBuildId.current = nextState.build_id;
      }
      const publishedState = snapshot ? { ...nextState, revision: snapshot.revision } : nextState;
      const restoredQueryCount = progressQueryCount(nextState);
      if (restoredQueryCount !== null) setQueryCount(restoredQueryCount);
      if (nextState.progress?.mode) setConstructionMode(nextState.progress.mode);
      setState(publishedState);
      callbacks.current.onStateChange?.(callbacks.current.dataset, publishedState);
      const waiting = nextState.status === 'building' || nextState.status === 'not_built' && postPending.current;
      setMonitoringBuild(waiting);
      setError(nextState.status === 'failed' ? nextState.message || translation.current('Graph construction failed. The source file is preserved; adjust the settings and try again.', '构图失败，源文件已保留。可调整参数后重试。') : '');
      if (!quiet || snapshot) {
        setSavedGraph(snapshot?.graph || null); setSavedSchema(assets?.schema || null); setSchemaWarning(assets?.warning || '');
        setPreview(null); setOperations([]); setConflict(false);
        setSelected(null); setSchemaSelected(null); setNodeIds([]); setEdgeIds([]); setProperty('');
      }
      if (waiting) setNotice(translation.current('Graph construction is running on the server. You can close this window and reopen it later to view the result.', '构图正在服务器上运行。可以关闭此窗口，稍后重新打开查看结果。'));
      else if (publish && snapshot) {
        buildResolved.current = true; postPending.current = false;
        setBusy(previous => previous === 'build' ? '' : previous);
        if (nextState.status === 'ready' && notifiedRevision.current !== snapshot.revision) {
          notifiedRevision.current = snapshot.revision;
          callbacks.current.onSaved(callbacks.current.dataset, snapshot.graph, snapshot.revision);
        }
        setNotice(nextState.status === 'draft' ? translation.current('Graph draft ready. Save the construction result to use it for analysis.', '构图草稿已生成，请点击保存构图结果后用于分析。') : translation.current('The graph has been saved to this dataset and is ready for future use.', '构图已保存到此数据集，后续可直接使用。'));
      } else if (nextState.status === 'failed' || nextState.status === 'algorithm_unavailable') {
        buildResolved.current = true; postPending.current = false;
        setBusy(previous => previous === 'build' ? '' : previous);
        if (nextState.status === 'algorithm_unavailable') setNotice(nextState.message || translation.current('Graph construction is not available for this file type yet. The source file is preserved.', '此文件类型的构图算法暂不可用，源文件已保留。'));
      } else if (publish && nextState.status === 'not_built') {
        setNotice(translation.current('The server has not produced a graph yet. Review the settings and start construction manually.', '服务器尚未生成构图结果。可确认参数后手动开始构图。'));
      }
      return nextState;
    } catch (cause) {
      if (!signal?.aborted && alive.current && version === lifecycle.current && attempt === buildAttempt.current) {
        const reason = failure(cause, translation.current);
        setError(quiet
          ? translation.current(`Unable to read the construction status: ${reason}. Status checks will continue without starting another build.`, `暂时无法读取构图状态：${reason}。将继续查询状态，不会重复启动构图。`)
          : reason);
      }
      return null;
    } finally { if (!quiet && !signal?.aborted && alive.current && version === lifecycle.current) setBusy(''); }
  }, [dataset.dataset_id, dataset.project_id]);

  useEffect(() => { setDatasetName(dataset.name); setNameInput(dataset.name); }, [dataset.name]);
  useEffect(() => {
    const controller = new AbortController();
    void getConstructionModels(controller.signal).then(result => {
      if (!controller.signal.aborted && result.models.length) {
        setConstructionModels(result.models); setConstructionModel(result.default_model);
      }
    }).catch(() => undefined);
    return () => controller.abort();
  }, [dataset.dataset_id, dataset.file_type]);
  async function renameDataset() {
    const name = nameInput.trim();
    if (!name || name.length > 120) { setError(t('Enter a name of 1–120 characters.', '请输入 1 到 120 字的数据集名称。')); return; }
    setBusy('rename'); setError('');
    try {
      const result = await renameProjectDataset(dataset.project_id, dataset.dataset_id, name);
      if (!alive.current) return;
      setDatasetName(result.dataset.name); setRenaming(false); onDatasetUpdated?.(result.dataset);
    } catch (cause) { if (alive.current) handleFailure(cause); }
    finally { if (alive.current) setBusy(''); }
  }

  useEffect(() => {
    alive.current = true;
    lifecycle.current += 1;
    postPending.current = false; buildResolved.current = false; notifiedRevision.current = null;
    currentBuildId.current = null;
    buildAttempt.current += 1;
    setState(null); setSavedGraph(null); setSavedSchema(null); setSchemaWarning(''); setMonitoringBuild(false); setNotice(''); setQueryCount('15'); setDomain('');
    setSelected(null); setSchemaSelected(null); setSchemaView('tables');
    setDomainCategory('auto'); setConstructionMode('rules'); setExperienceEnabled(true); setDelimiter(''); setEntityTypes(''); setTextRefineEnabled(false);
    const controller = new AbortController();
    const priorFocus = document.activeElement as HTMLElement | null;
    if (!embedded) closeButton.current?.focus();
    void load(controller.signal, false, dataset.status !== 'ready');
    return () => { alive.current = false; lifecycle.current += 1; controller.abort(); priorFocus?.focus(); };
  }, [load, embedded]);

  useEffect(() => {
    if (!monitoringBuild) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await load(controller.signal, true, true);
      if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 3000);
    };
    timer = setTimeout(() => void poll(), 3000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [monitoringBuild, load]);

  function requestClose() {
    if (busy && busy !== 'loading' && busy !== 'build') return;
    if (operations.length) setConfirmClose(true);
    else onClose();
  }
  function handleFailure(cause: unknown) {
    if (cause instanceof ApiError && (cause.code === 'revision_conflict' || cause.code === 'REVISION_CONFLICT')) {
      setConflict(true);
      setError(t('This dataset was updated elsewhere. Reload the latest version before editing; this preview has not been saved.', '数据集已在其他位置更新。请重新加载最新版本后再编辑，本次预览尚未保存。'));
    } else setError(failure(cause, t));
  }
  async function build() {
    if (postPending.current || isBuilding || modelSettingsDirty || !state || state.algorithm_available === false) return;
    const count = Number(queryCount);
    if (!semanticTextMode && constructionMode === 'llm' && (!Number.isInteger(count) || count < 1 || count > 100)) {
      setError(t('The number of validation queries must be an integer from 1 to 100.', '验证查询数量必须是 1 到 100 之间的整数。')); return;
    }
    if (semanticTextMode && textRefineEnabled && (!Number.isInteger(count) || count < 1 || count > 100)) {
      setError(t('The number of validation queries must be an integer from 1 to 100.', '验证查询数量必须是 1 到 100 之间的整数。')); return;
    }
    const selectedEntityTypes = [...new Set(entityTypes.split(/[,，、;；\n]+/).map(value => value.trim()).filter(Boolean))];
    if (semanticTextMode && (selectedEntityTypes.length > 12 || selectedEntityTypes.some(value => value.length > 30 || /[|｜<>\r]/.test(value)))) {
      setError(t('Enter up to 12 entity types, each no longer than 30 characters and without |, ｜, < or >.', '最多填写 12 种实体类型，每种不超过 30 字，且不能包含 |、｜、< 或 >。')); return;
    }
    const version = lifecycle.current;
    const attempt = ++buildAttempt.current;
    postPending.current = true; buildResolved.current = false; notifiedRevision.current = null;
    currentBuildId.current = null;
    setMonitoringBuild(true);
    setBusy('build'); setError(''); setNotice('');
    // Keep the state payload locale-neutral. ConstructionAgentStream supplies
    // the localized startup copy while the server has not returned a message.
    const optimisticState: ConstructionState = { ...state, status: 'building', message: undefined };
    setState(optimisticState);
    callbacks.current.onStateChange?.(callbacks.current.dataset, optimisticState);
    try {
      // No AbortSignal: closing this view must not cancel the server-side build.
      const sourceDescription = domain.trim() || dataset.description?.trim() || '';
      const options: ConstructionBuildOptions = semanticTextMode
        ? { domain: sourceDescription, txt_mode: 'entities', mode: 'llm', entity_types: selectedEntityTypes, construction_model: constructionModel, text_refine_enabled: textRefineEnabled, ...(textRefineEnabled ? { query_count: count } : {}) }
        : constructionMode === 'llm'
          ? { mode: 'llm', construction_model: constructionModel, query_count: count, domain: sourceDescription, domain_category: domainCategory, experience_enabled: experienceEnabled }
          : { mode: 'rules', domain: sourceDescription, experience_enabled: false };
      if (!semanticTextMode && sourceType === 'txt' && delimiter === '__lines__') options.txt_mode = 'lines';
      else if (!semanticTextMode && supportsDelimiter && delimiter) options.delimiter = delimiter;
      const result = await buildDatasetGraph(dataset, options);
      if (!alive.current || version !== lifecycle.current || attempt !== buildAttempt.current || buildResolved.current) return;
      postPending.current = false;
      currentBuildId.current = result.build_id || null;
      if (['ready', 'draft'].includes(result.status)) {
        const assets = await loadReadyAssets(dataset);
        const snapshot = assets.snapshot;
        if (!alive.current || version !== lifecycle.current || attempt !== buildAttempt.current || buildResolved.current) return;
        buildResolved.current = true; setMonitoringBuild(false);
        const completedState = { ...result, revision: snapshot.revision };
        setState(completedState); setSavedGraph(snapshot.graph); setSavedSchema(assets.schema); setSchemaWarning(assets.warning);
        setSchemaSelected(null);
        callbacks.current.onStateChange?.(callbacks.current.dataset, completedState);
        if (result.status === 'ready' && notifiedRevision.current !== snapshot.revision) {
          notifiedRevision.current = snapshot.revision;
          onSaved(dataset, snapshot.graph, snapshot.revision);
        }
        setNotice(result.status === 'draft' ? t('Graph draft ready. Save the construction result to use it for analysis.', '构图草稿已生成，请点击保存构图结果后用于分析。') : t('The graph has been saved to this dataset and is ready for future use.', '构图已保存到此数据集，后续可直接使用。'));
      } else {
        setState(result); setMonitoringBuild(result.status === 'building');
        callbacks.current.onStateChange?.(callbacks.current.dataset, result);
        if (result.status === 'failed') setError(result.message || t('Graph construction failed. The source file is preserved.', '构图失败，源文件已保留。'));
        else setNotice(result.message || (result.status === 'building'
          ? t('Graph construction is running on the server. You can close this window and check again later.', '构图正在服务器上运行，可以关闭此窗口后再查看。')
          : t('The source file is preserved. You can continue later.', '源文件已保留，可稍后继续。')));
      }
    } catch (cause) {
      if (!alive.current || version !== lifecycle.current || attempt !== buildAttempt.current || buildResolved.current) return;
      postPending.current = false;
      if (cause instanceof ApiError && ['construction_capacity_exhausted', 'construction_build_conflict', 'construction_busy'].includes(cause.code || '')) {
        buildResolved.current = true;
        setMonitoringBuild(false); setNotice(''); setState(state);
        callbacks.current.onStateChange?.(callbacks.current.dataset, state);
        handleFailure(cause);
      } else if (cause instanceof ApiError && (cause.status === 501 || cause.code === 'algorithm_unavailable')) {
        setMonitoringBuild(false);
        handleFailure(cause);
        const unavailable: ConstructionState = { ...state, status: 'algorithm_unavailable', algorithm_available: false };
        setState(unavailable);
        callbacks.current.onStateChange?.(callbacks.current.dataset, unavailable);
      } else {
        const uncertain = !(cause instanceof ApiError) || cause.status >= 500 || cause.code === 'construction_running_unconfirmed';
        setMonitoringBuild(true);
        setNotice(uncertain
          ? t('The connection was interrupted, but construction may still be running on the server. Checking the existing task status.', '请求连接已中断，服务器可能仍在构图。正在查询已有任务状态。')
          : t('Confirming the graph construction result on the server.', '正在确认服务器上的构图结果。'));
        const latest = await load(undefined, true, true);
        if (alive.current && version === lifecycle.current && attempt === buildAttempt.current && latest?.status !== 'building' && latest?.status !== 'ready' && latest?.status !== 'draft') {
          if (!uncertain) handleFailure(cause);
        }
      }
    } finally {
      if (alive.current && version === lifecycle.current && attempt === buildAttempt.current) { postPending.current = false; setBusy(previous => previous === 'build' ? '' : previous); }
    }
  }
  function makeOperation(): ConstructionOperation {
    const requireValue = (value: string, message: string) => { if (!value.trim()) throw new Error(message); return value.trim(); };
    const requireIds = (ids: string[], message: string) => { if (!ids.length) throw new Error(message); return ids; };
    if (rule === 'collapse_node') return { op: rule, node_id: requireIds(nodeIds, t('Select one intermediate node.', '请选择一个中间节点'))[0], edge_type: requireValue(edgeType, t('Enter a new relationship type.', '请输入新关系类型')) };
    if (rule === 'node_property_to_edge') return { op: rule, node_id: requireIds(nodeIds, t('Select a source node.', '请选择来源节点'))[0], property: requireValue(property, t('Select a node property.', '请选择节点属性')), edge_ids: requireIds(edgeIds, t('Select the related edges that should receive the property.', '请选择接收属性的关联边')), ...(targetProperty.trim() ? { target_property: targetProperty.trim() } : {}), remove_from_node: removeProperty };
    if (rule === 'extract_property_node') return { op: rule, node_ids: requireIds(nodeIds, t('Select at least one source node.', '请选择来源节点')), property: requireValue(property, t('Select a node property.', '请选择节点属性')), node_type: requireValue(nodeType, t('Enter a new node type.', '请输入新节点类型')), edge_type: requireValue(edgeType, t('Enter a new relationship type.', '请输入新关系类型')), remove_from_node: removeProperty };
    if (rule === 'reverse_edges') return { op: rule, edge_ids: requireIds(edgeIds, t('Select the edges to reverse.', '请选择需要反转的边')) };
    if (rule === 'rename_type') return { op: rule, target, old_type: requireValue(oldType, t('Select the original type.', '请选择原类型')), new_type: requireValue(newType, t('Enter the new type name.', '请输入新类型名称')) };
    return { op: rule, target, ids: requireIds(target === 'node' ? nodeIds : edgeIds, t('Select the objects to delete.', '请选择需要删除的对象')), ...(deleteMode === 'property' ? { property: requireValue(property, t('Select the property to delete.', '请选择要删除的属性')) } : { cascade }) };
  }
  async function updatePreview(next: ConstructionOperation[]) {
    if (!state) return;
    if (!next.length) { setOperations([]); setPreview(null); setSelected(null); setSchemaSelected(null); setNotice(t('Restored the saved version.', '已还原到保存的版本。')); return; }
    setBusy('preview'); setError(''); setNotice('');
    try {
      const result = await previewConstruction(dataset, state.revision, next);
      if (!alive.current) return;
      setOperations(next); setPreview(result); setSelected(null); setSchemaSelected(null); setNodeIds([]); setEdgeIds([]); setProperty('');
      setTab('Graph');
    } catch (cause) { if (alive.current) handleFailure(cause); }
    finally { if (alive.current) setBusy(''); }
  }
  async function save() {
    if (!state || (!operations.length && state.status !== 'draft') || conflict) return;
    setBusy('save'); setError(''); setNotice('');
    try {
      const result = await commitConstruction(dataset, state.revision, operations);
      if (!alive.current) return;
      const completedState: ConstructionState = { ...state, revision: result.revision, status: 'ready', node_count: result.graph.nodes.length, edge_count: result.graph.edges.length };
      const schemaResult = await resolveConstructionSchema(dataset, result.revision, result.graph);
      if (!alive.current) return;
      setState(completedState);
      callbacks.current.onStateChange?.(callbacks.current.dataset, completedState);
      setSavedGraph(result.graph); setSavedSchema(schemaResult.response); setSchemaWarning(schemaResult.warning);
      setPreview(null); setOperations([]); setSelected(null); setSchemaSelected(null);
      onSaved(dataset, result.graph, result.revision);
      setNotice(t(`Version ${result.revision} saved. This dataset is ready for use in future conversations.`, `已保存版本 ${result.revision}。该数据集可在后续会话中直接使用。`));
    } catch (cause) { if (alive.current) handleFailure(cause); }
    finally { if (alive.current) setBusy(''); }
  }
  async function useDataset() {
    if (!graph || !state || !onUse) return;
    setBusy('use'); setError('');
    try { await onUse({ ...dataset, status: 'ready' }); if (alive.current) onClose(); }
    catch (cause) { if (alive.current) handleFailure(cause); }
    finally { if (alive.current) setBusy(''); }
  }
  function switchRule(next: Rule) { setRule(next); setError(''); setProperty(''); setEdgeIds([]); }
  const wantsNodes = ['collapse_node', 'node_property_to_edge', 'extract_property_node'].includes(rule) || rule === 'delete' && target === 'node';
  const wantsEdges = ['node_property_to_edge', 'reverse_edges'].includes(rule) || rule === 'delete' && target === 'edge';
  const wantsProperty = ['node_property_to_edge', 'extract_property_node'].includes(rule) || rule === 'delete' && deleteMode === 'property';
  const selectableEdges = useMemo(() => rule === 'node_property_to_edge'
    ? edges.filter(edge => String(edge.source) === nodeIds[0] || String(edge.target) === nodeIds[0]) : edges,
  [rule, edges, nodeIds]);
  const searchText = search.trim().toLowerCase();
  const filteredNodes = useMemo(() => !searchText ? nodes : nodes.filter(node =>
    `${node.id} ${node.type} ${instanceNodeName(node, t)} ${display(properties(node))}`.toLowerCase().includes(searchText)), [nodes, searchText, t]);
  const filteredEdges = useMemo(() => !searchText ? selectableEdges : selectableEdges.filter(edge =>
    `${edge.id} ${edge.source} ${edge.target} ${edge.type} ${endpointName(edge.source)} ${endpointName(edge.target)}`.toLowerCase().includes(searchText)), [selectableEdges, searchText, endpointName]);
  const inspectedEdges = useMemo(() => {
    if (!edgeIds.length) return [];
    const selectedIds = new Set(edgeIds);
    return edges.filter(edge => selectedIds.has(String(edge.id))).slice(0, 10);
  }, [edges, edgeIds]);
  const schemaNodeTypeCount = activeSchema?.schema.node_type_count ?? activeSchema?.schema.node_types.length ?? 0;
  const schemaEdgeTypeCount = activeSchema?.schema.edge_type_count ?? activeSchema?.schema.edge_types.length ?? 0;
  const selectedSchemaProperties = rawProperties(schemaSelected || undefined);
  const selectedSchemaTypeName = localizedSchemaLabel(selectedSchemaProperties.type_name || schemaSelected?.label || schemaSelected?.id || '', t);
  const selectedSchemaPaths = Array.isArray(selectedSchemaProperties.property_paths)
    ? selectedSchemaProperties.property_paths.filter((item): item is ConstructionSchemaPropertyPath => Boolean(item && typeof item === 'object' && !Array.isArray(item) && 'path' in item))
    : [];
  const schemaWarningText = schemaWarning === 'version_mismatch'
    ? t('The Graph Schema version is not synchronized yet. A local schema overview was generated from the current graph; optimization and saving are unaffected.', 'Graph Schema 版本尚未同步，已从当前实例图生成本地结构概览；图数据优化与保存不受影响。')
    : schemaWarning === 'unavailable'
      ? t('The Graph Schema service is temporarily unavailable. A local schema overview was generated from the current graph; optimization and saving are unaffected.', 'Graph Schema 接口暂时不可用，已从当前实例图生成本地结构概览；图数据优化与保存不受影响。')
      : '';

  const Backdrop = embedded ? 'div' : PopupBackdrop;
  const Panel = embedded ? 'section' : Popup.section;
  return <Backdrop className={embedded ? 'construction-page' : 'construction-backdrop'}><Panel className={`construction-panel ${embedded ? 'is-embedded' : ''} ${graph ? 'has-graph' : ''}`} role={embedded ? 'region' : 'dialog'} aria-modal={embedded ? undefined : true} aria-labelledby="construction-title" ref={dialog} onKeyDown={event => {
    if (embedded) return;
    if (event.key === 'Escape') { event.stopPropagation(); requestClose(); }
    if (event.key === 'Tab') {
      const focusable = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]') || []).filter(element => element.getClientRects().length > 0);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  }}>
    <header className="construction-header"><div className="construction-heading"><span className="construction-mark"><Icon name="schema" size={23} /></span><div><span className="construction-eyebrow">{t('Graph Construction / Data Source', '构图工作台 / 数据源')}</span><h2 id="construction-title">{labels.name(datasetName)}</h2>{renaming ? <form className="construction-rename" onSubmit={event => { event.preventDefault(); void renameDataset(); }}><input aria-label={t('Dataset name', '数据集名称')} maxLength={120} value={nameInput} onChange={event => setNameInput(event.target.value)} autoFocus /><button type="submit" disabled={editingBusy}>{t('Save name', '保存名称')}</button><button type="button" onClick={() => setRenaming(false)}>{t('Cancel', '取消')}</button></form> : <button className="construction-preset-link" disabled={editingBusy} onClick={() => { setNameInput(datasetName); setRenaming(true); }}>{t('Rename dataset', '重命名数据集')}</button>}<p>{state?.source_filename || dataset.original_filename}</p></div></div><div className="construction-header-actions"><span className={`construction-status ${graph && !isBuilding ? 'ready' : ''}`}>{busy === 'loading' ? t('Loading', '读取中') : isBuilding ? t('Building graph', '构图进行中') : state?.status === 'draft' ? t('Draft · Unsaved', '草稿 · 待保存') : graph ? t(`Saved · Version ${state?.revision}`, `已保存 · 版本 ${state?.revision}`) : state?.status === 'failed' ? t('Construction failed', '构图失败') : t('Waiting to build', '等待构图')}</span>{embedded ? <button ref={closeButton} className="secondary-command construction-back-button" disabled={Boolean(busy) && busy !== 'loading' && busy !== 'build'} onClick={requestClose}><Icon name="arrow-left" size={16} /> {t('Back to construction', '返回构图')}</button> : <button ref={closeButton} className="plain-icon" aria-label={t('Close graph construction workspace', '关闭构图工作台')} disabled={Boolean(busy) && busy !== 'loading' && busy !== 'build'} onClick={requestClose}><Icon name="close" size={20} /></button>}</div></header>
    {(error || notice || conflict) && <div className={`construction-banner ${error ? 'error' : ''}`} role={error ? 'alert' : 'status'}><span>{error || notice}</span>{conflict && <button className="secondary-command" disabled={editingBusy} onClick={() => { setNotice(''); void load(); }}>{t('Reload latest version', '重新加载最新版本')}</button>}{error && !conflict && (!state || isBuilding || state.status === 'failed') && <button className="secondary-command" disabled={busy === 'loading'} onClick={() => void load(undefined, isBuilding, isBuilding)}>{isBuilding ? t('Check status again', '重新查询状态') : t('Retry', '重试')}</button>}</div>}
    {confirmClose && <div className="construction-banner warning" role="alert"><span>{embedded
      ? t(`${operations.length} preview ${operations.length === 1 ? 'change has' : 'changes have'} not been saved. Returning will discard ${operations.length === 1 ? 'it' : 'them'}.`, `预览中的 ${operations.length} 项修改尚未保存。返回后将丢弃这些修改。`)
      : t(`${operations.length} preview ${operations.length === 1 ? 'change has' : 'changes have'} not been saved. Closing will discard ${operations.length === 1 ? 'it' : 'them'}.`, `预览中的 ${operations.length} 项修改尚未保存。关闭后将丢弃这些修改。`)}</span><button className="secondary-command" onClick={() => setConfirmClose(false)}>{t('Keep editing', '继续编辑')}</button><button className="secondary-command" onClick={onClose}>{embedded ? t('Discard and return', '丢弃并返回') : t('Discard and close', '丢弃并关闭')}</button></div>}
    <div className="construction-body">
      <aside className="construction-controls">
        <DatasetDescriptionEditor dataset={dataset} onSaved={onDatasetUpdated} disabled={editingBusy} />
        <ConstructionModelSettings key={dataset.dataset_id} model={constructionModel} models={constructionModels} disabled={editingBusy}
          onModelChange={setConstructionModel} onDirtyChange={setModelSettingsDirty} onSavedModel={model => {
            setConstructionModel(model); setConstructionModels(current => [...new Set([...current, model])]);
          }} />
        <div className="construction-source"><div><Icon name="file" size={18} /><strong>{t('Source file', '源文件')}</strong><b>{(state?.source_type || dataset.file_type).replace('.', '').toUpperCase()}</b></div>{!graph && <>
          <fieldset className="construction-build-options" disabled={editingBusy || !state || state.algorithm_available === false}>
            {!semanticTextMode && <><label className="construction-check"><input type="checkbox" role="switch" checked={constructionMode === 'llm'} onChange={event => setConstructionMode(event.target.checked ? 'llm' : 'rules')} aria-describedby="construction-mode-help" />{t('Use Agent to optimize the graph', '使用 Agent 优化构图')}</label><small id="construction-mode-help">{constructionMode === 'llm' ? t('Agent optimizes and validates the graph built from rules.', 'Agent 会优化并验证规则化方法构建的图。') : t('Build directly from primary and foreign keys (PK/FK), without automatic optimization.', '仅按主键和外键（PK/FK）构图，不进行自动优化。')}</small></>}
            {!semanticTextMode && constructionMode === 'llm' && <label>{t('Validation queries', '验证查询数量')}<input type="number" min={1} max={100} step={1} value={queryCount} onChange={event => setQueryCount(event.target.value)} /></label>}
            {!semanticTextMode && constructionMode === 'llm' && <label>{t('Domain category', '领域类别')}<select value={domainCategory} onChange={event => setDomainCategory(event.target.value as ConstructionDomainCategory)}>{DOMAIN_CATEGORIES.map(category => <option key={category.value} value={category.value}>{t(category.label.en, category.label.zh)}</option>)}</select></label>}
            {sourceType === 'txt' && <button type="button" className="construction-preset-link" onClick={() => {
              setDelimiter('__entities__');
              setEntityTypes('人物');
              setDomain('抽取小说全文中明确出现的所有人物作为节点，人物之间明确陈述的关系作为边；保留没有明确关系的人物，不把地点、组织或事件作为人物，不编造关系。');
            }}><span>{t('Novel character relationships', '小说人物关系')}</span><Icon name="arrow-right" size={15} /></button>}
            {supportsDelimiter && <label>{sourceType === 'txt' ? t('TXT processing mode', 'TXT 处理方式') : t('Text delimiter (optional)', '文本分隔符（选填）')}<select aria-label={sourceType === 'txt' ? t('TXT processing mode', 'TXT 处理方式') : t('Text delimiter (optional)', '文本分隔符（选填）')} value={delimiter} onChange={event => setDelimiter(event.target.value)}><option value="">{sourceType === 'txt' ? t('Auto: tabular when tab-separated, otherwise one record per line', '自动：制表符表格，否则逐行记录') : t('Comma (default)', '默认逗号')}</option>{sourceType === 'txt' && <option value="__entities__">{t('Extract entities and relationships with AI (LLM)', '智能抽取实体与关系（LLM）')}</option>}{sourceType === 'txt' && <option value="__lines__">{t('Force one record per line (do not infer relationships)', '强制逐行记录（不推断关系）')}</option>}<option value={'\t'}>{t('Tab', '制表符')}</option><option value=",">{t('Comma', '逗号')}</option><option value="|">{t('Vertical bar', '竖线')}</option><option value=";">{t('Semicolon', '分号')}</option><option value=" ">{t('Space', '空格')}</option></select><small className={semanticTextMode ? 'construction-text-disclosure' : undefined}>{sourceType === 'txt' ? semanticTextMode ? t('The full TXT content is sent to the graph model in chunks. The LLM identifies explicit entities and relationships; review the generated result manually.', 'TXT 全文将分块发送至构图模型；LLM 会识别文本中明确出现的实体和关系，生成结果需人工核对。') : t('Auto mode recognizes tab-separated tables; otherwise it creates one record per line. You can also choose another table delimiter or force line-by-line records.', '自动模式只识别制表符表格，否则按逐行记录构图；也可选择其他表格分隔符或强制逐行记录。') : t('A blank CSV delimiter uses commas. Other delimiters still enforce header and column-count validation.', 'CSV 留空时使用逗号；选择其他分隔符后仍会严格校验表头和列数。')}</small></label>}
            <label>{semanticTextMode ? t('Extraction focus (optional)', '抽取目标（选填）') : t('Domain description (optional)', '领域描述（选填）')}<textarea rows={3} maxLength={16000} value={domain} onChange={event => setDomain(event.target.value)} placeholder={semanticTextMode ? t('For example: focus on people, organizations, locations, and their explicit relationships', '例如：关注人物、组织、地点及其明确关系') : t('For example: e-commerce order fulfillment, focusing on relationships among customers, products, and orders', '例如：电商订单履约，关注客户、商品和订单之间的关系')} /></label>
            {semanticTextMode && <><label>{t('Entity types to extract (optional)', '提取的实体类型（选填）')}<input value={entityTypes} onChange={event => setEntityTypes(event.target.value)} placeholder={t('People, events, organizations', '人物、事件、组织')} aria-describedby="construction-entity-types-help" /></label><small id="construction-entity-types-help">{t('Separate types with commas. Only entities in these categories will be used as graph nodes. Leave blank to extract all types.', '用逗号或顿号分隔；图中只保留这些类型的实体。留空则不限类型。')}</small><label className="construction-check"><input type="checkbox" role="switch" checked={textRefineEnabled} onChange={event => setTextRefineEnabled(event.target.checked)} aria-describedby="construction-text-refine-help" />{t('Use Agent to refine the TXT graph', '使用 Agent refine TXT 构图')}</label><small id="construction-text-refine-help">{textRefineEnabled ? t('Agent creates questions from the source text, checks their answers on the graph, and repairs missing facts in the draft.', 'Agent 会从原文提出查询，在图上核对答案，并修正草稿中的缺失事实。') : t('Extraction generates a draft without query-based refinement. Save the result to use it for analysis.', '抽取完成后生成草稿，不进行查询与修正；点击保存构图结果后可用于分析。')}</small>{textRefineEnabled && <label>{t('Validation queries', '验证查询数量')}<input type="number" min={1} max={100} step={1} value={queryCount} onChange={event => setQueryCount(event.target.value)} /></label>}</>}
            {!semanticTextMode && constructionMode === 'llm' && <><label className="construction-check"><input type="checkbox" checked={experienceEnabled} onChange={event => setExperienceEnabled(event.target.checked)} aria-describedby="construction-experience-help" />{t('Reuse domain experience', '复用领域经验')}</label>
            <small id="construction-experience-help">{t('Experience is reused only within graph construction for the same user and domain. New graphs still require validation. When disabled, this build neither reads nor writes experience.', '仅在构图模块内复用同一用户、同一领域的经验。新图仍需验收；关闭后，本次构图不读取或写入经验。')}</small></>}
          </fieldset>
          <button className="primary-command" onClick={build} disabled={editingBusy || modelSettingsDirty || !state || state.algorithm_available === false}>{isBuilding ? t('Building graph…', '正在构图…') : state?.algorithm_available === false ? t('Construction algorithm unavailable', '构图算法待接入') : state?.status === 'failed' ? t('Rebuild graph', '重新构图') : t('Start construction', '开始构图')}</button>
          {(isBuilding || state?.algorithm_available === false || semanticTextMode) && <small>{isBuilding ? t('Construction continues on the server and its status is checked every 3 seconds. You can close this window and reopen it to view progress and results.', '构图在服务器持续运行，每 3 秒查询一次状态。关闭窗口后，可重新打开查看进度与结果。') : state?.algorithm_available === false ? t('Graph construction is not available for this file type yet. The source file is preserved.', '此文件类型的构图算法暂不可用，源文件已保留。') : t('The LLM extracts entities and relationships from line-based text to build the graph. Review semantic results manually.', '依据 LLM 从逐行文本中抽取实体与关系并构图；语义结果需要人工核对。')}</small>}
        </>}</div>
        <div className="construction-rule-heading"><strong>{t('Graph data optimization', '图数据优化')}</strong><span>{t('6 operations', '6 项操作')}</span></div>
        <div className="construction-rule-list" role="group" aria-label={t('Optimization rules', '优化规则')}>{RULES.map((item, index) => <button key={item.id} className={rule === item.id ? 'active' : ''} aria-pressed={rule === item.id} onClick={() => switchRule(item.id)} disabled={editingBusy || !graph || conflict}><span>{String(index + 1).padStart(2, '0')}</span>{t(item.label.en, item.label.zh)}<Icon name="chevron" size={13} /></button>)}</div>
        {graph && <form className="construction-form" onSubmit={event => { event.preventDefault(); try { void updatePreview([...operations, makeOperation()]); } catch (cause) { setError(failure(cause, t)); } }}><h3>{t(activeRule.label.en, activeRule.label.zh)}</h3><p>{t(activeRule.description.en, activeRule.description.zh)}</p><fieldset disabled={editingBusy || conflict}>
          {(rule === 'delete' || rule === 'rename_type') && <label>{t('Object type', '对象类别')}<select value={target} onChange={event => { setTarget(event.target.value as 'node' | 'edge'); setProperty(''); setOldType(''); }}><option value="node">{t('Node', '节点')}</option><option value="edge">{t('Edge', '边')}</option></select></label>}
          {rule === 'delete' && <label>{t('Content to delete', '删除内容')}<select value={deleteMode} onChange={event => { setDeleteMode(event.target.value as 'entity' | 'property'); setProperty(''); }}><option value="entity">{t('The object itself', '对象本身')}</option><option value="property">{t('A property of the selected object', '所选对象的属性')}</option></select></label>}
          {(wantsNodes || wantsEdges) && <label>{t('Find objects', '查找对象')}<input value={search} onChange={event => setSearch(event.target.value)} placeholder={t('Search by ID, type, or property', '搜索 ID、类型或属性')} /></label>}
          {wantsNodes && <label>{['extract_property_node', 'delete'].includes(rule) ? t('Select nodes (multiple)', '选择节点（可多选）') : t('Select a node', '选择节点')}<select aria-label={t('Select a node', '选择节点')} multiple={['extract_property_node', 'delete'].includes(rule)} size={['extract_property_node', 'delete'].includes(rule) ? 4 : undefined} value={['extract_property_node', 'delete'].includes(rule) ? nodeIds : nodeIds[0] || ''} onChange={event => { const ids = idsFromSelect(event.currentTarget).filter(Boolean); setNodeIds(ids); setSelected(nodeById.get(ids[0]) || null); setSchemaSelected(null); setProperty(''); setEdgeIds([]); }}><option value="" disabled>{t('Select an instance node; search first to narrow the list', '请选择实例节点，可先搜索定位')}</option>{filteredNodes.slice(0, 500).map(node => <option value={String(node.id)} key={String(node.id)}>{instanceNodeName(node, t)}</option>)}</select></label>}
          {wantsEdges && <label>{rule === 'node_property_to_edge' ? t('Related edges to receive the property (multiple)', '接收属性的关联边（可多选）') : t('Select edges (multiple)', '选择边（可多选）')}<select aria-label={t('Select edges', '选择边')} multiple size={4} value={edgeIds} onChange={event => setEdgeIds(idsFromSelect(event.currentTarget))}>{filteredEdges.slice(0, 500).map(edge => <option value={String(edge.id)} key={String(edge.id)}>{endpointName(edge.source)} → {endpointName(edge.target)} · {String(edge.type || t('Relationship', '关系'))} [{String(edge.id)}]</option>)}</select>{!filteredEdges.length && <small>{rule === 'node_property_to_edge' ? t('No matching edges. Select a node first.', '暂无匹配的边，请先选择节点') : t('No matching edges', '暂无匹配的边')}</small>}</label>}
          {(wantsNodes || wantsEdges) && <small>{t('Hold Ctrl / ⌘ to select multiple items. Up to 500 items are shown; search to find more.', '多选时按住 Ctrl / ⌘；列表最多显示 500 项，可搜索定位更多对象。')}</small>}
          {wantsProperty && <label>{t('Select a property', '选择属性')}<select aria-label={t('Select a property', '选择属性')} value={property} onChange={event => setProperty(event.target.value)}><option value="">{t('Select a property', '请选择属性')}</option>{availableProperties.map(key => <option key={key} value={key}>{key}</option>)}</select>{property && selectedNode && !(rule === 'delete' && target === 'edge') && <small>{t(`Current value: ${display(properties(selectedNode)[property])}`, `当前值：${display(properties(selectedNode)[property])}`)}</small>}</label>}
          {rule === 'node_property_to_edge' && <label>{t('Target edge property name', '目标边属性名')}<input value={targetProperty} onChange={event => setTargetProperty(event.target.value)} placeholder={t('Leave blank to keep the original property name', '留空则保留原属性名')} /></label>}
          {rule === 'extract_property_node' && <label>{t('New node type', '新节点类型')}<input value={nodeType} onChange={event => setNodeType(event.target.value)} placeholder={t('For example: PhoneNumber', '例如：电话号码')} /></label>}
          {['collapse_node', 'extract_property_node'].includes(rule) && <label>{t('New relationship type', '新关系类型')}<input value={edgeType} onChange={event => setEdgeType(event.target.value)} placeholder={t('For example: RELATED_TO / HAS_PHONE', '例如：关联 / 拥有号码')} /></label>}
          {rule === 'collapse_node' && <small>{t('Only intermediate nodes with exactly two incident edges are supported. Directed graphs require one incoming and one outgoing edge. Existing properties are retained in the provenance data of the new edge.', '只支持恰有两条关联边的中间节点；有向图需满足一入一出。原有属性会保留到新边的溯源信息中。')}</small>}
          {['node_property_to_edge', 'extract_property_node'].includes(rule) && <label className="construction-check"><input type="checkbox" checked={removeProperty} onChange={event => setRemoveProperty(event.target.checked)} />{t('Remove this property from the original node', '从原节点移除该属性')}</label>}
          {rule === 'delete' && target === 'node' && deleteMode === 'entity' && <label className="construction-check"><input type="checkbox" checked={cascade} onChange={event => setCascade(event.target.checked)} />{t('Also delete edges connected to this node', '同时删除与该节点相连的边')}</label>}
          {rule === 'rename_type' && <><label>{t('Original type', '原类型')}<select value={oldType} onChange={event => setOldType(event.target.value)}><option value="">{t('Select the original type', '请选择原类型')}</option>{types.map(type => <option key={type} value={type}>{type}</option>)}</select></label><label>{t('New type name', '新类型名称')}<input value={newType} onChange={event => setNewType(event.target.value)} placeholder={t('Enter a domain name', '输入业务名称')} /></label><small>{target === 'node' ? t('This will apply to every node in the current graph that uses this type.', '将应用于当前图中所有使用该类型的节点。') : t('This will apply to every edge in the current graph that uses this type.', '将应用于当前图中所有使用该类型的边。')}</small></>}
          <button className="primary-command construction-preview-button" type="submit">{busy === 'preview' ? t('Generating preview…', '正在预览…') : t('Apply and preview', '应用并预览')}</button>
        </fieldset></form>}
      </aside>
      <main className="construction-visual"><div className="construction-visual-head"><div><strong>{showInstanceGraph ? t('Entities and relationships', '实体关系图') : showTableSchema ? t('Table relationships', '表关联模式') : preview ? t('Graph Schema · Change Preview', 'Graph Schema · 修改预览') : 'Graph Schema'}</strong><span>{showInstanceGraph ? t('Nodes show entity names; links show their relationships.', '节点显示人物等实体的真实名称，连线表示实体间关系') : showTableSchema ? preview ? t('Source PK/FK schema; inspect pending changes in Optimized structure.', '源表主外键关系；待保存的优化请切换“优化后结构”查看。') : t('Nodes are tables; links are foreign keys.', '节点为表，连线为外键') : preview ? t('The preview is not saved. Type and relationship statistics reflect the preview.', '预览尚未保存；类型与关系统计已按预览实时更新') : graph ? t('Nodes represent node types; links represent relationship types.', '节点代表节点类型，连线代表关系类型') : t('Node and relationship types appear here after construction.', '构图完成后展示节点类型与关系类型')}</span>{schemaWarningText && !preview && <span className="construction-schema-warning">{schemaWarningText}</span>}</div>{graph && <div className="construction-counts"><span><b>{formatNumber(showInstanceGraph ? nodes.length : showTableSchema ? sourceTables.length : schemaNodeTypeCount)}</b> {showInstanceGraph ? t('nodes', '个节点') : showTableSchema ? t('tables', '张表') : t(schemaNodeTypeCount === 1 ? 'node type' : 'node types', '节点类型')}</span><span><b>{formatNumber(showInstanceGraph ? edges.length : showTableSchema ? tableGraph!.edges.length : schemaEdgeTypeCount)}</b> {showInstanceGraph ? t('relationships', '条关系') : showTableSchema ? t('foreign keys', '外键关系') : t(schemaEdgeTypeCount === 1 ? 'relationship type' : 'relationship types', '关系类型')}</span></div>}</div>
        <SourceDataPreview key={`${dataset.project_id || 'personal'}:${dataset.dataset_id}`} dataset={dataset} delimiter={sourceType === 'csv' ? delimiter : undefined} graphReady={Boolean(graph)} />
        {graph && tableGraph && <div className="construction-schema-switch" role="group" aria-label={t('Preview mode', '预览模式')}>
          <button type="button" aria-pressed={showTableSchema} onClick={() => setSchemaView('tables')}>{t('Table relationships', '表关联模式')}</button>
          <button type="button" aria-pressed={!showTableSchema} onClick={() => setSchemaView('optimized')}>{t('Optimized structure', '优化后结构')}</button>
        </div>}
        {graph && sourceType === 'txt' && <div className="construction-schema-switch" role="group" aria-label={t('Preview mode', '预览模式')}>
          <button type="button" aria-pressed={showInstanceGraph} onClick={() => { setSchemaView('instances'); setSchemaSelected(null); }}>{t('Entities and relationships', '实体关系图')}</button>
          <button type="button" aria-pressed={!showInstanceGraph} onClick={() => { setSchemaView('optimized'); setSelected(null); }}>{t('Type structure', '类型结构')}</button>
        </div>}
        {busy === 'loading' ? <div className="construction-empty"><span className="loader" /><strong>{t('Loading construction status', '正在读取构图状态')}</strong></div> : graph && showTableSchema && tableGraph ? <TableSchemaPreview key={dataset.dataset_id} tables={sourceTables} graph={tableGraph} /> : graph && localizedCanvasGraph ? <><div className="construction-graph"><GraphWorkspace data={localizedCanvasGraph} tab={tab} onTab={setTab} selected={showInstanceGraph ? selected : schemaSelected} onSelect={showInstanceGraph ? setSelected : setSchemaSelected} collapsed={false} onToggle={() => undefined} embedded /></div><div className="construction-canvas-note">{showInstanceGraph ? t(`Showing ${formatNumber(nodes.length)} entities and ${formatNumber(edges.length)} relationships. Select a node to inspect its name and properties.`, `共 ${formatNumber(nodes.length)} 个实体、${formatNumber(edges.length)} 条关系；点击节点查看姓名与属性。`) : preview
          ? t(`Graph Schema has aggregated the complete preview graph with ${formatNumber(Number(activeSchema?.schema.node_count ?? nodes.length))} nodes and ${formatNumber(Number(activeSchema?.schema.edge_count ?? edges.length))} edges.`, `Graph Schema 预览已聚合完整预览图中的 ${formatNumber(Number(activeSchema?.schema.node_count ?? nodes.length))} 个节点和 ${formatNumber(Number(activeSchema?.schema.edge_count ?? edges.length))} 条边。`)
          : t(`Graph Schema has aggregated the complete graph with ${formatNumber(Number(activeSchema?.schema.node_count ?? nodes.length))} nodes and ${formatNumber(Number(activeSchema?.schema.edge_count ?? edges.length))} edges.`, `Graph Schema 已聚合完整图中的 ${formatNumber(Number(activeSchema?.schema.node_count ?? nodes.length))} 个节点和 ${formatNumber(Number(activeSchema?.schema.edge_count ?? edges.length))} 条边。`)}</div><div className="construction-inspection">{!showInstanceGraph && schemaSelected && <div className="construction-properties construction-schema-properties"><strong>{t(`Node type ${selectedSchemaTypeName}`, `节点类型 ${selectedSchemaTypeName}`)}</strong><span>{t(`${formatNumber(Number(selectedSchemaProperties.instance_count || 0))} instances · ${formatNumber(selectedSchemaPaths.length)} property paths`, `${formatNumber(Number(selectedSchemaProperties.instance_count || 0))} 个实例 · ${formatNumber(selectedSchemaPaths.length)} 个属性路径`)}</span>{selectedSchemaPaths.length ? <div className="construction-schema-paths">{selectedSchemaPaths.slice(0, 24).map(item => <span key={item.path} title={item.types?.join(' / ')}>{item.path}<small>{item.types?.join(' / ') || t('unknown', '未知')}</small></span>)}{selectedSchemaPaths.length > 24 && <em>{t(`${formatNumber(selectedSchemaPaths.length - 24)} more property paths`, `另有 ${formatNumber(selectedSchemaPaths.length - 24)} 个属性路径`)}</em>}</div> : <span>{t('This node type has no property paths.', '该节点类型没有属性路径')}</span>}</div>}{currentSelection && <div className="construction-properties"><strong>{t(`Properties of instance node ${instanceNodeName(currentSelection, t)}`, `实例节点 ${instanceNodeName(currentSelection, t)} 的属性`)}</strong>{Object.keys(properties(currentSelection)).length ? <dl>{Object.entries(properties(currentSelection)).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{display(value)}</dd></div>)}</dl> : <span>{t('This node has no properties.', '此节点暂无属性')}</span>}</div>}{inspectedEdges.map(edge => <div className="construction-properties" key={String(edge.id)}><strong>{t(`Instance edge ${String(edge.id)} · ${endpointName(edge.source)} → ${endpointName(edge.target)}`, `实例边 ${String(edge.id)} · ${endpointName(edge.source)} → ${endpointName(edge.target)}`)}</strong><span>{t(`Type: ${String(edge.type)}`, `类型：${String(edge.type)}`)}</span>{Object.keys(properties(edge)).length ? <dl>{Object.entries(properties(edge)).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{display(value)}</dd></div>)}</dl> : <span>{t('This edge has no properties.', '此边暂无属性')}</span>}</div>)}</div></> : <div className={`construction-empty ${isBuilding ? 'with-agent-stream' : ''}`}><span className="construction-empty-icon"><Icon name="graph" size={42} /></span><strong>{isBuilding ? t('Building and validating graph data', '正在构建并验证图数据') : state?.algorithm_available === false ? t('Source file ready; waiting for a construction algorithm', '源文件已就绪，等待构图算法') : t('Turn a file into analysis-ready graph data', '从文件到可分析的图数据')}</strong>{isBuilding && <p>{t('The server is generating and validating graph data.', '服务器正在生成和验证图数据。')}<br />{t('You can close this window and reopen it later to view the result.', '可以关闭窗口，稍后重新打开查看结果。')}</p>}{isBuilding ? <ConstructionAgentStream state={state} mode={semanticTextMode ? 'llm' : constructionMode} /> : <div className="construction-flow"><span>SQL / CSV / TXT</span><Icon name="arrow-right" size={17} /><span>{t('Construct', '构图')}</span><Icon name="arrow-right" size={17} /><span>Graph Schema</span></div>}</div>}
        {!showTableSchema && !showInstanceGraph && <ConstructionSchemaFields dataset={dataset} schema={savedSchema} graph={graph} selectedType={String(selectedSchemaProperties.type_name || schemaSelected?.id || '')} />}
      </main>
    </div>
    <footer className="construction-footer"><div className="construction-save-info">{preview ? <><strong>{t(`${operations.length} ${operations.length === 1 ? 'change' : 'changes'} waiting to be saved`, `${operations.length} 项修改待保存`)}</strong><span>{t(`Nodes ${preview.summary.before.node_count} → ${preview.summary.after.node_count} · Edges ${preview.summary.before.edge_count} → ${preview.summary.after.edge_count}`, `节点 ${preview.summary.before.node_count} → ${preview.summary.after.node_count} · 边 ${preview.summary.before.edge_count} → ${preview.summary.after.edge_count}`)}</span><details><summary>{t('View change list', '查看修改清单')}</summary><ol>{operations.map((operation, index) => <li key={index}>{operationLabel(operation, t)}</li>)}</ol></details></> : <><strong>{state?.status === 'draft' ? t('Graph draft ready · Unsaved', '构图草稿已生成 · 待保存') : graph ? t('Graph construction result saved', '构图结果已保存') : t('Source file saved', '源文件已保存')}</strong><span>{state?.status === 'draft' ? t('Save the construction result before using this graph for analysis.', '点击保存构图结果后，即可使用此图进行分析。') : graph ? t('Use this dataset now or continue optimizing the graph.', '可直接使用此数据集，或继续优化图数据。') : isBuilding ? t('Construction continues on the server. You can close this window and check again later.', '构图在服务器继续运行，可关闭窗口后再查看。') : state?.algorithm_available === false ? t('This file type cannot be constructed yet. You do not need to upload it again.', '此文件类型暂不可构图，无需重复上传。') : t('Review the source data and construction mode, then start construction.', '确认源数据与构图方式后即可开始构图。')}</span></>}</div><div className="construction-footer-actions">{operations.length > 0 && <><button className="secondary-command" disabled={editingBusy} onClick={() => void updatePreview(operations.slice(0, -1))}>{t('Undo last step', '撤销上一步')}</button><button className="secondary-command" disabled={editingBusy} onClick={() => void updatePreview([])}>{t('Restore all', '还原全部')}</button></>}{onUse && graph && state?.status === 'ready' && !operations.length && <button className="secondary-command" disabled={editingBusy} onClick={() => void useDataset()}>{busy === 'use' ? t('Applying…', '正在使用…') : t('Use in current conversation', '在当前会话使用')}</button>}<button className="primary-command" disabled={editingBusy || (!operations.length && state?.status !== 'draft') || conflict} onClick={() => void save()}><Icon name="check" size={15} />{busy === 'save' ? t('Saving…', '正在保存…') : state?.status === 'draft' ? t('Save construction result', '保存构图结果') : t('Save to dataset', '保存到数据集')}</button></div></footer>
  </Panel></Backdrop>;
}

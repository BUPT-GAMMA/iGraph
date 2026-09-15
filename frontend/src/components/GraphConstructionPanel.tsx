import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, buildDatasetGraph, commitConstruction, getConstructionGraph, getConstructionSchema, getConstructionState, previewConstruction } from '../api';
import { isGraphData } from '../types';
import type { ConstructionBuildOptions, ConstructionDomainCategory, ConstructionGraph, ConstructionOperation, ConstructionResult, ConstructionSchemaPropertyPath, ConstructionSchemaResponse, ConstructionState, Dataset, GraphData } from '../types';
import { GraphWorkspace } from './GraphWorkspace';
import { Icon } from './Icon';
import { constructionProperties as properties } from '../constructionProperties';
import './GraphConstructionPanel.css';

type Rule = ConstructionOperation['op'];
const DOMAIN_CATEGORIES: Array<{ value: ConstructionDomainCategory; label: string }> = [
  { value: 'auto', label: '自动识别' },
  { value: 'finance', label: '金融' },
  { value: 'academic', label: '学术' },
  { value: 'commerce', label: '电商' },
  { value: 'healthcare', label: '医疗' },
  { value: 'social', label: '社区' },
  { value: 'general', label: '通用' },
];
const RULES: Array<{ id: Rule; label: string; description: string }> = [
  { id: 'collapse_node', label: '节点转成边', description: '把只承担连接作用的中间节点压缩成直接关系，减少遍历跳数。' },
  { id: 'node_property_to_edge', label: '节点属性转成边属性', description: '把遍历时需要读取的节点属性移动到选中的关联边。' },
  { id: 'extract_property_node', label: '节点属性提取为独立节点', description: '将电话等共享属性建模为独立节点，便于查找关联实体。' },
  { id: 'delete', label: '删除冗余节点、边、属性', description: '明确选择需要删除的内容，先预览影响再保存。' },
  { id: 'reverse_edges', label: '调整边方向', description: '反转所选边，使方向适配常用查询的遍历方式。' },
  { id: 'rename_type', label: '重命名节点、边类型', description: '统一替换选中类型的名称，让图数据符合业务语义。' },
];

function rawProperties(item: Record<string, unknown> | undefined) {
  const value = item?.properties;
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function display(value: unknown) { return typeof value === 'object' ? JSON.stringify(value) : String(value ?? ''); }
function failure(cause: unknown) { return cause instanceof Error ? cause.message : '操作失败，请重试'; }
function operationLabel(operation: ConstructionOperation) { return RULES.find(rule => rule.id === operation.op)?.label || operation.op; }
function idsFromSelect(select: HTMLSelectElement) { return Array.from(select.selectedOptions, option => option.value); }
function elapsedLabel(seconds: number | undefined) {
  if (!Number.isFinite(seconds)) return '';
  const value = Math.max(0, Math.floor(seconds || 0));
  return value < 60 ? `${value} 秒` : `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
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
async function resolveConstructionSchema(dataset: Dataset, revision: number, graph: GraphData, signal?: AbortSignal) {
  try {
    const response = await getConstructionSchema(dataset, signal);
    if (!validSchemaResponse(response, dataset, revision)) {
      return { response: deriveConstructionSchema(dataset, revision, graph), warning: 'Graph Schema 版本尚未同步，已从当前实例图生成本地结构概览；图数据优化与保存不受影响。' };
    }
    return {
      response: { ...response, schema: { ...response.schema, graph: { ...response.schema.graph, visualization_kind: 'schema' } } },
      warning: '',
    };
  } catch (cause) {
    if (signal?.aborted) throw cause;
    return { response: deriveConstructionSchema(dataset, revision, graph), warning: `Graph Schema 接口暂时不可用，已从当前实例图生成本地结构概览；图数据优化与保存不受影响。` };
  }
}
async function loadReadyAssets(dataset: Dataset, signal?: AbortSignal): Promise<{ snapshot: ConstructionGraph; schema: ConstructionSchemaResponse; warning: string }> {
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
      ? 'Graph Schema 版本尚未同步，已从当前实例图生成本地结构概览；图数据优化与保存不受影响。'
      : 'Graph Schema 接口暂时不可用，已从当前实例图生成本地结构概览；图数据优化与保存不受影响。',
  };
}

function ConstructionAgentStream({ state }: { state: ConstructionState | null }) {
  const progress = state?.progress;
  const events = (progress?.events || []).slice(-8);
  const metrics = progress?.metrics || {};
  const currentStageEvent = [...events].reverse().find(event => event.kind === 'stage' && event.stage === progress?.stage)?.id;
  return <section className="construction-agent-stream" aria-live="polite" aria-label="构图 Agent 消息流">
    <div className="construction-agent-stream-head"><div><span className="construction-agent-live" /><strong>Agent 构图消息</strong><small>运行中</small></div>{elapsedLabel(progress?.elapsed_seconds) && <time>已运行 {elapsedLabel(progress?.elapsed_seconds)}</time>}</div>
    <p className="construction-agent-current">{progress?.current_message || state?.message || '构图 Agent 已接收任务，正在启动。'}</p>
    <ol>{events.length ? events.map(event => <li key={event.id} className={`${event.outcome || event.kind} ${event.id === currentStageEvent ? 'active' : ''}`}><span>{event.id === currentStageEvent ? <i className="construction-agent-pulse" /> : event.outcome === 'rejected' ? '—' : <Icon name="check" size={12} />}</span><p>{event.message}</p></li>) : <li className="stage active"><span><i className="construction-agent-pulse" /></span><p>构图 Agent 已接收任务，正在启动。</p></li>}</ol>
    {(metrics.evaluated_candidates !== undefined || metrics.iterations !== undefined || metrics.chunks_completed !== undefined || metrics.chunks_total !== undefined || metrics.extracted_nodes !== undefined || metrics.extracted_edges !== undefined) && <div className="construction-agent-metrics">
      {metrics.iterations !== undefined && <span>迭代 <b>{metrics.iterations}{metrics.max_iterations ? ` / ${metrics.max_iterations}` : ''}</b></span>}
      {metrics.evaluated_candidates !== undefined && <span>候选 <b>{metrics.evaluated_candidates}</b></span>}
      {metrics.accepted_candidates !== undefined && <span>已采用 <b>{metrics.accepted_candidates}</b></span>}
      {metrics.tool_calls !== undefined && <span>工具调用 <b>{metrics.tool_calls}</b></span>}
      {(metrics.chunks_completed !== undefined || metrics.chunks_total !== undefined) && <span>文本分块 <b>{metrics.chunks_completed ?? 0}{metrics.chunks_total !== undefined ? ` / ${metrics.chunks_total}` : ''}</b></span>}
      {metrics.extracted_nodes !== undefined && <span>已识别实体 <b>{metrics.extracted_nodes}</b></span>}
      {metrics.extracted_edges !== undefined && <span>已识别关系 <b>{metrics.extracted_edges}</b></span>}
    </div>}
    <small className="construction-agent-disclosure">这里只展示可审计的运行阶段与验收结果，不展示模型内部推理。</small>
  </section>;
}

type Props = {
  dataset: Dataset;
  onClose: () => void;
  onSaved: (dataset: Dataset, graph: GraphData, revision: number) => void;
  onStateChange?: (dataset: Dataset, state: ConstructionState) => void;
  onUse?: (dataset: Dataset) => Promise<void>;
  embedded?: boolean;
};

export function GraphConstructionPanel({ dataset, onClose, onSaved, onStateChange, onUse, embedded = false }: Props) {
  const [state, setState] = useState<ConstructionState | null>(null);
  const [savedGraph, setSavedGraph] = useState<GraphData | null>(null);
  const [savedSchema, setSavedSchema] = useState<ConstructionSchemaResponse | null>(null);
  const [schemaWarning, setSchemaWarning] = useState('');
  const [preview, setPreview] = useState<ConstructionResult | null>(null);
  const [operations, setOperations] = useState<ConstructionOperation[]>([]);
  const [busy, setBusy] = useState('loading');
  const [monitoringBuild, setMonitoringBuild] = useState(false);
  const [queryCount, setQueryCount] = useState('15');
  const [domain, setDomain] = useState('');
  const [domainCategory, setDomainCategory] = useState<ConstructionDomainCategory>('auto');
  const [experienceEnabled, setExperienceEnabled] = useState(true);
  const [delimiter, setDelimiter] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const [tab, setTab] = useState<'Overview' | 'Graph' | 'Data' | 'ML' | 'Artifacts'>('Graph');
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
  const [schemaSelected, setSchemaSelected] = useState<Record<string, unknown> | null>(null);
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
  const canvasGraph = activeSchema?.schema.graph || null;
  const nodes = useMemo(() => graph?.nodes || [], [graph]);
  const edges = useMemo(() => graph?.edges || [], [graph]);
  const nodeById = useMemo(() => new Map(nodes.map(node => [String(node.id), node])), [nodes]);
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
      const assets = nextState.status === 'ready' ? await loadReadyAssets(dataset, signal) : null;
      const snapshot = assets?.snapshot || null;
      if (signal?.aborted || !alive.current || version !== lifecycle.current || attempt !== buildAttempt.current) return null;
      // A later status response must not replace a completed graph or its edits.
      if (quiet && publish && buildResolved.current) return nextState;
      if (quiet && publish) {
        if (currentBuildId.current && nextState.build_id && currentBuildId.current !== nextState.build_id) return null;
        // Until the POST or a building snapshot identifies this attempt, an
        // earlier failure cannot resolve it or discard its eventual success.
        if (postPending.current && nextState.status !== 'building' && nextState.status !== 'ready' &&
            (!currentBuildId.current || nextState.build_id !== currentBuildId.current)) return null;
        if (nextState.status === 'building' && nextState.build_id) currentBuildId.current = nextState.build_id;
      }
      const publishedState = snapshot ? { ...nextState, revision: snapshot.revision } : nextState;
      const restoredQueryCount = progressQueryCount(nextState);
      if (restoredQueryCount !== null) setQueryCount(restoredQueryCount);
      setState(publishedState);
      callbacks.current.onStateChange?.(callbacks.current.dataset, publishedState);
      const waiting = nextState.status === 'building' || nextState.status === 'not_built' && postPending.current;
      setMonitoringBuild(waiting);
      setError(nextState.status === 'failed' ? nextState.message || '构图失败，源文件已保留。可调整参数后重试。' : '');
      if (!quiet || snapshot) {
        setSavedGraph(snapshot?.graph || null); setSavedSchema(assets?.schema || null); setSchemaWarning(assets?.warning || '');
        setPreview(null); setOperations([]); setConflict(false);
        setSelected(null); setSchemaSelected(null); setNodeIds([]); setEdgeIds([]); setProperty('');
      }
      if (waiting) setNotice('构图正在服务器上运行。可以关闭此窗口，稍后重新打开查看结果。');
      else if (publish && snapshot) {
        buildResolved.current = true; postPending.current = false;
        setBusy(previous => previous === 'build' ? '' : previous);
        if (notifiedRevision.current !== snapshot.revision) {
          notifiedRevision.current = snapshot.revision;
          callbacks.current.onSaved(callbacks.current.dataset, snapshot.graph, snapshot.revision);
        }
        setNotice('构图已保存到此数据集，后续可直接使用。');
      } else if (nextState.status === 'failed' || nextState.status === 'algorithm_unavailable') {
        buildResolved.current = true; postPending.current = false;
        setBusy(previous => previous === 'build' ? '' : previous);
        if (nextState.status === 'algorithm_unavailable') setNotice(nextState.message || '此文件类型的构图算法暂不可用，源文件已保留。');
      } else if (publish && nextState.status === 'not_built') {
        setNotice('服务器尚未生成构图结果。可确认参数后手动开始构图。');
      }
      return nextState;
    } catch (cause) {
      if (!signal?.aborted && alive.current && version === lifecycle.current && attempt === buildAttempt.current) {
        setError(quiet ? `暂时无法读取构图状态：${failure(cause)}。将继续查询状态，不会重复启动构图。` : failure(cause));
      }
      return null;
    } finally { if (!quiet && !signal?.aborted && alive.current && version === lifecycle.current) setBusy(''); }
  }, [dataset.dataset_id, dataset.project_id]);

  useEffect(() => {
    alive.current = true;
    lifecycle.current += 1;
    postPending.current = false; buildResolved.current = false; notifiedRevision.current = null;
    currentBuildId.current = null;
    buildAttempt.current += 1;
    setState(null); setSavedGraph(null); setSavedSchema(null); setSchemaWarning(''); setMonitoringBuild(false); setNotice(''); setQueryCount('15'); setDomain('');
    setSelected(null); setSchemaSelected(null);
    setDomainCategory('auto'); setExperienceEnabled(true); setDelimiter('');
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
      setError('数据集已在其他位置更新。请重新加载最新版本后再编辑，本次预览尚未保存。');
    } else setError(failure(cause));
  }
  async function build() {
    if (postPending.current || isBuilding || !state || state.algorithm_available === false) return;
    const count = Number(queryCount);
    if (!semanticTextMode && (!Number.isInteger(count) || count < 1 || count > 100)) {
      setError('验证查询数量必须是 1 到 100 之间的整数。'); return;
    }
    const version = lifecycle.current;
    const attempt = ++buildAttempt.current;
    postPending.current = true; buildResolved.current = false; notifiedRevision.current = null;
    currentBuildId.current = null;
    setMonitoringBuild(true);
    setBusy('build'); setError(''); setNotice('');
    const optimisticState: ConstructionState = { ...state, status: 'building', message: '构图 Agent 已接收任务，正在启动。' };
    setState(optimisticState);
    callbacks.current.onStateChange?.(callbacks.current.dataset, optimisticState);
    try {
      // No AbortSignal: closing this view must not cancel the server-side build.
      const options: ConstructionBuildOptions = semanticTextMode
        ? { domain: domain.trim(), txt_mode: 'entities', mode: 'llm' }
        : { query_count: count, domain: domain.trim(), domain_category: domainCategory, experience_enabled: experienceEnabled };
      if (!semanticTextMode && sourceType === 'txt' && delimiter === '__lines__') options.txt_mode = 'lines';
      else if (!semanticTextMode && supportsDelimiter && delimiter) options.delimiter = delimiter;
      const result = await buildDatasetGraph(dataset, options);
      if (!alive.current || version !== lifecycle.current || attempt !== buildAttempt.current || buildResolved.current) return;
      postPending.current = false;
      currentBuildId.current = result.build_id || null;
      if (result.status === 'ready') {
        const assets = await loadReadyAssets(dataset);
        const snapshot = assets.snapshot;
        if (!alive.current || version !== lifecycle.current || attempt !== buildAttempt.current || buildResolved.current) return;
        buildResolved.current = true; setMonitoringBuild(false);
        const completedState = { ...result, revision: snapshot.revision };
        setState(completedState); setSavedGraph(snapshot.graph); setSavedSchema(assets.schema); setSchemaWarning(assets.warning);
        setSchemaSelected(null);
        callbacks.current.onStateChange?.(callbacks.current.dataset, completedState);
        if (notifiedRevision.current !== snapshot.revision) {
          notifiedRevision.current = snapshot.revision;
          onSaved(dataset, snapshot.graph, snapshot.revision);
        }
        setNotice('构图已保存到此数据集，后续可直接使用。');
      } else {
        setState(result); setMonitoringBuild(result.status === 'building');
        callbacks.current.onStateChange?.(callbacks.current.dataset, result);
        if (result.status === 'failed') setError(result.message || '构图失败，源文件已保留。');
        else setNotice(result.message || (result.status === 'building' ? '构图正在服务器上运行，可以关闭此窗口后再查看。' : '源文件已保留，可稍后继续。'));
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
        setNotice(uncertain ? '请求连接已中断，服务器可能仍在构图。正在查询已有任务状态。' : '正在确认服务器上的构图结果。');
        const latest = await load(undefined, true, true);
        if (alive.current && version === lifecycle.current && attempt === buildAttempt.current && latest?.status !== 'building' && latest?.status !== 'ready') {
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
    if (rule === 'collapse_node') return { op: rule, node_id: requireIds(nodeIds, '请选择一个中间节点')[0], edge_type: requireValue(edgeType, '请输入新关系类型') };
    if (rule === 'node_property_to_edge') return { op: rule, node_id: requireIds(nodeIds, '请选择来源节点')[0], property: requireValue(property, '请选择节点属性'), edge_ids: requireIds(edgeIds, '请选择接收属性的关联边'), ...(targetProperty.trim() ? { target_property: targetProperty.trim() } : {}), remove_from_node: removeProperty };
    if (rule === 'extract_property_node') return { op: rule, node_ids: requireIds(nodeIds, '请选择来源节点'), property: requireValue(property, '请选择节点属性'), node_type: requireValue(nodeType, '请输入新节点类型'), edge_type: requireValue(edgeType, '请输入新关系类型'), remove_from_node: removeProperty };
    if (rule === 'reverse_edges') return { op: rule, edge_ids: requireIds(edgeIds, '请选择需要反转的边') };
    if (rule === 'rename_type') return { op: rule, target, old_type: requireValue(oldType, '请选择原类型'), new_type: requireValue(newType, '请输入新类型名称') };
    return { op: rule, target, ids: requireIds(target === 'node' ? nodeIds : edgeIds, '请选择需要删除的对象'), ...(deleteMode === 'property' ? { property: requireValue(property, '请选择要删除的属性') } : { cascade }) };
  }
  async function updatePreview(next: ConstructionOperation[]) {
    if (!state) return;
    if (!next.length) { setOperations([]); setPreview(null); setSelected(null); setSchemaSelected(null); setNotice('已还原到保存的版本。'); return; }
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
    if (!state || !operations.length || conflict) return;
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
      setNotice(`已保存版本 ${result.revision}。该数据集可在后续会话中直接使用。`);
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
    `${node.id} ${node.type} ${display(properties(node))}`.toLowerCase().includes(searchText)), [nodes, searchText]);
  const filteredEdges = useMemo(() => !searchText ? selectableEdges : selectableEdges.filter(edge =>
    `${edge.id} ${edge.source} ${edge.target} ${edge.type}`.toLowerCase().includes(searchText)), [selectableEdges, searchText]);
  const inspectedEdges = useMemo(() => {
    if (!edgeIds.length) return [];
    const selectedIds = new Set(edgeIds);
    return edges.filter(edge => selectedIds.has(String(edge.id))).slice(0, 10);
  }, [edges, edgeIds]);
  const schemaNodeTypeCount = activeSchema?.schema.node_type_count ?? activeSchema?.schema.node_types.length ?? 0;
  const schemaEdgeTypeCount = activeSchema?.schema.edge_type_count ?? activeSchema?.schema.edge_types.length ?? 0;
  const selectedSchemaProperties = rawProperties(schemaSelected || undefined);
  const selectedSchemaTypeName = String(selectedSchemaProperties.type_name || schemaSelected?.label || schemaSelected?.id || '');
  const selectedSchemaPaths = Array.isArray(selectedSchemaProperties.property_paths)
    ? selectedSchemaProperties.property_paths.filter((item): item is ConstructionSchemaPropertyPath => Boolean(item && typeof item === 'object' && !Array.isArray(item) && 'path' in item))
    : [];

  return <div className={embedded ? 'construction-page' : 'construction-backdrop'}><section className={`construction-panel ${embedded ? 'is-embedded' : ''} ${graph ? 'has-graph' : ''}`} role={embedded ? 'region' : 'dialog'} aria-modal={embedded ? undefined : true} aria-labelledby="construction-title" ref={dialog} onKeyDown={event => {
    if (embedded) return;
    if (event.key === 'Escape') { event.stopPropagation(); requestClose(); }
    if (event.key === 'Tab') {
      const focusable = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]') || []).filter(element => element.getClientRects().length > 0);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  }}>
    <header className="construction-header"><div className="construction-heading"><span className="construction-mark"><Icon name="graph" size={23} /></span><div><span className="construction-eyebrow">构图工作台 / 数据源</span><h2 id="construction-title">{dataset.name}</h2><p>{state?.source_filename || dataset.original_filename}</p></div></div><div className="construction-header-actions"><span className={`construction-status ${graph && !isBuilding ? 'ready' : ''}`}>{busy === 'loading' ? '读取中' : isBuilding ? '构图进行中' : graph ? `已保存 · 版本 ${state?.revision}` : state?.status === 'failed' ? '构图失败' : '等待构图'}</span>{embedded ? <button ref={closeButton} className="secondary-command construction-switch-source" disabled={Boolean(busy) && busy !== 'loading' && busy !== 'build'} onClick={requestClose}><Icon name="database" size={15} /> 更换数据源</button> : <button ref={closeButton} className="plain-icon" aria-label="关闭构图工作台" disabled={Boolean(busy) && busy !== 'loading' && busy !== 'build'} onClick={requestClose}><Icon name="close" size={20} /></button>}</div></header>
    {(error || notice || conflict) && <div className={`construction-banner ${error ? 'error' : ''}`} role={error ? 'alert' : 'status'}><span>{error || notice}</span>{conflict && <button className="secondary-command" disabled={editingBusy} onClick={() => { setNotice(''); void load(); }}>重新加载最新版本</button>}{error && !conflict && (!state || isBuilding || state.status === 'failed') && <button className="secondary-command" disabled={busy === 'loading'} onClick={() => void load(undefined, isBuilding, isBuilding)}>{isBuilding ? '重新查询状态' : '重试'}</button>}</div>}
    {confirmClose && <div className="construction-banner warning" role="alert"><span>预览中的 {operations.length} 项修改尚未保存。{embedded ? '更换数据源' : '关闭'}后将丢弃这些修改。</span><button className="secondary-command" onClick={() => setConfirmClose(false)}>继续编辑</button><button className="secondary-command" onClick={onClose}>丢弃并{embedded ? '更换' : '关闭'}</button></div>}
    <div className="construction-body">
      <aside className="construction-controls">
        <div className="construction-source"><div><Icon name="file" size={18} /><strong>源文件</strong><b>{(state?.source_type || dataset.file_type).replace('.', '').toUpperCase()}</b></div><p>构图结果保存在此数据集中，可重复调用，无需每次重新构图。</p>{!graph && <>
          <fieldset className="construction-build-options" disabled={editingBusy || !state || state.algorithm_available === false}>
            {!semanticTextMode && <label>验证查询数量<input type="number" min={1} max={100} step={1} value={queryCount} onChange={event => setQueryCount(event.target.value)} /></label>}
            {!semanticTextMode && <label>领域类别<select value={domainCategory} onChange={event => setDomainCategory(event.target.value as ConstructionDomainCategory)}>{DOMAIN_CATEGORIES.map(category => <option key={category.value} value={category.value}>{category.label}</option>)}</select></label>}
            {supportsDelimiter && <label>{sourceType === 'txt' ? 'TXT 处理方式' : '文本分隔符（选填）'}<select aria-label={sourceType === 'txt' ? 'TXT 处理方式' : '文本分隔符（选填）'} value={delimiter} onChange={event => setDelimiter(event.target.value)}><option value="">{sourceType === 'txt' ? '自动：制表符表格，否则逐行记录' : '默认逗号'}</option>{sourceType === 'txt' && <option value="__entities__">智能抽取实体与关系（LLM）</option>}{sourceType === 'txt' && <option value="__lines__">强制逐行记录（不推断关系）</option>}<option value={'\t'}>制表符</option><option value=",">逗号</option><option value="|">竖线</option><option value=";">分号</option><option value=" ">空格</option></select><small className={semanticTextMode ? 'construction-text-disclosure' : undefined}>{sourceType === 'txt' ? semanticTextMode ? 'TXT 全文将分块发送至构图模型；LLM 会识别文本中明确出现的实体和关系，生成结果需人工核对。' : '自动模式只识别制表符表格，否则按逐行记录构图；也可选择其他表格分隔符或强制逐行记录。' : 'CSV 留空时使用逗号；选择其他分隔符后仍会严格校验表头和列数。'}</small></label>}
            <label>{semanticTextMode ? '抽取目标（选填）' : '领域描述（选填）'}<textarea rows={3} maxLength={16000} value={domain} onChange={event => setDomain(event.target.value)} placeholder={semanticTextMode ? '例如：关注人物、组织、地点及其明确关系' : '例如：电商订单履约，关注客户、商品和订单之间的关系'} /></label>
            {!semanticTextMode && <><label className="construction-check"><input type="checkbox" checked={experienceEnabled} onChange={event => setExperienceEnabled(event.target.checked)} aria-describedby="construction-experience-help" />复用领域经验</label>
            <small id="construction-experience-help">仅在构图模块内复用同一用户、同一领域的经验。新图仍需验收；关闭后，本次构图不读取或写入经验。</small></>}
          </fieldset>
          <button className="primary-command" onClick={build} disabled={editingBusy || !state || state.algorithm_available === false}>{isBuilding ? '正在构图…' : state?.algorithm_available === false ? '构图算法待接入' : state?.status === 'failed' ? '重新构图' : '开始构图'}</button>
          <small>{isBuilding ? '构图在服务器持续运行，每 3 秒查询一次状态。关闭窗口后，可重新打开查看进度与结果。' : state?.algorithm_available === false ? '此文件类型的构图算法暂不可用，源文件已保留。' : semanticTextMode ? '依据 LLM 从逐行文本中抽取实体与关系并构图；语义结果需要人工核对。' : '依据主键与外键生成初始图，并使用领域查询验证和优化图结构。'}</small>
        </>}</div>
        <div className="construction-rule-heading"><strong>图数据优化</strong><span>6 项操作</span></div>
        <div className="construction-rule-list" role="group" aria-label="优化规则">{RULES.map((item, index) => <button key={item.id} className={rule === item.id ? 'active' : ''} aria-pressed={rule === item.id} onClick={() => switchRule(item.id)} disabled={editingBusy || !graph || conflict}><span>{String(index + 1).padStart(2, '0')}</span>{item.label}<Icon name="chevron" size={13} /></button>)}</div>
        {graph && <form className="construction-form" onSubmit={event => { event.preventDefault(); try { void updatePreview([...operations, makeOperation()]); } catch (cause) { setError(failure(cause)); } }}><h3>{activeRule.label}</h3><p>{activeRule.description}</p><fieldset disabled={editingBusy || conflict}>
          {(rule === 'delete' || rule === 'rename_type') && <label>对象类别<select value={target} onChange={event => { setTarget(event.target.value as 'node' | 'edge'); setProperty(''); setOldType(''); }}><option value="node">节点</option><option value="edge">边</option></select></label>}
          {rule === 'delete' && <label>删除内容<select value={deleteMode} onChange={event => { setDeleteMode(event.target.value as 'entity' | 'property'); setProperty(''); }}><option value="entity">对象本身</option><option value="property">所选对象的属性</option></select></label>}
          {(wantsNodes || wantsEdges) && <label>查找对象<input value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索 ID、类型或属性" /></label>}
          {wantsNodes && <label>{['extract_property_node', 'delete'].includes(rule) ? '选择节点（可多选）' : '选择节点'}<select aria-label="选择节点" multiple={['extract_property_node', 'delete'].includes(rule)} size={['extract_property_node', 'delete'].includes(rule) ? 4 : undefined} value={['extract_property_node', 'delete'].includes(rule) ? nodeIds : nodeIds[0] || ''} onChange={event => { const ids = idsFromSelect(event.currentTarget).filter(Boolean); setNodeIds(ids); setSelected(nodeById.get(ids[0]) || null); setSchemaSelected(null); setProperty(''); setEdgeIds([]); }}><option value="" disabled>请选择实例节点，可先搜索定位</option>{filteredNodes.slice(0, 500).map(node => <option value={String(node.id)} key={String(node.id)}>{String(node.id)} · {String(node.type || '节点')}</option>)}</select></label>}
          {wantsEdges && <label>{rule === 'node_property_to_edge' ? '接收属性的关联边（可多选）' : '选择边（可多选）'}<select aria-label="选择边" multiple size={4} value={edgeIds} onChange={event => setEdgeIds(idsFromSelect(event.currentTarget))}>{filteredEdges.slice(0, 500).map(edge => <option value={String(edge.id)} key={String(edge.id)}>{String(edge.source)} → {String(edge.target)} · {String(edge.type || '关系')} [{String(edge.id)}]</option>)}</select>{!filteredEdges.length && <small>暂无匹配的边{rule === 'node_property_to_edge' ? '，请先选择节点' : ''}</small>}</label>}
          {(wantsNodes || wantsEdges) && <small>多选时按住 Ctrl / ⌘；列表最多显示 500 项，可搜索定位更多对象。</small>}
          {wantsProperty && <label>选择属性<select aria-label="选择属性" value={property} onChange={event => setProperty(event.target.value)}><option value="">请选择属性</option>{availableProperties.map(key => <option key={key} value={key}>{key}</option>)}</select>{property && selectedNode && !(rule === 'delete' && target === 'edge') && <small>当前值：{display(properties(selectedNode)[property])}</small>}</label>}
          {rule === 'node_property_to_edge' && <label>目标边属性名<input value={targetProperty} onChange={event => setTargetProperty(event.target.value)} placeholder="留空则保留原属性名" /></label>}
          {rule === 'extract_property_node' && <label>新节点类型<input value={nodeType} onChange={event => setNodeType(event.target.value)} placeholder="例如：电话号码" /></label>}
          {['collapse_node', 'extract_property_node'].includes(rule) && <label>新关系类型<input value={edgeType} onChange={event => setEdgeType(event.target.value)} placeholder="例如：关联 / 拥有号码" /></label>}
          {rule === 'collapse_node' && <small>只支持恰有两条关联边的中间节点；有向图需满足一入一出。原有属性会保留到新边的溯源信息中。</small>}
          {['node_property_to_edge', 'extract_property_node'].includes(rule) && <label className="construction-check"><input type="checkbox" checked={removeProperty} onChange={event => setRemoveProperty(event.target.checked)} />从原节点移除该属性</label>}
          {rule === 'delete' && target === 'node' && deleteMode === 'entity' && <label className="construction-check"><input type="checkbox" checked={cascade} onChange={event => setCascade(event.target.checked)} />同时删除与该节点相连的边</label>}
          {rule === 'rename_type' && <><label>原类型<select value={oldType} onChange={event => setOldType(event.target.value)}><option value="">请选择原类型</option>{types.map(type => <option key={type} value={type}>{type}</option>)}</select></label><label>新类型名称<input value={newType} onChange={event => setNewType(event.target.value)} placeholder="输入业务名称" /></label><small>将应用于当前图中所有使用该类型的{target === 'node' ? '节点' : '边'}。</small></>}
          <button className="primary-command construction-preview-button" type="submit">{busy === 'preview' ? '正在预览…' : '应用并预览'}</button>
        </fieldset></form>}
      </aside>
      <main className="construction-visual"><div className="construction-visual-head"><div><strong>{preview ? 'Graph Schema · 修改预览' : 'Graph Schema'}</strong><span>{preview ? '预览尚未保存；类型与关系统计已按预览实时更新' : graph ? '节点代表节点类型，连线代表关系类型' : '构图完成后展示节点类型与关系类型'}</span>{schemaWarning && !preview && <span className="construction-schema-warning">{schemaWarning}</span>}</div>{graph && <div className="construction-counts"><span><b>{schemaNodeTypeCount.toLocaleString()}</b> 节点类型</span><span><b>{schemaEdgeTypeCount.toLocaleString()}</b> 关系类型</span></div>}</div>
        {busy === 'loading' ? <div className="construction-empty"><span className="loader" /><strong>正在读取构图状态</strong></div> : graph && canvasGraph ? <><div className="construction-graph"><GraphWorkspace data={canvasGraph} tab={tab} onTab={setTab} selected={schemaSelected} onSelect={setSchemaSelected} collapsed={false} onToggle={() => undefined} embedded /></div><div className="construction-canvas-note">{`Graph Schema ${preview ? '预览已' : '已'}聚合完整${preview ? '预览' : ''}图中的 ${Number(activeSchema?.schema.node_count ?? nodes.length).toLocaleString()} 个节点和 ${Number(activeSchema?.schema.edge_count ?? edges.length).toLocaleString()} 条边。拖动类型节点、滚轮缩放，点击节点类型可查看属性路径与实例数量；实例级编辑仍在左侧选择。`}</div><div className="construction-inspection">{schemaSelected && <div className="construction-properties construction-schema-properties"><strong>节点类型 {selectedSchemaTypeName}</strong><span>{Number(selectedSchemaProperties.instance_count || 0).toLocaleString()} 个实例 · {selectedSchemaPaths.length.toLocaleString()} 个属性路径</span>{selectedSchemaPaths.length ? <div className="construction-schema-paths">{selectedSchemaPaths.slice(0, 24).map(item => <span key={item.path} title={item.types?.join(' / ')}>{item.path}<small>{item.types?.join(' / ') || 'unknown'}</small></span>)}{selectedSchemaPaths.length > 24 && <em>另有 {selectedSchemaPaths.length - 24} 个属性路径</em>}</div> : <span>该节点类型没有属性路径</span>}</div>}{currentSelection && <div className="construction-properties"><strong>实例节点 {String(currentSelection.id)} 的属性</strong>{Object.keys(properties(currentSelection)).length ? <dl>{Object.entries(properties(currentSelection)).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{display(value)}</dd></div>)}</dl> : <span>此节点暂无属性</span>}</div>}{inspectedEdges.map(edge => <div className="construction-properties" key={String(edge.id)}><strong>实例边 {String(edge.id)} · {String(edge.source)} → {String(edge.target)}</strong><span>类型：{String(edge.type)}</span>{Object.keys(properties(edge)).length ? <dl>{Object.entries(properties(edge)).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{display(value)}</dd></div>)}</dl> : <span>此边暂无属性</span>}</div>)}</div></> : <div className={`construction-empty ${isBuilding ? 'with-agent-stream' : ''}`}><span className="construction-empty-icon"><Icon name="graph" size={42} /></span><strong>{isBuilding ? '正在构建并验证图数据' : state?.algorithm_available === false ? '源文件已就绪，等待构图算法' : '从文件到可分析的图数据'}</strong><p>{isBuilding ? <>服务器正在生成和验证图数据。<br />可以关闭窗口，稍后重新打开查看结果。</> : <>节点、关系和属性将在构图完成后呈现在这里。<br />源文件已保存在数据集中，可随时回来继续。</>}</p>{isBuilding ? <ConstructionAgentStream state={state} /> : <div className="construction-flow"><span>SQL / CSV / TXT</span><Icon name="arrow-right" size={17} /><span>构图</span><Icon name="arrow-right" size={17} /><span>Graph Schema</span></div>}</div>}
      </main>
    </div>
    <footer className="construction-footer"><div className="construction-save-info">{preview ? <><strong>{operations.length} 项修改待保存</strong><span>节点 {preview.summary.before.node_count} → {preview.summary.after.node_count} · 边 {preview.summary.before.edge_count} → {preview.summary.after.edge_count}</span><details><summary>查看修改清单</summary><ol>{operations.map((operation, index) => <li key={index}>{operationLabel(operation)}</li>)}</ol></details></> : <><strong>{graph ? '构图结果已保存' : '源文件已保存'}</strong><span>{graph ? '可直接使用此数据集，或继续优化图数据。' : isBuilding ? '构图在服务器继续运行，可关闭窗口后再查看。' : state?.algorithm_available === false ? '此文件类型暂不可构图，无需重复上传。' : '设置查询数量和领域信息后即可开始构图。'}</span></>}</div><div className="construction-footer-actions">{operations.length > 0 && <><button className="secondary-command" disabled={editingBusy} onClick={() => void updatePreview(operations.slice(0, -1))}>撤销上一步</button><button className="secondary-command" disabled={editingBusy} onClick={() => void updatePreview([])}>还原全部</button></>}{onUse && graph && !operations.length && <button className="secondary-command" disabled={editingBusy} onClick={() => void useDataset()}>{busy === 'use' ? '正在使用…' : '在当前会话使用'}</button>}<button className="primary-command" disabled={editingBusy || !operations.length || conflict} onClick={() => void save()}><Icon name="check" size={15} />{busy === 'save' ? '正在保存…' : '保存到数据集'}</button></div></footer>
  </section></div>;
}

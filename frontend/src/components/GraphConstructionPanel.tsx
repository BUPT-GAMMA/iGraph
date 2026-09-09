import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, buildDatasetGraph, commitConstruction, getConstructionGraph, getConstructionState, previewConstruction } from '../api';
import type { ConstructionDomainCategory, ConstructionOperation, ConstructionResult, ConstructionState, Dataset, GraphData } from '../types';
import { GraphWorkspace } from './GraphWorkspace';
import { Icon } from './Icon';
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

function properties(item: Record<string, unknown> | undefined) {
  const value = item?.properties;
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function display(value: unknown) { return typeof value === 'object' ? JSON.stringify(value) : String(value ?? ''); }
function failure(cause: unknown) { return cause instanceof Error ? cause.message : '操作失败，请重试'; }
function operationLabel(operation: ConstructionOperation) { return RULES.find(rule => rule.id === operation.op)?.label || operation.op; }
function idsFromSelect(select: HTMLSelectElement) { return Array.from(select.selectedOptions, option => option.value); }

type Props = {
  dataset: Dataset;
  onClose: () => void;
  onSaved: (dataset: Dataset, graph: GraphData, revision: number) => void;
  onUse?: (dataset: Dataset) => Promise<void>;
};

export function GraphConstructionPanel({ dataset, onClose, onSaved, onUse }: Props) {
  const [state, setState] = useState<ConstructionState | null>(null);
  const [savedGraph, setSavedGraph] = useState<GraphData | null>(null);
  const [preview, setPreview] = useState<ConstructionResult | null>(null);
  const [operations, setOperations] = useState<ConstructionOperation[]>([]);
  const [busy, setBusy] = useState('loading');
  const [monitoringBuild, setMonitoringBuild] = useState(false);
  const [queryCount, setQueryCount] = useState('15');
  const [domain, setDomain] = useState('');
  const [domainCategory, setDomainCategory] = useState<ConstructionDomainCategory>('auto');
  const [experienceEnabled, setExperienceEnabled] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const [tab, setTab] = useState<'Overview' | 'Graph' | 'Data' | 'ML' | 'Artifacts'>('Graph');
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
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
  const callbacks = useRef({ dataset, onSaved });
  callbacks.current = { dataset, onSaved };
  const isBuilding = state?.status === 'building' || monitoringBuild || busy === 'build';
  const editingBusy = Boolean(busy) || isBuilding;
  const graph = preview?.graph || savedGraph;
  const nodes = graph?.nodes || [];
  const edges = graph?.edges || [];
  const selectedNode = nodes.find(node => String(node.id) === nodeIds[0]);
  const activeRule = RULES.find(item => item.id === rule)!;
  const availableProperties = useMemo(() => {
    const records = rule === 'delete' && target === 'edge' ? edges.filter(edge => edgeIds.includes(String(edge.id))) : nodes.filter(node => nodeIds.includes(String(node.id)));
    return Array.from(new Set(records.flatMap(record => Object.keys(properties(record))))).sort();
  }, [rule, target, graph, nodeIds, edgeIds]);
  const types = Array.from(new Set((target === 'node' ? nodes : edges).map(item => String(item.type || '')))).filter(Boolean).sort();
  const currentSelection = selected ? nodes.find(node => String(node.id) === String(selected.id)) : undefined;

  const load = useCallback(async (signal?: AbortSignal, quiet = false, publish = false) => {
    const version = lifecycle.current;
    const attempt = buildAttempt.current;
    if (!quiet) { setBusy('loading'); setError(''); }
    try {
      const nextState = await getConstructionState(dataset, signal);
      const snapshot = nextState.status === 'ready' ? await getConstructionGraph(dataset, signal) : null;
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
      setState(snapshot ? { ...nextState, revision: snapshot.revision } : nextState);
      const waiting = nextState.status === 'building' || nextState.status === 'not_built' && postPending.current;
      setMonitoringBuild(waiting);
      setError(nextState.status === 'failed' ? nextState.message || '构图失败，源文件已保留。可调整参数后重试。' : '');
      if (!quiet || snapshot) {
        setSavedGraph(snapshot?.graph || null); setPreview(null); setOperations([]); setConflict(false);
        setSelected(null); setNodeIds([]); setEdgeIds([]); setProperty('');
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
    setState(null); setSavedGraph(null); setMonitoringBuild(false); setNotice(''); setQueryCount('15'); setDomain('');
    setDomainCategory('auto'); setExperienceEnabled(true);
    const controller = new AbortController();
    const priorFocus = document.activeElement as HTMLElement | null;
    closeButton.current?.focus();
    void load(controller.signal, false, dataset.status !== 'ready');
    return () => { alive.current = false; lifecycle.current += 1; controller.abort(); priorFocus?.focus(); };
  }, [load]);

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
    if (!Number.isInteger(count) || count < 1 || count > 100) {
      setError('验证查询数量必须是 1 到 100 之间的整数。'); return;
    }
    const version = lifecycle.current;
    const attempt = ++buildAttempt.current;
    postPending.current = true; buildResolved.current = false; notifiedRevision.current = null;
    currentBuildId.current = null;
    setMonitoringBuild(true);
    setBusy('build'); setError(''); setNotice('');
    try {
      // No AbortSignal: closing this view must not cancel the server-side build.
      const result = await buildDatasetGraph(dataset, {
        query_count: count, domain: domain.trim(), domain_category: domainCategory, experience_enabled: experienceEnabled,
      });
      if (!alive.current || version !== lifecycle.current || attempt !== buildAttempt.current || buildResolved.current) return;
      postPending.current = false;
      currentBuildId.current = result.build_id || null;
      if (result.status === 'ready') {
        const snapshot = await getConstructionGraph(dataset);
        if (!alive.current || version !== lifecycle.current || attempt !== buildAttempt.current || buildResolved.current) return;
        buildResolved.current = true; setMonitoringBuild(false);
        setState({ ...result, revision: snapshot.revision }); setSavedGraph(snapshot.graph);
        if (notifiedRevision.current !== snapshot.revision) {
          notifiedRevision.current = snapshot.revision;
          onSaved(dataset, snapshot.graph, snapshot.revision);
        }
        setNotice('构图已保存到此数据集，后续可直接使用。');
      } else {
        setState(result); setMonitoringBuild(result.status === 'building');
        if (result.status === 'failed') setError(result.message || '构图失败，源文件已保留。');
        else setNotice(result.message || (result.status === 'building' ? '构图正在服务器上运行，可以关闭此窗口后再查看。' : '源文件已保留，可稍后继续。'));
      }
    } catch (cause) {
      if (!alive.current || version !== lifecycle.current || attempt !== buildAttempt.current || buildResolved.current) return;
      postPending.current = false;
      if (cause instanceof ApiError && ['construction_capacity_exhausted', 'construction_build_conflict', 'construction_busy'].includes(cause.code || '')) {
        buildResolved.current = true;
        setMonitoringBuild(false); setNotice('');
        handleFailure(cause);
      } else if (cause instanceof ApiError && (cause.status === 501 || cause.code === 'algorithm_unavailable')) {
        setMonitoringBuild(false);
        handleFailure(cause);
        setState(previous => previous ? { ...previous, status: 'algorithm_unavailable', algorithm_available: false } : previous);
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
    if (!next.length) { setOperations([]); setPreview(null); setSelected(null); setNotice('已还原到保存的版本。'); return; }
    setBusy('preview'); setError(''); setNotice('');
    try {
      const result = await previewConstruction(dataset, state.revision, next);
      if (!alive.current) return;
      setOperations(next); setPreview(result); setSelected(null); setNodeIds([]); setEdgeIds([]); setProperty('');
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
      setState({ ...state, revision: result.revision, status: 'ready', node_count: result.graph.nodes.length, edge_count: result.graph.edges.length });
      setSavedGraph(result.graph); setPreview(null); setOperations([]); setSelected(null);
      onSaved(dataset, result.graph, result.revision);
      setNotice(`已保存版本 ${result.revision}。该数据集可在后续会话中直接使用。`);
    } catch (cause) { if (alive.current) handleFailure(cause); }
    finally { if (alive.current) setBusy(''); }
  }
  async function useDataset() {
    if (!graph || !state || !onUse) return;
    setBusy('use'); setError('');
    try { await onUse({ ...dataset, status: 'ready' }); onClose(); }
    catch (cause) { if (alive.current) handleFailure(cause); }
    finally { if (alive.current) setBusy(''); }
  }
  function selectNode(node: Record<string, unknown>) { setSelected(node); setNodeIds([String(node.id)]); setProperty(''); setEdgeIds([]); }
  function switchRule(next: Rule) { setRule(next); setError(''); setProperty(''); setEdgeIds([]); }
  const wantsNodes = ['collapse_node', 'node_property_to_edge', 'extract_property_node'].includes(rule) || rule === 'delete' && target === 'node';
  const wantsEdges = ['node_property_to_edge', 'reverse_edges'].includes(rule) || rule === 'delete' && target === 'edge';
  const wantsProperty = ['node_property_to_edge', 'extract_property_node'].includes(rule) || rule === 'delete' && deleteMode === 'property';
  const selectableEdges = rule === 'node_property_to_edge' ? edges.filter(edge => String(edge.source) === nodeIds[0] || String(edge.target) === nodeIds[0]) : edges;
  const filteredNodes = nodes.filter(node => !search.trim() || `${node.id} ${node.type} ${display(properties(node))}`.toLowerCase().includes(search.toLowerCase()));
  const filteredEdges = selectableEdges.filter(edge => !search.trim() || `${edge.id} ${edge.source} ${edge.target} ${edge.type}`.toLowerCase().includes(search.toLowerCase()));

  return <div className="construction-backdrop"><section className={`construction-panel ${graph ? 'has-graph' : ''}`} role="dialog" aria-modal="true" aria-labelledby="construction-title" ref={dialog} onKeyDown={event => {
    if (event.key === 'Escape') { event.stopPropagation(); requestClose(); }
    if (event.key === 'Tab') {
      const focusable = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]') || []).filter(element => element.getClientRects().length > 0);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  }}>
    <header className="construction-header"><div className="construction-heading"><span className="construction-mark"><Icon name="graph" size={23} /></span><div><span className="construction-eyebrow">数据集 / 构图</span><h2 id="construction-title">{dataset.name}</h2><p>{state?.source_filename || dataset.original_filename}</p></div></div><div className="construction-header-actions"><span className={`construction-status ${graph && !isBuilding ? 'ready' : ''}`}>{busy === 'loading' ? '读取中' : isBuilding ? '构图进行中' : graph ? `已保存 · 版本 ${state?.revision}` : state?.status === 'failed' ? '构图失败' : '等待构图'}</span><button ref={closeButton} className="plain-icon" aria-label="关闭构图工作台" disabled={Boolean(busy) && busy !== 'loading' && busy !== 'build'} onClick={requestClose}><Icon name="close" size={20} /></button></div></header>
    {(error || notice || conflict) && <div className={`construction-banner ${error ? 'error' : ''}`} role={error ? 'alert' : 'status'}><span>{error || notice}</span>{conflict && <button className="secondary-command" disabled={editingBusy} onClick={() => { setNotice(''); void load(); }}>重新加载最新版本</button>}{error && !conflict && (!state || isBuilding || state.status === 'failed') && <button className="secondary-command" disabled={busy === 'loading'} onClick={() => void load(undefined, isBuilding, isBuilding)}>{isBuilding ? '重新查询状态' : '重试'}</button>}</div>}
    {confirmClose && <div className="construction-banner warning" role="alert"><span>预览中的 {operations.length} 项修改尚未保存。关闭后将丢弃这些修改。</span><button className="secondary-command" onClick={() => setConfirmClose(false)}>继续编辑</button><button className="secondary-command" onClick={onClose}>丢弃并关闭</button></div>}
    <div className="construction-body">
      <aside className="construction-controls">
        <div className="construction-source"><div><Icon name="file" size={18} /><strong>源文件</strong><b>{(state?.source_type || dataset.file_type).replace('.', '').toUpperCase()}</b></div><p>构图结果保存在此数据集中，可重复调用，无需每次重新构图。</p>{!graph && <>
          <fieldset className="construction-build-options" disabled={editingBusy || !state || state.algorithm_available === false}>
            <label>验证查询数量<input type="number" min={1} max={100} step={1} value={queryCount} onChange={event => setQueryCount(event.target.value)} /></label>
            <label>领域类别<select value={domainCategory} onChange={event => setDomainCategory(event.target.value as ConstructionDomainCategory)}>{DOMAIN_CATEGORIES.map(category => <option key={category.value} value={category.value}>{category.label}</option>)}</select></label>
            <label>领域描述（选填）<textarea rows={3} maxLength={16000} value={domain} onChange={event => setDomain(event.target.value)} placeholder="例如：电商订单履约，关注客户、商品和订单之间的关系" /></label>
            <label className="construction-check"><input type="checkbox" checked={experienceEnabled} onChange={event => setExperienceEnabled(event.target.checked)} aria-describedby="construction-experience-help" />复用领域经验</label>
            <small id="construction-experience-help">仅在构图模块内复用同一用户、同一领域的经验。新图仍需验收；关闭后，本次构图不读取或写入经验。</small>
          </fieldset>
          <button className="primary-command" onClick={build} disabled={editingBusy || !state || state.algorithm_available === false}>{isBuilding ? '正在构图…' : state?.algorithm_available === false ? '构图算法待接入' : state?.status === 'failed' ? '重新构图' : '开始构图'}</button>
          <small>{isBuilding ? '构图在服务器持续运行，每 3 秒查询一次状态。关闭窗口后，可重新打开查看进度与结果。' : state?.algorithm_available === false ? '此文件类型的构图算法暂不可用，源文件已保留。' : '依据主键与外键生成初始图，并使用领域查询验证和优化图结构。'}</small>
        </>}</div>
        <div className="construction-rule-heading"><strong>图数据优化</strong><span>6 项操作</span></div>
        <div className="construction-rule-list" role="group" aria-label="优化规则">{RULES.map((item, index) => <button key={item.id} className={rule === item.id ? 'active' : ''} aria-pressed={rule === item.id} onClick={() => switchRule(item.id)} disabled={editingBusy || !graph || conflict}><span>{String(index + 1).padStart(2, '0')}</span>{item.label}<Icon name="chevron" size={13} /></button>)}</div>
        {graph && <form className="construction-form" onSubmit={event => { event.preventDefault(); try { void updatePreview([...operations, makeOperation()]); } catch (cause) { setError(failure(cause)); } }}><h3>{activeRule.label}</h3><p>{activeRule.description}</p><fieldset disabled={editingBusy || conflict}>
          {(rule === 'delete' || rule === 'rename_type') && <label>对象类别<select value={target} onChange={event => { setTarget(event.target.value as 'node' | 'edge'); setProperty(''); setOldType(''); }}><option value="node">节点</option><option value="edge">边</option></select></label>}
          {rule === 'delete' && <label>删除内容<select value={deleteMode} onChange={event => { setDeleteMode(event.target.value as 'entity' | 'property'); setProperty(''); }}><option value="entity">对象本身</option><option value="property">所选对象的属性</option></select></label>}
          {(wantsNodes || wantsEdges) && <label>查找对象<input value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索 ID、类型或属性" /></label>}
          {wantsNodes && <label>{['extract_property_node', 'delete'].includes(rule) ? '选择节点（可多选）' : '选择节点'}<select aria-label="选择节点" multiple={['extract_property_node', 'delete'].includes(rule)} size={['extract_property_node', 'delete'].includes(rule) ? 4 : undefined} value={['extract_property_node', 'delete'].includes(rule) ? nodeIds : nodeIds[0] || ''} onChange={event => { const ids = idsFromSelect(event.currentTarget).filter(Boolean); setNodeIds(ids); setSelected(nodes.find(node => String(node.id) === ids[0]) || null); setProperty(''); setEdgeIds([]); }}><option value="" disabled>请选择节点，也可点击图中节点</option>{filteredNodes.slice(0, 500).map(node => <option value={String(node.id)} key={String(node.id)}>{String(node.id)} · {String(node.type || '节点')}</option>)}</select></label>}
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
      <main className="construction-visual"><div className="construction-visual-head"><div><strong>{preview ? '修改预览' : '图数据'}</strong><span>{preview ? '预览尚未保存' : graph ? '已保存的数据集版本' : '构图完成后展示节点与关系'}</span></div>{graph && <div className="construction-counts"><span><b>{nodes.length.toLocaleString()}</b> 节点</span><span><b>{edges.length.toLocaleString()}</b> 边</span></div>}</div>
        {busy === 'loading' ? <div className="construction-empty"><span className="loader" /><strong>正在读取构图状态</strong></div> : graph ? <><div className="construction-graph"><GraphWorkspace data={graph} tab={tab} onTab={setTab} selected={currentSelection || null} onSelect={selectNode} collapsed={false} onToggle={() => undefined} embedded /></div><div className="construction-canvas-note">画布最多展示 180 个节点和 400 条边；上方统计及保存操作针对完整图数据。拖动节点、滚轮缩放，点击节点可查看属性。</div><div className="construction-inspection">{currentSelection && <div className="construction-properties"><strong>节点 {String(currentSelection.id)} 的属性</strong>{Object.keys(properties(currentSelection)).length ? <dl>{Object.entries(properties(currentSelection)).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{display(value)}</dd></div>)}</dl> : <span>此节点暂无属性</span>}</div>}{edges.filter(edge => edgeIds.includes(String(edge.id))).slice(0, 10).map(edge => <div className="construction-properties" key={String(edge.id)}><strong>边 {String(edge.id)} · {String(edge.source)} → {String(edge.target)}</strong><span>类型：{String(edge.type)}</span>{Object.keys(properties(edge)).length ? <dl>{Object.entries(properties(edge)).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{display(value)}</dd></div>)}</dl> : <span>此边暂无属性</span>}</div>)}</div></> : <div className="construction-empty"><span className="construction-empty-icon"><Icon name="graph" size={42} /></span><strong>{isBuilding ? '正在构建并验证图数据' : state?.algorithm_available === false ? '源文件已就绪，等待构图算法' : '从文件到可分析的图数据'}</strong><p>{isBuilding ? <>服务器正在生成和验证图数据。<br />可以关闭窗口，稍后重新打开查看结果。</> : <>节点、关系和属性将在构图完成后呈现在这里。<br />源文件已保存在数据集中，可随时回来继续。</>}</p><div className="construction-flow"><span>SQL / CSV / TXT</span><Icon name="arrow-right" size={17} /><span>构图</span><Icon name="arrow-right" size={17} /><span>可复用的图数据</span></div></div>}
      </main>
    </div>
    <footer className="construction-footer"><div className="construction-save-info">{preview ? <><strong>{operations.length} 项修改待保存</strong><span>节点 {preview.summary.before.node_count} → {preview.summary.after.node_count} · 边 {preview.summary.before.edge_count} → {preview.summary.after.edge_count}</span><details><summary>查看修改清单</summary><ol>{operations.map((operation, index) => <li key={index}>{operationLabel(operation)}</li>)}</ol></details></> : <><strong>{graph ? '构图结果已保存' : '源文件已保存'}</strong><span>{graph ? '可直接使用此数据集，或继续优化图数据。' : isBuilding ? '构图在服务器继续运行，可关闭窗口后再查看。' : state?.algorithm_available === false ? '此文件类型暂不可构图，无需重复上传。' : '设置查询数量和领域信息后即可开始构图。'}</span></>}</div><div className="construction-footer-actions">{operations.length > 0 && <><button className="secondary-command" disabled={editingBusy} onClick={() => void updatePreview(operations.slice(0, -1))}>撤销上一步</button><button className="secondary-command" disabled={editingBusy} onClick={() => void updatePreview([])}>还原全部</button></>}{onUse && graph && !operations.length && <button className="secondary-command" disabled={editingBusy} onClick={() => void useDataset()}>{busy === 'use' ? '正在使用…' : '在当前会话使用'}</button>}<button className="primary-command" disabled={editingBusy || !operations.length || conflict} onClick={() => void save()}><Icon name="check" size={15} />{busy === 'save' ? '正在保存…' : '保存到数据集'}</button></div></footer>
  </section></div>;
}

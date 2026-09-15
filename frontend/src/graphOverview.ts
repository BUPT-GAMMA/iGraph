import type { GraphData } from './types';
import { sampleStructure, topologyFirstEdges } from './structureSampling';

export const GRAPH_OVERVIEW_NODE_LIMIT = 800;
export const GRAPH_OVERVIEW_EDGE_LIMIT = 2_000;

type GraphRecord = Record<string, unknown>;

type NodeEntry = {
  id: string;
  node: GraphRecord;
  degree: number;
  type: string;
  community?: string;
  sourceDegree?: number;
};

type EdgeEntry = {
  index: number;
  edge: GraphRecord;
  source: string;
  target: string;
  stableKey: string;
};

export type GraphOverviewOptions = {
  nodeLimit?: number;
  edgeLimit?: number;
  query?: string;
  selectedNodeId?: unknown;
  highlightedNodeIds?: Iterable<unknown>;
  highlightedEdgePairs?: Iterable<string>;
  connectedOnly?: boolean;
  /** Schema graphs are already aggregates, so every schema element remains visible. */
  forceComplete?: boolean;
};

export type GraphOverview = {
  nodes: GraphRecord[];
  nodeIds: string[];
  edges: GraphRecord[];
  mode: 'complete' | 'overview';
  totalNodes: number;
  totalEdges: number;
  availableNodes: number;
  availableEdges: number;
  /** The API declared more data than this payload contains. */
  sourcePartial: boolean;
  locallySampledNodes: boolean;
  locallySampledEdges: boolean;
  queryMatchCount: number;
  retainedQueryMatchCount: number;
  highlightedNodeCount: number;
  retainedHighlightedNodeCount: number;
  selectedNodeRetained: boolean;
};

function nodeId(node: GraphRecord, index: number) {
  return String(node.id ?? node.label ?? index);
}

function endpointId(value: unknown) {
  return String(value && typeof value === 'object' && 'id' in value ? (value as { id: unknown }).id : value ?? '');
}

function edgeEndpoints(edge: GraphRecord) {
  return {
    source: endpointId(edge.src ?? edge.source),
    target: endpointId(edge.dst ?? edge.target),
  };
}

function graphCount(...values: unknown[]) {
  return values.reduce<number>((largest, value) => {
    const candidate = Number(value);
    return Number.isFinite(candidate) && candidate >= 0 ? Math.max(largest, Math.trunc(candidate)) : largest;
  }, 0);
}

function nodeType(node: GraphRecord) {
  const attrs = node.attrs && typeof node.attrs === 'object' && !Array.isArray(node.attrs)
    ? node.attrs as GraphRecord
    : undefined;
  const properties = node.properties && typeof node.properties === 'object' && !Array.isArray(node.properties)
    ? node.properties as GraphRecord
    : undefined;
  return String(node.type ?? node.category ?? node.kind ?? node.table ?? attrs?.type ?? attrs?.node_type ?? attrs?.category ?? properties?.table ?? '未分类');
}

function stableValue(value: unknown, depth = 0, seen = new WeakSet<object>()): string {
  if (value == null || typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') return String(value);
  if (typeof value === 'string') return JSON.stringify(value.slice(0, 256));
  if (typeof value !== 'object') return String(value);
  if (seen.has(value)) return '[Circular]';
  if (depth >= 4) return '[Object]';
  seen.add(value);
  if (Array.isArray(value)) return `[${value.slice(0, 64).map(item => stableValue(item, depth + 1, seen)).join(',')}]`;
  const record = value as GraphRecord;
  return `{${Object.keys(record).sort().slice(0, 64).map(key => `${JSON.stringify(key)}:${stableValue(record[key], depth + 1, seen)}`).join(',')}}`;
}

function stableHash(value: string) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function compareIds(left: string, right: string) {
  const hashDifference = stableHash(left) - stableHash(right);
  return hashDifference || (left < right ? -1 : left > right ? 1 : 0);
}

function compareNodes(left: NodeEntry, right: NodeEntry) {
  return right.degree - left.degree || compareIds(left.id, right.id);
}

function isNodeEntry(value: NodeEntry | undefined): value is NodeEntry {
  return value !== undefined;
}

function pairKey(source: unknown, target: unknown) {
  return `${String(source)}\u0000${String(target)}`;
}

type Adjacent = { node: string; edgeIndex: number; stableKey: string };

function addRanked(selected: Set<string>, entries: Iterable<NodeEntry>, limit: number) {
  for (const entry of entries) {
    if (selected.size >= limit) break;
    selected.add(entry.id);
  }
}

/**
 * Select a stable, topology-aware SVG preview without depending on input order.
 * Components receive proportional budgets; each sampled component grows through
 * real paths and neighbors. Tree construction is linear in the loaded graph.
 */
export function createGraphOverview(data: GraphData, options: GraphOverviewOptions = {}): GraphOverview {
  const forceComplete = options.forceComplete === true;
  const nodeLimit = forceComplete ? Number.POSITIVE_INFINITY : Math.max(1, Math.trunc(options.nodeLimit ?? GRAPH_OVERVIEW_NODE_LIMIT));
  const edgeLimit = forceComplete ? Number.POSITIVE_INFINITY : Math.max(1, Math.trunc(options.edgeLimit ?? GRAPH_OVERVIEW_EDGE_LIMIT));
  const rawCommunity = data.community || data.communities;
  const communityMapping = rawCommunity?.node_community || {};
  const nodeById = new Map<string, NodeEntry>();
  data.nodes.forEach((node, index) => {
    const id = nodeId(node, index);
    if (nodeById.has(id)) return;
    const attrs = node.attrs && typeof node.attrs === 'object' && !Array.isArray(node.attrs) ? node.attrs as GraphRecord : undefined;
    const inlineCommunity = node.community ?? node.community_id ?? attrs?.community ?? attrs?.community_id;
    const community = communityMapping[id] ?? inlineCommunity;
    nodeById.set(id, { id, node, degree: 0, sourceDegree: typeof node.total_degree === 'number' ? node.total_degree : undefined, type: nodeType(node), community: community == null ? undefined : String(community) });
  });

  const edgeEntries: EdgeEntry[] = [];
  const adjacency = new Map<string, Adjacent[]>();
  data.edges.forEach((edge, index) => {
    const { source, target } = edgeEndpoints(edge);
    if (!nodeById.has(source) || !nodeById.has(target)) return;
    nodeById.get(source)!.degree += 1;
    nodeById.get(target)!.degree += 1;
    const stableKey = edge.id == null ? `${source}\u0000${target}\u0000${stableValue(edge)}` : `id:${String(edge.id)}`;
    const edgeIndex = edgeEntries.length;
    edgeEntries.push({ index: edgeIndex, edge, source, target, stableKey });
    const sourceNeighbors = adjacency.get(source);
    if (sourceNeighbors) sourceNeighbors.push({ node: target, edgeIndex, stableKey });
    else adjacency.set(source, [{ node: target, edgeIndex, stableKey }]);
    if (source !== target) {
      const targetNeighbors = adjacency.get(target);
      if (targetNeighbors) targetNeighbors.push({ node: source, edgeIndex, stableKey });
      else adjacency.set(target, [{ node: source, edgeIndex, stableKey }]);
    }
  });

  adjacency.forEach(neighbors => neighbors.sort((left, right) => {
    const leftNode = nodeById.get(left.node);
    const rightNode = nodeById.get(right.node);
    if (leftNode && rightNode) return compareNodes(leftNode, rightNode) || compareIds(left.stableKey, right.stableKey);
    return compareIds(left.node, right.node) || compareIds(left.stableKey, right.stableKey);
  }));

  const query = (options.query || '').trim().toLowerCase();
  const queryMatches = [...nodeById.values()].filter(entry => query && (
    entry.id.toLowerCase().includes(query)
    || String(entry.node.label ?? '').toLowerCase().includes(query)
  )).sort(compareNodes);
  const selectedId = options.selectedNodeId == null ? '' : String(options.selectedNodeId);
  const highlightedIds = new Set(Array.from(options.highlightedNodeIds || [], value => String(value)).filter(id => nodeById.has(id)));
  const pinnedIds = new Set<string>([
    ...(selectedId && nodeById.has(selectedId) ? [selectedId] : []),
    ...highlightedIds,
    ...queryMatches.map(entry => entry.id),
  ]);
  const eligible = [...nodeById.values()].filter(entry => !options.connectedOnly || entry.degree > 0 || pinnedIds.has(entry.id));
  const eligibleById = new Map(eligible.map(entry => [entry.id, entry]));
  const selected = new Set<string>();

  if (forceComplete || eligible.length <= nodeLimit) {
    eligible.sort((left, right) => compareIds(left.id, right.id)).forEach(entry => selected.add(entry.id));
  } else {
    // User context always wins over statistical representatives.
    if (selectedId && eligibleById.has(selectedId)) selected.add(selectedId);
    addRanked(selected, [...highlightedIds].map(id => eligibleById.get(id)).filter(isNodeEntry).sort(compareNodes), nodeLimit);
    addRanked(selected, queryMatches.filter(entry => eligibleById.has(entry.id)), nodeLimit);

    const structural = sampleStructure(eligible, adjacency, nodeLimit, selected, compareNodes);
    selected.clear(); structural.forEach(id => selected.add(id));
  }

  const selectedNodes = [...selected]
    .map(id => nodeById.get(id))
    .filter(Boolean)
    .sort((left, right) => compareIds(left!.id, right!.id)) as NodeEntry[];
  const highlightedPairs = new Set(options.highlightedEdgePairs || []);
  const rankedEdges = edgeEntries
    .filter(edge => selected.has(edge.source) && selected.has(edge.target))
    .sort((left, right) => {
      const leftHighlight = highlightedPairs.has(pairKey(left.source, left.target)) || highlightedPairs.has(pairKey(left.target, left.source));
      const rightHighlight = highlightedPairs.has(pairKey(right.source, right.target)) || highlightedPairs.has(pairKey(right.target, right.source));
      const leftPriority = leftHighlight ? 0 : pinnedIds.has(left.source) || pinnedIds.has(left.target) ? 2 : 3;
      const rightPriority = rightHighlight ? 0 : pinnedIds.has(right.source) || pinnedIds.has(right.target) ? 2 : 3;
      if (leftPriority !== rightPriority) return leftPriority - rightPriority;
      const degreeDifference = (nodeById.get(right.source)!.degree + nodeById.get(right.target)!.degree)
        - (nodeById.get(left.source)!.degree + nodeById.get(left.target)!.degree);
      return degreeDifference || compareIds(left.stableKey, right.stableKey);
    });
  const selectedEdges = topologyFirstEdges(selected, rankedEdges, edge => [edge.source, edge.target], edge => highlightedPairs.has(pairKey(edge.source, edge.target)) || highlightedPairs.has(pairKey(edge.target, edge.source)), edgeLimit);

  const totalNodes = graphCount(data.total_nodes, data.num_nodes, nodeById.size);
  const totalEdges = graphCount(data.total_edges, data.num_edges, edgeEntries.length);
  const remoteTruncated = data.truncated === true || totalNodes > nodeById.size || totalEdges > edgeEntries.length;
  const locallySampledNodes = selected.size < nodeById.size;
  const locallySampledEdges = selectedEdges.length < edgeEntries.filter(edge => selected.has(edge.source) && selected.has(edge.target)).length;
  const mode = !remoteTruncated && !locallySampledNodes && !locallySampledEdges ? 'complete' : 'overview';
  return {
    nodes: selectedNodes.map(entry => entry.node),
    nodeIds: selectedNodes.map(entry => entry.id),
    edges: selectedEdges.map(entry => entry.edge),
    mode,
    totalNodes,
    totalEdges,
    availableNodes: nodeById.size,
    availableEdges: edgeEntries.length,
    sourcePartial: remoteTruncated,
    locallySampledNodes,
    locallySampledEdges,
    queryMatchCount: queryMatches.length,
    retainedQueryMatchCount: queryMatches.filter(entry => selected.has(entry.id)).length,
    highlightedNodeCount: highlightedIds.size,
    retainedHighlightedNodeCount: [...highlightedIds].filter(id => selected.has(id)).length,
    selectedNodeRetained: !selectedId || selected.has(selectedId),
  };
}

export function graphOverviewPairKey(source: unknown, target: unknown) {
  return pairKey(source, target);
}

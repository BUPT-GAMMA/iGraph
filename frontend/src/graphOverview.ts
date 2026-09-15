import type { GraphData } from './types';

export const GRAPH_OVERVIEW_NODE_LIMIT = 800;
export const GRAPH_OVERVIEW_EDGE_LIMIT = 2_000;

type GraphRecord = Record<string, unknown>;

type NodeEntry = {
  id: string;
  node: GraphRecord;
  degree: number;
  type: string;
  community?: string;
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

function sortedGroups(entries: NodeEntry[], key: (entry: NodeEntry) => string | undefined) {
  const groups = new Map<string, NodeEntry[]>();
  entries.forEach(entry => {
    const value = key(entry);
    if (value === undefined) return;
    const group = groups.get(value);
    if (group) group.push(entry);
    else groups.set(value, [entry]);
  });
  groups.forEach(group => group.sort(compareNodes));
  return [...groups.entries()].sort(([left], [right]) => compareIds(left, right));
}

function proportionalTargets(groups: Array<[string, NodeEntry[]]>, total: number, limit: number) {
  const targets = new Map<string, number>();
  if (!groups.length || total <= 0 || limit <= 0) return targets;
  const fractions = groups.map(([key, entries]) => {
    const exact = (entries.length / total) * limit;
    return { key, exact, target: Math.floor(exact), remainder: exact - Math.floor(exact) };
  });
  if (limit >= fractions.length) fractions.forEach(item => { item.target = Math.max(1, item.target); });
  let assigned = fractions.reduce((sum, item) => sum + item.target, 0);
  while (assigned > limit) {
    const candidate = [...fractions]
      .filter(item => item.target > (limit >= fractions.length ? 1 : 0))
      .sort((left, right) => left.remainder - right.remainder || compareIds(right.key, left.key))[0];
    if (!candidate) break;
    candidate.target -= 1;
    assigned -= 1;
  }
  const additions = [...fractions].sort((left, right) => right.remainder - left.remainder || compareIds(left.key, right.key));
  let cursor = 0;
  while (assigned < limit && additions.length) {
    additions[cursor % additions.length].target += 1;
    cursor += 1;
    assigned += 1;
  }
  fractions.forEach(item => targets.set(item.key, item.target));
  return targets;
}

type Adjacent = { node: string; edgeIndex: number; stableKey: string };

function boundedPath(
  start: string,
  targets: Set<string>,
  adjacency: Map<string, Adjacent[]>,
  nodeById: Map<string, NodeEntry>,
  visitBudget: number,
  maxDepth: number,
) {
  const queue: Array<{ id: string; depth: number }> = [{ id: start, depth: 0 }];
  const visited = new Set([start]);
  const parent = new Map<string, { id: string; edgeIndex: number }>();
  let cursor = 0;
  let visits = 0;
  let found = '';
  while (cursor < queue.length && visits < visitBudget && !found) {
    const current = queue[cursor++];
    visits += 1;
    if (current.depth >= maxDepth) continue;
    const neighbors = adjacency.get(current.id) || [];
    for (const neighbor of neighbors) {
      if (visited.has(neighbor.node)) continue;
      visited.add(neighbor.node);
      parent.set(neighbor.node, { id: current.id, edgeIndex: neighbor.edgeIndex });
      if (targets.has(neighbor.node)) {
        found = neighbor.node;
        break;
      }
      queue.push({ id: neighbor.node, depth: current.depth + 1 });
    }
  }
  if (!found) return { nodes: [] as string[], edgeIndexes: [] as number[], visits };
  const pathNodes = [found];
  const edgeIndexes: number[] = [];
  let current = found;
  while (current !== start) {
    const step = parent.get(current);
    if (!step) return { nodes: [] as string[], edgeIndexes: [] as number[], visits };
    edgeIndexes.push(step.edgeIndex);
    current = step.id;
    pathNodes.push(current);
  }
  pathNodes.reverse();
  edgeIndexes.reverse();
  return { nodes: pathNodes, edgeIndexes, visits };
}

function addRanked(selected: Set<string>, entries: Iterable<NodeEntry>, limit: number) {
  for (const entry of entries) {
    if (selected.size >= limit) break;
    selected.add(entry.id);
  }
}

/**
 * Select a stable, topology-aware SVG preview without depending on input order.
 * The algorithm is deliberately bounded: at most 64 representative path searches,
 * six hops per search, and 12,000 visited nodes in total.
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
    nodeById.set(id, { id, node, degree: 0, type: nodeType(node), community: community == null ? undefined : String(community) });
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
  const pathEdgeIndexes = new Set<number>();

  if (forceComplete || eligible.length <= nodeLimit) {
    eligible.sort((left, right) => compareIds(left.id, right.id)).forEach(entry => selected.add(entry.id));
  } else {
    // User context always wins over statistical representatives.
    if (selectedId && eligibleById.has(selectedId)) selected.add(selectedId);
    addRanked(selected, [...highlightedIds].map(id => eligibleById.get(id)).filter(isNodeEntry).sort(compareNodes), nodeLimit);
    addRanked(selected, queryMatches.filter(entry => eligibleById.has(entry.id)), nodeLimit);

    const communities = sortedGroups(eligible, entry => entry.community);
    const types = sortedGroups(eligible, entry => entry.type);
    const representatives = new Map<string, NodeEntry>();
    communities.forEach(([, entries]) => { if (entries[0]) representatives.set(entries[0].id, entries[0]); });
    types.forEach(([, entries]) => { if (entries[0]) representatives.set(entries[0].id, entries[0]); });
    const rankedRepresentatives = [...representatives.values()].sort(compareNodes);
    addRanked(selected, rankedRepresentatives, nodeLimit);

    // Join nearby type/community representatives so the preview keeps meaningful
    // routes instead of a bag of disconnected high-degree vertices.
    const pathRepresentatives = rankedRepresentatives.filter(entry => selected.has(entry.id)).slice(0, 64);
    if (pathRepresentatives.length > 1 && selected.size < nodeLimit) {
      const connected = new Set([pathRepresentatives[0].id]);
      let remainingVisits = 12_000;
      const pathNodeBudget = Math.min(nodeLimit - selected.size, Math.max(8, Math.floor(nodeLimit * .2)));
      let addedPathNodes = 0;
      for (const representative of pathRepresentatives.slice(1)) {
        if (remainingVisits <= 0 || selected.size >= nodeLimit || addedPathNodes >= pathNodeBudget) break;
        const path = boundedPath(representative.id, connected, adjacency, nodeById, Math.min(1_500, remainingVisits), 6);
        remainingVisits -= path.visits;
        if (!path.nodes.length) {
          connected.add(representative.id);
          continue;
        }
        const additions = path.nodes.filter(id => !selected.has(id));
        if (additions.length + addedPathNodes > pathNodeBudget || additions.length + selected.size > nodeLimit) continue;
        additions.forEach(id => selected.add(id));
        path.nodes.forEach(id => connected.add(id));
        path.edgeIndexes.forEach(index => pathEdgeIndexes.add(index));
        addedPathNodes += additions.length;
      }
    }

    // Fill each type toward its proportional quota; within a type, degree and a
    // stable ID hash select useful representatives without depending on row order.
    const targets = proportionalTargets(types, eligible.length, nodeLimit);
    const currentByType = new Map<string, number>();
    selected.forEach(id => {
      const type = eligibleById.get(id)?.type;
      if (type) currentByType.set(type, (currentByType.get(type) || 0) + 1);
    });
    const cursors = new Map<string, number>();
    while (selected.size < nodeLimit) {
      const candidateType = types
        .map(([type, entries]) => ({ type, entries, deficit: (targets.get(type) || 0) - (currentByType.get(type) || 0) }))
        .filter(item => item.deficit > 0)
        .sort((left, right) => right.deficit - left.deficit || compareIds(left.type, right.type))[0];
      if (!candidateType) break;
      let cursor = cursors.get(candidateType.type) || 0;
      while (cursor < candidateType.entries.length && selected.has(candidateType.entries[cursor].id)) cursor += 1;
      cursors.set(candidateType.type, cursor + 1);
      const candidate = candidateType.entries[cursor];
      if (!candidate) {
        targets.set(candidateType.type, currentByType.get(candidateType.type) || 0);
        continue;
      }
      selected.add(candidate.id);
      currentByType.set(candidateType.type, (currentByType.get(candidateType.type) || 0) + 1);
    }
    addRanked(selected, eligible.filter(entry => !selected.has(entry.id)).sort(compareNodes), nodeLimit);
  }

  const selectedNodes = [...selected]
    .map(id => nodeById.get(id))
    .filter(Boolean)
    .sort((left, right) => compareIds(left!.id, right!.id)) as NodeEntry[];
  const highlightedPairs = new Set(options.highlightedEdgePairs || []);
  const selectedEdges = edgeEntries
    .filter(edge => selected.has(edge.source) && selected.has(edge.target))
    .sort((left, right) => {
      const leftHighlight = highlightedPairs.has(pairKey(left.source, left.target)) || highlightedPairs.has(pairKey(left.target, left.source));
      const rightHighlight = highlightedPairs.has(pairKey(right.source, right.target)) || highlightedPairs.has(pairKey(right.target, right.source));
      const leftPriority = leftHighlight ? 0 : pathEdgeIndexes.has(left.index) ? 1 : pinnedIds.has(left.source) || pinnedIds.has(left.target) ? 2 : 3;
      const rightPriority = rightHighlight ? 0 : pathEdgeIndexes.has(right.index) ? 1 : pinnedIds.has(right.source) || pinnedIds.has(right.target) ? 2 : 3;
      if (leftPriority !== rightPriority) return leftPriority - rightPriority;
      const degreeDifference = (nodeById.get(right.source)!.degree + nodeById.get(right.target)!.degree)
        - (nodeById.get(left.source)!.degree + nodeById.get(left.target)!.degree);
      return degreeDifference || compareIds(left.stableKey, right.stableKey);
    })
    .slice(0, edgeLimit);

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

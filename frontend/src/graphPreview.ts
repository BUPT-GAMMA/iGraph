import type { GraphData, Highlight } from './types';
import { createGraphOverview } from './graphOverview';
import { tr } from './i18n';
import type { Translate } from './i18n';

export const NODE_LIMIT = 180;
export const EDGE_LIMIT = 400;
export const endpoint = (value: unknown): string => String(value && typeof value === 'object' && 'id' in value ? (value as { id: unknown }).id : value ?? '');
export type PreviewNode = Record<string, unknown> & { id: string; degree: number; degreeLabel: string };
export type PreviewEdge = { source: string; target: string; key: string; raw: Record<string, unknown> };
export function graphIndex(data: GraphData, translate: Translate = tr) {
  const nodes = new Map<string, PreviewNode>();
  const neighbors = new Map<string, Set<string>>();
  data.nodes.forEach((node, index) => {
    const id = String(node.id ?? node.label ?? index);
    const indexed = Object.assign({ id, degree: 0, degreeLabel: translate('Preview connections', '预览连接数') }, node);
    indexed.id = id; indexed.degree = 0; indexed.degreeLabel = translate('Preview connections', '预览连接数');
    nodes.set(id, indexed);
    neighbors.set(id, new Set());
  });
  const edges: PreviewEdge[] = [];
  data.edges.forEach((raw, i) => {
    const source = endpoint(raw.src ?? raw.source), target = endpoint(raw.dst ?? raw.target);
    const sourceNode = nodes.get(source), targetNode = nodes.get(target);
    if (!sourceNode || !targetNode) return;
    edges.push({ source, target, key: String(raw.id ?? i), raw });
    sourceNode.degree++; targetNode.degree++;
    neighbors.get(source)!.add(target); neighbors.get(target)!.add(source);
  });
  nodes.forEach(node => {
    if (typeof node.total_degree === 'number' && Number.isFinite(node.total_degree) && node.total_degree >= 0) {
      node.degree = node.total_degree;
      node.degreeLabel = data.degree_scope === 'full_graph'
        ? translate('Full graph degree', '全图度数')
        : data.degree_scope === 'subgraph'
          ? translate('Subgraph degree', '子图度数')
          : translate('Data-range degree', '数据范围度数');
    }
  });
  let maxDegree = 1; nodes.forEach(node => { maxDegree = Math.max(maxDegree, node.degree); });
  const overview = nodes.size > NODE_LIMIT || edges.length > EDGE_LIMIT
    ? createGraphOverview(data, { nodeLimit: NODE_LIMIT, edgeLimit: EDGE_LIMIT }) : null;
  const overviewEdges = new Set(overview?.edges);
  return { nodes, edges, neighbors, maxDegree, directed: Boolean(data.directed), overview, overviewEdges };
}
export type GraphIndex = ReturnType<typeof graphIndex>;
export function previewGraph(index: GraphIndex, highlights: Highlight[], query: string, filterMatches: boolean, connectedOnly: boolean, focusId: string, depth: number) {
  const text = query.trim().toLowerCase();
  const matches = [...index.nodes.values()].filter(node => text && (node.id.toLowerCase().includes(text) || String(node.label ?? '').toLowerCase().includes(text)));
  const matchIds = new Set(matches.map(node => node.id));
  let local: Set<string> | null = null;
  if (depth && index.nodes.has(focusId)) {
    local = new Set([focusId]); let layer = [focusId];
    for (let hop = 0; hop < depth; hop++) {
      const next: string[] = [];
      layer.forEach(id => index.neighbors.get(id)?.forEach(nb => { if (!local!.has(nb)) { local!.add(nb); next.push(nb); } }));
      layer = next;
    }
  }
  const eligible = (id: string) => index.nodes.has(id) && (!local || local.has(id)) && (!connectedOnly || index.nodes.get(id)!.degree > 0) && (!filterMatches || !text || matchIds.has(id));
  const chosen = new Set<string>(), queue: string[] = [];
  const add = (id: string) => { if (chosen.size < NODE_LIMIT && eligible(id) && !chosen.has(id)) { chosen.add(id); queue.push(id); } };
  const important = new Set<string>();
  const importantEdges = new Set<string>();
  highlights.forEach(h => {
    const ids = (h.nodes || []).map(String); ids.forEach(id => important.add(id));
    if (h.type === 'path') for (let i = 1; i < ids.length; i++) importantEdges.add(JSON.stringify([ids[i - 1], ids[i]]));
    (h.edges || []).forEach(edge => {
      const s = endpoint(Array.isArray(edge) ? edge[0] : edge.src ?? edge.source), t = endpoint(Array.isArray(edge) ? edge[1] : edge.dst ?? edge.target);
      important.add(s); important.add(t); importantEdges.add(JSON.stringify([s, t]));
    });
  });
  add(focusId); important.forEach(add);
  // Reserve space for neighbors instead of letting a broad search consume the canvas.
  matches.slice(0, 20).forEach(node => add(node.id));
  // Keep the selected neighborhood useful while retaining representatives of
  // types, communities and connecting paths from the smart overview.
  if (focusId && !depth && !filterMatches) {
    for (const id of index.neighbors.get(focusId) || []) { if (chosen.size >= 20) break; add(id); }
  }
  index.overview?.nodeIds.forEach(add);
  if (!queue.length) {
    let best: PreviewNode | undefined;
    index.nodes.forEach(node => { if (eligible(node.id) && (!best || node.degree > best.degree)) best = node; });
    if (best) add(best.id);
  }
  for (let i = 0; i < queue.length && chosen.size < NODE_LIMIT; i++) index.neighbors.get(queue[i])?.forEach(add);
  index.nodes.forEach(node => add(node.id));
  const candidateEdges = index.edges.filter(edge => chosen.has(edge.source) && chosen.has(edge.target));
  const score = (edge: PreviewEdge) => importantEdges.has(JSON.stringify([edge.source, edge.target])) || (!index.directed && importantEdges.has(JSON.stringify([edge.target, edge.source]))) ? 0 : edge.source === focusId || edge.target === focusId ? 1 : index.overviewEdges.has(edge.raw) ? 2 : 3;
  // Stable order, with result paths and the focused neighborhood first.
  const edges: PreviewEdge[] = [];
  for (let priority = 0; priority < 4 && edges.length < EDGE_LIMIT; priority++) {
    for (const edge of candidateEdges) if (score(edge) === priority && edges.length < EDGE_LIMIT) edges.push(edge);
  }
  return { maxDegree: index.maxDegree, nodes: [...index.nodes.values()].filter(node => chosen.has(node.id)), edges, matches: matchIds,
    eligibleCount: [...index.nodes.keys()].filter(eligible).length, candidateEdges: candidateEdges.length,
    omittedHighlights: [...important].filter(id => index.nodes.has(id) && !chosen.has(id)).length };
}
export type GraphPreview = ReturnType<typeof previewGraph>;
export function degreeRadius(degree: number, max: number) {
  return 5 + 10 * Math.sqrt(Math.max(0, degree) / Math.max(1, max));
}

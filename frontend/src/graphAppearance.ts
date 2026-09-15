import * as d3 from 'd3';
import type { GraphData, Highlight } from './types';
const COMMUNITY_COLORS = ['#3b82f6', '#f97316', '#10b981', '#a855f7', '#eab308', '#ef4444', '#06b6d4', '#ec4899'];
export const DEFAULT_HIGHLIGHT_COLORS: Record<string, string> = { node: 'var(--accent, #2563eb)', path: '#d97706', subgraph: '#7c3aed' };

export function normalizeHighlights(value: GraphData['highlights']): Highlight[] {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== 'object') return [];
  const output: Highlight[] = [];
  (value.nodes || []).forEach((item, index) => output.push({ id: `node-${index}`, type: 'node', nodes: item.ids || [], color: item.color, label: item.label }));
  (value.paths || []).forEach((item, index) => output.push({ id: `path-${index}`, type: 'path', nodes: item.node_sequence || [], edges: item.edges as Highlight['edges'], color: item.color, label: item.label }));
  (value.subgraphs || []).forEach((item, index) => output.push({ id: `subgraph-${index}`, type: 'subgraph', nodes: item.nodes || [], edges: item.edges as Highlight['edges'], color: item.color, label: item.label }));
  return output;
}

export function edgeKey(source: unknown, target: unknown) { return `${String(source)}\u0000${String(target)}`; }

export function endpointId(value: unknown) {
  return String(value && typeof value === 'object' && 'id' in value ? (value as { id: unknown }).id : value);
}

export function highlightedEdgeColor(mapping: Map<string, string>, source: unknown, target: unknown, directed = false) {
  const sourceId = endpointId(source);
  const targetId = endpointId(target);
  return mapping.get(edgeKey(sourceId, targetId)) || (!directed ? mapping.get(edgeKey(targetId, sourceId)) : undefined);
}

export function communityColor(community: string | undefined) {
  if (community === undefined) return 'var(--graph-node-fill)';
  const numeric = Number(community);
  const index = Number.isFinite(numeric)
    ? Math.abs(Math.trunc(numeric))
    : [...community].reduce((hash, character) => ((hash * 31) + character.charCodeAt(0)) >>> 0, 0);
  return COMMUNITY_COLORS[index % COMMUNITY_COLORS.length];
}

export function paddedHullPath(points: Array<[number, number]>, padding = 15) {
  if (!points.length) return null;
  const expanded = points.flatMap(([x, y]) => Array.from({ length: 8 }, (_, index) => {
    const angle = index * Math.PI / 4;
    return [x + Math.cos(angle) * padding, y + Math.sin(angle) * padding] as [number, number];
  }));
  const hull = d3.polygonHull(expanded);
  return hull ? `M${hull.map(point => point.join(',')).join('L')}Z` : null;
}

export function highlightEdgePairs(highlight: Highlight) {
  const pairs = new Set<string>();
  const sequence = (highlight.nodes || []).map(String);
  for (let index = 1; highlight.type === 'path' && index < sequence.length; index += 1) pairs.add(edgeKey(sequence[index - 1], sequence[index]));
  (highlight.edges || []).forEach(edge => {
    if (Array.isArray(edge)) pairs.add(edgeKey(edge[0], edge[1]));
    else pairs.add(edgeKey(edge.src ?? edge.source, edge.dst ?? edge.target));
  });
  return pairs;
}

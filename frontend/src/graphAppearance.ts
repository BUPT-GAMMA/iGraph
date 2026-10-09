import * as d3 from 'd3';
import type { GraphData, Highlight } from './types';
import { currentLanguage, tr } from './i18n';
import type { Translate } from './i18n';
const COMMUNITY_COLORS = ['#3b82f6', '#f97316', '#10b981', '#a855f7', '#eab308', '#ef4444', '#06b6d4', '#ec4899'];
export const DEFAULT_HIGHLIGHT_COLORS: Record<string, string> = { node: 'var(--accent, #2563eb)', path: '#f97316', subgraph: '#7c3aed' };

export function normalizeHighlights(value: GraphData['highlights'], edges: GraphData['edges'] = []): Highlight[] {
  if (!value || typeof value !== 'object') return [];
  const output: Highlight[] = Array.isArray(value) ? [...value] : [];
  if (!Array.isArray(value)) {
    (value.nodes || []).forEach((item, index) => output.push({ id: `node-${index}`, type: 'node', nodes: item.ids || [], color: item.color, label: item.label }));
    (value.paths || []).forEach((item, index) => output.push({ id: `path-${index}`, type: 'path', nodes: item.node_sequence || [], edges: item.edges as Highlight['edges'], color: item.color, label: item.label }));
    (value.subgraphs || []).forEach((item, index) => output.push({ id: `subgraph-${index}`, type: 'subgraph', nodes: item.nodes || [], edges: item.edges as Highlight['edges'], color: item.color, label: item.label }));
  }
  // Older tool results specify only nodes. Show their existing connections too,
  // but never broaden an explicitly selected path/subgraph with extra edges.
  if (output.length && output.every(item => !item.type || item.type === 'node')) {
    const ids = new Set(output.flatMap(item => (item.nodes || []).map(endpointId)));
    const connections: NonNullable<Highlight['edges']> = (edges || [])
      .map(edge => [endpointId(edge.src ?? edge.source), endpointId(edge.dst ?? edge.target)] as [string, string])
      .filter(([source, target]) => ids.has(source) && ids.has(target));
    if (connections.length) output.push({ id: 'highlighted-node-edges', type: 'path', edges: connections,
      color: DEFAULT_HIGHLIGHT_COLORS.path, label: tr('Highlighted connections', '高亮关系') });
  }
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

function compactLabel(value: unknown, maxLength: number) {
  const label = String(value ?? '');
  if (label.length <= maxLength) return label;
  const visibleLength = Math.max(2, maxLength - 1);
  const headLength = Math.ceil(visibleLength * .65);
  return `${label.slice(0, headLength)}…${label.slice(-(visibleLength - headLength))}`;
}

export function schemaEdgeLabel(edge: Record<string, unknown>, compact = false, translate: Translate = tr) {
  const properties = edge.properties && typeof edge.properties === 'object' && !Array.isArray(edge.properties)
    ? edge.properties as Record<string, unknown>
    : {};
  const type = String(edge.type ?? edge.label ?? edge.relationship_type ?? translate('Relationship', '关系'));
  const count = Number(edge.relationship_count ?? properties.relationship_count ?? edge.count ?? properties.count);
  const visibleType = compact ? compactLabel(type, properties.schema_kind === 'foreign_key' ? 38 : /^fk_[0-9a-f]{20,}$/i.test(type) ? 14 : 18) : type;
  const locale = currentLanguage() === 'zh' ? 'zh-CN' : 'en-US';
  return Number.isFinite(count) ? `${visibleType} · ${count.toLocaleString(locale)}` : visibleType;
}

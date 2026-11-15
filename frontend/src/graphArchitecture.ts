import type { GraphData, Highlight } from './types';
import { endpoint } from './graphPreview';
import { highlightNodeIds } from './graphExploration';
import { highlightEdgePairs, highlightedEdgeColor } from './graphAppearance';

type RecordData = Record<string, unknown>;
type ArchitectureItem = { id: string; type: string | null; label: string | null; count: number; fields: string[] };
export type ArchitectureNode = ArchitectureItem;
export type ArchitectureRelation = ArchitectureItem & { source: string; target: string };
export type GraphArchitecture = {
  nodes: ArchitectureNode[];
  relations: ArchitectureRelation[];
  directed: boolean;
  partial: boolean;
  nodeCount: number;
  edgeCount: number;
};

function record(value: unknown): RecordData {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as RecordData : {};
}

function named(...values: unknown[]): string | null {
  for (const value of values) {
    if ((typeof value === 'string' && value.trim()) || (typeof value === 'number' && Number.isFinite(value))) return String(value);
  }
  return null;
}

function nodeType(node: RecordData) {
  const attrs = record(node.attrs);
  const props = record(node.properties ?? attrs.properties);
  return named(node.type, node.node_type, attrs.type, attrs.node_type, node.category, props.table);
}

function edgeType(edge: RecordData) {
  const attrs = record(edge.attrs);
  return named(edge.type, edge.edge_type, edge.relationship_type, attrs.type, attrs.edge_type, edge.label);
}

function properties(row: RecordData) {
  return record(row.properties ?? record(row.attrs).properties);
}

function schemaFields(props: RecordData) {
  if (!Array.isArray(props.property_paths)) return [];
  return props.property_paths.flatMap(path => typeof path?.path === 'string' ? [path.path] : []);
}

const typeId = (type: string | null) => JSON.stringify(type);
const relationId = (source: string, type: string | null, target: string) => JSON.stringify([source, type, target]);

function ends(edge: RecordData, directed: boolean, nodes: Map<string, string>) {
  const source = nodes.get(endpoint(edge.src ?? edge.source)), target = nodes.get(endpoint(edge.dst ?? edge.target));
  if (!source || !target) return null;
  return !directed && target < source ? { source: target, target: source } : { source, target };
}

function order<T extends ArchitectureItem>(items: Iterable<T>) {
  return [...items].sort((left, right) => right.count - left.count || left.id.localeCompare(right.id));
}

function count(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

function fromSchema(data: GraphData): GraphArchitecture {
  const directed = Boolean(data.directed);
  const byId = new Map<string, string>();
  const nodes = data.nodes.map(node => {
    const props = properties(node);
    const type = named(props.type_name, node.id, node.label);
    const id = typeId(type);
    byId.set(String(node.id ?? node.label), id);
    return { id, type, label: named(node.label, type), count: count(props.instance_count ?? node.instance_count), fields: schemaFields(props) };
  });
  const relations: ArchitectureRelation[] = [];
  for (const edge of data.edges) {
    const pair = ends(edge, directed, byId);
    if (!pair) continue;
    const props = properties(edge);
    const type = named(props.type_name, edgeType(edge));
    relations.push({
      ...pair, id: relationId(pair.source, type, pair.target), type, label: named(edge.label, type),
      count: count(props.relationship_count ?? edge.relationship_count), fields: schemaFields(props),
    });
  }
  return {
    nodes: order(nodes), relations: order(relations), directed, partial: Boolean(data.truncated),
    nodeCount: nodes.reduce((total, node) => total + node.count, 0),
    edgeCount: relations.reduce((total, edge) => total + edge.count, 0),
  };
}

export function buildGraphArchitecture(data: GraphData): GraphArchitecture {
  if (data.visualization_kind === 'schema') return fromSchema(data);
  const byId = new Map<string, string>();
  const nodes = new Map<string, ArchitectureNode>();
  const relations = new Map<string, ArchitectureRelation>();
  const fields = new Map<ArchitectureItem, Set<string>>();
  function add(item: ArchitectureItem, row: RecordData) {
    if (!fields.has(item)) fields.set(item, new Set());
    const names = fields.get(item)!;
    item.count += 1;
    const props = properties(row);
    for (const name of Object.keys(props)) names.add(name);
    for (const name of Object.keys(record(props.data))) names.add('data.' + name);
  }

  data.nodes.forEach((node, index) => {
    const type = nodeType(node);
    const id = typeId(type);
    const current = nodes.get(id) || { id, type, label: type, count: 0, fields: [] };
    add(current, node);
    nodes.set(id, current);
    byId.set(String(node.id ?? node.label ?? index), id);
  });

  const directed = Boolean(data.directed);
  let edgeCount = 0;
  data.edges.forEach(edge => {
    const pair = ends(edge, directed, byId);
    if (!pair) return;
    const type = edgeType(edge);
    const id = relationId(pair.source, type, pair.target);
    const current = relations.get(id) || { ...pair, id, type, label: type, count: 0, fields: [] };
    add(current, edge);
    relations.set(id, current);
    edgeCount += 1;
  });
  fields.forEach((names, item) => { item.fields = [...names].sort(); });
  const partial = Boolean(data.truncated || data.degree_scope === 'subgraph'
    || Math.max(data.total_nodes ?? 0, data.num_nodes ?? 0) > byId.size
    || Math.max(data.total_edges ?? 0, data.num_edges ?? 0) > edgeCount || edgeCount < data.edges.length);
  return { nodes: order(nodes.values()), relations: order(relations.values()), directed, partial, nodeCount: byId.size, edgeCount };
}

export function architectureHighlights(data: GraphData, highlights: Highlight[]) {
  const nodes = new Set<string>();
  const relations = new Set<string>();
  if (highlights.length === 0) return { nodes, relations };

  const instanceTypes = new Map(data.nodes.map((node, index) => [String(node.id ?? node.label ?? index), typeId(nodeType(node))]));
  highlightNodeIds(highlights).forEach(id => {
    const type = instanceTypes.get(id);
    if (type) nodes.add(type);
  });
  const pairs = new Map(highlights.flatMap(highlight => [...highlightEdgePairs(highlight)].map(key => [key, 'highlighted'] as const)));
  for (const edge of data.edges) {
    if (!highlightedEdgeColor(pairs, endpoint(edge.src ?? edge.source), endpoint(edge.dst ?? edge.target), data.directed)) continue;
    const pair = ends(edge, Boolean(data.directed), instanceTypes);
    if (pair) relations.add(relationId(pair.source, edgeType(edge), pair.target));
  }
  return { nodes, relations };
}

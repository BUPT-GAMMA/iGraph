import type { GraphData } from './types';

const endpoint = (value: unknown) => String(value && typeof value === 'object' && 'id' in value ? value.id : value ?? '');
const nodeId = (node: Record<string, unknown>, index: number) => String(node.id ?? node.label ?? index);

export function contextHighlights(highlights: GraphData['highlights']) {
  if (!Array.isArray(highlights)) return highlights;
  return {
    nodes: highlights.filter(h => !h.type || h.type === 'node').map(h => ({ ids: h.nodes, color: h.color, label: h.label })),
    paths: highlights.filter(h => h.type === 'path').map(h => ({ node_sequence: h.nodes, edges: h.edges, color: h.color, label: h.label })),
    subgraphs: highlights.filter(h => h.type === 'subgraph').map(h => ({ nodes: h.nodes, edges: h.edges, color: h.color, label: h.label })),
  };
}

/** Overlay an analysis result on the graph preview, including result nodes that
 * were outside the preview sample. Only the latest result supplies highlights. */
export function mergeGraphContext(context: GraphData | null | undefined, result: GraphData): GraphData {
  if (!context) return result;
  const nodes = new Map(context.nodes.map((node, index) => [nodeId(node, index), node]));
  result.nodes.forEach((node, index) => {
    const id = nodeId(node, index);
    nodes.set(id, { ...nodes.get(id), ...node });
  });
  const directed = context.directed ?? result.directed;
  const edges = new Map<string, Record<string, unknown>>();
  for (const edge of [...context.edges, ...result.edges]) {
    const source = endpoint(edge.source ?? edge.src), target = endpoint(edge.target ?? edge.dst);
    const pair = directed || source <= target ? [source, target] : [target, source];
    const key = JSON.stringify([...pair, edge.id ?? edge.edge_id ?? edge.key ?? null, edge.type ?? edge.relationship_type ?? edge.label ?? null]);
    edges.set(key, { ...edges.get(key), ...edge, source, target, src: source, dst: target });
  }
  const community = context.community ?? context.communities ?? result.community ?? result.communities;
  const totalNodes = Math.max(nodes.size, context.total_nodes ?? context.num_nodes ?? context.nodes.length, result.total_nodes ?? result.num_nodes ?? result.nodes.length);
  const totalEdges = Math.max(edges.size, context.total_edges ?? context.num_edges ?? context.edges.length, result.total_edges ?? result.num_edges ?? result.edges.length);
  const truncated = totalNodes > nodes.size || totalEdges > edges.size;
  return {
    ...context, ...result,
    nodes: [...nodes.values()], edges: [...edges.values()], directed,
    degree_scope: context.degree_scope ?? result.degree_scope,
    num_nodes: nodes.size, num_edges: edges.size,
    total_nodes: totalNodes, total_edges: totalEdges,
    truncated, truncated_to: truncated ? nodes.size : undefined,
    community, communities: community,
    highlights: result.highlights,
  };
}

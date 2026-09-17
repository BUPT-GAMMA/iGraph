import { describe, expect, it } from 'vitest';
import { createGraphOverview } from './graphOverview';
import { graphIndex, previewGraph } from './graphPreview';
import type { GraphData } from './types';

function components(nodes: Array<Record<string, unknown>>, edges: Array<Record<string, unknown>>) {
  const neighbors = new Map(nodes.map(n => [String(n.id), new Set<string>()]));
  edges.forEach(e => { const a = String(e.source ?? e.src), b = String(e.target ?? e.dst); neighbors.get(a)!.add(b); neighbors.get(b)!.add(a); });
  const seen = new Set<string>(), sizes: number[] = [];
  for (const id of neighbors.keys()) {
    if (seen.has(id)) continue;
    const queue = [id]; seen.add(id);
    for (let i = 0; i < queue.length; i++) for (const n of neighbors.get(queue[i])!) if (!seen.has(n)) { seen.add(n); queue.push(n); }
    sizes.push(queue.length);
  }
  return sizes.sort((a, b) => b - a);
}

describe('structural graph preview', () => {
  it('retains major-component proportions without creating isolated representatives', () => {
    const nodes = Array.from({ length: 1_000 }, (_, i) => ({ id: String(i) }));
    const edges = Array.from({ length: 899 }, (_, i) => ({ source: String(i), target: String(i + 1) }));
    for (let i = 900; i < 1_000; i += 2) edges.push({ source: String(i), target: String(i + 1) });
    const overview = createGraphOverview({ nodes, edges }, { nodeLimit: 180 });
    const sizes = components(overview.nodes, overview.edges);
    expect(overview.nodes).toHaveLength(180);
    expect(sizes[0]).toBeGreaterThanOrEqual(158);
    expect(sizes[0]).toBeLessThanOrEqual(166);
    expect(sizes).not.toContain(1);
    expect(sizes.length).toBeGreaterThan(1);
  });

  it('preserves bridges when dense internal edges exceed the display budget', () => {
    const nodes = Array.from({ length: 80 }, (_, i) => ({ id: String(i) }));
    const edges: GraphData['edges'] = [];
    for (let offset = 0; offset < 80; offset += 20) for (let a = offset; a < offset + 20; a++) for (let b = a + 1; b < offset + 20; b++) edges.push({ source: String(a), target: String(b) });
    edges.push({ source: '19', target: '20' }, { source: '39', target: '40' }, { source: '59', target: '60' });
    const overview = createGraphOverview({ nodes, edges }, { nodeLimit: 80, edgeLimit: 90 });
    expect(overview.edges).toHaveLength(90);
    expect(components(overview.nodes, overview.edges)).toEqual([80]);
    const preview = previewGraph(graphIndex({ nodes, edges }), [], '', false, false, '', 0);
    expect(preview.edges).toHaveLength(400);
    expect(components(preview.nodes, preview.edges.map(e => e.raw))).toEqual([80]);
  });

  it('does not use disconnected upstream remnants as automatic representatives', () => {
    const nodes = Array.from({ length: 100 }, (_, i) => ({ id: String(i), total_degree: 2 }));
    const edges = Array.from({ length: 79 }, (_, i) => ({ source: String(i), target: String(i + 1) }));
    const result = createGraphOverview({ nodes, edges, truncated: true, total_nodes: 1_000 }, { nodeLimit: 40 });
    expect(result.nodes).toHaveLength(40);
    expect(components(result.nodes, result.edges)).toEqual([40]);
    const pinned = createGraphOverview({ nodes, edges }, { nodeLimit: 40, selectedNodeId: '99' });
    expect(pinned.nodeIds).toContain('99');
  });

  it('keeps true isolates and their types proportionally', () => {
    const nodes = Array.from({ length: 1_000 }, (_, i) => ({ id: String(i), type: i < 750 ? 'A' : 'B' }));
    const result = createGraphOverview({ nodes, edges: [] }, { nodeLimit: 100 });
    expect(result.nodes.filter(n => n.type === 'A')).toHaveLength(75);
    expect(result.nodes.filter(n => n.type === 'B')).toHaveLength(25);
    expect(result.edges).toHaveLength(0);
  });
});

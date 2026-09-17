import { describe, it, expect } from 'vitest';
import { graphIndex, previewGraph, degreeRadius, NODE_LIMIT, EDGE_LIMIT } from './graphPreview';

describe('graph preview', () => {
  it('uses authoritative degrees without confusing preview connections or counting invalid edges', () => {
    const index = graphIndex({ degree_scope: 'full_graph', nodes: [{ id: 'a', total_degree: 100 }, { id: 'b' }, { id: 'c' }], edges: [{ src: 'a', dst: 'b' }, { src: 'b', dst: 'b' }, { src: 'b', dst: 'missing' }] });
    expect(index.nodes.get('a')?.degree).toBe(100);
    expect(index.nodes.get('a')?.degreeLabel).toBe('全图度数');
    expect(index.nodes.get('b')?.degree).toBe(3);
    expect(index.nodes.get('b')?.degreeLabel).toBe('预览连接数');
    expect(degreeRadius(100, 100)).toBeGreaterThan(degreeRadius(3, 100));
    expect(degreeRadius(0, 0)).toBe(5);
  });
  it('caps large graphs but keeps search targets and highlighted paths', () => {
    const index = graphIndex({ nodes: Array.from({ length: 10000 }, (_, id) => ({ id: String(id) })), edges: Array.from({ length: 30000 }, (_, i) => ({ src: String(i % 10000), dst: String((i + 1) % 10000) })) });
    const args: Parameters<typeof previewGraph> = [index, [{ type: 'path', nodes: ['9998', '9999'] }], '8500', false, false, '8500', 0];
    const result = previewGraph(...args);
    expect(result.nodes.length).toBeLessThanOrEqual(NODE_LIMIT);
    expect(result.edges.length).toBeLessThanOrEqual(EDGE_LIMIT);
    expect(result.nodes.map(n => n.id)).toEqual(expect.arrayContaining(['8500', '9998', '9999']));
    expect(result.edges.some(e => e.source === '9998' && e.target === '9999')).toBe(true);
    expect(previewGraph(...args).nodes.map(n => n.id)).toEqual(result.nodes.map(n => n.id));
  });
  it('search retains neighbors unless filtering is explicitly requested and limits local depth', () => {
    const index = graphIndex({ nodes: ['a', 'b', 'c', 'd'].map(id => ({ id })), edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }] });
    expect(previewGraph(index, [], 'a', false, false, '', 0).nodes).toHaveLength(4);
    expect(previewGraph(index, [], 'a', true, false, '', 0).nodes.map(n => n.id)).toEqual(['a']);
    expect(previewGraph(index, [], '', false, false, 'a', 1).nodes.map(n => n.id)).toEqual(['a', 'b']);
    expect(previewGraph(index, [], '', false, false, 'a', 2).nodes.map(n => n.id)).toEqual(['a', 'b', 'c']);
  });
});

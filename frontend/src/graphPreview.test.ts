import { describe, it, expect } from 'vitest';
import { graphIndex, previewGraph, degreeRadius, NODE_LIMIT, EDGE_LIMIT } from './graphPreview';

describe('graph preview', () => {
  it('retains complete highlighted SCC evidence beyond the ordinary node and edge budgets', () => {
    const size = 220;
    const members = Array.from({ length: size }, (_, i) => `member-${i}`);
    const records = Array.from({ length: size }, (_, i) => `holding-${i}`);
    const ordinary = Array.from({ length: 100 }, (_, i) => `ordinary-${i}`);
    const internalEdges = records.flatMap((record, i) => [
      { source: record, target: members[i] }, { source: record, target: members[(i + 1) % size] },
    ]);
    const graph = graphIndex({ directed: true, nodes: [...ordinary, ...members, ...records].map(id => ({ id })),
      edges: [...ordinary.slice(1).map(id => ({ source: ordinary[0], target: id })), ...internalEdges] });
    const highlights = [{ type: 'node', nodes: [...members, ...records] },
      { type: 'path', edges: internalEdges.map(edge => [edge.source, edge.target]) }];
    const preview = previewGraph(graph, highlights, '', false, false, '', 0);
    const displayed = new Set(preview.nodes.map(node => node.id));
    [...members, ...records].forEach(id => expect(displayed.has(id)).toBe(true));
    expect(preview.edges.filter(edge => edge.source.startsWith('holding-'))).toHaveLength(size * 2);
    expect(preview.omittedHighlights).toBe(0);
    expect(preview.nodes.some(node => node.id.startsWith('ordinary-'))).toBe(true);
    expect(preview.edges.some(edge => edge.source.startsWith('ordinary-'))).toBe(true);
    const isolated = previewGraph(graph, highlights, '', false, false, '', 0, true);
    expect(isolated.nodes).toHaveLength(size * 2);
    expect(isolated.edges).toHaveLength(size * 2);
  });

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
  it('isolates an edge-defined result subgraph and reports unloaded highlights', () => {
    const index = graphIndex({ nodes: ['a', 'b', 'c'].map(id => ({ id })), edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }] });
    const highlights = [{ type: 'path', edges: [['a', 'b'], ['b', 'not-loaded']] }];
    const preview = previewGraph(index, highlights, '', false, false, '', 0, true);
    expect(preview.nodes.map(node => node.id)).toEqual(['a', 'b']);
    expect(preview.edges.map(edge => [edge.source, edge.target])).toEqual([['a', 'b']]);
    expect(preview.omittedHighlights).toBe(1);
  });
  it('reserves ordinary neighbors of highlighted subjects before filling a large overview', () => {
    const ordinary = Array.from({ length: 300 }, (_, i) => `ordinary-${i}`);
    const index = graphIndex({ nodes: [...ordinary, 'subject', 'neighbor'].map(id => ({ id })),
      edges: [...ordinary.slice(1).map(id => ({ source: ordinary[0], target: id })), { source: 'subject', target: 'neighbor' }] });
    const result = previewGraph(index, [{ type: 'node', nodes: ['subject'] }], '', false, false, '', 0);
    expect(result.nodes).toHaveLength(NODE_LIMIT);
    expect(result.nodes.map(node => node.id)).toEqual(expect.arrayContaining(['subject', 'neighbor']));
    expect(result.edges.some(edge => edge.source === 'subject' && edge.target === 'neighbor')).toBe(true);
    expect(result.nodes.some(node => node.id.startsWith('ordinary-'))).toBe(true);
  });
});

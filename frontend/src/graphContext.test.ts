import { describe, expect, it } from 'vitest';
import { contextHighlights, mergeGraphContext } from './graphContext';
import type { GraphData } from './types';

describe('analysis over the graph context', () => {
  it('includes array-format result nodes and paths in the server context request', () => {
    expect(contextHighlights([{ type: 'node', nodes: ['a'] }, { type: 'path', nodes: ['a', 'b'] }])).toMatchObject({
      nodes: [{ ids: ['a'] }], paths: [{ node_sequence: ['a', 'b'] }],
    });
  });
  it('keeps ordinary neighbors, deduplicates shared records and preserves full-graph coverage', () => {
    const context: GraphData = {
      nodes: [{ id: 'a', label: 'Alice', total_degree: 9 }, { id: 'b' }, { id: 'c' }],
      edges: [{ src: 'a', dst: 'b', type: 'owns' }, { src: 'b', dst: 'c', type: 'owns' }],
      directed: true, total_nodes: 1000, total_edges: 2000, degree_scope: 'full_graph',
      community: { node_community: { a: 0, b: 0, c: 1 } },
    };
    const result: GraphData = {
      nodes: [{ id: 'a', name: 'analysis subject' }, { id: 'b' }, { id: 'outside-preview' }],
      edges: [{ source: { id: 'a' }, target: 'b', type: 'owns' }, { source: 'b', target: 'outside-preview' }],
      highlights: { paths: [{ node_sequence: ['a', 'b', 'outside-preview'] }] },
      num_nodes: 3, num_edges: 2, total_nodes: 3, total_edges: 2,
    };
    const merged = mergeGraphContext(context, result);
    expect(merged.nodes).toHaveLength(4);
    expect(merged.nodes[0]).toMatchObject({ id: 'a', label: 'Alice', total_degree: 9, name: 'analysis subject' });
    expect(merged.edges).toHaveLength(3);
    expect(merged.edges[0]).toMatchObject({ source: 'a', target: 'b' });
    expect(merged.highlights).toEqual(result.highlights);
    expect(merged.community).toEqual(context.community);
    expect(merged).toMatchObject({ total_nodes: 1000, total_edges: 2000, truncated: true, degree_scope: 'full_graph' });
    expect(context.nodes).toHaveLength(3);
    expect(context.edges).toHaveLength(2);
  });

  it('deduplicates reversed undirected edges without collapsing distinct parallel relationships', () => {
    const context: GraphData = { nodes: [{ id: 'a' }, { id: 'b' }], directed: false,
      edges: [{ src: 'a', dst: 'b', id: 'one', type: 'owns' }, { src: 'a', dst: 'b', id: 'two', type: 'owns' }] };
    const result: GraphData = { nodes: context.nodes,
      edges: [{ source: 'b', target: 'a', id: 'one', type: 'owns' }, { source: 'a', target: 'b', type: 'employs' }] };
    const merged = mergeGraphContext(context, result);
    expect(merged.edges).toHaveLength(3);
    expect(merged.truncated).toBe(false);
  });
});


it('retains legacy node selections without an explicit type in refresh requests', () => {
  expect(contextHighlights([{ nodes: ['a'] }])).toMatchObject({ nodes: [{ ids: ['a'] }] });
});

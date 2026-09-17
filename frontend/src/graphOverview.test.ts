import { describe, expect, it } from 'vitest';
import { createGraphOverview, graphOverviewPairKey } from './graphOverview';
import type { GraphData } from './types';

function ids(result: ReturnType<typeof createGraphOverview>) {
  return new Set(result.nodeIds);
}

describe('createGraphOverview', () => {
  it('keeps every node and edge for a graph within the node and edge limits', () => {
    const graph: GraphData = {
      nodes: [{ id: 'c' }, { id: 'a' }, { id: 'b' }],
      edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }],
    };

    const result = createGraphOverview(graph);

    expect(result.mode).toBe('complete');
    expect(result.nodeIds).toEqual(expect.arrayContaining(['a', 'b', 'c']));
    expect(result.edges).toHaveLength(2);
    expect(result.locallySampledNodes).toBe(false);
    expect(result.locallySampledEdges).toBe(false);
  });

  it('retains selected, highlighted, and search-matching nodes before statistical sampling', () => {
    const nodes = Array.from({ length: 1_000 }, (_, index) => ({
      id: `node-${index}`,
      label: index === 997 ? 'Needle account' : `Account ${index}`,
      type: index < 700 ? 'Account' : 'Article',
    }));
    const edges = Array.from({ length: 500 }, (_, index) => ({ source: 'node-0', target: `node-${index + 1}` }));

    const result = createGraphOverview({ nodes, edges }, {
      nodeLimit: 80,
      selectedNodeId: 'node-999',
      highlightedNodeIds: ['node-998'],
      query: 'needle',
    });

    expect(result.nodes).toHaveLength(80);
    expect(ids(result).has('node-999')).toBe(true);
    expect(ids(result).has('node-998')).toBe(true);
    expect(ids(result).has('node-997')).toBe(true);
    expect(result.queryMatchCount).toBe(1);
    expect(result.retainedQueryMatchCount).toBe(1);
    expect(result.selectedNodeRetained).toBe(true);
    expect(result.retainedHighlightedNodeCount).toBe(1);
  });

  it('samples node types proportionally and never falls back to the first input rows', () => {
    const nodes = Array.from({ length: 1_000 }, (_, index) => ({ id: `node-${index}`, attrs: { node_type: index < 750 ? 'A' : 'B' } }));
    const edges = [
      ...Array.from({ length: 400 }, (_, index) => ({ source: 'node-999', target: `node-${index}` })),
      ...Array.from({ length: 300 }, (_, index) => ({ source: 'node-0', target: `node-${index + 1}` })),
    ];

    const result = createGraphOverview({ nodes, edges }, { nodeLimit: 100 });
    const typeCounts = result.nodes.reduce<Record<string, number>>((counts, node) => {
      const type = String((node.attrs as { node_type: string }).node_type);
      counts[type] = (counts[type] || 0) + 1;
      return counts;
    }, {});

    expect(result.mode).toBe('overview');
    expect(result.nodes).toHaveLength(100);
    expect(typeCounts.A).toBeGreaterThanOrEqual(65);
    expect(typeCounts.A).toBeLessThanOrEqual(85);
    expect(typeCounts.B).toBeGreaterThanOrEqual(15);
    expect(typeCounts.B).toBeLessThanOrEqual(35);
    expect(ids(result).has('node-999')).toBe(true);
  });

  it('keeps a representative from every community when the cap permits it', () => {
    const nodes = Array.from({ length: 900 }, (_, index) => ({ id: `node-${index}`, type: `type-${index % 3}` }));
    const nodeCommunity = Object.fromEntries(nodes.map((node, index) => [node.id, index % 9]));
    const edges = Array.from({ length: 899 }, (_, index) => ({ source: `node-${index}`, target: `node-${index + 1}` }));

    const result = createGraphOverview({ nodes, edges, community: { node_community: nodeCommunity } }, { nodeLimit: 90 });
    const retainedCommunities = new Set(result.nodeIds.map(id => String(nodeCommunity[id])));

    expect(retainedCommunities.size).toBe(9);
  });

  it('uses bounded paths to join nearby type and community representatives', () => {
    const graph: GraphData = {
      nodes: ['a', 'x', 'y', 'b', 'leaf-1', 'leaf-2'].map(id => ({ id, type: 'Node' })),
      edges: [
        { id: 'ax', source: 'a', target: 'x' },
        { id: 'xy', source: 'x', target: 'y' },
        { id: 'yb', source: 'y', target: 'b' },
        { id: 'xl', source: 'x', target: 'leaf-1' },
        { id: 'yl', source: 'y', target: 'leaf-2' },
      ],
      community: { node_community: { a: 'left', b: 'right' } },
    };

    const result = createGraphOverview(graph, { nodeLimit: 4, edgeLimit: 10 });

    expect(result.nodeIds).toEqual(expect.arrayContaining(['a', 'x', 'y', 'b']));
    expect(result.edges.map(edge => edge.id)).toEqual(expect.arrayContaining(['ax', 'xy', 'yb']));
  });

  it('returns the same preview for equivalent input ordering', () => {
    const nodes = Array.from({ length: 900 }, (_, index) => ({ id: `n-${index}`, type: index % 2 ? 'odd' : 'even' }));
    const edges = Array.from({ length: 899 }, (_, index) => ({ source: `n-${index}`, target: `n-${index + 1}`, weight: index / 10, attrs: { relation: `r-${index % 4}` } }));
    const options = { nodeLimit: 70, edgeLimit: 120 };

    const forward = createGraphOverview({ nodes, edges }, options);
    const reversed = createGraphOverview({ nodes: [...nodes].reverse(), edges: [...edges].reverse() }, options);

    expect(forward.nodeIds).toEqual(reversed.nodeIds);
    expect(forward.edges.map(edge => `${edge.source}-${edge.target}-${edge.weight}`)).toEqual(reversed.edges.map(edge => `${edge.source}-${edge.target}-${edge.weight}`));
  });

  it('caps dense edges but keeps a highlighted edge ahead of ordinary edges', () => {
    const nodes = Array.from({ length: 20 }, (_, index) => ({ id: `n-${index}` }));
    const edges = Array.from({ length: 80 }, (_, index) => ({ id: `e-${index}`, source: `n-${index % 20}`, target: `n-${(index * 7 + 3) % 20}` }));
    edges[79] = { id: 'highlighted', source: 'n-18', target: 'n-19' };

    const result = createGraphOverview({ nodes, edges }, {
      edgeLimit: 10,
      highlightedNodeIds: ['n-18', 'n-19'],
      highlightedEdgePairs: [graphOverviewPairKey('n-18', 'n-19')],
    });

    expect(result.edges).toHaveLength(10);
    expect(result.edges.some(edge => edge.id === 'highlighted')).toBe(true);
    expect(result.locallySampledEdges).toBe(true);
  });

  it('retains relationships and type quotas when all nodes have the same degree', () => {
    const nodes = Array.from({ length: 2_000 }, (_, i) => ({ id: String(i), type: ['A', 'B', 'C'][i % 3] }));
    const edges = Array.from({ length: 10_000 }, (_, i) => ({ id: `e${i}`, source: String(i % 2_000), target: String((i * 7 + 3) % 2_000) }));
    const options = { nodeLimit: 180, edgeLimit: 400 };
    const result = createGraphOverview({ nodes, edges }, options);
    const reversed = createGraphOverview({ nodes: [...nodes].reverse(), edges: [...edges].reverse() }, options);

    expect(result.nodes).toHaveLength(180);
    expect(result.edges.length).toBeGreaterThan(100);
    expect(result.edges.length).toBeLessThanOrEqual(400);
    for (const type of ['A', 'B', 'C']) {
      expect(result.nodes.filter(node => node.type === type).length).toBeGreaterThanOrEqual(45);
      expect(result.nodes.filter(node => node.type === type).length).toBeLessThanOrEqual(75);
    }
    expect(result.nodeIds).toEqual(reversed.nodeIds);
    expect(result.edges.map(edge => edge.id)).toEqual(reversed.edges.map(edge => edge.id));
  });

  it('reports an upstream partial payload separately from local smart sampling', () => {
    const nodes = Array.from({ length: 200 }, (_, index) => ({ id: `n-${index}` }));
    const result = createGraphOverview({ nodes, edges: [], total_nodes: 10_000, total_edges: 25_000, truncated: true });

    expect(result.sourcePartial).toBe(true);
    expect(result.locallySampledNodes).toBe(false);
    expect(result.availableNodes).toBe(200);
    expect(result.totalNodes).toBe(10_000);
  });

  it('does not sample schema nodes or relationship edges', () => {
    const nodes = Array.from({ length: 12 }, (_, index) => ({ id: `Type-${index}` }));
    const edges = Array.from({ length: 40 }, (_, index) => ({ id: `R-${index}`, source: `Type-${index % 12}`, target: `Type-${(index + 1) % 12}` }));

    const result = createGraphOverview({ nodes, edges, visualization_kind: 'schema' }, { nodeLimit: 3, edgeLimit: 4, forceComplete: true });

    expect(result.nodes).toHaveLength(12);
    expect(result.edges).toHaveLength(40);
    expect(result.mode).toBe('complete');
  });
});

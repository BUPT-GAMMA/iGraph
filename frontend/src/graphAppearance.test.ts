import { describe, expect, it } from 'vitest';
import { normalizeHighlights } from './graphAppearance';

describe('node-only highlight connections', () => {
  const edges = [{ source: 1, target: 2 }, { source: 2, target: 3 }, { source: 1, target: 3 }, { source: 3, target: 4 }];

  it('uses real edge endpoints and leaves its input untouched', () => {
    const input = [{ type: 'node', nodes: [1, 2, 3] }];
    const result = normalizeHighlights(input, edges);
    expect(input).toHaveLength(1);
    expect(result[1].edges).toEqual([['1', '2'], ['2', '3'], ['1', '3']]);
    expect(result[1].color).toBe('#f97316');
    expect(normalizeHighlights({ nodes: [{ ids: ['missing', '4'] }] }, edges)).toHaveLength(1);
    expect(normalizeHighlights(null, edges)).toEqual([]);
    expect(normalizeHighlights(input, [{ src: { id: 2 }, dst: 3 }])[1].edges).toEqual([['2', '3']]);
  });

  it.each(['path', 'subgraph'])('does not add chords or override explicit %s results', type => {
    const input = [{ type: 'node', nodes: [1, 2, 3] }, { type, edges: [[1, 2], [2, 3]], color: '#ef4444' }];
    expect(normalizeHighlights(input, edges)).toEqual(input);
  });
});

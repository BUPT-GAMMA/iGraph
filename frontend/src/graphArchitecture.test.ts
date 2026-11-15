import { describe, expect, it } from 'vitest';
import { architectureHighlights, buildGraphArchitecture } from './graphArchitecture';
import type { GraphData } from './types';

describe('architecture projection', () => {
  it('keeps direction, parallel relationship types and self-relations while projecting the actual result', () => {
    const data: GraphData = {
      directed: true, total_nodes: 100, total_edges: 200, truncated: true,
      nodes: [{ id: 'a', attrs: { type: 'Company' } }, { id: 'b', type: 'Person' }, { id: 'c', type: 'Company' }],
      edges: [
        { src: 'a', dst: 'b', type: 'employs', properties: { since: 2020 } },
        { src: 'c', dst: 'b', type: 'employs' },
        { src: 'a', dst: 'b', type: 'paid' },
        { src: 'b', dst: 'a', type: 'employs' },
        { src: 'a', dst: 'c', type: 'subsidiary' },
      ],
    };
    const architecture = buildGraphArchitecture(data);
    expect(architecture.partial).toBe(true);
    expect(architecture.nodes.map(node => [node.type, node.count])).toEqual([['Company', 2], ['Person', 1]]);
    const fromCompany = architecture.relations.find(relation => relation.type === 'employs' && relation.source === '"Company"')!;
    const fromPerson = architecture.relations.find(relation => relation.type === 'employs' && relation.source === '"Person"')!;
    expect(fromCompany).toMatchObject({ count: 2, fields: ['since'] });
    expect(fromPerson.count).toBe(1);
    expect(architecture.relations.find(relation => relation.type === 'subsidiary')).toMatchObject({ source: '"Company"', target: '"Company"' });
    const highlighted = architectureHighlights(data, [{ type: 'path', nodes: ['b', 'a'] }]);
    expect(highlighted.nodes).toEqual(new Set(['"Company"', '"Person"']));
    expect(highlighted.relations).toEqual(new Set([fromPerson.id]));
  });

  it('reuses complete aggregate counts and property paths from the existing construction schema', () => {
    const schema: GraphData = {
      visualization_kind: 'schema', directed: true,
      nodes: [{ id: 'Company', label: 'Company', properties: { instance_count: 6000, property_paths: [{ path: 'data.name' }] } }],
      edges: [{ source: 'Company', target: 'Company', type: 'owns', properties: { relationship_count: 13436 } }],
    };
    expect(buildGraphArchitecture(schema)).toMatchObject({
      partial: false, nodeCount: 6000, edgeCount: 13436,
      nodes: [{ count: 6000, fields: ['data.name'] }], relations: [{ type: 'owns', count: 13436 }],
    });
  });

  it('aggregates undirected edges without treating data labels as declared node types', () => {
    const data: GraphData = {
      directed: false,
      nodes: [{ id: 1, attrs: { label: 8 }, label: 'Paper A' }, { id: 2, attrs: { label: 9 }, label: 'Paper B' }],
      edges: [{ source: 1, target: 2 }, { source: 2, target: 1 }],
    };
    expect(buildGraphArchitecture(data)).toMatchObject({
      nodes: [{ type: null, count: 2 }], relations: [{ type: null, count: 2 }], partial: false,
    });
  });
});

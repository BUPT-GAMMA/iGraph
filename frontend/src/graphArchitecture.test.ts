import { describe, expect, it } from 'vitest';
import { architectureHighlights, buildGraphArchitecture } from './graphArchitecture';
import { schemaEdgeLabel } from './graphAppearance';
import type { GraphData } from './types';

describe('architecture projection', () => {
  it('shows FK roles in both languages while keeping parallel relationships distinct in both layers', () => {
    const edges = ['fk_464ac5706e769fd8efe7b986bec5bb7b9cf4e13c','fk_8cc261eadd35b2bfa6c388bc4a9d7e0f53ecf913'].map(type => ({
      source: 'h', target: 'c', type, relationship_count: 2,
    }));
    const graph: GraphData = { directed: true, nodes: [{ id: 'h', type: 'holding' }, { id: 'c', type: 'company' }], edges };
    for (const data of [graph, { ...graph, visualization_kind: 'schema' }]) {
      const zh = buildGraphArchitecture(data, (_, chinese) => chinese);
      expect(new Set(zh.relations.map(edge => edge.label))).toEqual(new Set(['被持股企业', '企业股东']));
      expect(new Set(zh.relations.map(edge => edge.id)).size).toBe(2);
      expect(buildGraphArchitecture(data, english => english).relations.map(edge => edge.label)).toContain('Corporate shareholder');
    }
    expect(schemaEdgeLabel(edges[0], false, (_, chinese) => chinese)).toBe('被持股企业 · 2');
    const generic = { type: 'fk_' + 'a'.repeat(40), properties: { foreign_key: { table: 'sales', columns: ['buyer_id'], ref_table: 'buyers', ref_columns: ['id'] } } };
    expect(schemaEdgeLabel(generic, false, (_, chinese) => chinese)).toBe('买方');
    expect(schemaEdgeLabel(generic, false, english => english)).toBe('Buyer');
  });
  it('shows the same order roles on source tables, constructed schema, and live graph edges', () => {
    const zh = (_en: string, chinese: string) => chinese;
    for (const [source, column, target, label] of [
      ['orders', 'customer_id', 'customers', '下单客户'], ['order_items', 'order_id', 'orders', '所属订单'],
      ['order_items', 'product_id', 'products', '订购商品'],
    ]) {
      const fk = { table: source, columns: [column], ref_table: target, ref_columns: [column] };
      const type = 'fk_' + '1'.repeat(40);
      const table = { source, target, type: column + ' → ' + column, properties: { schema_kind: 'foreign_key', ...fk } };
      expect(schemaEdgeLabel(table, false, zh)).toBe(label);
      const edge = { type, src: 'a', dst: 'b', properties: { foreign_key: fk } };
      expect(schemaEdgeLabel({ ...edge, properties: { schema_kind: 'edge_type', type_name: type, foreign_key: fk } }, false, zh)).toBe(label);
      expect(buildGraphArchitecture({ directed: true, nodes: [{ id: 'a', type: source }, { id: 'b', type: target }], edges: [edge] }, zh).relations[0].label).toBe(label);
    }
  });
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

import { describe, expect, it } from 'vitest';
import type { ConstructionSourceTable, Dataset } from './types';
import { constructionSourceTables, sourceTablesFrom, tableRelationGraph } from './sourceSchema';
import { schemaEdgeLabel } from './graphAppearance';

const tables: ConstructionSourceTable[] = [
  { name: 'customers', columns: ['tenant', 'id', 'name'], column_types: { tenant: 'TEXT', id: 'INT', name: 'TEXT' }, primary_key: ['tenant', 'id'], foreign_keys: [] },
  { name: 'orders', columns: ['id', 'tenant', 'customer', 'parent'], primary_key: ['id'], foreign_keys: [
    { columns: ['tenant', 'customer'], ref_table: 'customers', ref_columns: ['tenant', 'id'] },
    { columns: ['parent'], ref_table: 'orders', ref_columns: ['id'] },
  ] },
  { name: 'isolated', columns: ['id'], primary_key: [], foreign_keys: [] },
];
describe('source table relationship schema', () => {
  it('draws one node per source table and one edge per FK, including composite keys and self-links', () => {
    const graph = tableRelationGraph(tables);
    expect(graph.nodes.map(node => node.id)).toEqual(['customers', 'orders', 'isolated']);
    expect(graph.edges).toHaveLength(2);
    expect(graph.edges[0]).toMatchObject({ source: 'orders', target: 'customers', label: 'tenant, customer → tenant, id' });
    expect(graph.edges[1]).toMatchObject({ source: 'orders', target: 'orders' });
    expect(graph).toMatchObject({ directed: true, visualization_kind: 'schema', schema_kind: 'tables' });
    expect(schemaEdgeLabel(graph.edges[0], true)).toBe('tenant, customer → tenant, id');
  });
  it('does not expose source row values or duplicate definitions and ignores malformed FKs', () => {
    const parsed = sourceTablesFrom([{ ...tables[0], rows: [{ name: 'PRIVATE_VALUE' }] }, tables[0],
      { ...tables[1], foreign_keys: [...tables[1].foreign_keys, { columns: ['id'], ref_table: 'missing', ref_columns: ['id'] },
        { columns: ['bad'], ref_table: 'customers', ref_columns: ['id'] }, { columns: [], ref_table: 'customers', ref_columns: [] }, null] },
      { name: 'bad', columns: [123] }, null]);
    expect(parsed).toHaveLength(2);
    expect(JSON.stringify(parsed)).not.toContain('PRIVATE_VALUE');
    expect(tableRelationGraph(parsed).edges).toHaveLength(2);
    expect(tableRelationGraph([{ ...tables[1], foreign_keys: [...tables[1].foreign_keys, ...tables[1].foreign_keys] }, tables[0]]).edges).toHaveLength(2);
  });
  it('prefers saved source definitions, and falls back to CSV metadata without using optimized types', () => {
    const dataset: Dataset = { dataset_id: 'd', project_id: null, name: 'Sales', original_filename: 'sales.csv',
      file_type: 'csv', status: 'ready', metadata: { csv_bundle: { tables } } };
    expect(constructionSourceTables(dataset, null)).toEqual(sourceTablesFrom(tables));
    const schema = { dataset_id: 'd', revision: 2, schema: { node_type_count: 1, edge_type_count: 0,
      node_types: [], edge_types: [], graph: { nodes: [{ id: 'derived_type' }], edges: [] }, source_tables: [tables[0]] } };
    expect(constructionSourceTables(dataset, schema).map(table => table.name)).toEqual(['customers']);
    expect(tableRelationGraph(constructionSourceTables(dataset, schema)).nodes.map(node => node.id)).not.toContain('derived_type');
  });
});

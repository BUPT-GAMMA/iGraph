import type { ConstructionSchemaResponse, ConstructionSourceTable, Dataset, GraphData } from './types';

/** Source definitions only: never copy record values or optimization internals. */
export function sourceTablesFrom(value: unknown): ConstructionSourceTable[] {
  if (!Array.isArray(value)) return [];
  const names = new Set<string>();
  return value.flatMap(table => {
    if (!table || typeof table !== 'object' || typeof table.name !== 'string' || !table.name
      || names.has(table.name) || !Array.isArray(table.columns)
      || !table.columns.every((column: unknown) => typeof column === 'string')) return [];
    names.add(table.name);
    const columns = [...new Set<string>(table.columns)];
    return [{ name: table.name, columns,
      column_types: Object.fromEntries(columns.flatMap(column => typeof table.column_types?.[column] === 'string'
        ? [[column, table.column_types[column]]] : [])),
      primary_key: Array.isArray(table.primary_key) ? table.primary_key.filter((key: unknown) => typeof key === 'string' && columns.includes(key)) : [],
      foreign_keys: (Array.isArray(table.foreign_keys) ? table.foreign_keys : []).flatMap((key: unknown) => {
        if (!key || typeof key !== 'object') return [];
        const fk = key as Record<string, unknown>;
        if (typeof fk.ref_table !== 'string' || !Array.isArray(fk.columns) || !Array.isArray(fk.ref_columns)
          || !fk.columns.length || fk.columns.length !== fk.ref_columns.length
          || !fk.columns.every((column): column is string => typeof column === 'string' && columns.includes(column))
          || !fk.ref_columns.every((column): column is string => typeof column === 'string')) return [];
        return [{ columns: [...fk.columns], ref_table: fk.ref_table, ref_columns: [...fk.ref_columns] }];
      }),
    }];
  });
}

export function constructionSourceTables(dataset: Dataset, schema: ConstructionSchemaResponse | null) {
  const saved = sourceTablesFrom(schema?.schema.source_tables);
  return saved.length ? saved : sourceTablesFrom((dataset.metadata?.csv_bundle as { tables?: unknown } | undefined)?.tables);
}

export function tableRelationGraph(tables: ConstructionSourceTable[]): GraphData {
  const byName = new Map(tables.map(table => [table.name, table]));
  const seen = new Set<string>();
  return {
    directed: true, visualization_kind: 'schema', schema_kind: 'tables',
    nodes: tables.map(table => ({ id: table.name, label: table.name, type: 'table',
      properties: { schema_kind: 'table', type_name: table.name, field_count: table.columns.length } })),
    edges: tables.flatMap(table => table.foreign_keys.flatMap(fk => {
      const target = byName.get(fk.ref_table);
      if (!target || !fk.ref_columns.every(column => target.columns.includes(column))) return [];
      const id = JSON.stringify([table.name, fk.columns, fk.ref_table, fk.ref_columns]);
      if (seen.has(id)) return [];
      seen.add(id);
      return [{ id: `table_fk:${id}`, source: table.name, target: fk.ref_table, type: `${fk.columns.join(', ')} → ${fk.ref_columns.join(', ')}`,
        label: `${fk.columns.join(', ')} → ${fk.ref_columns.join(', ')}`,
        properties: { schema_kind: 'foreign_key', columns: [...fk.columns], ref_columns: [...fk.ref_columns] } }];
    })),
  };
}

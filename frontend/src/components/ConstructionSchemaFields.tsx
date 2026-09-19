import { useI18n } from '../i18n';
import type { ConstructionSchemaResponse, ConstructionSourceTable, Dataset, GraphData } from '../types';
import { constructionSourceTables, sourceTablesFrom } from '../sourceSchema';
import './ConstructionSchemaFields.css';

export function ConstructionSchemaFields({ dataset, schema, graph, selectedType }: {
  dataset: Dataset; schema: ConstructionSchemaResponse | null; graph: GraphData | null; selectedType?: string;
}) {
  const { t } = useI18n();
  const builtTables = sourceTablesFrom(schema?.schema.source_tables);
  const tables = constructionSourceTables(dataset, schema);
  if (!tables.length) return null;
  const selectedSources = new Set((graph?.nodes || []).filter(node => node.type === selectedType).flatMap(node => {
    const properties = node.properties as Record<string, unknown> | undefined;
    return typeof properties?.table === 'string' ? [properties.table] : [];
  }));
  if (selectedType) selectedSources.add(selectedType);
  const matched = tables.filter(table => selectedSources.has(table.name));
  const visible = matched.length ? matched : tables;
  return <details className="construction-schema-fields" open>
    <summary>{t('Source schema · fields and PK/FK', '源数据 Schema · 字段与主外键')}</summary>
    <p>{t('Field definitions, not records · PK: primary key · FK: foreign key', '字段定义（非记录） · PK 主键 · FK 外键')}</p>
    <div className="schema-table-grid">{visible.map(table => <SourceTableFields key={table.name} table={table} built={Boolean(builtTables.length)} />)}</div>
    <div className="schema-relationships" aria-label={t('Foreign-key relationships', '外键关系')}>{visible.flatMap(table => table.foreign_keys.map((fk, index) => <code key={`${table.name}-${index}`}>{table.name}.({fk.columns.join(', ')}) <span>→</span> {fk.ref_table}.({fk.ref_columns.join(', ')})</code>))}</div>
  </details>;
}

export function SourceTableFields({ table, built = true }: { table: ConstructionSourceTable; built?: boolean }) {
  const { t } = useI18n();
  return <article className="schema-table-card">
      <header><strong>{table.name}</strong><small>{table.columns.length} {t('fields', '个字段')}</small></header>
      <table><thead><tr><th>{t('Feature / field', '特征 / 字段')}</th><th>{t('Type', '类型')}</th><th>{t('Key', '键')}</th></tr></thead><tbody>{table.columns.map(column => {
        const primary = table.primary_key.includes(column);
        const foreign = table.foreign_keys.filter(fk => fk.columns.includes(column));
        return <tr key={column}><td><code>{column}</code>{foreign.map((fk, index) => <small className="schema-field-reference" key={index}>→ {fk.ref_table}.{fk.ref_columns[fk.columns.indexOf(column)]}</small>)}</td><td>{table.column_types?.[column] || t('Unspecified', '未声明')}</td><td>{primary && <b className="schema-key pk">PK</b>}{foreign.length > 0 && <b className="schema-key fk">FK</b>}{!primary && !foreign.length && <small>{t('Attribute', '属性')}</small>}</td></tr>;
      })}</tbody></table>
      {!table.primary_key.length && <small className="schema-no-pk">{built ? t('No primary key; uses generated row identities.', '无主键，使用生成的行标识。') : t('Primary key will be detected during construction.', '构图时尝试自动识别主键。')}</small>}
    </article>;
}

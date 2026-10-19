import { Popup, PopupBackdrop } from './PopupMotion';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import type { CsvBundleSchema, CsvBundleTableSchema, CsvForeignKey } from '../types';
import { Icon } from './Icon';
import './ConstructionSchemaFields.css';

type DraftForeignKey = CsvForeignKey & { id: number };
type DraftTable = Omit<CsvBundleTableSchema, 'foreign_keys'> & { foreign_keys: DraftForeignKey[] };

type Props = {
  files: File[];
  onCancel: () => void;
  onSubmit: (files: File[], schema: CsvBundleSchema, name: string) => void;
};

function normalized(value: string) { return value.toLowerCase().replace(/[^a-z0-9]/g, ''); }
function singular(value: string) {
  if (value.endsWith('ies') && value.length > 3) return value.slice(0, -3) + 'y';
  if (value.endsWith('s') && !value.endsWith('ss') && value.length > 1) return value.slice(0, -1);
  return value;
}
function tableName(filename: string) {
  let value = filename.replace(/\.csv$/i, '').replace(/[^A-Za-z0-9_]+/g, '_').replace(/^_+|_+$/g, '');
  value = value.replace(/_dataset$/i, '').replace(/^olist_/i, '');
  return value || 'table';
}
function inferredPrimaryKey(name: string, columns: string[]) {
  const base = normalized(name);
  const one = singular(base);
  const candidates = new Set(['id', `${base}id`, `${one}id`]);
  return columns.find(column => candidates.has(normalized(column))) || '';
}

export function parseCsvHeader(text: string): string[] {
  const cells: string[] = [];
  let value = '';
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') { value += '"'; index += 1; }
      else if (char === '"') quoted = false;
      else value += char;
    } else if (char === '"' && value === '') quoted = true;
    else if (char === ',') { cells.push(value); value = ''; }
    else if (char === '\n' || char === '\r') {
      if (char === '\r' && text[index + 1] === '\n') index += 1;
      cells.push(value);
      return cells.map((cell, cellIndex) => cellIndex === 0 ? cell.replace(/^\uFEFF/, '') : cell);
    } else value += char;
  }
  if (quoted) throw new Error('CSV_HEADER_INCOMPLETE');
  cells.push(value);
  return cells.map((cell, cellIndex) => cellIndex === 0 ? cell.replace(/^\uFEFF/, '') : cell);
}

async function inspectFile(file: File): Promise<DraftTable> {
  const sample = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error || new Error('CSV_HEADER_READ_FAILED'));
    reader.readAsText(file.slice(0, 1024 * 1024), 'utf-8');
  });
  const columns = parseCsvHeader(sample);
  if (!columns.length || columns.some(column => !column) || new Set(columns).size !== columns.length) throw new Error(`INVALID_HEADER:${file.name}`);
  const name = tableName(file.name);
  const candidate = inferredPrimaryKey(name, columns);
  return { file_name: file.name, name, columns, primary_key: candidate ? [candidate] : [], foreign_keys: [] };
}

function suggestedRelationships(tables: DraftTable[], nextId: () => number) {
  return tables.map((table, tableIndex) => {
    const foreign_keys: DraftForeignKey[] = [];
    table.columns.forEach(column => {
      const matches = tables.flatMap((parent, parentIndex) => parent.primary_key.length === 1
        && normalized(parent.primary_key[0]) === normalized(column)
        && parentIndex !== tableIndex
        ? [{ parent, parentIndex }] : []);
      if (matches.length === 1 && !table.primary_key.includes(column)) {
        foreign_keys.push({ id: nextId(), columns: [column], ref_table: matches[0].parent.name, ref_columns: [matches[0].parent.primary_key[0]] });
      }
    });
    return { ...table, foreign_keys };
  });
}

export function CsvBundleDialog({ files, onCancel, onSubmit }: Props) {
  const { t } = useI18n();
  const [tables, setTables] = useState<DraftTable[]>([]);
  const firstTableName = tableName(files[0]?.name || 'CSV');
  const additionalTableCount = Math.max(0, files.length - 1);
  const [datasetName, setDatasetName] = useState(additionalTableCount
    ? `${firstTableName} and ${additionalTableCount} more table${additionalTableCount === 1 ? '' : 's'}`
    : firstTableName);
  const [error, setError] = useState('');
  const [description, setDescription] = useState('');
  const [loading, setLoading] = useState(true);
  const sequence = useRef(0);
  const nextId = () => ++sequence.current;
  const tableNames = useMemo(() => tables.map(table => table.name), [tables]);

  useEffect(() => {
    let alive = true;
    Promise.all(files.map(inspectFile)).then(result => {
      if (!alive) return;
      const seen = new Map<string, number>();
      const unique = result.map(table => {
        const count = (seen.get(table.name) || 0) + 1;
        seen.set(table.name, count);
        return count === 1 ? table : { ...table, name: `${table.name}_${count}` };
      });
      setTables(suggestedRelationships(unique, nextId));
    }).catch(cause => {
      if (alive) setError(String(cause?.message || cause));
    }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [files]);

  function replaceTable(index: number, update: (table: DraftTable) => DraftTable) {
    setTables(current => current.map((table, tableIndex) => tableIndex === index ? update(table) : table));
  }
  function renameTable(index: number, name: string) {
    const previous = tables[index].name;
    setTables(current => current.map((table, tableIndex) => ({
      ...table,
      name: tableIndex === index ? name : table.name,
      foreign_keys: table.foreign_keys.map(fk => fk.ref_table === previous ? { ...fk, ref_table: name } : fk),
    })));
  }
  function addForeignKey(index: number) {
    const table = tables[index];
    const parent = tables.find((_, parentIndex) => parentIndex !== index) || table;
    replaceTable(index, current => ({ ...current, foreign_keys: [...current.foreign_keys, {
      id: nextId(), columns: current.columns.slice(0, 1), ref_table: parent.name,
      ref_columns: (parent.primary_key.length ? parent.primary_key : parent.columns).slice(0, 1),
    }] }));
  }
  function updateForeignKey(tableIndex: number, id: number, patch: Partial<DraftForeignKey>) {
    replaceTable(tableIndex, table => ({ ...table, foreign_keys: table.foreign_keys.map(fk => fk.id === id ? { ...fk, ...patch } : fk) }));
  }
  function submit() {
    const names = tables.map(table => table.name.trim());
    if (!datasetName.trim()) { setError(t('Enter a dataset name.', '请输入数据集名称。')); return; }
    if (names.some(name => !name) || new Set(names).size !== names.length) { setError(t('Table names must be non-empty and unique.', '表名不能为空且不能重复。')); return; }
    for (const table of tables) {
      if (!table.primary_key.length && new Set(table.foreign_keys.map(fk => JSON.stringify(fk.columns))).size !== 2) {
        setError(t(`Table ${table.name}: configure a primary key for nodes, or two foreign-key column groups for edges.`, `表 ${table.name}：请设置主键以生成节点，或配置两组外键列以生成边。`)); return;
      }
      for (const fk of table.foreign_keys) {
        if (!fk.columns.length || fk.columns.length !== fk.ref_columns.length || !names.includes(fk.ref_table)) {
          setError(t('Each foreign key must map the same number of source and target columns.', '每个外键的来源列和目标列数量必须相同。')); return;
        }
        if (!tables.find(parent => parent.name === fk.ref_table)?.primary_key.length) {
          setError(t(`Target table ${fk.ref_table} needs a primary key to create endpoint nodes.`, `目标表 ${fk.ref_table} 需要设置主键，才能生成边的端点节点。`)); return;
        }
      }
      if (new Set(table.foreign_keys.map(({ columns, ref_table, ref_columns }) => JSON.stringify([columns, ref_table, ref_columns]))).size !== table.foreign_keys.length) {
        setError(t(`Table ${table.name} contains duplicate foreign keys.`, `表 ${table.name} 存在重复外键，请为边的两端配置不同的外键。`)); return;
      }
    }
    const schema: CsvBundleSchema = { version: 'csv_bundle.v1', description: description.trim(), tables: tables.map(table => ({
      file_name: table.file_name, name: table.name.trim(), columns: table.columns,
      primary_key: table.primary_key, foreign_keys: table.foreign_keys.map(({ id: _id, ...fk }) => fk),
    })) };
    onSubmit(files, schema, datasetName.trim());
  }

  return <PopupBackdrop className="csv-bundle-backdrop" role="presentation">
    <Popup.section className="csv-bundle-dialog" role="dialog" aria-modal="true" aria-labelledby="csv-bundle-title">
      <header><div><span>{t('MULTI-TABLE CSV', '多表 CSV')}</span><h2 id="csv-bundle-title">{t('Configure primary and foreign keys', '配置主键与外键')}</h2></div><button onClick={onCancel} aria-label={t('Close', '关闭')}><Icon name="close" size={18} /></button></header>
      <div className="csv-bundle-body">
        <label className="csv-bundle-name">{t('Dataset name', '数据集名称')}<input value={datasetName} onChange={event => setDatasetName(event.target.value)} /></label>
        <label className="csv-bundle-name">{t('Dataset description', '数据集描述')}<textarea rows={3} maxLength={4000} value={description} onChange={event => setDescription(event.target.value)} placeholder={t('Describe the data source, entities, and relationships.', '描述数据来源、实体含义和关系。')} /></label>
        <p className="csv-bundle-help">{t('Key suggestions use column names only; please review them.', '主外键建议仅基于列名，请核对。')}</p>
        <details className="csv-key-guide"><summary>{t('PK/FK configuration guide', 'PK/FK 配置说明')}</summary><p><b>PK · {t('Primary key', '主键')}</b> {t('Uniquely identifies one row (or a combination of columns). Values must be unique and non-null.', '唯一标识一条记录，可由多个字段组成；值必须唯一且不能为空。')}</p><p><b>FK · {t('Foreign key', '外键')}</b> {t('References another table’s key to create graph relationships. Composite key columns are paired in the displayed order.', '引用另一张表的键以生成图中的连接；复合键按界面显示的列顺序一一对应。')}</p><p>{t('Tables with a primary key create nodes. Tables without a primary key create edges using two foreign-key column groups to node tables; the first group is the source and the second is the target. A group can reference alternative target tables, with exactly one match per row. Other fields become edge properties. Blank PK stays blank.', '设置主键的表生成节点；无主键的表通过两组外键列生成边，第一组为起点，第二组为终点，目标表均须设置主键。同组可配置多个候选目标表，每条记录须唯一匹配。其他字段保留为边属性。主键留空不会自动补充。')}</p></details>
        {loading && <div className="csv-bundle-loading">{t('Reading CSV headers…', '正在读取 CSV 表头…')}</div>}
        {!loading && tables.map((table, tableIndex) => <article className="csv-table-card" key={table.file_name}>
          <div className="csv-table-head"><div><strong>{table.file_name}</strong><small>{table.columns.length} {t('columns', '列')}</small></div><label>{t('Table name', '表名')}<input value={table.name} onChange={event => renameTable(tableIndex, event.target.value)} /></label></div>
          <fieldset><legend>{t('Primary key', '主键')} <small>{t('(multiple columns allowed; leave blank for an edge table)', '（可多选；关系表请留空）')}</small></legend><div className="csv-column-options">{table.columns.map(column => <label key={column}><input type="checkbox" checked={table.primary_key.includes(column)} onChange={event => replaceTable(tableIndex, current => ({ ...current, primary_key: event.target.checked ? [...current.primary_key, column] : current.primary_key.filter(value => value !== column) }))} />{column}</label>)}</div></fieldset>
          <p className="csv-bundle-help">{table.primary_key.length ? t('Node table: each row becomes a node.', '节点表：每条记录生成一个节点。') : t('Edge table: configure two foreign-key column groups for its endpoints; other fields become edge properties.', '关系表：配置两组外键列作为边的两端，其他字段作为边属性。')}</p>
          <div className="csv-field-summary" aria-label={t(`Field roles for ${table.name}`, `${table.name} 字段角色`)}>{table.columns.map(column => <span key={column}><code>{column}</code>{table.primary_key.includes(column) && <b>PK</b>}{table.foreign_keys.some(fk => fk.columns.includes(column)) && <b>FK</b>}{!table.primary_key.includes(column) && !table.foreign_keys.some(fk => fk.columns.includes(column)) && <small>{t('Attribute', '特征属性')}</small>}</span>)}</div>
          <div className="csv-fk-heading"><strong>{t('Foreign keys', '外键')}</strong><button className="secondary-command" onClick={() => addForeignKey(tableIndex)}><Icon name="plus" size={13} />{t('Add relationship', '添加关系')}</button></div>
          {table.foreign_keys.length === 0 && <small className="csv-no-fk">{t('No foreign keys configured for this table.', '此表尚未配置外键。')}</small>}
          {table.foreign_keys.map(fk => {
            const parent = tables.find(candidate => candidate.name === fk.ref_table) || tables[0];
            return <div className="csv-fk-entry" key={fk.id}><div className="csv-fk-row">
              <label>{t('Source columns', '来源列')}<select multiple value={fk.columns} onChange={event => updateForeignKey(tableIndex, fk.id, { columns: Array.from(event.target.selectedOptions, option => option.value) })}>{table.columns.map(column => <option key={column}>{column}</option>)}</select></label>
              <span>→</span>
              <label>{t('Target table', '目标表')}<select value={fk.ref_table} onChange={event => { const target = tables.find(candidate => candidate.name === event.target.value)!; updateForeignKey(tableIndex, fk.id, { ref_table: target.name, ref_columns: (target.primary_key.length ? target.primary_key : target.columns).slice(0, 1) }); }}>{tableNames.map(name => <option key={name}>{name}</option>)}</select></label>
              <label>{t('Target columns', '目标列')}<select multiple value={fk.ref_columns} onChange={event => updateForeignKey(tableIndex, fk.id, { ref_columns: Array.from(event.target.selectedOptions, option => option.value) })}>{parent.columns.map(column => <option key={column}>{column}</option>)}</select></label>
              <button className="danger" onClick={() => replaceTable(tableIndex, current => ({ ...current, foreign_keys: current.foreign_keys.filter(item => item.id !== fk.id) }))} aria-label={t('Remove foreign key', '删除外键')}><Icon name="trash" size={14} /></button>
            </div><small className="csv-key-mapping">{fk.columns.map((column, index) => `${table.name}.${column} → ${fk.ref_table}.${fk.ref_columns[index] || '?'}`).join(' · ')}</small></div>;
          })}
        </article>)}
        {error && <div className="csv-bundle-error" role="alert">{error}</div>}
      </div>
      <footer><button className="secondary-command" onClick={onCancel}>{t('Cancel', '取消')}</button><button className="primary-command" onClick={submit} disabled={loading || !tables.length}>{t(`Upload ${files.length} table${files.length === 1 ? '' : 's'}`, `上传 ${files.length} 张表`)}</button></footer>
    </Popup.section>
  </PopupBackdrop>;
}

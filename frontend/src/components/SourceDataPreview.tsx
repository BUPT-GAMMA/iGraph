import { useEffect, useState } from 'react';
import { getConstructionSourcePreview } from '../api';
import { useI18n } from '../i18n';
import type { Translate } from '../i18n';
import type { ConstructionSourcePreview, Dataset } from '../types';
import { Icon } from './Icon';
import './SourceDataPreview.css';

function sampleValue(row: Record<string, unknown>, column: string, t: Translate) {
  if (!Object.prototype.hasOwnProperty.call(row, column)) return { value: t('(missing)', '（缺失）'), empty: true };
  const value = row[column];
  if (value === null) return { value: 'NULL', empty: true };
  if (value === '') return { value: t('(empty string)', '（空字符串）'), empty: true };
  const text = typeof value === 'object' ? JSON.stringify(value) : String(value);
  return { value: text.length > 300 ? `${text.slice(0, 300)}…` : text, empty: false };
}

export function SourceDataPreview({ dataset, delimiter, graphReady = false }: { dataset: Dataset; delimiter?: string; graphReady?: boolean }) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(!graphReady);
  const [preview, setPreview] = useState<ConstructionSourcePreview | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [selectedTable, setSelectedTable] = useState('');
  useEffect(() => { setExpanded(!graphReady); }, [graphReady]);
  useEffect(() => {
    const controller = new AbortController();
    setPreview(null); setError(''); setLoading(true); setSelectedTable('');
    void getConstructionSourcePreview(dataset, controller.signal, delimiter).then(result => {
      if (controller.signal.aborted) return;
      if (result.dataset_id !== dataset.dataset_id || !Array.isArray(result.tables)) throw new Error('Invalid source preview response');
      setPreview(result); setSelectedTable(result.tables[0]?.name || '');
    }).catch(cause => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [dataset.dataset_id, dataset.project_id, delimiter, attempt]);
  const table = preview?.tables.find(item => item.name === selectedTable) || preview?.tables[0];
  const rows = table?.sample_rows.slice(0, 5) || [];
  const descriptionSource = (source: string) => source === 'comment' ? t('Source comment', '源文件注释')
    : source === 'schema' ? t('Schema definition', '结构定义')
      : source === 'inferred' ? t('Inferred · verify', '推断 · 待核对') : t('Not documented', '未提供说明');
  return <details className="source-data-preview" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><Icon name="table" size={16} /><strong>{t('Source data preview', '源数据示例')}</strong><span>{dataset.file_type.toUpperCase()}</span></summary>
    <div className="source-preview-content">
      <p className="source-preview-intro">{t('Inspect sample records and field definitions before constructing the graph.', '构图前先查看样例记录与字段说明，确认源数据内容。')}</p>
      {loading && <p role="status">{t('Loading source samples…', '正在读取源数据示例…')}</p>}
      {error && <div className="source-preview-error" role="status"><p>{t('Source preview could not be loaded. You can retry or continue constructing the graph.', '源数据示例暂时无法读取，可重试或继续构图。')} <span>{error}</span></p><button type="button" className="secondary-command" onClick={() => setAttempt(value => value + 1)}>{t('Retry preview', '重试预览')}</button></div>}
      {preview && <>
        {preview.tables.length > 0 && <>
          <div className="source-preview-table-picker"><label>{t('Source table', '源数据表')}<select value={table?.name || ''} onChange={event => setSelectedTable(event.target.value)}>{preview.tables.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}</select></label><span>{t(`${preview.tables.length} tables`, `${preview.tables.length} 张表`)}</span></div>
          {table && <>
            <div className="source-preview-caption"><strong>{table.name}</strong><span>{t(`${table.columns.length} fields · ${rows.length} sample rows`, `${table.columns.length} 个字段 · ${rows.length} 条示例`)}{table.row_count !== null && ` · ${t(`${table.row_count} rows in source`, `源表共 ${table.row_count} 行`)}`}</span></div>
            {rows.length ? <div className="source-preview-table-scroll" tabIndex={0} role="region" aria-label={t(`Sample records: ${table.name}`, `示例记录：${table.name}`)}><table className="source-preview-records"><thead><tr><th scope="col">#</th>{table.columns.map(column => <th scope="col" key={column}>{column}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}><th scope="row">{index + 1}</th>{table.columns.map(column => { const cell = sampleValue(row, column, t); return <td key={column} className={cell.empty ? 'source-preview-empty-value' : ''}>{cell.value}</td>; })}</tr>)}</tbody></table></div>
              : <p className="source-preview-note">{t('No sample records were found. The available field definitions are shown below.', '未找到示例记录，可查看下方已有字段定义。')}</p>}
            <p className="source-preview-note">{t('Only a bounded sample is displayed; long values may be shortened. NULL, empty strings and missing fields are shown separately.', '这里只展示有限样例，长值可能截断；NULL、空字符串与缺失字段分别标注。')}{table.sample_truncated && ` ${t('More records exist outside this sample.', '源表还有未展示的记录。')}`}</p>
            <details className="source-preview-field-details" open><summary>{t('Field meanings and definitions', '字段含义与定义')}</summary>
              <p className="source-preview-note">{t('Source comments are shown when available. Structural definitions and inferences are labeled; they do not establish an undocumented business meaning.', '优先展示源文件注释；结构定义和推断会单独标注，不能替代未提供的业务含义。')}</p>
              <div className="source-preview-definitions-scroll"><table className="source-preview-definitions"><thead><tr><th scope="col">{t('Field', '字段')}</th><th scope="col">{t('Type / key', '类型 / 键')}</th><th scope="col">{t('Meaning / definition', '含义 / 定义')}</th></tr></thead><tbody>{table.columns.map(column => {
                const meaning = table.column_descriptions?.[column];
                const references = table.foreign_keys.filter(key => key.columns.includes(column));
                return <tr key={column}><th scope="row"><code>{column}</code></th><td>{table.column_types?.[column] || t('Unspecified', '未声明')}{table.primary_key.includes(column) && <b className="schema-key pk">PK</b>}{references.map((key, index) => <small key={index}>FK → {key.ref_table}.{key.ref_columns[key.columns.indexOf(column)] || t('primary key', '主键')}</small>)}</td><td><span className={`source-preview-meaning-source ${meaning?.source || 'unavailable'}`}>{descriptionSource(meaning?.source || 'unavailable')}</span><span>{meaning?.text || t('No field description was provided in the source.', '源文件未提供该字段的含义说明。')}</span></td></tr>;
              })}</tbody></table></div>
            </details>
          </>}
        </>}
        {preview.text && <section className="source-preview-text" aria-label={t('Text sample', 'TXT 示例内容')}><div className="source-preview-caption"><strong>{t('Text excerpt', '文本片段')}</strong>{preview.text.total_lines !== null && <span>{t(`${preview.text.total_lines} lines in source`, `源文件共 ${preview.text.total_lines} 行`)}</span>}</div>{preview.text.excerpt ? <pre>{preview.text.excerpt.slice(0, 4000)}</pre> : <p>{t('The source text is empty.', '源文本为空。')}</p>}{preview.text.truncated && <p className="source-preview-note">{t('This excerpt is shortened; the original file is unchanged.', '此处仅显示截断后的片段，预览不会修改原始文件。')}</p>}</section>}
        {!preview.tables.length && !preview.text && <p>{t('There is no previewable source content.', '暂无可预览的源数据内容。')}</p>}
        {preview.warnings?.length > 0 && <ul className="source-preview-warnings">{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
      </>}
    </div>
  </details>;
}

import { useEffect, useId, useRef, useState } from 'react';
import { getConstructionSourcePreview } from '../api';
import { useI18n, type Translate } from '../i18n';
import type { ConstructionSourcePreview, ConstructionSourcePreviewTable, Dataset } from '../types';
import { Icon } from './Icon';
import { Popup } from './PopupMotion';
import './SourceDataPreview.css';

function sampleValue(row: Record<string, unknown>, column: string, t: Translate) {
  if (!Object.prototype.hasOwnProperty.call(row, column)) return { value: t('(missing)', '（缺失）'), empty: true };
  const value = row[column];
  if (value === null) return { value: 'NULL', empty: true };
  if (value === '') return { value: t('(empty string)', '（空字符串）'), empty: true };
  const text = typeof value === 'object' ? JSON.stringify(value) : String(value);
  return { value: text.length > 300 ? `${text.slice(0, 300)}…` : text, empty: false };
}

export function SourceRecordTable({ columns, rows, label, primaryKey, foreignColumns }: {
  columns: string[];
  rows: Array<Record<string, unknown>>;
  label: string;
  primaryKey: string[];
  foreignColumns: string[];
}) {
  const { t } = useI18n();
  return <div className="source-preview-table-scroll" tabIndex={0} role="region" aria-label={label}>
      <table className="source-preview-records">
        <thead><tr><th scope="col">#</th>{columns.map(column => <th scope="col" key={column} title={column}>
          {column}{primaryKey.includes(column) && <span className="source-table-key">PK</span>}
          {foreignColumns.includes(column) && <span className="source-table-key">FK</span>}
        </th>)}</tr></thead>
        <tbody>{rows.map((row, index) => <tr key={index}>
          <th scope="row">{index + 1}</th>{columns.map(column => {
            const cell = sampleValue(row, column, t);
            return <td key={column} title={cell.value} className={cell.empty ? 'source-preview-empty-value' : ''}>{cell.value}</td>;
          })}
        </tr>)}</tbody>
      </table>
    </div>;
}

function SourceTable({ table }: { table: ConstructionSourcePreviewTable }) {
  const { language, t } = useI18n();
  const format = new Intl.NumberFormat(language);
  const rows = table.sample_rows.slice(0, 5);
  const moreRecords = table.sample_truncated || rows.length < table.sample_rows.length;
  const label = t(`Sample records: ${table.name}`, `示例记录：${table.name}`);
  const descriptionSource = (source: string) => source === 'comment' ? t('Source comment', '源文件注释')
    : source === 'schema' ? t('Schema definition', '结构定义')
      : source === 'inferred' ? t('Inferred · verify', '推断 · 待核对') : t('Not documented', '未提供说明');
  return <div className="source-preview-table-panel">
    <header className="source-preview-caption">
      <strong>{table.name}</strong>
      <span>{t(`${format.format(table.columns.length)} fields · ${format.format(rows.length)} sample rows`, `${format.format(table.columns.length)} 个字段 · ${format.format(rows.length)} 条示例`)}
        {table.row_count !== null && ` · ${t(`${format.format(table.row_count)} rows in source`, `源表共 ${format.format(table.row_count)} 行`)}`}</span>
    </header>
    {rows.length ? <SourceRecordTable columns={table.columns} rows={rows} label={label}
      primaryKey={table.primary_key} foreignColumns={table.foreign_keys.flatMap(key => key.columns)} /> : <p className="source-preview-note">{t('No sample records were found. The available field definitions are shown below.', '未找到示例记录，可查看下方已有字段定义。')}</p>}
    {(moreRecords || table.values_truncated) && <p className="source-preview-note">
      {moreRecords && t('More records exist outside this sample.', '源表还有未展示的记录。')}
      {table.values_truncated && t(' Long values are shortened.', '长字段值已截断。')}
    </p>}
    <details className="source-preview-field-details" open>
      <summary>{t('Field meanings and definitions', '字段含义与定义')}</summary>
      <div className="source-preview-definitions-scroll">
        <table className="source-preview-definitions">
          <thead><tr><th scope="col">{t('Field', '字段')}</th><th scope="col">{t('Type / key', '类型 / 键')}</th><th scope="col">{t('Meaning / definition', '含义 / 定义')}</th></tr></thead>
          <tbody>{table.columns.map(column => {
            const meaning = table.column_descriptions?.[column];
            const references = table.foreign_keys.filter(key => key.columns.includes(column));
            return <tr key={column}>
              <th scope="row"><code>{column}</code></th>
              <td>{table.column_types?.[column] || t('Unspecified', '未声明')}
                {table.primary_key.includes(column) && <span className="source-table-key">PK</span>}
                {references.map((key, index) => <small key={index}>FK → {key.ref_table}.{key.ref_columns[key.columns.indexOf(column)] || t('primary key', '主键')}</small>)}
              </td>
              <td><span className={`source-preview-meaning-source ${meaning?.source || 'unavailable'}`}>{descriptionSource(meaning?.source || 'unavailable')}</span>
                <span>{meaning?.text || t('No field description was provided in the source.', '源文件未提供该字段的含义说明。')}</span></td>
            </tr>;
          })}</tbody>
        </table>
      </div>
    </details>
  </div>;
}

function SourcePreviewContent({ dataset, delimiter }: { dataset: Dataset; delimiter?: string }) {
  const { language, t } = useI18n();
  const [preview, setPreview] = useState<ConstructionSourcePreview | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [selectedTable, setSelectedTable] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    setPreview(null);
    setError('');
    setLoading(true);
    setSelectedTable('');
    void getConstructionSourcePreview(dataset, controller.signal, delimiter).then(result => {
      if (controller.signal.aborted) return;
      if (result.dataset_id !== dataset.dataset_id || !Array.isArray(result.tables)) throw new Error('Invalid source preview response');
      setPreview(result);
      setSelectedTable(result.tables[0]?.name || '');
    }).catch(cause => {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : String(cause));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [dataset.dataset_id, dataset.project_id, delimiter, attempt]);

  const table = preview?.tables.find(item => item.name === selectedTable) || preview?.tables[0];
  const format = new Intl.NumberFormat(language);
  return <div className="source-preview-content" aria-busy={loading}>
    {loading && <p className="source-preview-placeholder" role="status">{t('Loading source samples…', '正在读取源数据示例…')}</p>}
    {error && <div className="source-preview-error" role="status">
      <p>{t('Source preview could not be loaded.', '源数据示例暂时无法读取。')} <span>{error}</span></p>
      <button type="button" className="secondary-command" onClick={() => setAttempt(value => value + 1)}>{t('Retry preview', '重试预览')}</button>
    </div>}
    {preview && <>
      {table && <div className="source-preview-browser">
        <div className="source-preview-tables" role="group" aria-label={t('Source tables', '源数据表')}>
          {preview.tables.map(item => <button key={item.name} type="button" aria-pressed={item === table}
            aria-label={item.name} title={item.name} onClick={() => setSelectedTable(item.name)}>
            <Icon name="table" size={16} />
            <span><strong>{item.name}</strong>
              <small>{item.row_count === null
                ? t(`${format.format(item.columns.length)} fields`, `${format.format(item.columns.length)} 列`)
                : t(`${format.format(item.row_count)} rows × ${format.format(item.columns.length)} fields`, `${format.format(item.row_count)} 行 × ${format.format(item.columns.length)} 列`)}</small>
            </span>
          </button>)}
        </div>
        <SourceTable key={table.name} table={table} />
      </div>}
      {preview.text && <section className="source-preview-text" aria-label={t('Text sample', 'TXT 示例内容')}>
        <header className="source-preview-caption"><strong>{preview.source_filename}</strong>
          {preview.text.total_lines !== null && <span>{t(`${format.format(preview.text.total_lines)} lines in source`, `源文件共 ${format.format(preview.text.total_lines)} 行`)}</span>}
        </header>
        {preview.text.excerpt ? <pre>{preview.text.excerpt.slice(0, 4000)}</pre> : <p>{t('The source text is empty.', '源文本为空。')}</p>}
        {(preview.text.truncated || preview.text.excerpt.length > 4000) && <p className="source-preview-note">{t('Only the available excerpt is shown.', '此处仅显示截断后的片段。')}</p>}
      </section>}
      {!preview.tables.length && !preview.text && <p>{t('There is no previewable source content.', '暂无可预览的源数据内容。')}</p>}
      {preview.warnings?.length > 0 && <ul className="source-preview-warnings">{preview.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
    </>}
  </div>;
}

export function SourceDataPreview({ dataset, delimiter }: { dataset: Dataset; delimiter?: string }) {
  return <section className="source-data-preview">
    <SourcePreviewContent dataset={dataset} delimiter={delimiter} />
  </section>;
}

export function SourceDataDialog({ dataset, onClose }: { dataset: Dataset; onClose: () => void }) {
  const { t } = useI18n();
  const heading = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    const previousFocus = document.activeElement;
    if (typeof element?.showModal === 'function') element.showModal();
    else element?.setAttribute('open', '');
    return () => {
      if (typeof element?.close === 'function') element.close();
      else element?.removeAttribute('open');
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
    };
  }, []);
  return <Popup.dialog className="source-preview-dialog" ref={dialog} aria-labelledby={heading}
    onCancel={event => { event.preventDefault(); onClose(); }}
    onClick={event => {
      if (event.target !== event.currentTarget) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX >= bounds.right || event.clientY < bounds.top || event.clientY >= bounds.bottom) onClose();
    }}>
    <header className="source-preview-dialog-head">
      <div><h2 id={heading}>{t('Source data preview', '源数据示例')}</h2><span title={dataset.original_filename}>{dataset.original_filename}</span></div>
      <button type="button" autoFocus className="plain-icon" onClick={onClose} aria-label={t('Close source preview', '关闭源数据示例')}><Icon name="close" size={20} /></button>
    </header>
    <SourcePreviewContent dataset={dataset} />
  </Popup.dialog>;
}

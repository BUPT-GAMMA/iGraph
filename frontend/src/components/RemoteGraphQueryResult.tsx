import { useI18n } from '../i18n';
import { remoteGraphError } from '../remoteGraph';
import type { RemoteQueryResult } from '../types';
import './RemoteGraphConnections.css';

type Evidence = Partial<RemoteQueryResult> & {
  source?: RemoteQueryResult['source']; query?: string;
  labels?: string[]; relationship_types?: string[]; properties?: unknown[];
  coverage?: { sampled?: boolean; truncated?: boolean };
};
function decode(value: unknown): Evidence | null {
  try {
    const result = typeof value === 'string' ? JSON.parse(value) : value;
    if (!result || typeof result !== 'object') return null;
    if (result.success === false && typeof result.error_code === 'string') return result;
    if (!result.source) return null;
    if (!(typeof result.query_id === 'string' && Array.isArray(result.columns) && Array.isArray(result.rows)) && !Array.isArray(result.labels)) return null;
    return result;
  } catch { return null; }
}
function queryArgs(args: unknown): Record<string, unknown> {
  try { const value = typeof args === 'string' ? JSON.parse(args) : args; return value && typeof value === 'object' ? value : {}; } catch { return {}; }
}
function summarizeCell(cell: unknown): unknown {
  if (Array.isArray(cell)) return cell.map(summarizeCell);
  if (cell && typeof cell === 'object') {
    const value = cell as Record<string, unknown>;
    if (value.type === 'node' && Array.isArray(value.labels)) return value.labels.join(', ');
    if (value.type === 'relationship') return String(value.relationship_type || value.type);
    if (value.type === 'path') return 'Path';
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, summarizeCell(item)]));
  }
  return cell;
}
function cellSummary(cell: unknown): string {
  const summary = summarizeCell(cell);
  return typeof summary === 'string' ? summary : JSON.stringify(summary) ?? '';
}
export function RemoteGraphQueryResult({ result, args }: { result: unknown; args?: unknown }) {
  const { t } = useI18n();
  const value = decode(result);
  if (!value) return null;
  const path = value.artifact_path;
  const safePath = typeof path === 'string' && /^Ephemeral\/remote_graph\/[a-zA-Z0-9_-]+\.json$/.test(path) ? path : null;
  const input = queryArgs(args);
  const previewRows = value.rows?.slice(0, 5);
  const shortenedCells = previewRows?.some(row => row.some(cell => cellSummary(cell).length > 160));
  const query = typeof input.query === 'string' ? input.query : value.query;
  return <section className="remote-query-result" aria-label={t('Remote query evidence', '远程查询依据')}>
    <strong>{value.source ? `${value.source.kind === 'falkordb' ? 'FalkorDB' : 'Neo4j'} · ${value.source.name} · ${value.source.database}` : t('Remote database', '远程数据库')}</strong>
    {!value.success && <p role="alert">{remoteGraphError(value.error_code)}</p>}
    {value.truncated && <p className="remote-query-warning">{t('Result truncated: the underlying query was not read in full.', '结果未完整读取：已达到查询上限。')}</p>}
    {value.preview_truncated && <p className="remote-query-warning">{t('Preview truncated: only part of the read result is shown.', '预览已截断：仅展示已读取结果的一部分。')}</p>}
    {value.coverage?.sampled && <p className="remote-query-warning">{t('Sampled structure: this does not describe the entire database.', '结构来自采样，不能代表完整数据库。')}</p>}
    {value.coverage?.truncated && <p className="remote-query-warning">{t('Structure truncated: some schema information is missing.', '结构已截断：部分结构信息未返回。')}</p>}
    {value.success && <>
      {(value.rows?.length || 0) > 5 && <p className="remote-query-warning">{t('Showing the first 5 preview rows; expand technical details for all returned preview rows.', '仅展示前 5 行预览；展开技术详情可查看全部已返回的预览行。')}</p>}
      {shortenedCells && <p className="remote-query-warning">{t('Long cells are shortened; full preview values are in technical details.', '长单元格已缩短显示；完整预览值见技术详情。')}</p>}
      {value.labels ? <p>{t('Node labels', '节点标签')}: {value.labels.join(', ')}<br />{t('Relationship types', '关系类型')}: {value.relationship_types?.join(', ')}</p> : <>
        <p>{t(`Read ${value.returned_rows} rows · Preview ${value.rows?.length} rows`, `已读取 ${value.returned_rows} 行 · 预览 ${value.rows?.length} 行`)}</p>
        <div className="remote-query-table"><table><thead><tr>{value.columns?.map((column, i) => <th key={i}>{column}</th>)}</tr></thead><tbody>{previewRows?.map((row, i) => <tr key={i}>{Array.isArray(row) && row.map((cell, j) => <td key={j}>{/^(element_?id|start_node|end_node)$|^elementId\(/i.test(value.columns?.[j] || '') ? t('See details', '见技术详情') : cellSummary(cell).length > 160 ? `${cellSummary(cell).slice(0, 160)}…` : cellSummary(cell)}</td>)}</tr>)}</tbody></table></div>
      </>}
      {safePath && <p>{t('Workspace result', '工作区结果')} <code>{safePath}</code></p>}
    </>}
    <details><summary>{t('Technical details', '技术详情')}</summary>
      {value.source && <p>{t('Connection', '连接')} <code>{value.source.connection_id}</code> · v{value.source.revision}</p>}
      {value.query_id && <p>{t('Query', '查询')} <code>{value.query_id}</code> · {value.elapsed_ms} ms</p>}
      {query && <pre>{query}</pre>}
      {input.parameters != null && <pre>{JSON.stringify(input.parameters, null, 2)}</pre>}
      {value.rows && <pre>{JSON.stringify({ columns: value.columns, rows: value.rows }, null, 2)}</pre>}
      {value.properties && <pre>{JSON.stringify(value.properties, null, 2)}</pre>}
    </details>
  </section>;
}

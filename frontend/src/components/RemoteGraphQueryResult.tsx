import { useI18n } from '../i18n';
import { remoteGraphError } from '../remoteGraph';
import type { RemoteQueryResult } from '../types';
import './RemoteGraphConnections.css';

function decode(value: unknown): RemoteQueryResult | null {
  try {
    const result = typeof value === 'string' ? JSON.parse(value) : value;
    if (!result || typeof result !== 'object' || typeof result.query_id !== 'string' || !result.source || !Array.isArray(result.columns) || !Array.isArray(result.rows)) return null;
    return result;
  } catch { return null; }
}
export function RemoteGraphQueryResult({ result }: { result: unknown }) {
  const { t } = useI18n();
  const value = decode(result);
  if (!value) return null;
  const path = value.artifact_path;
  const safePath = typeof path === 'string' && /^Ephemeral\/remote_graph\/[a-zA-Z0-9_-]+\.json$/.test(path) ? path : null;
  return <section className="remote-query-result" aria-label={t('Remote query evidence', '远程查询依据')}>
    <strong>{value.source.name} · {value.source.database} · v{value.source.revision}</strong>
    <small>{t('Query', '查询')} {value.query_id} · {value.elapsed_ms} ms</small>
    {!value.success ? <p role="alert">{remoteGraphError(value.error_code)}</p> : <>
      <p>{t(`Read ${value.returned_rows} rows · Preview ${value.rows.length} rows`, `已读取 ${value.returned_rows} 行 · 预览 ${value.rows.length} 行`)}</p>
      {value.truncated && <p>{t('Result truncated: the underlying query was not read in full.', '结果未完整读取：已达到查询上限。')}</p>}
      {value.preview_truncated && <p>{t('Preview truncated: only part of the read result is shown.', '预览已截断：仅展示已读取结果的一部分。')}</p>}
      <div className="remote-query-table"><table><thead><tr>{value.columns.map((column, i) => <th key={i}>{column}</th>)}</tr></thead><tbody>{value.rows.map((row, i) => <tr key={i}>{Array.isArray(row) && row.map((cell, j) => <td key={j}>{typeof cell === 'string' ? cell : JSON.stringify(cell)}</td>)}</tr>)}</tbody></table></div>
      {safePath && <p>{t('Workspace result', '工作区结果')} <code>{safePath}</code></p>}
    </>}
  </section>;
}

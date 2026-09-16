import { useEffect, useState } from 'react';
import { apiFetch, ApiError, getUserId } from '../api';
import { tr, useI18n } from '../i18n';

export type WorkspaceFile = { path: string; name: string; zone: 'Uploaded' | 'Derived'; size: number; downloadable?: boolean };

export async function fetchArtifact(sessionId: string, path: string, signal?: AbortSignal) {
  if (!path.startsWith('Derived/') || path.split('/').includes('..')) throw new Error(tr('Invalid artifact path', '无效的产物路径'));
  const userId = getUserId();
  const relative = path.split('/').slice(1).map(encodeURIComponent).join('/');
  const response = await apiFetch('/api/sessions/' + encodeURIComponent(sessionId) + '/workspace/derived/' + relative, { signal });
  if (!response.ok) throw ApiError.fromResponse(response.status, await response.json().catch(() => ({})));
  const blob = await response.blob();
  if (signal?.aborted || userId !== getUserId()) throw new DOMException(tr('Download cancelled', '下载已取消'), 'AbortError');
  return blob;
}

export function WorkspaceFiles({ sessionId, revision = '' }: { sessionId: string; revision?: string }) {
  const { language, t } = useI18n();
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [downloading, setDownloading] = useState('');
  useEffect(() => {
    const changed = (event: Event) => {
      if ((event as CustomEvent).detail?.sessionId === sessionId) setRefresh(value => value + 1);
    };
    window.addEventListener('igraph:workspace-changed', changed);
    return () => window.removeEventListener('igraph:workspace-changed', changed);
  }, [sessionId]);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setFiles([]); setError(''); setLoading(true);
    const userId = getUserId();
    void (async () => {
      try {
        const response = await apiFetch('/api/sessions/' + encodeURIComponent(sessionId) + '/workspace/files', { signal: controller.signal });
        const data = await response.json();
        if (!response.ok) throw ApiError.fromResponse(response.status, data);
        if (active && userId === getUserId()) setFiles(Array.isArray(data.files) ? data.files : []);
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : t('Failed to load file list', '文件列表读取失败'));
      } finally { if (active) setLoading(false); }
    })();
    return () => { active = false; controller.abort(); };
  }, [sessionId, revision, refresh, t]);
  async function download(file: WorkspaceFile) {
    setDownloading(file.path); setError('');
    try {
      const blob = await fetchArtifact(sessionId, file.path);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url; link.download = file.name; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t('Failed to download file', '文件下载失败'));
    } finally { setDownloading(''); }
  }
  const locale = language === 'zh' ? 'zh-CN' : 'en';
  return <section className="workspace-files" aria-label={t('Conversation files', '会话文件')}>
    <header><strong>{t('Conversation files', '会话文件')}</strong><button type="button" onClick={() => setRefresh(value => value + 1)} disabled={loading}>{t('Refresh files', '刷新文件')}</button></header>
    {error && <p role="alert">{error}</p>}
    {loading ? <p role="status">{t('Loading files…', '正在读取文件…')}</p> : files.length === 0 ? <p>{t('No uploaded files or published artifacts', '暂无上传文件或已发布产物')}</p> : <ul>{files.map(file => <li key={file.path}>
      <div><strong>{file.name}</strong><small>{file.zone === 'Derived' ? t('Analysis artifact', '分析产物') : t('Uploaded file', '上传文件')} · {t(`${file.size.toLocaleString(locale)} byte${file.size === 1 ? '' : 's'}`, `${file.size.toLocaleString(locale)} 字节`)}</small></div>
      {file.zone === 'Derived' && file.downloadable !== false && <button type="button" disabled={Boolean(downloading)} onClick={() => void download(file)}>{downloading === file.path ? t('Downloading…', '下载中…') : t('Download', '下载')}</button>}
    </li>)}</ul>}
  </section>;
}

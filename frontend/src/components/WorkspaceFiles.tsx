import { useEffect, useState } from 'react';
import { apiFetch, ApiError, getUserId } from '../api';

export type WorkspaceFile = { path: string; name: string; zone: 'Uploaded' | 'Derived'; size: number; downloadable?: boolean };

export async function fetchArtifact(sessionId: string, path: string, signal?: AbortSignal) {
  if (!path.startsWith('Derived/') || path.split('/').includes('..')) throw new Error('无效的产物路径');
  const userId = getUserId();
  const relative = path.split('/').slice(1).map(encodeURIComponent).join('/');
  const response = await apiFetch('/api/sessions/' + encodeURIComponent(sessionId) + '/workspace/derived/' + relative, { signal });
  if (!response.ok) throw ApiError.fromResponse(response.status, await response.json().catch(() => ({})));
  const blob = await response.blob();
  if (signal?.aborted || userId !== getUserId()) throw new DOMException('下载已取消', 'AbortError');
  return blob;
}

export function WorkspaceFiles({ sessionId, revision = '' }: { sessionId: string; revision?: string }) {
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
        if (active) setError(cause instanceof Error ? cause.message : '文件列表读取失败');
      } finally { if (active) setLoading(false); }
    })();
    return () => { active = false; controller.abort(); };
  }, [sessionId, revision, refresh]);
  async function download(file: WorkspaceFile) {
    setDownloading(file.path); setError('');
    try {
      const blob = await fetchArtifact(sessionId, file.path);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url; link.download = file.name; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '文件下载失败');
    } finally { setDownloading(''); }
  }
  return <section className="workspace-files" aria-label="会话文件">
    <header><strong>会话文件</strong><button type="button" onClick={() => setRefresh(value => value + 1)} disabled={loading}>刷新文件</button></header>
    {error && <p role="alert">{error}</p>}
    {loading ? <p role="status">正在读取文件…</p> : files.length === 0 ? <p>暂无上传文件或已发布产物</p> : <ul>{files.map(file => <li key={file.path}>
      <div><strong>{file.name}</strong><small>{file.zone === 'Derived' ? '分析产物' : '上传文件'} · {file.size.toLocaleString()} 字节</small></div>
      {file.zone === 'Derived' && file.downloadable !== false && <button type="button" disabled={Boolean(downloading)} onClick={() => void download(file)}>{downloading === file.path ? '下载中…' : '下载'}</button>}
    </li>)}</ul>}
  </section>;
}

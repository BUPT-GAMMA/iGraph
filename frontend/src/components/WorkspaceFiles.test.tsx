import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { WorkspaceFiles, fetchArtifact } from './WorkspaceFiles';
import { bindRequestAccount, setUserId } from '../api';

let unbindRequestAccount: (() => void) | undefined;
afterEach(() => { cleanup(); unbindRequestAccount?.(); unbindRequestAccount = undefined; vi.unstubAllGlobals(); localStorage.clear(); });

function readBlobText(blob: Blob): Promise<string> {
  if (typeof blob.text === 'function') return blob.text();
  // jsdom's Blob has no text() method; FileReader keeps the content assertion.
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

it('sends the pinned identity for downloads and rejects cross-zone paths', async () => {
  setUserId('alice');
  unbindRequestAccount = bindRequestAccount('alice');
  const fetchMock = vi.fn().mockResolvedValue(new Response('report'));
  vi.stubGlobal('fetch', fetchMock);
  expect(await readBlobText(await fetchArtifact('session', 'Derived/a b.txt'))).toBe('report');
  expect(new Headers(fetchMock.mock.calls[0][1].headers).get('X-User-ID')).toBe('admin');
  expect(fetchMock.mock.calls[0][0]).toContain('/workspace/derived/a%20b.txt');
  await expect(fetchArtifact('session', 'Derived/../Ephemeral/private')).rejects.toThrow();
});

it('refreshes files after an upload event without changing the session', async () => {
  const fetchMock = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ files: [] })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ files: [{ path: 'Derived/new.txt', name: 'new.txt', zone: 'Derived', size: 2 }] })));
  vi.stubGlobal('fetch', fetchMock);
  render(<WorkspaceFiles sessionId="s" />);
  await screen.findByText('暂无上传文件或已发布产物');
  window.dispatchEvent(new CustomEvent('igraph:workspace-changed', { detail: { sessionId: 's' } }));
  await screen.findByText('new.txt');
  expect(fetchMock).toHaveBeenCalledTimes(2);
});

it('does not let a late previous-session response replace current files', async () => {
  let complete!: (response: Response) => void;
  vi.stubGlobal('fetch', vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => { complete = resolve; }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ files: [{ path: 'Derived/new', name: 'current', zone: 'Derived', size: 2 }] }))));
  const { rerender } = render(<WorkspaceFiles sessionId="old" />);
  rerender(<WorkspaceFiles sessionId="new" />);
  await screen.findByText('current');
  complete(new Response(JSON.stringify({ files: [{ path: 'Derived/old', name: 'stale', zone: 'Derived', size: 1 }] })));
  await waitFor(() => expect(screen.queryByText('stale')).toBeNull());
});

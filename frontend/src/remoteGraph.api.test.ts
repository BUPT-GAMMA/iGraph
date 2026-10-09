import { afterEach, expect, it, vi } from 'vitest';
import { listRemoteGraphs, listRemoteGraphTypes, saveRemoteGraph, testRemoteGraph, deleteRemoteGraph, selectRemoteGraph, USER_KEY } from './api';

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });
it('uses workspace remote graph endpoints and revision without saving a test', async () => {
  localStorage.setItem(USER_KEY, 'alice');
  const fetcher = vi.fn().mockImplementation(async () => new Response('{}'));
  vi.stubGlobal('fetch', fetcher);
  const form = { name: 'Cora', uri: 'bolt+s://db', database: 'neo4j', username: 'reader', password: '' };
  await listRemoteGraphs(); await testRemoteGraph(form); await saveRemoteGraph(form);
  await deleteRemoteGraph('id/a', 2); await selectRemoteGraph('session/a', 'id/a'); await selectRemoteGraph('session/a', null);
  expect(fetcher.mock.calls.map(call => [call[0], call[1].method || 'GET'])).toEqual([
    ['/api/remote-graphs', 'GET'], ['/api/remote-graphs/test', 'POST'], ['/api/remote-graphs', 'POST'],
    ['/api/remote-graphs/id%2Fa?revision=2', 'DELETE'], ['/api/sessions/session%2Fa/remote-graph', 'PUT'], ['/api/sessions/session%2Fa/remote-graph', 'PUT'],
  ]);
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual(form);
  expect(JSON.parse(fetcher.mock.calls[5][1].body)).toEqual({ connection_id: null });
  expect(new Headers(fetcher.mock.calls[0][1].headers).get('X-User-ID')).toBe('admin');
});
it('loads product types through the workspace API', async () => {
  localStorage.setItem(USER_KEY, 'alice');
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ types: [] }), { status: 200 }));
  vi.stubGlobal('fetch', fetcher);
  expect(await listRemoteGraphTypes()).toEqual({ types: [] });
  expect(fetcher.mock.calls[0][0]).toBe('/api/remote-graph-types');
  expect(new Headers(fetcher.mock.calls[0][1].headers).get('X-User-ID')).toBe('admin');
});

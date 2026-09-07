import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, buildDatasetGraph, commitConstruction, getConstructionGraph, getConstructionState, previewConstruction, setUserId } from './api';
import type { Dataset } from './types';

const dataset: Dataset = { dataset_id: 'data /1', project_id: null, name: '关系', original_filename: 'data.sql', file_type: 'sql', status: 'source_ready' };
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });

describe('construction API boundaries', () => {
  it.each([null, 'project /1'])('uses the correct dataset scope (%s), identity, and revision in every operation', async projectId => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ success: true }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock); setUserId('user-test');
    const current = { ...dataset, project_id: projectId };
    const path = projectId ? '/api/projects/project%20%2F1/datasets/data%20%2F1/construction' : '/api/personal-datasets/data%20%2F1/construction';
    const operations = [{ op: 'reverse_edges' as const, edge_ids: ['e1'] }];
    await getConstructionState(current); await buildDatasetGraph(current); await getConstructionGraph(current);
    await previewConstruction(current, 7, operations); await commitConstruction(current, 7, operations);
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual([path, `${path}/build`, `${path}/graph`, `${path}/preview`, `${path}/commit`]);
    expect(fetchMock.mock.calls[0][1].headers.get('X-User-ID')).toBe('user-test');
    for (const index of [3, 4]) expect(JSON.parse(fetchMock.mock.calls[index][1].body)).toEqual({ base_revision: 7, operations });
  });
  it('preserves revision conflict details for the editor without retrying a write', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: { code: 'revision_conflict', message: '版本已更新' } }), { status: 409 }));
    vi.stubGlobal('fetch', fetchMock);
    const promise = commitConstruction(dataset, 4, [{ op: 'reverse_edges', edge_ids: ['e1'] }]);
    await expect(promise).rejects.toBeInstanceOf(ApiError);
    await expect(promise).rejects.toMatchObject({ status: 409, code: 'revision_conflict', message: '版本已更新' });
    expect(fetchMock).toHaveBeenCalledOnce();
  });
});

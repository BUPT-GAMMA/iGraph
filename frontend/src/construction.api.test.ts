import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, buildDatasetGraph, commitConstruction, getConstructionGraph, getConstructionSourcePreview, getConstructionState, previewConstruction, setUserId } from './api';
import type { ConstructionBuildOptions, Dataset } from './types';

const dataset: Dataset = { dataset_id: 'data /1', project_id: null, name: '关系', original_filename: 'data.sql', file_type: 'sql', status: 'source_ready' };
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });

describe('construction API boundaries', () => {
  it.each([null, 'project /1'])('reads bounded source samples in the right scope with identity and cancellation (%s)', async projectId => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ success: true, tables: [] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock); setUserId('user-test');
    const signal = new AbortController().signal;
    await getConstructionSourcePreview({ ...dataset, project_id: projectId }, signal, '\t');
    const scope = projectId ? '/api/projects/project%20%2F1/datasets' : '/api/personal-datasets';
    expect(fetchMock.mock.calls[0][0]).toBe(`${scope}/data%20%2F1/construction/source-preview?delimiter=%09`);
    expect(fetchMock.mock.calls[0][1].signal).toBe(signal);
    expect(fetchMock.mock.calls[0][1].headers.get('X-User-ID')).toBe('user-test');
  });
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
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ options: {} });
    for (const index of [3, 4]) expect(JSON.parse(fetchMock.mock.calls[index][1].body)).toEqual({ base_revision: 7, operations });
  });
  it('forwards construction options and leaves the long build independent of view cancellation', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'building' }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const options: ConstructionBuildOptions = {
      query_count: 15, domain: '订单履约', domain_category: 'commerce', experience_enabled: false,
    };
    await buildDatasetGraph(dataset, options);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ options });
    expect(fetchMock.mock.calls[0][1].method).toBe('POST');
    expect(fetchMock.mock.calls[0][1].signal).toBeUndefined();
  });
  it('preserves an unconfirmed build timeout without automatically repeating the POST', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: {
      code: 'construction_running_unconfirmed', message: '请查询现有构图任务状态',
    } }), { status: 504 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(buildDatasetGraph(dataset, { query_count: 15 })).rejects.toMatchObject({
      status: 504, code: 'construction_running_unconfirmed', message: '请查询现有构图任务状态',
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
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

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Dataset } from '../types';

const mocks = vi.hoisted(() => ({ state: vi.fn() }));
vi.mock('../api', () => ({ getConstructionState: mocks.state }));

import { CreateGraphWorkspace } from './CreateGraphWorkspace';

afterEach(() => { cleanup(); vi.useRealTimers(); mocks.state.mockReset(); });

const datasets: Dataset[] = [
  { dataset_id: 'csv-source', project_id: null, name: '订单源数据', original_filename: 'orders.csv', file_type: 'csv', status: 'source_ready' },
  { dataset_id: 'sql-graph', project_id: null, name: '客户关系图', original_filename: 'customers.sql', file_type: 'sql', status: 'ready', metadata: { num_nodes: 12, num_edges: 18, construction: { status: 'ready', revision: 2 } } },
  { dataset_id: 'ready-graph', project_id: null, name: 'Cora', original_filename: 'cora.pt', file_type: 'pt', status: 'ready' },
];

describe('create graph workspace', () => {
  it('shows a complete empty workbench with supported source upload', () => {
    const { container } = render(<CreateGraphWorkspace datasets={[]} onUpload={vi.fn()} onConstruct={vi.fn()} />);
    expect(screen.getByRole('heading', { name: '构图工作台' })).toBeTruthy();
    expect(screen.getByText('还没有可构图的数据源')).toBeTruthy();
    expect(container.querySelector('input[type="file"]')?.getAttribute('accept')).toBe('.sql,.csv,.txt');
  });

  it('opens constructible sources and explains ready graph datasets separately', () => {
    const onConstruct = vi.fn();
    render(<CreateGraphWorkspace datasets={datasets} onUpload={vi.fn()} onConstruct={onConstruct} />);
    fireEvent.click(screen.getByRole('button', { name: '构图 订单源数据' }));
    fireEvent.click(screen.getByRole('button', { name: '查看构图 客户关系图' }));
    expect(onConstruct).toHaveBeenNthCalledWith(1, datasets[0]);
    expect(onConstruct).toHaveBeenNthCalledWith(2, datasets[1]);
    expect(screen.queryByRole('button', { name: /Cora/ })).toBeNull();
    expect(screen.getByText('另有 1 个现成图数据集无需构图，可在分析任务中直接使用。')).toBeTruthy();
  });

  it('prefers construction metadata for active and failed source states', () => {
    const projected: Dataset[] = [
      { dataset_id: 'building', project_id: null, name: '正在构图', original_filename: 'building.sql', file_type: 'sql', status: 'source_ready', metadata: { construction: { status: 'building', revision: 3 } } },
      { dataset_id: 'failed', project_id: null, name: '构图失败', original_filename: 'failed.csv', file_type: 'csv', status: 'source_ready', metadata: { construction: { status: 'failed', revision: 2 } } },
      { dataset_id: 'unavailable', project_id: null, name: '算法不可用', original_filename: 'source.txt', file_type: 'txt', status: 'source_ready', metadata: { construction: { status: 'algorithm_unavailable', revision: 0 } } },
    ];
    render(<CreateGraphWorkspace datasets={projected} onUpload={vi.fn()} onConstruct={vi.fn()} />);

    expect(screen.getByText('构图处理中，可重新进入查看进度')).toBeTruthy();
    expect(screen.getByRole('button', { name: '查看进度 正在构图' })).toBeTruthy();
    expect(screen.getByText('上次构图失败，源文件仍已保留')).toBeTruthy();
    expect(screen.getByRole('button', { name: '重新构图 构图失败' })).toBeTruthy();
    expect(screen.getByText('构图算法暂不可用，源文件已保留')).toBeTruthy();
    expect(screen.getByRole('button', { name: '算法待接入 算法不可用' })).toBeTruthy();
  });

  it('keeps monitoring a build after returning to the source cards', async () => {
    vi.useFakeTimers();
    const building = { dataset_id: 'building', project_id: null, name: '正在构图', original_filename: 'building.sql', file_type: 'sql', status: 'source_ready', metadata: { construction: { status: 'building', revision: 0 } } } satisfies Dataset;
    const ready = { dataset_id: 'building', status: 'ready', revision: 1, node_count: 12, edge_count: 18 } as const;
    mocks.state.mockResolvedValue(ready);
    const onStateChange = vi.fn();
    render(<CreateGraphWorkspace datasets={[building]} onUpload={vi.fn()} onConstruct={vi.fn()} onStateChange={onStateChange} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(mocks.state).toHaveBeenCalledWith(building, expect.any(AbortSignal));
    expect(onStateChange).toHaveBeenCalledWith(building, ready);
  });

  it('forwards the selected source file and disables actions while busy', () => {
    const onUpload = vi.fn();
    const { container, rerender } = render(<CreateGraphWorkspace datasets={datasets} onUpload={onUpload} onConstruct={vi.fn()} />);
    const file = new File(['from,to\na,b'], 'graph.csv', { type: 'text/csv' });
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } });
    expect(onUpload).toHaveBeenCalledWith(file);

    rerender(<CreateGraphWorkspace datasets={datasets} busy onUpload={onUpload} onConstruct={vi.fn()} />);
    expect((screen.getByRole('button', { name: '上传数据源' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '构图 订单源数据' }) as HTMLButtonElement).disabled).toBe(true);
  });
});

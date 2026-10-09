import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
    expect(container.querySelector('.create-graph-title p')).toBeNull();
    expect(container.querySelector('.create-graph-intro-copy p')).toBeNull();
    expect(container.querySelector('.create-graph-section-head p')).toBeNull();
    expect(container.querySelector('.create-graph-kicker')).toBeNull();
    expect(screen.getByText('SQL / CSV / TXT / PT')).toBeTruthy();
    expect(screen.queryByText('结构与数据校验')).toBeNull();
    expect(screen.queryByText('可复用的图数据')).toBeNull();
    expect(container.querySelectorAll('.create-graph-step')).toHaveLength(3);
    expect(container.querySelectorAll('.create-graph-step-icon')).toHaveLength(3);
    expect(container.querySelector('input[type="file"]')?.getAttribute('accept')).toBe('.sql,.csv,.txt,.pt');
    expect(container.querySelector('input[type="file"]')?.hasAttribute('multiple')).toBe(true);
  });

  it('opens construction sources and starts analysis for ready PT graphs', () => {
    const onConstruct = vi.fn();
    const onAnalyze = vi.fn();
    render(<CreateGraphWorkspace datasets={datasets} onUpload={vi.fn()} onConstruct={onConstruct} onAnalyze={onAnalyze} />);
    fireEvent.click(screen.getByRole('button', { name: '构图 订单源数据' }));
    fireEvent.click(screen.getByRole('button', { name: '查看构图 客户关系图' }));
    expect(onConstruct).toHaveBeenNthCalledWith(1, datasets[0]);
    expect(onConstruct).toHaveBeenNthCalledWith(2, datasets[1]);
    fireEvent.click(screen.getByRole('button', { name: '开始分析 Cora' }));
    expect(onAnalyze).toHaveBeenCalledWith(datasets[2]);
    expect(onConstruct).toHaveBeenCalledTimes(2);
    expect(screen.getByText('3 个可用')).toBeTruthy();
    expect(screen.queryByText(/现成图数据集无需构图/)).toBeNull();
  });

  it('accepts a PT file without CSV configuration and prevents mixed or unsupported uploads', () => {
    const onUpload = vi.fn();
    const { container } = render(<CreateGraphWorkspace datasets={[]} onUpload={onUpload} onConstruct={vi.fn()} />);
    const input = container.querySelector('input[type="file"]')!;
    const pt = new File(['graph'], 'graph.PT');
    fireEvent.change(input, { target: { files: [pt] } });
    expect(onUpload).toHaveBeenCalledExactlyOnceWith(pt);
    fireEvent.change(input, { target: { files: [pt, new File(['id\n1'], 'table.csv')] } });
    expect(screen.getByRole('alert').textContent).toContain('多文件构图只能同时选择 CSV 文件');
    fireEvent.change(input, { target: { files: [new File(['other'], 'other.pdf')] } });
    expect(screen.getByRole('alert').textContent).toContain('请选择 SQL、CSV、TXT 或 PT 文件');
    expect(onUpload).toHaveBeenCalledTimes(1);
  });

  it('does not offer failed PT uploads for analysis or construction', () => {
    const onAnalyze = vi.fn();
    render(<CreateGraphWorkspace datasets={[{ ...datasets[2], status: 'failed' }]} onUpload={vi.fn()} onConstruct={vi.fn()} onAnalyze={onAnalyze} />);
    const action = screen.getByRole('button', { name: '开始分析 Cora' }) as HTMLButtonElement;
    expect(action.disabled).toBe(true);
    fireEvent.click(action);
    expect(onAnalyze).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '构图 Cora' })).toBeNull();
  });

  it('keeps user descriptions and graph counts after removing introductory copy', () => {
    render(<CreateGraphWorkspace datasets={[{ ...datasets[1], description: '客户与订单关系，用于分析复购。' }]} onUpload={vi.fn()} onConstruct={vi.fn()} />);
    expect(screen.getByText('客户与订单关系，用于分析复购。')).toBeTruthy();
    expect(screen.getByText('12 个节点 · 18 条边')).toBeTruthy();
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
    const file = new File(['CREATE TABLE graph(id INTEGER PRIMARY KEY);'], 'graph.sql', { type: 'text/plain' });
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } });
    expect(onUpload).toHaveBeenCalledWith(file);

    rerender(<CreateGraphWorkspace datasets={datasets} busy onUpload={onUpload} onConstruct={vi.fn()} />);
    expect((screen.getByRole('button', { name: '上传数据源' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '构图 订单源数据' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('collects multiple CSV files and submits one PK/FK schema', async () => {
    const onUpload = vi.fn();
    const { container } = render(<CreateGraphWorkspace datasets={[]} onUpload={onUpload} onConstruct={vi.fn()} />);
    const customers = new File(['customer_id,name\nc1,Alice\n'], 'customers.csv', { type: 'text/csv' });
    const orders = new File(['order_id,customer_id\no1,c1\n'], 'orders.csv', { type: 'text/csv' });
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [customers, orders] } });
    expect(await screen.findByRole('heading', { name: '配置主键与外键' })).toBeTruthy();
    const guide = container.querySelector<HTMLDetailsElement>('.csv-key-guide');
    expect(guide?.open).toBe(false);
    expect(guide?.querySelector('summary')?.textContent).toBe('PK/FK 配置说明');
    expect(guide?.textContent).toContain('唯一标识一条记录');
    expect(guide?.textContent).toContain('复合键按界面显示的列顺序');
    expect(screen.getByText('主外键建议仅基于列名，请核对。')).toBeTruthy();
    expect((screen.getByLabelText('数据集名称') as HTMLInputElement).value).toBe('customers and 1 more table');
    const submit = await screen.findByRole('button', { name: '上传 2 张表' });
    await waitFor(() => expect((submit as HTMLButtonElement).disabled).toBe(false));
    fireEvent.change(screen.getByLabelText('数据集描述'), { target: { value: '  客户及订单关系  ' } });
    expect(screen.getByLabelText('orders 字段角色').textContent).toContain('FK');
    expect(screen.getByText('orders.customer_id → customers.customer_id')).toBeTruthy();
    fireEvent.click(submit);
    expect(onUpload).toHaveBeenCalledOnce();
    const [files, schema] = onUpload.mock.calls[0];
    expect(files).toEqual([customers, orders]);
    expect(schema.version).toBe('csv_bundle.v1');
    expect(schema.description).toBe('客户及订单关系');
    expect(schema.tables.map((table: { name: string }) => table.name)).toEqual(['customers', 'orders']);
    expect(schema.tables[1].foreign_keys).toEqual([{
      columns: ['customer_id'], ref_table: 'customers', ref_columns: ['customer_id'],
    }]);
  });
});

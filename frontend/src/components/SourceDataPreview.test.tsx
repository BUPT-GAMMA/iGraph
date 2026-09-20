import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConstructionSourcePreview, Dataset } from '../types';
import { SourceDataPreview } from './SourceDataPreview';

const { source } = vi.hoisted(() => ({ source: vi.fn() }));
vi.mock('../api', () => ({ getConstructionSourcePreview: source }));
const dataset: Dataset = { dataset_id: 'd1', project_id: null, name: '订单', original_filename: 'orders.sql', file_type: 'sql', status: 'source_ready' };
const preview: ConstructionSourcePreview = {
  success: true, dataset_id: 'd1', source_type: 'sql', source_filename: 'orders.sql',
  tables: [{ name: 'customers', columns: ['customer_id', 'name', 'active', 'note', 'missing'], column_types: { customer_id: 'INTEGER', name: 'TEXT' }, primary_key: ['customer_id'], foreign_keys: [], row_count: 100, sample_truncated: true,
    sample_rows: [{ customer_id: 0, name: '客户甲', active: false, note: null }, { customer_id: 1, name: '客户乙', active: true, note: '' }],
    column_descriptions: { name: { text: '客户姓名（注册时填写）', source: 'comment' }, customer_id: { text: 'Primary key', source: 'schema' }, active: { text: '可能为启用状态', source: 'inferred' } },
  }, { name: 'orders', columns: ['order_id', 'customer_id'], column_types: {}, primary_key: ['order_id'], foreign_keys: [{ columns: ['customer_id'], ref_table: 'customers', ref_columns: ['customer_id'] }], row_count: 1, sample_truncated: false, sample_rows: [{ order_id: 'ORDER-001', customer_id: 0 }], column_descriptions: {} }],
  limits: { sample_rows: 5, cell_chars: 300, text_chars: 4000 }, warnings: [], truncated: true,
};
beforeEach(() => source.mockResolvedValue(preview));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe('source data preview', () => {
  it('renders real row values including zero/false and distinguishes null, empty and missing', async () => {
    render(<SourceDataPreview dataset={dataset} />);
    const records = await screen.findByRole('region', { name: '示例记录：customers' });
    expect(within(records).getByRole('columnheader', { name: 'customer_id' })).toBeTruthy();
    for (const value of ['0', 'false', 'NULL', '（空字符串）', '客户甲']) expect(within(records).getByText(value)).toBeTruthy();
    expect(within(records).getAllByText('（缺失）')).toHaveLength(2);
    expect(screen.getByText('客户姓名（注册时填写）')).toBeTruthy();
    expect(screen.getByText('源文件注释')).toBeTruthy();
    expect(screen.getByText('结构定义')).toBeTruthy();
    expect(screen.getByText('推断 · 待核对')).toBeTruthy();
    expect(screen.getByText(/源表共 100 行/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('源数据表'), { target: { value: 'orders' } });
    expect(screen.getByText('ORDER-001')).toBeTruthy();
    expect(screen.queryByText('客户甲')).toBeNull();
    expect(screen.getByText('FK → customers.customer_id')).toBeTruthy();
  });

  it('renders TXT excerpt as literal text with line breaks and a truncation notice', async () => {
    const excerpt = '张三在北京任职。\n<script>alert("example")</script>';
    source.mockResolvedValue({ ...preview, source_type: 'txt', tables: [], text: { excerpt, truncated: true, total_lines: 320 } });
    render(<SourceDataPreview dataset={{ ...dataset, file_type: 'txt', original_filename: 'notes.txt' }} />);
    const text = await screen.findByLabelText('TXT 示例内容');
    expect(text.querySelector('pre')?.textContent).toBe(excerpt);
    expect(text.querySelector('script')).toBeNull();
    expect(screen.getByText('源文件共 320 行')).toBeTruthy();
    expect(screen.getByText(/此处仅显示截断后的片段/)).toBeTruthy();
  });

  it('identifies an implicit referenced primary key when SQL omits the target column', async () => {
    source.mockResolvedValue({ ...preview, tables: [{ ...preview.tables[1], foreign_keys: [{ columns: ['customer_id'], ref_table: 'customers', ref_columns: [] }] }] });
    render(<SourceDataPreview dataset={dataset} />);
    expect(await screen.findByText('FK → customers.主键')).toBeTruthy();
    expect(screen.queryByText(/customers\.undefined/)).toBeNull();
    expect(screen.getByText('ORDER-001')).toBeTruthy();
  });

  it('keeps headers and field definitions for an empty table', async () => {
    source.mockResolvedValue({ ...preview, tables: [{ ...preview.tables[0], row_count: 0, sample_rows: [], sample_truncated: false }] });
    render(<SourceDataPreview dataset={dataset} />);
    expect(await screen.findByText(/未找到示例记录/)).toBeTruthy();
    expect(screen.getByText('customer_id')).toBeTruthy();
    expect(screen.getByText(/源表共 0 行/)).toBeTruthy();
  });

  it('retries a failed preview and clears stale table content while fetching another dataset', async () => {
    source.mockRejectedValueOnce(new Error('temporary outage'));
    const view = render(<SourceDataPreview dataset={dataset} />);
    fireEvent.click(await screen.findByRole('button', { name: '重试预览' }));
    expect(await screen.findByText('客户甲')).toBeTruthy();
    let resolve!: (value: ConstructionSourcePreview) => void;
    source.mockImplementationOnce(() => new Promise<ConstructionSourcePreview>(done => { resolve = done; }));
    view.rerender(<SourceDataPreview dataset={{ ...dataset, dataset_id: 'd2' }} />);
    expect(screen.queryByText('客户甲')).toBeNull();
    expect(screen.getByText('正在读取源数据示例…')).toBeTruthy();
    view.unmount();
    expect(source.mock.calls[source.mock.calls.length - 1][1].aborted).toBe(true);
    await act(async () => resolve({ ...preview, dataset_id: 'd2' }));
  });

  it('ignores a late result after changing datasets', async () => {
    let resolve!: (value: ConstructionSourcePreview) => void;
    source.mockImplementationOnce(() => new Promise<ConstructionSourcePreview>(done => { resolve = done; }));
    const view = render(<SourceDataPreview dataset={dataset} />);
    source.mockResolvedValueOnce({ ...preview, dataset_id: 'd2', tables: [], text: { excerpt: '第二份数据', truncated: false, total_lines: 1 } });
    view.rerender(<SourceDataPreview dataset={{ ...dataset, dataset_id: 'd2' }} />);
    expect(await screen.findByText('第二份数据')).toBeTruthy();
    await act(async () => resolve(preview));
    expect(screen.queryByText('客户甲')).toBeNull();
    expect(screen.getByText('第二份数据')).toBeTruthy();
  });

  it('refetches CSV samples for a changed delimiter and bounds the displayed values', async () => {
    source.mockResolvedValue({ ...preview, tables: [{ ...preview.tables[0], columns: ['name'], sample_rows: Array.from({ length: 9 }, () => ({ name: 'x'.repeat(1000) })) }] });
    const view = render(<SourceDataPreview dataset={{ ...dataset, file_type: 'csv' }} delimiter="," />);
    const records = await screen.findByRole('region', { name: '示例记录：customers' });
    expect(within(records).getAllByRole('cell')).toHaveLength(5);
    expect(within(records).getAllByRole('cell')[0].textContent).toHaveLength(301);
    view.rerender(<SourceDataPreview dataset={{ ...dataset, file_type: 'csv' }} delimiter=";" />);
    await screen.findByRole('region', { name: '示例记录：customers' });
    expect(source).toHaveBeenLastCalledWith(expect.objectContaining({ dataset_id: 'd1' }), expect.any(AbortSignal), ';');
  });
});

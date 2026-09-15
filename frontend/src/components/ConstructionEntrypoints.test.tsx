import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Dataset } from '../types';
import { DatasetPanel } from './DatasetPanel';
import { GraphWorkspace } from './GraphWorkspace';

afterEach(cleanup);
const datasets: Dataset[] = [
  { dataset_id: 'd1', project_id: null, name: 'SQL 源文件', original_filename: 'data.sql', file_type: 'sql', status: 'source_ready' },
  { dataset_id: 'd2', project_id: null, name: '客户关系图', original_filename: 'customers.csv', file_type: 'csv', status: 'ready', metadata: { construction: { status: 'ready', revision: 1 } } },
];

describe('construction entry points', () => {
  it('keeps source files selectable for construction without offering them as ready graphs', () => {
    const construct = vi.fn(); const select = vi.fn();
    const { container } = render(<DatasetPanel datasets={datasets} onConstruct={construct} onSelect={select} onUpload={vi.fn()} onRename={vi.fn()} onDelete={vi.fn()} onClose={vi.fn()} />);
    expect(container.querySelector('input[type="file"]')?.getAttribute('accept')).toContain('.sql');
    fireEvent.click(screen.getByRole('button', { name: '构图 SQL 源文件' }));
    expect(construct).toHaveBeenCalledWith(datasets[0]);
    fireEvent.click(screen.getByRole('button', { name: '查看构图 客户关系图' }));
    expect(construct).toHaveBeenLastCalledWith(datasets[1]);
    expect((container.querySelector('.dataset-row-main') as HTMLButtonElement).disabled).toBe(true);
    expect(select).not.toHaveBeenCalled();
  });
  it('exposes the current dataset construction action in the graph workspace', () => {
    const construct = vi.fn();
    render(<GraphWorkspace data={null} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} onConstruct={construct} constructionLabel="查看构图" datasetName="客户关系图" />);
    fireEvent.click(screen.getByRole('button', { name: '查看构图' }));
    expect(construct).toHaveBeenCalledOnce();
  });
});

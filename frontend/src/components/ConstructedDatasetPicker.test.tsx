import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { listConstructedDatasets } from '../api';
import { ConstructedDatasetPicker } from './ConstructedDatasetPicker';
import type { Dataset } from '../types';

vi.mock('../api', () => ({ listConstructedDatasets: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const dataset: Dataset = { dataset_id: 'built', project_id: null, name: 'Orders', description: 'Customer PK/FK', original_filename: 'orders.csv', file_type: 'csv', status: 'ready', metadata: { num_nodes: 7, num_edges: 4 } };
it('loads constructed datasets, searches descriptions, and creates a project copy', async () => {
  vi.mocked(listConstructedDatasets).mockResolvedValue({ datasets: [dataset] });
  const onImport = vi.fn().mockResolvedValue(undefined); const onClose = vi.fn();
  render(<ConstructedDatasetPicker projectId="p1" projectName="Research" onImport={onImport} onClose={onClose} />);
  expect(await screen.findByText('Orders')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('搜索已构图数据集'), { target: { value: 'PK/FK' } });
  fireEvent.click(screen.getByRole('button', { name: '加载到项目' }));
  await waitFor(() => expect(onImport).toHaveBeenCalledWith(dataset));
  await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
});
it('keeps import errors visible and allows retry', async () => {
  vi.mocked(listConstructedDatasets).mockResolvedValue({ datasets: [dataset] });
  const onImport = vi.fn().mockRejectedValue(new Error('源图已删除')); const onClose = vi.fn();
  render(<ConstructedDatasetPicker projectId="p1" projectName="Research" onImport={onImport} onClose={onClose} />);
  fireEvent.click(await screen.findByRole('button', { name: '加载到项目' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '源图已删除重试');
  expect(onClose).not.toHaveBeenCalled();
});

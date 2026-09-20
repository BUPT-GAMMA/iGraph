import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { I18nProvider } from './i18n';
import { serverDatasetActionLabel, serverDatasetPreparationLabel } from './serverDatasets';
import { DatasetPanel } from './components/DatasetPanel';
import type { ServerDataset } from './types';

afterEach(cleanup);

it.each(['not_required', 'ready', 'running', 'queued', 'not_prepared', 'unavailable', 'cancelled', 'failed', 'unknown'])('localizes server state %s', status => {
  const dataset = { preparation: { status, progress: .5, num_nodes: 10, num_edges: 20, message: '后端原始消息' } } as ServerDataset;
  expect(serverDatasetPreparationLabel(dataset, en => en)).not.toMatch(/[\u4e00-\u9fff]/);
  expect(serverDatasetActionLabel(dataset, 'Use', en => en)).not.toMatch(/[\u4e00-\u9fff]/);
  expect(serverDatasetPreparationLabel(dataset, (_, zh) => zh)).toMatch(/[\u4e00-\u9fff]/);
});

it('renders server datasets in English and preserves user dataset names', () => {
  render(<I18nProvider initialLanguage="en"><DatasetPanel datasets={[]} serverDatasets={[{
    server_dataset_id: 'server-1', name: '用户数据集', relative_path: 'data.pt', size_bytes: 20, directed: false,
    preparation: { status: 'ready', num_nodes: 10, num_edges: 20 },
  } as ServerDataset]} onUseServer={vi.fn()} onSelect={vi.fn()} onUpload={vi.fn()} onRename={vi.fn()} onDelete={vi.fn()} onClose={vi.fn()} /></I18nProvider>);
  expect(screen.getByText('Server datasets')).toBeTruthy();
  expect(screen.getByText('用户数据集')).toBeTruthy();
  expect(screen.getByText(/Undirected/)).toBeTruthy();
  expect(screen.getByText('Prepared · 10 nodes · 20 edges')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Use' })).toBeTruthy();
});

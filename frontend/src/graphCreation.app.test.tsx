import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConstructionState, Dataset } from './types';

const mocks = vi.hoisted(() => ({
  listDatasets: vi.fn(),
  uploadDataset: vi.fn(),
}));

vi.mock('react-dom/client', () => ({ createRoot: () => ({ render: () => undefined }) }));
vi.mock('./components/GraphWorkspace', () => ({ GraphWorkspace: () => null }));
vi.mock('./components/GraphConstructionPanel', () => ({
  GraphConstructionPanel: ({ dataset, onClose, onStateChange, embedded }: { dataset: Dataset; onClose: () => void; onStateChange?: (dataset: Dataset, state: ConstructionState) => void; embedded?: boolean }) =>
    <section role={embedded ? 'region' : 'dialog'} aria-label="构图编辑器"><strong>{dataset.name}</strong><button onClick={() => onStateChange?.(dataset, { dataset_id: dataset.dataset_id, status: 'building', revision: 0, build_id: 'build-1', message: 'Agent 正在工作' })}>模拟构图中</button><button onClick={onClose}>更换数据源</button></section>,
}));
vi.mock('./api', async () => ({
  ...await vi.importActual('./api'),
  getUserId: () => 'graph-user',
  validateUser: vi.fn().mockResolvedValue({ valid: true, default_model: 'model' }),
  listProjects: vi.fn().mockResolvedValue({ projects: [] }),
  listSessions: vi.fn().mockResolvedValue({ sessions: [] }),
  listCustomModels: vi.fn().mockResolvedValue({ models: [] }),
  getCapabilities: vi.fn().mockResolvedValue({ run_cancel_v1: true, job_cancel_v1: true }),
  listProjectDatasets: mocks.listDatasets,
  uploadProjectDataset: mocks.uploadDataset,
}));

import App from './main';

const source: Dataset = { dataset_id: 'source-1', project_id: null, name: '订单关系', original_filename: 'orders.csv', file_type: 'csv', status: 'source_ready' };

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); window.history.replaceState({}, '', '/');
  mocks.listDatasets.mockReset().mockResolvedValue({ datasets: [source], default_dataset_id: null });
  mocks.uploadDataset.mockReset().mockResolvedValue({ dataset: source, default_dataset_id: null });
});
afterEach(cleanup);

describe('actual App graph creation destination', () => {
  it('opens the independent workbench from the permanent sidebar entry', async () => {
    render(<App />);
    const entry = await screen.findByRole('button', { name: '创建你的图' });
    fireEvent.click(entry);

    await waitFor(() => expect(mocks.listDatasets).toHaveBeenCalledWith(null));
    expect(entry.getAttribute('aria-current')).toBe('page');
    expect(window.location.search).toBe('?view=create-graph');
    expect(screen.getByRole('heading', { name: '构图工作台' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '构图 订单关系' }));
    expect(screen.getByRole('region', { name: '构图编辑器' }).textContent).toContain('订单关系');
    fireEvent.click(screen.getByRole('button', { name: '模拟构图中' }));
    fireEvent.click(screen.getByRole('button', { name: '更换数据源' }));
    expect(screen.getByRole('heading', { name: '构图工作台' })).toBeTruthy();
    expect(screen.getByText('构图处理中，可重新进入查看进度')).toBeTruthy();
    expect(screen.getByRole('button', { name: '查看进度 订单关系' })).toBeTruthy();
  });

  it('uploads into personal scope and opens the embedded editor immediately', async () => {
    mocks.listDatasets.mockResolvedValue({ datasets: [], default_dataset_id: null });
    const { container } = render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '创建你的图' }));
    await screen.findByText('还没有可构图的数据源');
    const file = new File(['from,to\na,b'], 'orders.csv', { type: 'text/csv' });
    fireEvent.change(container.querySelector('.create-graph-workspace input[type="file"]')!, { target: { files: [file] } });
    const submit = await screen.findByRole('button', { name: '上传 1 张表' });
    await waitFor(() => expect((submit as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(submit);

    await waitFor(() => expect(mocks.uploadDataset).toHaveBeenCalledWith(null, [file], undefined, expect.objectContaining({ version: 'csv_bundle.v1' }), 'orders'));
    expect(screen.getByRole('region', { name: '构图编辑器' })).toBeTruthy();
  });
});

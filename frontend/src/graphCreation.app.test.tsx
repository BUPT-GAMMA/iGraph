import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConstructionState, Dataset } from './types';

const mocks = vi.hoisted(() => ({
  listDatasets: vi.fn(),
  getUserCases: vi.fn(),
  openUserCase: vi.fn(),
  uploadDataset: vi.fn(),
  deleteDataset: vi.fn(),
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
  getUserCases: mocks.getUserCases,
  openUserCase: mocks.openUserCase,
  listProjectDatasets: mocks.listDatasets,
  uploadProjectDataset: mocks.uploadDataset,
  deleteProjectDataset: mocks.deleteDataset,
}));

import App from './main';
import { I18nProvider } from './i18n';

const source: Dataset = { dataset_id: 'source-1', project_id: null, name: '订单关系', original_filename: 'orders.csv', file_type: 'csv', status: 'source_ready' };

it('keeps example questions and default project names localized after entering a project', async () => {
  const example = { id: 'tianlongbabu', name: '天龙八部人物关系示例', filename: 'tianlongbabu-characters.npz', description: '已构好的小说图',
    questions: ['检查人物关系'], available: true, translations: { en: { name: 'Character graph example', description: 'A ready-made character graph', questions: ['Inspect character relationships'] } } };
  mocks.getUserCases.mockResolvedValue({ cases: [example] });
  const dataset: Dataset = { ...source, project_id: 'novel-project', dataset_id: 'novel-dataset', name: example.name,
    original_filename: example.filename, description: example.description, status: 'ready', metadata: { user_case_id: example.id } };
  mocks.openUserCase.mockResolvedValue({ project: { project_id: 'novel-project', user_id: 'graph-user', name: example.name, default_dataset_id: dataset.dataset_id }, dataset });
  mocks.listDatasets.mockImplementation((projectId: string | null) => Promise.resolve({ datasets: [{ ...dataset, project_id: projectId }] }));
  render(<I18nProvider initialLanguage="zh"><App /></I18nProvider>);
  await screen.findByRole('heading', { name: example.name });
  fireEvent.click(screen.getByRole('button', { name: 'EN' }));
  fireEvent.click(screen.getByRole('button', { name: 'Use example graph' }));
  await screen.findByRole('heading', { name: 'Start graph analysis in Character graph example' });
  expect(await screen.findByRole('button', { name: 'Inspect character relationships' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Create a conversation in Character graph example' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '中文' }));
  expect(screen.getByRole('button', { name: '检查人物关系' })).toBeTruthy();
  expect(screen.getByRole('heading', { name: '开始在 天龙八部人物关系示例 中图分析' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'EN' }));
  fireEvent.click(screen.getByRole('button', { name: 'Create your graph' }));
  expect(await screen.findByRole('button', { name: 'View graph build Character graph example' })).toBeTruthy();
  expect(screen.getByText('A ready-made character graph')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '中文' }));
  expect(screen.getByRole('button', { name: '查看构图 天龙八部人物关系示例' })).toBeTruthy();
});

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); window.history.replaceState({}, '', '/');
  mocks.getUserCases.mockReset().mockResolvedValue({ cases: [
    { id: 'finance', name: '金融股权示例', filename: 'finance-ownership.npz', description: '已构好的金融图', questions: ['检查股权数据质量'], available: true },
    { id: 'tianlongbabu', name: '天龙八部人物关系示例', filename: 'tianlongbabu-characters.npz', description: '已构好的人物图', questions: ['展示主要人物关系'], available: true },
  ] });
  mocks.openUserCase.mockReset();
  mocks.listDatasets.mockReset().mockResolvedValue({ datasets: [source], default_dataset_id: null });
  mocks.uploadDataset.mockReset().mockResolvedValue({ dataset: source, default_dataset_id: null });
  mocks.deleteDataset.mockReset().mockResolvedValue({ success: true });
});
afterEach(cleanup);

describe('actual App graph creation destination', () => {
  it('retains a PT upload in the source library and binds it to an analysis draft without construction', async () => {
    const pt: Dataset = { dataset_id: 'pt-graph', project_id: null, name: 'PT 验收图', original_filename: 'PT 验收图.pt', file_type: 'pt', status: 'ready' };
    mocks.listDatasets.mockResolvedValue({ datasets: [] });
    mocks.uploadDataset.mockResolvedValue({ dataset: pt, default_dataset_id: null });
    const { container } = render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '创建你的图' }));
    await screen.findByText('还没有可构图的数据源');
    const file = new File(['pt bytes'], pt.original_filename);
    fireEvent.change(container.querySelector('.create-graph-workspace input[type="file"]')!, { target: { files: [file] } });
    const analyze = await screen.findByRole('button', { name: '开始分析 PT 验收图' });
    expect(mocks.uploadDataset).toHaveBeenCalledWith(null, file, undefined, undefined, undefined);
    expect(screen.queryByRole('region', { name: '构图编辑器' })).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    mocks.listDatasets.mockResolvedValue({ datasets: [pt] });
    fireEvent.click(analyze);
    await screen.findByRole('heading', { name: '开始图分析' });
    expect(window.location.search).toBe('?new=1');
    expect(screen.getByRole('button', { name: 'PT 验收图' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '创建你的图' }));
    expect(await screen.findByRole('button', { name: '开始分析 PT 验收图' })).toBeTruthy();
  });

  it('returns from an example project to both examples when New task is clicked', async () => {
    const dataset: Dataset = { dataset_id: 'novel-graph', project_id: 'novel-project', name: '天龙八部人物关系示例', original_filename: 'tianlongbabu-characters.npz', file_type: 'npz', status: 'ready' };
    mocks.openUserCase.mockResolvedValue({ project: { project_id: 'novel-project', user_id: 'graph-user', name: '天龙八部人物关系示例', default_dataset_id: dataset.dataset_id }, dataset });
    mocks.listDatasets.mockImplementation((projectId: string | null) => Promise.resolve({ datasets: projectId ? [dataset] : [source] }));
    render(<App />);
    const gallery = await screen.findByRole('region', { name: '示例图分析' });
    fireEvent.click(within(gallery).getAllByRole('button', { name: '使用示例图' })[1]);
    await screen.findByRole('heading', { name: '开始在 天龙八部人物关系示例 中图分析' });
    expect(window.location.search).toContain('project=novel-project');
    fireEvent.click(within(screen.getByRole('complementary')).getByRole('button', { name: '新建任务' }));
    await screen.findByRole('heading', { name: '开始图分析' });
    expect(window.location.search).toBe('?new=1');
    expect(screen.getByRole('heading', { name: '金融股权示例' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: '天龙八部人物关系示例' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '在 天龙八部人物关系示例 中新建会话' }));
    await screen.findByRole('heading', { name: '开始在 天龙八部人物关系示例 中图分析' });
    fireEvent.click(screen.getByRole('button', { name: '创建你的图' }));
    await screen.findByRole('button', { name: '构图 订单关系' });
    expect(screen.queryByRole('button', { name: '查看构图 天龙八部人物关系示例' })).toBeNull();
  });

  it('lists both bundled source datasets and opens their saved construction', async () => {
    const examples: Dataset[] = [
      { dataset_id: 'finance-source', project_id: null, name: '金融股权示例', original_filename: 'finance.csv', file_type: 'csv', status: 'ready', metadata: { user_case_id: 'finance', construction: { status: 'ready', revision: 1 }, num_nodes: 6000, num_edges: 13436 } },
      { dataset_id: 'novel-source', project_id: null, name: '天龙八部人物关系示例', original_filename: '天龙八部.txt', file_type: 'txt', status: 'ready', metadata: { user_case_id: 'tianlongbabu', construction: { status: 'ready', revision: 1 }, num_nodes: 304, num_edges: 791 } },
    ];
    mocks.listDatasets.mockResolvedValue({ datasets: [source, ...examples] });
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '创建你的图' }));
    await screen.findByRole('button', { name: '查看构图 天龙八部人物关系示例' });
    expect(screen.getByRole('button', { name: '查看构图 金融股权示例' })).toBeTruthy();
    expect(screen.getByText('3 个可用')).toBeTruthy();
    expect(screen.queryByRole('region', { name: '示例图分析' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '查看构图 天龙八部人物关系示例' }));
    expect(screen.getByRole('region', { name: '构图编辑器' }).textContent).toContain('天龙八部人物关系示例');
  });

  it('shows examples on the home and new-task pages while keeping My graph sources for datasets only', async () => {
    render(<App />);
    await screen.findByRole('heading', { name: '金融股权示例' });
    expect(screen.getByRole('heading', { name: '天龙八部人物关系示例' })).toBeTruthy();

    fireEvent.click(within(screen.getByRole('complementary')).getByRole('button', { name: '新建任务' }));
    await screen.findByRole('heading', { name: '开始图分析' });
    expect(window.location.search).toBe('?new=1');
    expect(screen.getByRole('heading', { name: '金融股权示例' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: '天龙八部人物关系示例' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '展示主要人物关系' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '创建你的图' }));
    await screen.findByRole('button', { name: '构图 订单关系' });
    expect(screen.getByText('1 个可用')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: '金融股权示例' })).toBeNull();
    expect(screen.queryByRole('heading', { name: '天龙八部人物关系示例' })).toBeNull();
    expect(screen.queryByRole('region', { name: '示例图分析' })).toBeNull();
  });

  it('confirms deletion, sends it once, and removes only the chosen construction source', async () => {
    const other = { ...source, dataset_id: 'source-2', name: '保留的数据集' };
    mocks.listDatasets.mockResolvedValue({ datasets: [source, other] });
    let finish!: (value: unknown) => void;
    mocks.deleteDataset.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '创建你的图' }));
    fireEvent.click(await screen.findByRole('button', { name: '删除数据集 订单关系' }));
    let dialog = screen.getByRole('alertdialog');
    expect(dialog.textContent).toContain('确认删除数据集「订单关系」吗？');
    fireEvent.click(within(dialog).getByRole('button', { name: '取消' }));
    expect(mocks.deleteDataset).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '删除数据集 订单关系' }));
    dialog = screen.getByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: '删除' }));
    await waitFor(() => expect(mocks.deleteDataset).toHaveBeenCalledExactlyOnceWith(null, 'source-1'));
    const remove = screen.getByRole('button', { name: '删除数据集 订单关系' }) as HTMLButtonElement;
    expect(remove.disabled).toBe(true);
    fireEvent.click(remove);
    expect(mocks.deleteDataset).toHaveBeenCalledTimes(1);
    await act(async () => { finish({ success: true }); });
    expect(screen.queryByRole('button', { name: '删除数据集 订单关系' })).toBeNull();
    expect(screen.getByRole('button', { name: '构图 保留的数据集' })).toBeTruthy();
  });

  it('keeps the source and reports a failed delete so the user can retry', async () => {
    mocks.deleteDataset.mockRejectedValue(new Error('构图正在运行，请稍后重试'));
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '创建你的图' }));
    fireEvent.click(await screen.findByRole('button', { name: '删除数据集 订单关系' }));
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '删除' }));
    await screen.findByText('构图正在运行，请稍后重试');
    expect((screen.getByRole('button', { name: '删除数据集 订单关系' }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByRole('button', { name: '构图 订单关系' })).toBeTruthy();
  });

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

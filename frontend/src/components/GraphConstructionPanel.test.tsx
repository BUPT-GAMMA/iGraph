import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api';
import type { ConstructionOperation, Dataset } from '../types';
import { GraphConstructionPanel } from './GraphConstructionPanel';

const mocks = vi.hoisted(() => ({ state: vi.fn(), graph: vi.fn(), build: vi.fn(), preview: vi.fn(), commit: vi.fn() }));
vi.mock('../api', async importOriginal => ({
  ...await importOriginal<typeof import('../api')>(),
  getConstructionState: mocks.state, getConstructionGraph: mocks.graph, buildDatasetGraph: mocks.build,
  previewConstruction: mocks.preview, commitConstruction: mocks.commit,
}));
vi.mock('./GraphWorkspace', () => ({ GraphWorkspace: ({ data, onSelect }: any) => <div aria-label="测试图画布">{data.nodes.map((node: any) => <button key={node.id} onClick={() => onSelect(node)}>图节点 {node.id}</button>)}<span>{data.edges.map((edge: any) => `${edge.source} → ${edge.target}`).join(', ')}</span></div> }));

const dataset: Dataset = { dataset_id: 'd1', project_id: null, name: '客户关系', original_filename: 'customers.csv', file_type: 'csv', status: 'ready' };
const graph = { directed: true, nodes: [{ id: 'a', type: 'Person', properties: { phone: '123', age: 29 } }, { id: 'b', type: 'Link', properties: { phone: '123' } }, { id: 'c', type: 'Person', properties: {} }], edges: [{ id: 'e1', source: 'a', target: 'b', type: 'Knows', properties: { weight: 1 } }, { id: 'e2', source: 'b', target: 'c', type: 'Knows', properties: {} }] };
const result = { success: true, status: 'preview', dataset_id: 'd1', base_revision: 2, revision: 3, graph, summary: { before: { node_count: 3, edge_count: 2 }, after: { node_count: 3, edge_count: 2 }, changed_nodes: 1, changed_edges: 1 } };
function choose(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }); }
function selectMany(label: string, values: string[]) {
  const select = screen.getByLabelText(label) as HTMLSelectElement;
  Array.from(select.options).forEach(option => { option.selected = values.includes(option.value); });
  fireEvent.change(select);
}
async function openPanel(onSaved = vi.fn(), onClose = vi.fn()) {
  render(<GraphConstructionPanel dataset={dataset} onClose={onClose} onSaved={onSaved} />);
  await screen.findByLabelText('测试图画布');
}

beforeEach(() => {
  mocks.state.mockResolvedValue({ dataset_id: 'd1', status: 'ready', revision: 2, algorithm_available: false });
  mocks.graph.mockResolvedValue({ dataset_id: 'd1', revision: 2, graph });
  mocks.preview.mockResolvedValue(result);
  mocks.commit.mockResolvedValue({ ...result, status: 'ready' });
});
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe('graph construction workflow', () => {
  it('allows closing while the initial state request is pending', () => {
    mocks.state.mockReturnValue(new Promise(() => undefined));
    const close = vi.fn();
    render(<GraphConstructionPanel dataset={dataset} onClose={close} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '关闭构图工作台' }));
    expect(close).toHaveBeenCalledOnce();
  });
  it('preserves the source-only state while construction algorithms are unavailable', async () => {
    mocks.state.mockResolvedValue({ dataset_id: 'd1', status: 'not_built', revision: 0, algorithm_available: false });
    render(<GraphConstructionPanel dataset={{ ...dataset, status: 'source_ready' }} onClose={vi.fn()} onSaved={vi.fn()} />);
    const build = await screen.findByRole('button', { name: '构图算法待接入' });
    expect((build as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '保存到数据集' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('源文件已就绪，等待构图算法')).toBeTruthy();
    expect(mocks.graph).not.toHaveBeenCalled();
    expect(mocks.build).not.toHaveBeenCalled();
  });

  const cases: Array<{ label: string; fill: () => void; expected: ConstructionOperation }> = [
    { label: '节点转成边', fill: () => { choose('选择节点', 'b'); choose('新关系类型', '关联'); }, expected: { op: 'collapse_node', node_id: 'b', edge_type: '关联' } },
    { label: '节点属性转成边属性', fill: () => { choose('选择节点', 'a'); selectMany('选择边', ['e1']); choose('选择属性', 'phone'); choose('目标边属性名', '电话'); }, expected: { op: 'node_property_to_edge', node_id: 'a', property: 'phone', edge_ids: ['e1'], target_property: '电话', remove_from_node: true } },
    { label: '节点属性提取为独立节点', fill: () => { selectMany('选择节点', ['a', 'b']); choose('选择属性', 'phone'); choose('新节点类型', '电话号码'); choose('新关系类型', '拥有'); }, expected: { op: 'extract_property_node', node_ids: ['a', 'b'], property: 'phone', node_type: '电话号码', edge_type: '拥有', remove_from_node: true } },
    { label: '删除冗余节点、边、属性', fill: () => { choose('删除内容', 'property'); selectMany('选择节点', ['a']); choose('选择属性', 'age'); }, expected: { op: 'delete', target: 'node', ids: ['a'], property: 'age' } },
    { label: '调整边方向', fill: () => { selectMany('选择边', ['e1', 'e2']); }, expected: { op: 'reverse_edges', edge_ids: ['e1', 'e2'] } },
    { label: '重命名节点、边类型', fill: () => { choose('对象类别', 'edge'); choose('原类型', 'Knows'); choose('新类型名称', '认识'); }, expected: { op: 'rename_type', target: 'edge', old_type: 'Knows', new_type: '认识' } },
  ];
  it.each(cases)('previews $label with explicit selections and only persists on save', async ({ label, fill, expected }) => {
    const saved = vi.fn(); await openPanel(saved);
    fireEvent.click(screen.getByRole('button', { name: new RegExp(label) }));
    fill();
    fireEvent.click(screen.getByRole('button', { name: '应用并预览' }));
    await screen.findByText('1 项修改待保存');
    expect(mocks.preview).toHaveBeenCalledWith(dataset, 2, [expected]);
    expect(mocks.commit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '保存到数据集' }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(dataset, graph, 3));
    expect(mocks.commit).toHaveBeenCalledWith(dataset, 2, [expected]);
    expect(screen.getByText('已保存 · 版本 3')).toBeTruthy();
    expect(screen.queryByText('1 项修改待保存')).toBeNull();
  });

  it('blocks empty selections before any request and supports canvas-based node selection', async () => {
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: '应用并预览' }));
    expect(screen.getByRole('alert').textContent).toContain('请选择一个中间节点');
    expect(mocks.preview).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '图节点 a' }));
    expect((screen.getByLabelText('选择节点') as HTMLSelectElement).value).toBe('a');
    expect(screen.getByText('节点 a 的属性')).toBeTruthy();
    expect(screen.getByText('29')).toBeTruthy();
  });

  it('shows selected edge properties alongside its direction before editing', async () => {
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: /调整边方向/ }));
    selectMany('选择边', ['e1']);
    expect(screen.getByText('边 e1 · a → b')).toBeTruthy();
    expect(screen.getByText('weight')).toBeTruthy();
    expect(screen.getByText('1')).toBeTruthy();
  });

  it('keeps server validation failures out of the preview queue', async () => {
    mocks.preview.mockRejectedValue(new ApiError(422, '节点需要恰好两条关联边'));
    await openPanel(); choose('选择节点', 'a'); choose('新关系类型', '关联');
    fireEvent.click(screen.getByRole('button', { name: '应用并预览' }));
    await screen.findByText('节点需要恰好两条关联边');
    expect(screen.queryByText('1 项修改待保存')).toBeNull();
    expect((screen.getByRole('button', { name: '保存到数据集' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('protects unsaved previews on close and supports undo without writing', async () => {
    const close = vi.fn(); await openPanel(vi.fn(), close);
    choose('选择节点', 'b'); choose('新关系类型', '关联');
    fireEvent.click(screen.getByRole('button', { name: '应用并预览' }));
    await screen.findByText('1 项修改待保存');
    fireEvent.click(screen.getByRole('button', { name: '关闭构图工作台' }));
    expect(close).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
    fireEvent.click(screen.getByRole('button', { name: '撤销上一步' }));
    expect(screen.queryByText('1 项修改待保存')).toBeNull();
    expect(mocks.commit).not.toHaveBeenCalled();
  });

  it('does not overwrite another revision and requires reloading after a conflict', async () => {
    mocks.commit.mockRejectedValue(new ApiError(409, '冲突', null, null, null, 'revision_conflict'));
    await openPanel(); choose('选择节点', 'b'); choose('新关系类型', '关联');
    fireEvent.click(screen.getByRole('button', { name: '应用并预览' }));
    await screen.findByText('1 项修改待保存');
    fireEvent.click(screen.getByRole('button', { name: '保存到数据集' }));
    await screen.findByText(/数据集已在其他位置更新/);
    expect((screen.getByRole('button', { name: '保存到数据集' }) as HTMLButtonElement).disabled).toBe(true);
    mocks.state.mockResolvedValue({ dataset_id: 'd1', status: 'ready', revision: 7 });
    mocks.graph.mockResolvedValue({ dataset_id: 'd1', revision: 7, graph });
    fireEvent.click(screen.getByRole('button', { name: '重新加载最新版本' }));
    await screen.findByText('已保存 · 版本 7');
    expect(screen.queryByText('1 项修改待保存')).toBeNull();
    expect(mocks.commit).toHaveBeenCalledTimes(1);
  });
});

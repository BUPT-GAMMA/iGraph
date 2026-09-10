import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

  it.each(['phone', 'data', 'table', 'a.b'])('edits the literal business column %s in an automatic row through preview and commit', async column => {
    const nested = { ...graph, nodes: [{ ...graph.nodes[0], properties: {
      table: 'customers', row_index: 0, relational_schema: {},
      data: { phone: '123', data: { note: 'nested' }, table: 'business value', 'a.b': null },
    } }, ...graph.nodes.slice(1)] };
    mocks.graph.mockResolvedValue({ dataset_id: 'd1', revision: 2, graph: nested });
    const saved = vi.fn();
    await openPanel(saved);
    fireEvent.click(screen.getByRole('button', { name: /节点属性提取为独立节点/ }));
    selectMany('选择节点', ['a']);
    const options = Array.from((screen.getByLabelText('选择属性') as HTMLSelectElement).options, option => option.value);
    expect(options).toContain(column);
    expect(options).not.toContain('row_index');
    expect(options).not.toContain('relational_schema');
    choose('选择属性', column); choose('新节点类型', 'BusinessValue'); choose('新关系类型', 'has_value');
    fireEvent.click(screen.getByRole('button', { name: '应用并预览' }));
    await screen.findByText('1 项修改待保存');
    const operation = { op: 'extract_property_node', node_ids: ['a'], property: column, node_type: 'BusinessValue', edge_type: 'has_value', remove_from_node: true };
    expect(mocks.preview).toHaveBeenCalledWith(dataset, 2, [operation]);
    fireEvent.click(screen.getByRole('button', { name: '保存到数据集' }));
    await waitFor(() => expect(mocks.commit).toHaveBeenCalledWith(dataset, 2, [operation]));
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

describe('long-running graph construction', () => {
  const sourceDataset: Dataset = { ...dataset, status: 'source_ready' };
  const notBuilt = { dataset_id: 'd1', status: 'not_built', revision: 0, algorithm_available: true };
  const building = { ...notBuilt, status: 'building' };
  const ready = { ...notBuilt, status: 'ready', revision: 3 };

  beforeEach(() => {
    vi.useFakeTimers();
    mocks.state.mockResolvedValue(notBuilt);
    mocks.graph.mockResolvedValue({ dataset_id: 'd1', revision: 3, graph });
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); });

  async function show(onSaved = vi.fn(), onClose = vi.fn()) {
    let view!: ReturnType<typeof render>;
    await act(async () => { view = render(<GraphConstructionPanel dataset={sourceDataset} onClose={onClose} onSaved={onSaved} />); });
    return view;
  }
  async function tick(milliseconds = 3000) {
    await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); });
  }

  it('shows a capacity rejection without polling or automatically retrying', async () => {
    mocks.build.mockRejectedValue(new ApiError(429, '构图资源繁忙，本次未启动，请稍后重试。', null, null, null, 'construction_capacity_exhausted'));
    await show();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(screen.getByRole('alert').textContent).toContain('本次未启动');
    expect(screen.getByRole('button', { name: '开始构图' }).matches(':disabled')).toBe(false);
    await tick(12000);
    expect(mocks.state).toHaveBeenCalledTimes(1);
    expect(mocks.build).toHaveBeenCalledTimes(1);
  });

  it.each(['previous', undefined])('does not let old failure %s discard the success of a pending retry', async oldId => {
    const failed = { ...notBuilt, status: 'failed', build_id: oldId, message: '上次失败' };
    mocks.state.mockResolvedValue(failed);
    let finish!: (value: unknown) => void;
    mocks.build.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const saved = vi.fn();
    await show(saved);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重新构图' })); });
    await tick();
    expect(saved).not.toHaveBeenCalled();
    await act(async () => { finish({ ...ready, build_id: 'current' }); });
    expect(saved).toHaveBeenCalledTimes(1);
    expect(screen.getByText('已保存 · 版本 3')).toBeTruthy();
  });

  it('associates a duplicate response and polling with the existing build id', async () => {
    mocks.build.mockResolvedValue({ ...building, build_id: 'current' });
    mocks.state.mockResolvedValueOnce(notBuilt)
      .mockResolvedValueOnce({ ...notBuilt, status: 'failed', build_id: 'previous' })
      .mockResolvedValue({ ...ready, build_id: 'current' });
    const saved = vi.fn();
    await show(saved);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    await tick();
    expect(saved).not.toHaveBeenCalled();
    expect(screen.getByText('构图进行中')).toBeTruthy();
    await tick();
    expect(saved).toHaveBeenCalledTimes(1);
    expect(mocks.build).toHaveBeenCalledTimes(1);
  });

  it.each(['previous', undefined])('keeps a retry alive after a lost POST response and stale failure %s', async oldId => {
    const failed = { ...notBuilt, status: 'failed', build_id: oldId, message: '上次失败' };
    mocks.state.mockResolvedValueOnce(failed).mockResolvedValueOnce(failed)
      .mockResolvedValue({ ...ready, build_id: 'current' });
    mocks.build.mockRejectedValue(new ApiError(504, '构图仍可能运行', null, null, null, 'construction_running_unconfirmed'));
    const saved = vi.fn();
    await show(saved);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重新构图' })); });
    expect(saved).not.toHaveBeenCalled();
    await tick();
    expect(saved).toHaveBeenCalledTimes(1);
    expect(screen.getByText('已保存 · 版本 3')).toBeTruthy();
    expect(mocks.build).toHaveBeenCalledTimes(1);
  });

  it('does not let an older building poll reopen a task explicitly failed by POST', async () => {
    let finishPost!: (value: unknown) => void, finishPoll!: (value: unknown) => void;
    mocks.state.mockResolvedValueOnce(notBuilt).mockReturnValueOnce(new Promise(resolve => { finishPoll = resolve; }));
    mocks.build.mockReturnValue(new Promise(resolve => { finishPost = resolve; }));
    await show();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    await tick();
    await act(async () => { finishPost({ ...notBuilt, status: 'failed', build_id: 'current', message: '本次验证失败' }); });
    await act(async () => { finishPoll({ ...building, build_id: 'current' }); });
    expect(screen.getByRole('alert').textContent).toContain('本次验证失败');
    expect(screen.getByRole('button', { name: '重新构图' }).matches(':disabled')).toBe(false);
    await tick(6000);
    expect(mocks.state).toHaveBeenCalledTimes(2);
  });

  it('accepts a new failure identity after an uncertain retry without polling forever', async () => {
    mocks.state.mockResolvedValueOnce({ ...notBuilt, status: 'failed', build_id: 'previous' })
      .mockResolvedValue({ ...notBuilt, status: 'failed', build_id: 'current', message: '本次验证失败' });
    mocks.build.mockRejectedValue(new TypeError('Failed to fetch'));
    await show();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重新构图' })); });
    expect(screen.getByRole('alert').textContent).toContain('本次验证失败');
    expect(screen.getByRole('button', { name: '重新构图' }).matches(':disabled')).toBe(false);
    await tick(9000);
    expect(mocks.state).toHaveBeenCalledTimes(2);
    expect(mocks.build).toHaveBeenCalledTimes(1);
  });

  it('restores retry controls when a new request is explicitly rejected as invalid', async () => {
    mocks.state.mockResolvedValue({ ...notBuilt, status: 'failed', build_id: 'previous' });
    mocks.build.mockRejectedValue(new ApiError(422, '参数无效', null, null, null, 'invalid_construction_options'));
    await show();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重新构图' })); });
    expect(screen.getByRole('alert').textContent).toContain('参数无效');
    expect(screen.getByRole('button', { name: '重新构图' }).matches(':disabled')).toBe(false);
    await tick(9000);
    expect(mocks.state).toHaveBeenCalledTimes(2);
    expect(mocks.build).toHaveBeenCalledTimes(1);
  });

  it('accepts a legacy failure after observing new building progress', async () => {
    const failed = { ...notBuilt, status: 'failed', message: '构图失败' };
    mocks.state.mockResolvedValueOnce(failed).mockResolvedValueOnce(failed)
      .mockResolvedValueOnce(building).mockResolvedValue(failed);
    mocks.build.mockRejectedValue(new TypeError('Failed to fetch'));
    await show();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重新构图' })); });
    await tick();
    expect(screen.getByText('构图进行中')).toBeTruthy();
    await tick();
    expect(screen.getByRole('button', { name: '重新构图' }).matches(':disabled')).toBe(false);
    await tick(9000);
    expect(mocks.state).toHaveBeenCalledTimes(4);
  });

  it.each([15, 23])('sends the configured query count (%s) and domain with exactly one build request', async count => {
    mocks.build.mockResolvedValue(ready);
    const saved = vi.fn();
    await show(saved);
    expect((screen.getByLabelText('验证查询数量') as HTMLInputElement).value).toBe('15');
    const category = screen.getByLabelText('领域类别') as HTMLSelectElement;
    expect(category.value).toBe('auto');
    expect(Array.from(category.options, option => [option.value, option.label])).toEqual([
      ['auto', '自动识别'], ['finance', '金融'], ['academic', '学术'], ['commerce', '电商'],
      ['healthcare', '医疗'], ['social', '社区'], ['general', '通用'],
    ]);
    expect((screen.getByLabelText('复用领域经验') as HTMLInputElement).checked).toBe(true);
    if (count !== 15) fireEvent.change(screen.getByLabelText('验证查询数量'), { target: { value: String(count) } });
    fireEvent.change(screen.getByLabelText('领域描述（选填）'), { target: { value: '  电商订单履约  ' } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(mocks.build).toHaveBeenCalledTimes(1);
    expect(mocks.build).toHaveBeenCalledWith(sourceDataset, {
      query_count: count, domain: '电商订单履约', domain_category: 'auto', experience_enabled: true,
    });
    expect(saved).toHaveBeenCalledTimes(1);
    expect(saved).toHaveBeenCalledWith(sourceDataset, graph, 3);
    await tick(9000);
    expect(saved).toHaveBeenCalledTimes(1);
    expect(mocks.build).toHaveBeenCalledTimes(1);
  });

  it('submits the selected domain and explicit experience opt-out without losing the business description', async () => {
    mocks.build.mockResolvedValue(ready);
    await show();
    fireEvent.change(screen.getByLabelText('领域类别'), { target: { value: 'finance' } });
    fireEvent.change(screen.getByLabelText('领域描述（选填）'), { target: { value: '  贷款与担保关系  ' } });
    fireEvent.click(screen.getByLabelText('复用领域经验'));
    expect((screen.getByLabelText('复用领域经验') as HTMLInputElement).checked).toBe(false);
    expect(screen.getByText(/关闭后，本次构图不读取或写入经验/).textContent).toContain('新图仍需验收');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(mocks.build).toHaveBeenCalledWith(sourceDataset, {
      query_count: 15, domain: '贷款与担保关系', domain_category: 'finance', experience_enabled: false,
    });
    expect(mocks.build).toHaveBeenCalledTimes(1);
  });

  it('resets domain and experience controls when switching to another dataset', async () => {
    const view = await show();
    fireEvent.change(screen.getByLabelText('领域类别'), { target: { value: 'healthcare' } });
    fireEvent.change(screen.getByLabelText('领域描述（选填）'), { target: { value: '门诊业务' } });
    fireEvent.change(screen.getByLabelText('验证查询数量'), { target: { value: '23' } });
    fireEvent.click(screen.getByLabelText('复用领域经验'));
    const nextDataset = { ...sourceDataset, dataset_id: 'd2', name: '另一份数据' };
    mocks.state.mockResolvedValue({ ...notBuilt, dataset_id: 'd2' });
    mocks.build.mockResolvedValue({ ...ready, dataset_id: 'd2' });
    mocks.graph.mockResolvedValue({ dataset_id: 'd2', revision: 3, graph });
    await act(async () => { view.rerender(<GraphConstructionPanel dataset={nextDataset} onClose={vi.fn()} onSaved={vi.fn()} />); });
    expect((screen.getByLabelText('领域类别') as HTMLSelectElement).value).toBe('auto');
    expect((screen.getByLabelText('复用领域经验') as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText('领域描述（选填）') as HTMLTextAreaElement).value).toBe('');
    expect((screen.getByLabelText('验证查询数量') as HTMLInputElement).value).toBe('15');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(mocks.build).toHaveBeenCalledWith(nextDataset, {
      query_count: 15, domain: '', domain_category: 'auto', experience_enabled: true,
    });
    expect(mocks.build).toHaveBeenCalledTimes(1);
  });

  it('rejects invalid query counts without sending a build request', async () => {
    await show();
    for (const value of ['0', '101', '1.5', '']) {
      fireEvent.change(screen.getByLabelText('验证查询数量'), { target: { value } });
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
      expect(screen.getByRole('alert').textContent).toContain('1 到 100');
    }
    expect(mocks.build).not.toHaveBeenCalled();
  });

  it('allows closing during a pending build and never cancels or repeats its POST', async () => {
    let finishBuild!: (value: unknown) => void;
    mocks.build.mockReturnValue(new Promise(resolve => { finishBuild = resolve; }));
    const close = vi.fn(), saved = vi.fn();
    const view = await show(saved, close);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect((screen.getByRole('button', { name: '正在构图…' }) as HTMLButtonElement).disabled).toBe(true);
    for (const label of ['验证查询数量', '领域类别', '领域描述（选填）', '复用领域经验']) {
      expect(screen.getByLabelText(label).matches(':disabled')).toBe(true);
    }
    expect((screen.getByRole('button', { name: '关闭构图工作台' }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: '关闭构图工作台' }));
    expect(close).toHaveBeenCalledTimes(1);
    view.unmount();
    await tick(6000);
    await act(async () => { finishBuild(ready); });
    expect(mocks.build).toHaveBeenCalledTimes(1);
    expect(mocks.build).toHaveBeenCalledWith(sourceDataset, {
      query_count: 15, domain: '', domain_category: 'auto', experience_enabled: true,
    });
    expect(mocks.state).toHaveBeenCalledTimes(1);
    expect(mocks.graph).not.toHaveBeenCalled();
    expect(saved).not.toHaveBeenCalled();
  });

  it('reopens an existing build, polls GET every three seconds, and loads the saved graph once', async () => {
    mocks.state.mockResolvedValueOnce(building).mockResolvedValue(ready);
    const saved = vi.fn();
    await show(saved);
    expect(screen.getByText('构图进行中')).toBeTruthy();
    for (const label of ['领域类别', '复用领域经验']) {
      expect(screen.getByLabelText(label).matches(':disabled')).toBe(true);
    }
    expect(mocks.state).toHaveBeenCalledTimes(1);
    await tick(2999);
    expect(mocks.state).toHaveBeenCalledTimes(1);
    await tick(1);
    expect(screen.getByText('已保存 · 版本 3')).toBeTruthy();
    expect(mocks.state).toHaveBeenCalledTimes(2);
    expect(mocks.graph).toHaveBeenCalledTimes(1);
    expect(saved).toHaveBeenCalledTimes(1);
    expect(saved).toHaveBeenCalledWith(sourceDataset, graph, 3);
    await tick(9000);
    expect(mocks.state).toHaveBeenCalledTimes(2);
    expect(saved).toHaveBeenCalledTimes(1);
    expect(mocks.build).not.toHaveBeenCalled();
  });

  it.each([
    new ApiError(504, '构图仍可能运行', null, null, null, 'construction_running_unconfirmed'),
    new TypeError('Failed to fetch'),
  ])('recovers interrupted POST %# by reading the existing task without resubmitting', async cause => {
    mocks.state.mockResolvedValueOnce(notBuilt).mockResolvedValueOnce(building).mockResolvedValue(ready);
    mocks.build.mockRejectedValue(cause);
    const saved = vi.fn();
    await show(saved);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(screen.getByText('构图进行中')).toBeTruthy();
    expect(mocks.state).toHaveBeenCalledTimes(2);
    expect(mocks.build).toHaveBeenCalledTimes(1);
    await tick();
    expect(screen.getByText('已保存 · 版本 3')).toBeTruthy();
    expect(saved).toHaveBeenCalledTimes(1);
    expect(saved).toHaveBeenCalledWith(sourceDataset, graph, 3);
    await tick(9000);
    expect(mocks.build).toHaveBeenCalledTimes(1);
    expect(saved).toHaveBeenCalledTimes(1);
  });

  it('does not notify twice when polling completes before the original POST response', async () => {
    let finishBuild!: (value: unknown) => void;
    mocks.build.mockReturnValue(new Promise(resolve => { finishBuild = resolve; }));
    mocks.state.mockResolvedValueOnce(notBuilt).mockResolvedValue(ready);
    const saved = vi.fn();
    await show(saved);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    await tick();
    expect(saved).toHaveBeenCalledTimes(1);
    await act(async () => { finishBuild(ready); });
    await tick(6000);
    expect(saved).toHaveBeenCalledTimes(1);
    expect(mocks.graph).toHaveBeenCalledTimes(1);
    expect(mocks.build).toHaveBeenCalledTimes(1);
  });
});

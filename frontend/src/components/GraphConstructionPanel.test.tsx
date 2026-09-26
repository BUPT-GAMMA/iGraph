import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api';
import type { ConstructionOperation, Dataset } from '../types';
import { GraphConstructionPanel } from './GraphConstructionPanel';

const mocks = vi.hoisted(() => ({ state: vi.fn(), graph: vi.fn(), schema: vi.fn(), source: vi.fn(), build: vi.fn(), preview: vi.fn(), commit: vi.fn() }));
vi.mock('../api', async importOriginal => ({
  ...await importOriginal<typeof import('../api')>(),
  getConstructionState: mocks.state, getConstructionGraph: mocks.graph, getConstructionSchema: mocks.schema, buildDatasetGraph: mocks.build,
  getConstructionSourcePreview: mocks.source,
  previewConstruction: mocks.preview, commitConstruction: mocks.commit,
}));
vi.mock('./GraphWorkspace', () => ({ GraphWorkspace: ({ data, onSelect }: any) => <div aria-label="测试图画布">{data.nodes.map((node: any) => <button key={node.id} onClick={() => onSelect(node)}>图节点 {node.id}</button>)}<span>{data.edges.map((edge: any) => `${edge.source} → ${edge.target}`).join(', ')}</span></div> }));

const dataset: Dataset = { dataset_id: 'd1', project_id: null, name: '客户关系', original_filename: 'customers.csv', file_type: 'csv', status: 'ready' };
const graph = { directed: true, nodes: [{ id: 'a', type: 'Person', properties: { phone: '123', age: 29 } }, { id: 'b', type: 'Link', properties: { phone: '123' } }, { id: 'c', type: 'Person', properties: {} }], edges: [{ id: 'e1', source: 'a', target: 'b', type: 'Knows', properties: { weight: 1 } }, { id: 'e2', source: 'b', target: 'c', type: 'Knows', properties: {} }] };
const schemaGraph = { directed: true, visualization_kind: 'schema', nodes: [
  { id: 'Link', type: 'schema_node', label: 'Link', properties: { schema_kind: 'node_type', type_name: 'Link', instance_count: 1, property_paths: [{ path: 'phone', types: ['string'], present_count: 1, value_count: 1 }] } },
  { id: 'Person', type: 'schema_node', label: 'Person', properties: { schema_kind: 'node_type', type_name: 'Person', instance_count: 2, property_paths: [{ path: 'age', types: ['integer'], present_count: 1, value_count: 1 }, { path: 'phone', types: ['string'], present_count: 1, value_count: 1 }] } },
], edges: [{ id: 'se1', source: 'Person', target: 'Link', type: 'Knows', label: 'Knows', properties: { schema_kind: 'edge_type', type_name: 'Knows', source_type: 'Person', target_type: 'Link', relationship_count: 1, property_paths: [{ path: 'weight', types: ['integer'], present_count: 1, value_count: 1 }] } }] };
const schema = { dataset_id: 'd1', revision: 2, schema: { directed: true, node_count: 3, edge_count: 2, node_type_count: 2, edge_type_count: 1, node_types: schemaGraph.nodes.map(node => node.properties), edge_types: schemaGraph.edges.map(edge => edge.properties), graph: schemaGraph } };
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
  mocks.schema.mockResolvedValue(schema);
  mocks.source.mockResolvedValue({ dataset_id: 'd1', tables: [], warnings: [] });
  mocks.preview.mockResolvedValue(result);
  mocks.commit.mockResolvedValue({ ...result, status: 'ready' });
});
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe('graph construction workflow', () => {
  it('shows source samples before a graph exists and keeps preview failure independent from building', async () => {
    mocks.state.mockResolvedValue({ dataset_id: 'd1', status: 'not_built', revision: 0, algorithm_available: true });
    mocks.source.mockResolvedValue({ dataset_id: 'd1', tables: [{ name: 'customers', columns: ['name'], column_types: {}, primary_key: [], foreign_keys: [], sample_rows: [{ name: '源表客户' }], row_count: 1, column_descriptions: {}, sample_truncated: false }], warnings: [] });
    render(<GraphConstructionPanel dataset={{ ...dataset, status: 'source_ready' }} onClose={vi.fn()} onSaved={vi.fn()} />);
    expect(await screen.findByText('源表客户')).toBeTruthy();
    expect((screen.getByRole('button', { name: '开始构图' }) as HTMLButtonElement).disabled).toBe(false);
    expect(mocks.graph).not.toHaveBeenCalled();
    expect(mocks.build).not.toHaveBeenCalled();
    mocks.source.mockRejectedValue(new Error('preview temporarily unavailable'));
    choose('文本分隔符（选填）', ';');
    expect(await screen.findByRole('button', { name: '重试预览' })).toBeTruthy();
    expect(mocks.source).toHaveBeenLastCalledWith(expect.objectContaining({ dataset_id: 'd1' }), expect.any(AbortSignal), ';');
    expect((screen.getByRole('button', { name: '开始构图' }) as HTMLButtonElement).disabled).toBe(false);
  });
  it('edits nested business columns while preserving raw schema property paths', async () => {
    const nested = { directed: true, nodes: [{ id: 'row1', type: 'Customer', properties: {
      table: 'customers', row_index: 0, data: { phone: '123', 'a.b': null, table: 'business' },
    } }], edges: [] };
    mocks.graph.mockResolvedValue({ dataset_id: 'd1', revision: 2, graph: nested });
    mocks.schema.mockRejectedValue(new ApiError(503, 'offline'));
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: /节点属性提取为独立节点/ }));
    const nodes = screen.getByLabelText('选择节点') as HTMLSelectElement;
    Array.from(nodes.options).forEach(option => { option.selected = option.value === 'row1'; });
    fireEvent.change(nodes);
    const names = Array.from((screen.getByLabelText('选择属性') as HTMLSelectElement).options, item => item.value);
    expect(names).toEqual(expect.arrayContaining(['phone', 'a.b', 'table']));
    expect(names).not.toContain('row_index');
    expect(names).not.toContain('data');
    fireEvent.click(screen.getByRole('button', { name: '图节点 Customer' }));
    expect(screen.getByText('data.phone')).toBeTruthy();
  });

  it('shows constructed row nodes by type and row number while preserving selection ids', async () => {
    const rowGraph = { directed: true, nodes: [
      { id: 'row_first', type: 'aisles', properties: { table: 'aisles', row_index: 0, data: { aisle_id: '1' } } },
      { id: 'row_second', type: 'products', properties: { table: 'products', row_index: 1, data: { product_id: '2' } } },
    ], edges: [{ id: 'e1', source: 'row_second', target: 'row_first', type: 'belongs_to', properties: {} }] };
    mocks.graph.mockResolvedValue({ dataset_id: 'd1', revision: 2, graph: rowGraph });
    mocks.schema.mockRejectedValue(new ApiError(503, 'offline'));
    await openPanel();
    const nodeOptions = Array.from((screen.getByLabelText('选择节点') as HTMLSelectElement).options, option => option.textContent);
    expect(nodeOptions).toContain('aisles · 第 1 行');
    expect(nodeOptions).toContain('products · 第 2 行');
    expect(nodeOptions.join(' ')).not.toContain('row_');
    choose('查找对象', 'products · 第 2 行');
    expect(Array.from((screen.getByLabelText('选择节点') as HTMLSelectElement).options, option => option.value)).toEqual(['', 'row_second']);
    choose('查找对象', '');
    choose('选择节点', 'row_first');
    expect(screen.getByText('实例节点 aisles · 第 1 行 的属性')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /调整边方向/ }));
    const edgeOptions = Array.from((screen.getByLabelText('选择边') as HTMLSelectElement).options, option => option.textContent);
    expect(edgeOptions.join(' ')).toContain('products · 第 2 行 → aisles · 第 1 行');
    expect(edgeOptions.join(' ')).not.toContain('row_');
    choose('查找对象', 'products · 第 2 行');
    expect(Array.from((screen.getByLabelText('选择边') as HTMLSelectElement).options, option => option.value)).toEqual(['e1']);

  });

  it('renders the saved graph as a Graph Schema with type-level counts', async () => {
    await openPanel();
    expect(screen.getByText('Graph Schema')).toBeTruthy();
    expect(screen.getByText('节点代表节点类型，连线代表关系类型')).toBeTruthy();
    expect(screen.getByText('2').parentElement?.textContent).toContain('节点类型');
    expect(screen.getByText('1').parentElement?.textContent).toContain('关系类型');
    expect(screen.getByRole('button', { name: '图节点 Person' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '图节点 a' })).toBeNull();
    expect(mocks.graph).toHaveBeenCalledWith(dataset, expect.any(AbortSignal));
    expect(mocks.schema).toHaveBeenCalledWith(dataset, expect.any(AbortSignal));
  });

  it('falls back to a local schema without blocking graph optimization when the schema endpoint fails', async () => {
    mocks.schema.mockRejectedValue(new ApiError(503, 'schema unavailable'));
    await openPanel();
    expect(screen.getByText(/Graph Schema 接口暂时不可用，已从当前实例图生成本地结构概览/)).toBeTruthy();
    expect(screen.getByRole('button', { name: '图节点 Person' })).toBeTruthy();
    expect((screen.getByRole('button', { name: '应用并预览' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('renders as an in-page workbench with an explicit return action', () => {
    mocks.state.mockReturnValue(new Promise(() => undefined));
    const close = vi.fn();
    render(<GraphConstructionPanel dataset={dataset} embedded onClose={close} onSaved={vi.fn()} />);
    expect(document.querySelector('.construction-page')?.hasAttribute('data-popup-kind')).toBe(false);
    expect(document.querySelector('.construction-panel')?.hasAttribute('data-popup-kind')).toBe(false);
    expect(screen.getByRole('region', { name: '客户关系' })).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '返回构图' }));
    expect(close).toHaveBeenCalledOnce();
  });

  it('returns after saving, but protects unsaved edits before returning', async () => {
    const close = vi.fn();
    render(<GraphConstructionPanel dataset={dataset} embedded onClose={close} onSaved={vi.fn()} />);
    await screen.findByLabelText('测试图画布');
    choose('选择节点', 'b'); choose('新关系类型', '关联');
    fireEvent.click(screen.getByRole('button', { name: '应用并预览' }));
    await screen.findByText('1 项修改待保存');
    fireEvent.click(screen.getByRole('button', { name: '返回构图' }));
    expect(close).not.toHaveBeenCalled();
    expect(screen.getByRole('alert').textContent).toContain('返回后将丢弃这些修改');
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
    fireEvent.click(screen.getByRole('button', { name: '保存到数据集' }));
    await screen.findByText(/已保存版本 3/);
    fireEvent.click(screen.getByRole('button', { name: '返回构图' }));
    expect(close).toHaveBeenCalledOnce();
  });

  it.each(['csv', 'sql'])('defaults %s to source tables and shows only the clicked table fields, not record values', async fileType => {
    mocks.schema.mockResolvedValue({ ...schema, schema: { ...schema.schema, source_tables: [
      { name: 'customers', columns: ['id', 'name'], column_types: { id: 'INTEGER', name: 'TEXT' }, primary_key: ['id'], foreign_keys: [] },
      { name: 'orders', columns: ['order_id', 'customer_id'], column_types: { order_id: 'INTEGER', customer_id: 'INTEGER' }, primary_key: ['order_id'], foreign_keys: [{ columns: ['customer_id'], ref_table: 'customers', ref_columns: ['id'] }] },
    ] } });
    mocks.graph.mockResolvedValue({ dataset_id: 'd1', revision: 2, graph: { ...graph, nodes: [...graph.nodes,
      { id: 'secret', type: 'customers', properties: { data: { id: 1, name: 'DO_NOT_RENDER_RECORD' } } }] } });
    render(<GraphConstructionPanel dataset={{ ...dataset, file_type: fileType, original_filename: `source.${fileType}` }} embedded onClose={vi.fn()} onSaved={vi.fn()} />);
    await screen.findByLabelText('测试图画布');
    expect(screen.getByRole('button', { name: '表关联模式' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('button', { name: '图节点 customers' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '图节点 Person' })).toBeNull();
    expect(screen.getByText('点击表节点查看字段与主外键')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '图节点 orders' }));
    const inspector = screen.getByLabelText('表节点属性');
    expect(inspector.textContent).toContain('order_id');
    expect(inspector.textContent).toContain('INTEGER');
    expect(inspector.textContent).toContain('PK');
    expect(inspector.textContent).toContain('FK');
    expect(inspector.textContent).toContain('→ customers.id');
    expect(screen.queryByText('DO_NOT_RENDER_RECORD')).toBeNull();
    expect(screen.queryByText('name', { exact: true })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '优化后结构' }));
    expect(screen.getByRole('button', { name: '图节点 Person' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '表关联模式' }));
    expect(screen.getByRole('button', { name: '图节点 customers' })).toBeTruthy();
  });

  it('allows closing while the initial state request is pending', () => {
    mocks.state.mockReturnValue(new Promise(() => undefined));
    const close = vi.fn();
    render(<GraphConstructionPanel dataset={dataset} onClose={close} onSaved={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '关闭构图工作台' }));
    expect(close).toHaveBeenCalledOnce();
  });
  it('does not close a replacement panel when an old use request finishes after unmount', async () => {
    let resolveUse!: () => void;
    const pendingUse = new Promise<void>(resolve => { resolveUse = resolve; });
    const close = vi.fn();
    const use = vi.fn(() => pendingUse);
    const view = render(<GraphConstructionPanel dataset={dataset} onClose={close} onSaved={vi.fn()} onUse={use} />);
    await screen.findByLabelText('测试图画布');
    fireEvent.click(screen.getByRole('button', { name: '在当前会话使用' }));
    expect(use).toHaveBeenCalledOnce();
    view.unmount();
    await act(async () => {
      resolveUse();
      await pendingUse;
    });
    expect(close).not.toHaveBeenCalled();
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

  it('keeps the canvas at schema level during a change preview and refreshes it after saving', async () => {
    mocks.preview.mockResolvedValue({
      ...result,
      graph: { ...graph, nodes: graph.nodes.map(node => node.id === 'b' ? { ...node, type: 'Bridge' } : node) },
    });
    await openPanel();
    choose('选择节点', 'b'); choose('新关系类型', '关联');
    fireEvent.click(screen.getByRole('button', { name: '应用并预览' }));
    await screen.findByText('Graph Schema · 修改预览');
    expect(screen.getByRole('button', { name: '图节点 Bridge' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '图节点 Person' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: '图节点 a' })).toBeNull();
    expect(screen.getByText('预览尚未保存；类型与关系统计已按预览实时更新')).toBeTruthy();
    mocks.schema.mockResolvedValue({ ...schema, revision: 3 });
    fireEvent.click(screen.getByRole('button', { name: '保存到数据集' }));
    await screen.findByText('Graph Schema');
    expect(screen.getByRole('button', { name: '图节点 Person' })).toBeTruthy();
    expect(mocks.schema).toHaveBeenCalledTimes(2);
  });

  it('derives the same recursive and undirected schema shape when the schema endpoint is unavailable', async () => {
    const localGraph = {
      directed: false,
      nodes: [
        { id: 'left', type: 'A', properties: { 'a.b': null, nested: { flag: true }, tags: [] } },
        { id: 'right', type: 'B', properties: {} },
      ],
      edges: [
        { id: 'forward', source: 'left', target: 'right', type: 'REL', properties: {} },
        { id: 'reverse', source: 'right', target: 'left', type: 'REL', properties: {} },
      ],
    };
    mocks.graph.mockResolvedValue({ dataset_id: 'd1', revision: 2, graph: localGraph });
    mocks.schema.mockRejectedValue(new ApiError(503, 'schema unavailable'));

    await openPanel();

    expect(screen.getByLabelText('测试图画布').textContent).toContain('A → B');
    expect(screen.getByLabelText('测试图画布').textContent?.match(/A → B/g)).toHaveLength(1);
    expect(screen.getByText('1').parentElement?.textContent).toContain('关系类型');
    fireEvent.click(screen.getByRole('button', { name: '图节点 A' }));
    expect(screen.getByText('["a.b"]')).toBeTruthy();
    expect(screen.getByText('nested')).toBeTruthy();
    expect(screen.getByText('nested.flag')).toBeTruthy();
    expect(screen.getByText('tags')).toBeTruthy();
  });

  it('keeps schema-node inspection separate from explicit instance selection', async () => {
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: '应用并预览' }));
    expect(screen.getByRole('alert').textContent).toContain('请选择一个中间节点');
    expect(mocks.preview).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '图节点 Person' }));
    expect(screen.getByText('节点类型 Person')).toBeTruthy();
    expect(screen.getByText('2 个实例 · 2 个属性路径')).toBeTruthy();
    expect((screen.getByLabelText('选择节点') as HTMLSelectElement).value).toBe('');
    choose('选择节点', 'a');
    expect((screen.getByLabelText('选择节点') as HTMLSelectElement).value).toBe('a');
    expect(screen.getByText('实例节点 a 的属性')).toBeTruthy();
    expect(screen.getByText('29')).toBeTruthy();
  });

  it('shows selected edge properties alongside its direction before editing', async () => {
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: /调整边方向/ }));
    selectMany('选择边', ['e1']);
    const inspection = screen.getByText('实例边 e1 · a → b').parentElement;
    expect(inspection?.textContent).toContain('weight');
    expect(inspection?.textContent).toContain('1');
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

  async function show(onSaved = vi.fn(), onClose = vi.fn(), onStateChange = vi.fn()) {
    let view!: ReturnType<typeof render>;
    await act(async () => { view = render(<GraphConstructionPanel dataset={sourceDataset} onClose={onClose} onSaved={onSaved} onStateChange={onStateChange} />); });
    return view;
  }
  async function tick(milliseconds = 3000) {
    await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); });
  }

  it('renders the audited Agent progress stream and updates it through status polling', async () => {
    const first = { ...building, build_id: 'build-1', progress: {
      version: 'construction_progress.v1', status: 'running', stage: 'evaluating_baseline',
      current_message: '正在用源数据答案校验初始图，并测量基线。', elapsed_seconds: 12,
      metrics: { query_count: 15, iterations: 0, evaluated_candidates: 0, accepted_candidates: 0 },
      events: [
        { id: 'stage:starting', kind: 'stage', stage: 'starting', message: '构图 Agent 已启动，正在读取数据源。' },
        { id: 'stage:evaluating_baseline', kind: 'stage', stage: 'evaluating_baseline', message: '正在校验初始图。' },
      ],
    } };
    const second = { ...building, build_id: 'build-1', progress: {
      ...first.progress, stage: 'evolving', current_message: 'Agent 正在尝试候选图结构，并逐项验收语义与性能。',
      elapsed_seconds: 18, metrics: { query_count: 15, iterations: 1, evaluated_candidates: 1, accepted_candidates: 0, tool_calls: 2 },
      events: [...first.progress.events,
        { id: 'stage:evolving', kind: 'stage', stage: 'evolving', message: 'Agent 正在尝试候选图结构。' },
        { id: 'candidate:1', kind: 'candidate', stage: 'evolving', message: '未采用候选：属性访问路径（未达到稳定性能收益门槛）。', outcome: 'rejected' },
      ],
    } };
    mocks.state.mockResolvedValueOnce(first).mockResolvedValue(second);
    const onStateChange = vi.fn();
    await show(vi.fn(), vi.fn(), onStateChange);
    expect(screen.getByRole('region', { name: '构图进度' })).toBeTruthy();
    expect(screen.getByText('正在用源数据答案校验初始图，并测量基线。')).toBeTruthy();
    expect(screen.getByText('已运行 12 秒')).toBeTruthy();
    await tick();
    expect(screen.getByText(/未采用候选：属性访问路径/)).toBeTruthy();
    expect(screen.getByText('已运行 18 秒')).toBeTruthy();
    expect(onStateChange).toHaveBeenLastCalledWith(sourceDataset, second);
    expect(mocks.build).not.toHaveBeenCalled();
  });

  it('shows semantic TXT extraction progress without requiring candidate metrics', async () => {
    mocks.state.mockResolvedValue({ ...building, build_id: 'build-text', source_type: 'txt', progress: {
      version: 'construction_progress.v1', status: 'running', stage: 'extracting_entities',
      current_message: '正在从文本中识别实体与关系。', elapsed_seconds: 7,
      metrics: { chunks_completed: 3, chunks_total: 7, extracted_nodes: 11, extracted_edges: 8 },
      events: [
        { id: 'stage:extracting_entities', kind: 'stage', stage: 'extracting_entities', message: '正在从文本中识别实体与关系。' },
      ],
    } });

    await show();

    const stream = screen.getByRole('region', { name: '构图进度' });
    expect(stream.textContent).toContain('文本分块 3 / 7');
    expect(stream.textContent).toContain('已识别实体 11');
    expect(stream.textContent).toContain('已识别关系 8');
    expect(stream.textContent).not.toContain('候选');
  });

  it('restores the active build query count from progress when the panel is reopened', async () => {
    mocks.state.mockResolvedValue({ ...building, build_id: 'build-1', progress: {
      version: 'construction_progress.v1', status: 'running', stage: 'evaluating_baseline',
      current_message: '正在校验初始图。', metrics: { query_count: 1 }, events: [],
    } });

    await show();

    const queryCount = screen.getByLabelText('验证查询数量') as HTMLInputElement;
    expect(queryCount.value).toBe('1');
    expect(queryCount.matches(':disabled')).toBe(true);
    expect(mocks.build).not.toHaveBeenCalled();
  });

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

  it('does not let an old failure discard the success of a pending retry', async () => {
    const failed = { ...notBuilt, status: 'failed', build_id: 'previous', message: '上次失败' };
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
    expect((screen.getByRole('switch', { name: '使用 Agent 优化构图' }) as HTMLInputElement).checked).toBe(false);
    if (count !== 15) fireEvent.change(screen.getByLabelText('验证查询数量'), { target: { value: String(count) } });
    fireEvent.change(screen.getByLabelText('领域描述（选填）'), { target: { value: '  电商订单履约  ' } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(mocks.build).toHaveBeenCalledTimes(1);
    expect(mocks.build).toHaveBeenCalledWith(sourceDataset, {
      mode: 'rules', query_count: count, domain: '电商订单履约', domain_category: 'auto', experience_enabled: true,
    });
    expect(saved).toHaveBeenCalledTimes(1);
    expect(saved).toHaveBeenCalledWith(sourceDataset, graph, 3);
    await tick(9000);
    expect(saved).toHaveBeenCalledTimes(1);
    expect(mocks.build).toHaveBeenCalledTimes(1);
  });

  it('uses Agent optimization only when the user enables the switch', async () => {
    mocks.build.mockResolvedValue(ready);
    await show();
    const agentSwitch = screen.getByRole('switch', { name: '使用 Agent 优化构图' }) as HTMLInputElement;
    expect(agentSwitch.checked).toBe(false);
    fireEvent.click(agentSwitch);
    expect(agentSwitch.checked).toBe(true);
    expect(screen.getByText('Agent 会优化并验证规则化方法构建的图。')).toBeTruthy();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(mocks.build).toHaveBeenCalledWith(sourceDataset, expect.objectContaining({ mode: 'llm' }));
  });

  it('offers TXT delimiter selection and forwards the exact character', async () => {
    const txtDataset: Dataset = {
      ...sourceDataset, original_filename: 'customers.txt', file_type: 'txt',
    };
    mocks.state.mockResolvedValue({ ...notBuilt, source_type: 'txt' });
    mocks.build.mockResolvedValue(ready);
    await act(async () => {
      render(<GraphConstructionPanel dataset={txtDataset} onClose={vi.fn()} onSaved={vi.fn()} />);
    });
    const select = screen.getByLabelText('TXT 处理方式') as HTMLSelectElement;
    expect(select.value).toBe('');
    expect(Array.from(select.options, option => [option.value, option.label])).toEqual([
      ['', '自动：制表符表格，否则逐行记录'], ['__entities__', '智能抽取实体与关系（LLM）'],
      ['__lines__', '强制逐行记录（不推断关系）'], ['\t', '制表符'], [',', '逗号'], ['|', '竖线'], [';', '分号'], [' ', '空格'],
    ]);
    fireEvent.change(select, { target: { value: '|' } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(mocks.build).toHaveBeenCalledWith(txtDataset, {
      mode: 'rules', query_count: 15, domain: '', domain_category: 'auto', experience_enabled: true, delimiter: '|',
    });
  });

  it('can force TXT line mode without sending a delimiter', async () => {
    const txtDataset: Dataset = {
      ...sourceDataset, original_filename: 'notes.txt', file_type: 'txt',
    };
    mocks.state.mockResolvedValue({ ...notBuilt, source_type: 'txt' });
    mocks.build.mockResolvedValue(ready);
    await act(async () => {
      render(<GraphConstructionPanel dataset={txtDataset} onClose={vi.fn()} onSaved={vi.fn()} />);
    });
    fireEvent.change(screen.getByLabelText('TXT 处理方式'), { target: { value: '__lines__' } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(mocks.build).toHaveBeenCalledWith(txtDataset, {
      mode: 'rules', query_count: 15, domain: '', domain_category: 'auto', experience_enabled: true, txt_mode: 'lines',
    });
  });

  it('explicitly discloses and enables LLM semantic extraction for TXT', async () => {
    const txtDataset: Dataset = {
      ...sourceDataset, original_filename: 'notes.txt', file_type: 'txt',
    };
    mocks.state.mockResolvedValue({ ...notBuilt, source_type: 'txt' });
    mocks.build.mockResolvedValue(ready);
    await act(async () => {
      render(<GraphConstructionPanel dataset={txtDataset} onClose={vi.fn()} onSaved={vi.fn()} />);
    });

    fireEvent.change(screen.getByLabelText('TXT 处理方式'), { target: { value: '__entities__' } });
    expect(screen.getByText('TXT 全文将分块发送至构图模型；LLM 会识别文本中明确出现的实体和关系，生成结果需人工核对。')).toBeTruthy();
    expect(screen.getByText('依据 LLM 从逐行文本中抽取实体与关系并构图；语义结果需要人工核对。')).toBeTruthy();
    expect(screen.queryByLabelText('验证查询数量')).toBeNull();
    expect(screen.queryByLabelText('领域类别')).toBeNull();
    expect(screen.queryByLabelText('复用领域经验')).toBeNull();
    expect(screen.queryByRole('switch', { name: '使用 Agent 优化构图' })).toBeNull();
    expect(screen.getByLabelText('抽取目标（选填）')).toBeTruthy();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });

    expect(mocks.build).toHaveBeenCalledWith(txtDataset, {
      domain: '', txt_mode: 'entities', mode: 'llm', entity_types: [], text_refine_enabled: false,
    });
  });

  it('sends selected TXT entity categories and enables query refinement only when requested', async () => {
    const txtDataset: Dataset = { ...sourceDataset, original_filename: 'story.txt', file_type: 'txt' };
    mocks.state.mockResolvedValue({ ...notBuilt, source_type: 'txt' });
    mocks.build.mockResolvedValue(ready);
    await act(async () => {
      render(<GraphConstructionPanel dataset={txtDataset} onClose={vi.fn()} onSaved={vi.fn()} />);
    });
    fireEvent.change(screen.getByLabelText('TXT 处理方式'), { target: { value: '__entities__' } });
    fireEvent.change(screen.getByLabelText('提取的实体类型（选填）'), { target: { value: '人物、事件、组织' } });
    fireEvent.click(screen.getByRole('switch', { name: '使用 Agent refine TXT 构图' }));
    fireEvent.change(screen.getByLabelText('验证查询数量'), { target: { value: '4' } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(mocks.build).toHaveBeenCalledWith(txtDataset, {
      domain: '', txt_mode: 'entities', mode: 'llm',
      entity_types: ['人物', '事件', '组织'], text_refine_enabled: true, query_count: 4,
    });
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
      mode: 'rules', query_count: 15, domain: '贷款与担保关系', domain_category: 'finance', experience_enabled: false,
    });
    expect(mocks.build).toHaveBeenCalledTimes(1);
  });

  it('uses the saved dataset description as construction context when domain is blank', async () => {
    const described = { ...sourceDataset, description: '客户与订单关系' };
    mocks.state.mockResolvedValue(notBuilt);
    mocks.build.mockResolvedValue(ready);
    await act(async () => { render(<GraphConstructionPanel dataset={described} onClose={vi.fn()} onSaved={vi.fn()} />); });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(mocks.build).toHaveBeenCalledWith(described, expect.objectContaining({ domain: '客户与订单关系' }));
  });

  it('resets domain and experience controls when switching to another dataset', async () => {
    const view = await show();
    fireEvent.change(screen.getByLabelText('领域类别'), { target: { value: 'healthcare' } });
    fireEvent.change(screen.getByLabelText('领域描述（选填）'), { target: { value: '门诊业务' } });
    fireEvent.change(screen.getByLabelText('验证查询数量'), { target: { value: '23' } });
    fireEvent.click(screen.getByLabelText('复用领域经验'));
    fireEvent.click(screen.getByRole('switch', { name: '使用 Agent 优化构图' }));
    const nextDataset = { ...sourceDataset, dataset_id: 'd2', name: '另一份数据' };
    mocks.state.mockResolvedValue({ ...notBuilt, dataset_id: 'd2' });
    mocks.build.mockResolvedValue({ ...ready, dataset_id: 'd2' });
    mocks.graph.mockResolvedValue({ dataset_id: 'd2', revision: 3, graph });
    await act(async () => { view.rerender(<GraphConstructionPanel dataset={nextDataset} onClose={vi.fn()} onSaved={vi.fn()} />); });
    expect((screen.getByLabelText('领域类别') as HTMLSelectElement).value).toBe('auto');
    expect((screen.getByLabelText('复用领域经验') as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole('switch', { name: '使用 Agent 优化构图' }) as HTMLInputElement).checked).toBe(false);
    expect((screen.getByLabelText('领域描述（选填）') as HTMLTextAreaElement).value).toBe('');
    expect((screen.getByLabelText('验证查询数量') as HTMLInputElement).value).toBe('15');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(mocks.build).toHaveBeenCalledWith(nextDataset, {
      mode: 'rules', query_count: 15, domain: '', domain_category: 'auto', experience_enabled: true,
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
    const close = vi.fn(), saved = vi.fn(), onStateChange = vi.fn();
    const view = await show(saved, close, onStateChange);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '开始构图' })); });
    expect(onStateChange.mock.calls[onStateChange.mock.calls.length - 1]?.[1].status).toBe('building');
    expect((screen.getByRole('button', { name: '正在构图…' }) as HTMLButtonElement).disabled).toBe(true);
    for (const label of ['验证查询数量', '领域类别', '领域描述（选填）', '复用领域经验', '使用 Agent 优化构图']) {
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
      mode: 'rules', query_count: 15, domain: '', domain_category: 'auto', experience_enabled: true,
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

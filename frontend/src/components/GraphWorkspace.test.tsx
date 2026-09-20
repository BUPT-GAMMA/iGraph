import { useState } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GraphWorkspace } from './GraphWorkspace';
import { I18nProvider, useI18n } from '../i18n';

const { apiFetchMock, listProjectTrainingRunsMock, visualizeGraphMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
  listProjectTrainingRunsMock: vi.fn(),
  visualizeGraphMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: apiFetchMock,
  getUserId: () => '',
  ApiError: class MockApiError extends Error { static fromResponse() { return new Error('request failed'); } },
  listProjectTrainingRuns: listProjectTrainingRunsMock,
  visualizeGraph: visualizeGraphMock,
}));

beforeEach(() => {
  apiFetchMock.mockResolvedValue({ ok: true, json: async () => ({ files: [] }) });
  visualizeGraphMock.mockResolvedValue({ nodes: [], edges: [] });
});

afterEach(() => {
  cleanup();
  apiFetchMock.mockReset();
  listProjectTrainingRunsMock.mockReset();
  visualizeGraphMock.mockReset();
});

describe('GraphWorkspace visualization layers', () => {
  it('shows selected instance properties from the latest graph but not in Schema mode', () => {
    const node = { id: 'customer', attrs: { type: 'customers', properties: { name: '张三' } } };
    const props = { data: { nodes: [node], edges: [] }, tab: 'Graph' as const, onTab: vi.fn(), selected: { id: 'customer' }, onSelect: vi.fn(), collapsed: false, onToggle: vi.fn() };
    const { rerender } = render(<GraphWorkspace {...props} />);
    expect(screen.getByText('张三')).toBeTruthy();
    expect(screen.getByText('customers')).toBeTruthy();
    rerender(<GraphWorkspace {...props} data={{ ...props.data, visualization_kind: 'schema' }} />);
    expect(screen.queryByRole('region', { name: '节点属性' })).toBeNull();
  });

  it('loads the complete selected-node text instead of keeping the visualization preview', async () => {
    const fullText = `完整正文 ${'内容 '.repeat(160)}`;
    visualizeGraphMock.mockResolvedValue({ nodes: [{ id: '566', attrs: { label: 4, text: fullText } }], edges: [] });
    render(<GraphWorkspace data={{ nodes: [{ id: '566', attrs: { label: 4, text: '预览内容...' } }], edges: [] }}
      sessionId="session-1" tab="Graph" onTab={vi.fn()} selected={{ id: '566' }} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText('text 的完整内容').textContent).toBe(fullText));
    expect(visualizeGraphMock).toHaveBeenCalledWith('session-1', {
      node_names: ['566'], include_community: false, max_nodes: 1,
    }, expect.any(AbortSignal));
  });

  it('opens a structured analysis report and renders its readable content', async () => {
    const report = {
      id: 'report-1', title: '引文网络图结构分析报告', subtitle: '结构、分布与关键发现摘要', summary: '这是报告总结。',
      version: 'analysis_report.v1', limitations: ['连接分布缺少统计摘要。'],
      sections: [
        { id: 'structure-overview', title: '结构概览', summary: '图的基础规模。', metrics: [{ label: '节点数', value: 2708 }, { label: '边数', value: 10556 }], insights: ['基础规模和属性状态为后续分析提供上下文。'] },
        { id: 'degree-distribution', title: '连接分布', summary: '旧连接分布。', metrics: [{ label: '平均入度', value: 3.8981 }], charts: [], tables: [] },
        { id: 'centrality-ranking', title: '关键对象', summary: '旧关键对象。', charts: [{ title: 'Top 关键节点', data: [{ label: '163', value: 0.012207 }] }], tables: [] },
        { id: 'global-metrics', title: '整体指标', metrics: [{ label: '密度', value: 0.00144 }], tables: [{ title: '整体指标', columns: [{ key: 'metric', label: '指标' }, { key: 'value', label: '值' }], rows: [{ metric: '密度', value: 0.00144 }] }] },
        { id: 'structure-patterns', title: '结构模式', metrics: [{ label: '分组数量', value: 104 }], insights: ['结构分组结果可用于观察图中局部聚集和整体分化现象。'] },
        { id: 'supplemental-results', title: '补充分析结果', tables: [{ title: '补充事实', rows: [{ field: 'is_weakly_connected', value: true }] }] },
      ],
    };
    const tools = [
      { tool: 'degree_analysis', done: true, result: { directed: true, avg_in_degree: 3.8981, avg_out_degree: 3.8981, total_degree_stats: { mean: 7.7962, median: 6, max: 336 }, top_degree_nodes: [{ node: '163', total: 336 }, { node: '565', total: 156 }] } },
      { tool: 'node_centrality', done: true, result: { centrality_type: 'pagerank', top_nodes: [{ node: '163', value: 0.012207 }, { node: '565', value: 0.005058 }] } },
      { tool: 'graph_connectivity', done: true, result: { directed: true, is_weakly_connected: true, is_strongly_connected: false, weakly_connected_components: { count: 1, largest_size: 2708 }, strongly_connected_components: { count: 371, largest_size: 2336 } } },
    ];
    render(<GraphWorkspace data={{ nodes: [], edges: [] }} messages={[{ role: 'assistant', content: '', reports: [report], tools }]} sessionId="session-1" tab="Artifacts" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: '查看报告：引文网络图结构分析报告' }));
    expect(screen.getByLabelText('引文网络图结构分析报告报告内容')).toBeTruthy();
    expect(screen.getByText('2,708 个节点 · 10,556 条边；平均度 7.7962；104 个社区。')).toBeTruthy();
    expect(screen.getAllByText('2,708').length).toBeGreaterThan(0);
    expect(screen.getByText('密度')).toBeTruthy();
    expect(screen.getByRole('heading', { name: '连接与关键节点' })).toBeTruthy();
    expect(screen.getByText('336 总度')).toBeTruthy();
    expect(screen.getByText('PageRank 0.012207')).toBeTruthy();
    expect(screen.getByText('弱连通')).toBeTruthy();
    expect(screen.getByText('社区结构')).toBeTruthy();
    expect(screen.getByText('识别到 104 个社区。')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: '连接分布' })).toBeNull();
    expect(screen.queryByRole('heading', { name: '关键对象' })).toBeNull();
    expect(screen.queryByText('is_weakly_connected')).toBeNull();
    expect(screen.queryByText('连接分布缺少统计摘要。')).toBeNull();
    expect(screen.queryByRole('table')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '返回报告列表' }));
    expect(screen.getByRole('button', { name: '查看报告：引文网络图结构分析报告' })).toBeTruthy();
  });

  it('isolates highlighted result nodes and restores the full graph through the session callback', async () => {
    const onGraphReset = vi.fn();
    const graph = { nodes: ['a', 'b', 'c'].map(id => ({ id })), edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }], highlights: [{ type: 'path', nodes: ['a', 'b'] }] };
    const { container } = render(<GraphWorkspace data={graph} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} onGraphReset={onGraphReset} />);
    expect(screen.getByRole('button', { name: '恢复全图' }).closest('.graph-toolbar')).toBeTruthy();
    expect(screen.queryByText('分析结果')).toBeNull();
    expect(screen.queryByRole('checkbox', { name: '仅看结果子图' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '显示设置' }));
    fireEvent.click(screen.getByRole('checkbox', { name: '仅看结果子图' }));
    await waitFor(() => expect(container.querySelectorAll('.graph-node')).toHaveLength(2));
    expect(screen.getByRole('meter', { name: '节点' }).getAttribute('aria-valuetext')).toBe('已显示 2 / 3（66.7%）');
    expect(screen.getByRole('meter', { name: '边' }).getAttribute('aria-valuetext')).toBe('已显示 1 / 2（50%）');
    fireEvent.click(screen.getByRole('button', { name: '恢复全图' }));
    expect(onGraphReset).toHaveBeenCalledOnce();
    await waitFor(() => expect(container.querySelectorAll('.graph-node')).toHaveLength(3));
    expect(screen.getByRole('meter', { name: '节点' }).getAttribute('aria-valuetext')).toBe('已显示 3 / 3（100%）');
  });

  it('resets exploration state when switching conversations', () => {
    const props = { data: { nodes: [{ id: 'a' }], edges: [] }, tab: 'Graph' as const, onTab: vi.fn(), selected: null, onSelect: vi.fn(), collapsed: false, onToggle: vi.fn(), onGraphQuery: vi.fn() };
    const { rerender } = render(<GraphWorkspace {...props} sessionId="first" />);
    fireEvent.click(screen.getByRole('button', { name: '图探索' }));
    fireEvent.change(screen.getByRole('textbox', { name: '图谱问题' }), { target: { value: 'old conversation question' } });
    rerender(<GraphWorkspace {...props} sessionId="second" />);
    fireEvent.click(screen.getByRole('button', { name: '图探索' }));
    expect((screen.getByRole('textbox', { name: '图谱问题' }) as HTMLTextAreaElement).value).toBe('');
  });

  it('clears node focus and neighborhood when leaving the graph tab', async () => {
    const graph = {
      nodes: [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }, { id: 'c', label: 'C' }],
      edges: [{ source: 'a', target: 'b' }],
    };
    function WorkspaceHarness() {
      const [tab, setTab] = useState<'Overview' | 'Graph' | 'Data' | 'ML' | 'Artifacts'>('Graph');
      const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
      return <GraphWorkspace data={graph} tab={tab} onTab={setTab} selected={selected} onSelect={setSelected} collapsed={false} onToggle={vi.fn()} />;
    }

    const { container } = render(<WorkspaceHarness />);
    await waitFor(() => expect(container.querySelectorAll('.graph-node')).toHaveLength(3));
    fireEvent.click(container.querySelector('[data-node-id="a"]')!);
    await waitFor(() => expect(container.querySelector('[data-node-id="a"]')?.classList.contains('is-selected')).toBe(true));

    fireEvent.change(screen.getByLabelText('查看邻域范围'), { target: { value: '1' } });
    await waitFor(() => expect(container.querySelectorAll('.graph-node')).toHaveLength(2));
    fireEvent.click(screen.getByRole('tab', { name: '概览' }));
    await waitFor(() => expect(screen.getByText('分析就绪')).toBeTruthy());

    fireEvent.click(screen.getByRole('tab', { name: '图谱' }));
    await waitFor(() => expect(container.querySelectorAll('.graph-node')).toHaveLength(3));
    expect(container.querySelector('.graph-node.is-selected')).toBeNull();
    expect(screen.getByText('点击图中节点查看属性')).toBeTruthy();
  });

  it('preserves the graph camera and node positions when returning from another tab', async () => {
    const graph = {
      nodes: [{ id: 'a' }, { id: 'b' }, { id: 'c' }],
      edges: [{ source: 'a', target: 'b' }, { source: 'b', target: 'c' }],
    };
    function WorkspaceHarness() {
      const [tab, setTab] = useState<'Overview' | 'Graph' | 'Data' | 'ML' | 'Artifacts'>('Graph');
      return <GraphWorkspace data={graph} tab={tab} onTab={setTab} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />;
    }

    const { container } = render(<WorkspaceHarness />);
    await waitFor(() => expect(container.querySelectorAll('.graph-node')).toHaveLength(3));
    fireEvent.click(screen.getByRole('button', { name: '适应画布' }));
    const canvas = screen.getByLabelText(/图谱画布/);
    await waitFor(() => expect(container.querySelector('.graph-viewport')?.getAttribute('transform')).toBeTruthy());
    const fittedCamera = container.querySelector('.graph-viewport')?.getAttribute('transform');
    fireEvent.wheel(canvas, { deltaY: -360, clientX: 180, clientY: 180 });
    await waitFor(() => expect(container.querySelector('.graph-viewport')?.getAttribute('transform')).not.toBe(fittedCamera));
    const camera = container.querySelector('.graph-viewport')?.getAttribute('transform');
    const positions = [...container.querySelectorAll('.graph-node')].map(node => node.getAttribute('transform'));

    fireEvent.click(screen.getByRole('tab', { name: '概览' }));
    await waitFor(() => expect(screen.getByText('分析就绪')).toBeTruthy());
    fireEvent.click(screen.getByRole('tab', { name: '图谱' }));
    await waitFor(() => expect(container.querySelectorAll('.graph-node')).toHaveLength(3));

    await waitFor(() => expect(container.querySelector('.graph-viewport')?.getAttribute('transform')).toBe(camera));
    expect([...container.querySelectorAll('.graph-node')].map(node => node.getAttribute('transform'))).toEqual(positions);
  });

  it('keeps the graph stable across language, appearance and panel visibility changes', async () => {
    const baseGraph = {
      nodes: [{ id: 'a', total_degree: 4 }],
      edges: [],
      degree_scope: 'full_graph' as const,
    };
    function WorkspaceHarness() {
      const { toggleLanguage } = useI18n();
      const [highlighted, setHighlighted] = useState(false);
      const [collapsed, setCollapsed] = useState(false);
      const data = highlighted ? {
        ...baseGraph,
        nodes: [{ ...baseGraph.nodes[0], label: 'renamed', total_degree: 12 }],
        highlights: [{ type: 'node' as const, nodes: ['a'], color: '#ef4444' }],
      } : baseGraph;
      return <>
        <button onClick={toggleLanguage}>切换测试语言</button>
        <button onClick={() => setHighlighted(value => !value)}>切换测试高亮</button>
        <button onClick={() => setCollapsed(value => !value)}>切换测试折叠</button>
        <GraphWorkspace data={data} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={collapsed} onToggle={vi.fn()} />
      </>;
    }

    const { container } = render(<I18nProvider initialLanguage="zh"><WorkspaceHarness /></I18nProvider>);
    await waitFor(() => expect(container.querySelectorAll('.graph-node')).toHaveLength(1));
    fireEvent.click(screen.getByRole('button', { name: '适应画布' }));
    fireEvent.wheel(screen.getByLabelText(/图谱画布/), { deltaY: -240, clientX: 180, clientY: 180 });
    await waitFor(() => expect(container.querySelector('.graph-viewport')?.getAttribute('transform')).toBeTruthy());
    const camera = container.querySelector('.graph-viewport')?.getAttribute('transform');
    const position = container.querySelector('.graph-node')?.getAttribute('transform');
    const expectStable = async () => {
      await waitFor(() => expect(container.querySelector('.graph-viewport')?.getAttribute('transform')).toBe(camera));
      expect(container.querySelector('.graph-node')?.getAttribute('transform')).toBe(position);
    };

    fireEvent.click(screen.getByRole('button', { name: '切换测试语言' }));
    await waitFor(() => expect(screen.getByLabelText(/Graph canvas/)).toBeTruthy());
    await expectStable();

    fireEvent.click(screen.getByRole('button', { name: '切换测试高亮' }));
    await waitFor(() => expect(Number(container.querySelector('.graph-node-halo')?.getAttribute('r'))).toBeGreaterThan(0));
    await expectStable();

    fireEvent.click(screen.getByRole('button', { name: '切换测试折叠' }));
    fireEvent.click(screen.getByRole('button', { name: '切换测试折叠' }));
    await expectStable();

    fireEvent.click(screen.getByRole('button', { name: '切换测试语言' }));
    await waitFor(() => expect(screen.getByLabelText(/图谱画布/)).toBeTruthy());
  });

  it('shows directed edge arrows with a marker unique to each canvas', async () => {
    const graph = { nodes: [{ id: 'a' }, { id: 'b' }], edges: [{ source: 'a', target: 'b' }], directed: true };
    const { container } = render(<><GraphWorkspace data={graph} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} /><GraphWorkspace data={graph} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} embedded /></>);
    await waitFor(() => expect(container.querySelectorAll('marker')).toHaveLength(2));
    const markers = Array.from(container.querySelectorAll('marker'), item => item.id);
    expect(new Set(markers).size).toBe(2);
    expect(Array.from(container.querySelectorAll('.graph-link'), item => item.getAttribute('marker-end'))).toEqual(markers.map(id => `url(#${id})`));
  });
  it('keeps edge-only highlights and respects directed path orientation', async () => {
    const { container } = render(<GraphWorkspace data={{
      directed: true, nodes: [{ id: 'a' }, { id: 'b' }],
      edges: [{ source: 'b', target: 'a' }, { source: 'a', target: 'b' }],
      highlights: [{ type: 'path', edges: [['a', 'b']], color: '#f59e0b' }],
    }} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);
    await waitFor(() => expect(container.querySelectorAll('.is-path-highlight')).toHaveLength(1));
    const edges = Array.from(container.querySelectorAll('.graph-link'));
    // Result edges are prioritized ahead of the unhighlighted reverse edge.
    expect(edges[0].classList.contains('is-path-highlight')).toBe(true);
    expect(edges[1].classList.contains('is-path-highlight')).toBe(false);
  });
  it('renders community colors, node halos, path edges, subgraph hulls, and legends', async () => {
    const { container } = render(<GraphWorkspace
      data={{
        nodes: [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'd' }],
        edges: [
          { src: 'a', dst: 'b' },
          { src: 'b', dst: 'c' },
          { src: 'c', dst: 'd' },
          { src: 'd', dst: 'a' },
        ],
        community: { num_communities: 3, node_community: { a: 0, b: 1, c: 2, d: 0 } },
        highlights: {
          nodes: [{ ids: ['a'], color: '#22c55e', label: '关键节点' }],
          paths: [{ node_sequence: ['a', 'b', 'c'], color: '#f59e0b', label: '最短路径' }],
          subgraphs: [{ nodes: ['c', 'd', 'a'], edges: [['c', 'd'], ['d', 'a']], color: '#8b5cf6', label: '目标子图' }],
        },
      }}
      tab="Graph"
      onTab={vi.fn()}
      selected={null}
      onSelect={vi.fn()}
      collapsed={false}
      onToggle={vi.fn()}
    />);

    await waitFor(() => expect(container.querySelectorAll('.graph-node')).toHaveLength(4));
    const nodeFills = new Set([...container.querySelectorAll('.graph-node-dot')].map(node => node.getAttribute('fill')));
    expect(nodeFills.size).toBe(3);
    expect([...container.querySelectorAll('.graph-node-halo')].filter(node => Number(node.getAttribute('r')) > 0)).toHaveLength(4);
    expect(container.querySelectorAll('.graph-link.is-path-highlight')).toHaveLength(2);
    expect(container.querySelectorAll('.graph-link.is-subgraph-highlight')).toHaveLength(2);
    await waitFor(() => expect(container.querySelector('.graph-subgraph-hull')?.getAttribute('d')).toBeTruthy());
    expect(container.querySelector('.graph-legend')?.textContent).toContain('社区着色');
    expect(container.querySelector('.graph-legend')?.textContent).toContain('节点高亮');
    expect(container.querySelector('.graph-legend')?.textContent).toContain('路径高亮');
    expect(container.querySelector('.graph-legend')?.textContent).toContain('子图高亮');
  });

  it('does not restart the graph when only the parent callback identity changes', async () => {
    const data = { nodes: [{ id: 'a' }, { id: 'b' }], edges: [{ src: 'a', dst: 'b' }] };
    const first = vi.fn();
    const { container, rerender } = render(<GraphWorkspace data={data} tab="Graph" onTab={vi.fn()} selected={null} onSelect={first} collapsed={false} onToggle={vi.fn()} />);
    await waitFor(() => expect(container.querySelectorAll('.graph-node')).toHaveLength(2));
    const firstNode = container.querySelector('.graph-node');
    rerender(<GraphWorkspace data={data} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);
    expect(container.querySelector('.graph-node')).toBe(firstNode);
  });

  it('skips Graph tab renders when chat-only props change', async () => {
    const data = { nodes: [{ id: 'a' }, { id: 'b' }], edges: [{ src: 'a', dst: 'b' }] };
    const { container, rerender } = render(<GraphWorkspace data={data} messages={[]} liveTools={[]} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);
    await waitFor(() => expect(container.querySelectorAll('.graph-node')).toHaveLength(2));
    const firstNode = container.querySelector('.graph-node');
    rerender(<GraphWorkspace data={data} messages={[{ role: 'assistant', content: 'typing update' }]} liveTools={[{ tool: 'visualize_subgraph' }]} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);
    expect(container.querySelector('.graph-node')).toBe(firstNode);
  });

  it('does not serialize the complete graph just to stabilize its reference', async () => {
    const data = { nodes: [{ id: 'a' }, { id: 'b' }], edges: [{ src: 'a', dst: 'b' }] };
    const stringify = vi.spyOn(JSON, 'stringify');
    const { container } = render(<GraphWorkspace data={data} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);

    await waitFor(() => expect(container.querySelectorAll('.graph-node')).toHaveLength(2));
    expect(stringify.mock.calls.some(([value]) => value === data)).toBe(false);
    stringify.mockRestore();
  });

  it('keeps canvas sampling details out of the graph overview', () => {
    const nodes = Array.from({ length: 21951 }, (_, index) => ({ id: `node-${index}` }));
    render(<GraphWorkspace data={{ nodes, edges: [], num_nodes: nodes.length, num_edges: 0 }} tab="Overview" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);

    expect(screen.getByText('21,951')).toBeTruthy();
    expect(screen.queryByText('当前画布')).toBeNull();
    expect(screen.queryByRole('meter', { name: '节点' })).toBeNull();
  });

  it('presents graph scale and structural signals in the overview', () => {
    const nodes = Array.from({ length: 200 }, (_, index) => ({ id: `node-${index}` }));
    const edges = Array.from({ length: 500 }, (_, index) => ({ source: `node-${index % 200}`, target: `node-${(index + 1) % 200}` }));
    render(<GraphWorkspace datasetName="cora" data={{ nodes, edges, directed: true, total_nodes: 2708, total_edges: 10556, community: { num_communities: 104 } }} tab="Overview" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);

    expect(screen.getByRole('heading', { name: 'cora' })).toBeTruthy();
    expect(screen.getByText('分析就绪')).toBeTruthy();
    expect(screen.getByText('2,708')).toBeTruthy();
    expect(screen.getByText('10,556')).toBeTruthy();
    expect(screen.getByText('有向图')).toBeTruthy();
    expect(screen.getByText('平均总度')).toBeTruthy();
    expect(screen.getByText('7.8')).toBeTruthy();
    expect(screen.getByText('连接密度')).toBeTruthy();
    expect(screen.getByText('0.14%')).toBeTruthy();
    expect(screen.getByText('社区数')).toBeTruthy();
    expect(screen.getByText('104')).toBeTruthy();
    expect(screen.queryByText('当前画布')).toBeNull();
    expect(screen.queryByRole('meter')).toBeNull();
  });

  it('reports displayed counts against full graph totals instead of the loaded sample', () => {
    const nodes = Array.from({ length: 200 }, (_, index) => ({ id: `node-${index}` }));
    render(<GraphWorkspace data={{ nodes, edges: [], total_nodes: 10000, total_edges: 24000, truncated: true }} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);

    expect(screen.getByRole('meter', { name: '节点' }).getAttribute('aria-valuetext')).toBe('已显示 180 / 10,000（1.8%）');
    expect(screen.getByRole('meter', { name: '边' }).getAttribute('aria-valuetext')).toBe('已显示 0 / 24,000（0%）');
    expect(screen.getByRole('status').textContent).not.toMatch(/上游部分图|已载入|智能概览|度数/);
  });

  it('renders relationship type and count labels for a complete schema graph', async () => {
    const { container } = render(<GraphWorkspace
      data={{
        visualization_kind: 'schema',
        total_nodes: 10000, total_edges: 12000,
        nodes: [{ id: 'Post', label: 'Post' }, { id: 'Tag', label: 'Tag' }],
        edges: [{ source: 'Post', target: 'Tag', type: 'HAS_TAG', properties: { relationship_count: 12000 } }],
        directed: true,
      }}
      tab="Graph"
      onTab={vi.fn()}
      selected={null}
      onSelect={vi.fn()}
      collapsed={false}
      onToggle={vi.fn()}
    />);

    await waitFor(() => expect(container.querySelector('.graph-schema-edge-label')?.textContent).toBe('HAS_TAG · 12,000'));
    expect(screen.getByRole('meter', { name: '节点类型' }).getAttribute('aria-valuetext')).toBe('已显示 2 / 2（100%）');
    expect(screen.getByRole('meter', { name: '关系' }).getAttribute('aria-valuetext')).toBe('已显示 1 / 1（100%）');
    expect(screen.getByLabelText(/Graph Schema 画布/)).toBeTruthy();
  });

  it('compacts long schema relationship labels while preserving the full accessible label', async () => {
    const relationshipType = 'fk_0c224b281af28bb360380a3a9fb22c3c694bef04';
    const { container } = render(<GraphWorkspace
      data={{
        visualization_kind: 'schema',
        nodes: [{ id: 'Post' }, { id: 'Tag' }],
        edges: [{ source: 'Post', target: 'Tag', type: relationshipType, relationship_count: 8000 }],
        directed: true,
      }}
      tab="Graph"
      onTab={vi.fn()}
      selected={null}
      onSelect={vi.fn()}
      collapsed={false}
      onToggle={vi.fn()}
    />);

    await waitFor(() => expect(container.querySelector('.graph-schema-edge-label')).toBeTruthy());
    const label = container.querySelector('.graph-schema-edge-label');
    expect(label?.textContent).toContain('…');
    expect(label?.textContent).toContain('· 8,000');
    expect(label?.textContent).not.toContain(relationshipType);
    expect(label?.getAttribute('aria-label')).toBe(`${relationshipType} · 8,000`);
    expect(container.querySelector('path.graph-schema-link')?.getAttribute('aria-label')).toBe(`${relationshipType} · 8,000`);
  });

  it('draws visible schema self-loops and separates parallel relationship curves', async () => {
    const { container } = render(<GraphWorkspace
      data={{
        visualization_kind: 'schema',
        nodes: [{ id: 'A' }, { id: 'B' }],
        edges: [
          { source: 'A', target: 'A', type: 'SELF', relationship_count: 3 },
          { source: 'A', target: 'B', type: 'FIRST', relationship_count: 4 },
          { source: 'A', target: 'B', type: 'SECOND', relationship_count: 5 },
        ],
        directed: true,
      }}
      tab="Graph"
      onTab={vi.fn()}
      selected={null}
      onSelect={vi.fn()}
      collapsed={false}
      onToggle={vi.fn()}
    />);

    await waitFor(() => expect(container.querySelectorAll('path.graph-schema-link')).toHaveLength(3));
    const paths = [...container.querySelectorAll<SVGPathElement>('path.graph-schema-link')];
    const selfLoop = paths.find(path => (path as any).__data__?.raw.type === 'SELF');
    const parallel = paths.filter(path => ['FIRST', 'SECOND'].includes((path as any).__data__?.raw.type));
    expect(selfLoop?.getAttribute('d')).toContain('C');
    expect(parallel.every(path => path.getAttribute('d')?.includes('Q'))).toBe(true);
    expect(new Set(parallel.map(path => path.getAttribute('d'))).size).toBe(2);
    expect([...container.querySelectorAll('.graph-schema-edge-label')].map(label => label.textContent)).toEqual(expect.arrayContaining(['SELF · 3', 'FIRST · 4', 'SECOND · 5']));
  });

  it.each([
    ['failed', '失败'], ['cancelled', '已终止'], ['cancelling', '正在停止'], ['queued', '排队中'], ['unavailable', '状态不可用'],
  ])('shows %s consistently in project comparison and training cards', async (status, label) => {
    listProjectTrainingRunsMock.mockResolvedValue({ training_runs: [{
      tool: 'train', training_run_id: 'job-1', session_id: 'old', done: true,
      result: { job_id: 'job-1', job_status: status },
    }], has_running: false });
    const { container } = render(<GraphWorkspace data={null} messages={[]} projectId="project-1" sessionId="new" tab="ML" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} onStopJob={vi.fn()} />);
    await waitFor(() => expect(container.querySelector('.ml-run-header > b')?.textContent).toBe(label));
    expect(container.querySelector(`.ml-status-${status}`)?.textContent).toBe(label);
    expect(screen.queryByText('已完成')).toBeNull();
    if (['failed', 'cancelled', 'unavailable'].includes(status)) expect(screen.queryByRole('button', { name: '终止作业' })).toBeNull();
  });

  it.each([{ success: false, error: 'Invalid model' }, 'Validation error: Invalid model'])('retains failed local runs and their error without borrowing successful retry metrics', result => {
    const { container } = render(<GraphWorkspace data={null} messages={[{ role: 'assistant', content: '| 测试集 | 90% |', tools: [{ tool: 'train', done: true, result }] }]} sessionId="new" tab="ML" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);
    expect(screen.getByRole('alert').textContent).toContain('Invalid model');
    expect(container.querySelector('.ml-run-header > b')?.textContent).toBe('失败');
    expect(screen.queryByText('90.0%')).toBeNull();
  });

  it('shows project training runs in a new session without local history', async () => {
    listProjectTrainingRunsMock.mockResolvedValue({
      training_runs: [{
        tool: 'train', training_run_id: 'job-gcn', session_id: 'session-old', session_title: 'GCN 实验', dataset_name: 'Cora', done: true,
        args: { task: 'node_classification', model: 'GCN', train_config: { epochs: 200 } },
        result: { metrics: { val_acc: .773, test_acc: .761, best_epoch: 1, trained_epochs: 200 } },
        history: [{ epoch: 200, val_acc: .773 }],
      }],
      has_running: false,
    });

    render(<GraphWorkspace data={null} messages={[]} liveTools={[]} projectId="project-1" sessionId="session-new" tab="ML" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('项目对比')).toBeTruthy());
    expect((await screen.findAllByText('GCN 实验')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('77.3%').length).toBeGreaterThan(0);
  });

  it('shows absent metrics as missing and preserves genuine zero measurements', async () => {
    listProjectTrainingRunsMock.mockResolvedValue({ training_runs: [{
      tool: 'train', training_run_id: 'job-1', session_id: 'old', done: true,
      result: { metrics: { val_acc: null, test_acc: '', train_acc: 0, train_loss: false, best_epoch: null } },
    }], has_running: false });
    const { container } = render(<GraphWorkspace data={null} projectId="p" sessionId="new" tab="ML" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);
    await waitFor(() => expect(container.querySelector('.ml-comparison')).toBeTruthy());
    const cells = container.querySelectorAll('.ml-comparison tbody td');
    expect([...cells].slice(-3).map(cell => cell.textContent)).toEqual(['—', '—', '—']);
    expect(container.querySelectorAll('.ml-metric-grid > div')).toHaveLength(1);
    expect(container.querySelector('.ml-metric-grid strong')?.textContent).toBe('0.0%');
    expect(container.querySelectorAll('.ml-table tbody tr')).toHaveLength(1);
    expect(container.querySelectorAll('.ml-table tbody td')[2].textContent).toBe('—');
  });

  it('does not draw through gaps in training metrics', () => {
    const history = [{ epoch: null, train_loss: 5 }, { epoch: 1, train_loss: 1 }, { epoch: 2, train_loss: null }, { epoch: 3, train_loss: .5 }, { epoch: 4, train_loss: 0 }];
    const { container } = render(<GraphWorkspace data={null} messages={[{ role: 'assistant', content: '', tools: [{ tool: 'train', done: true, history }] }]} tab="ML" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);
    expect(container.querySelectorAll('[data-metric="train_loss"] circle')).toHaveLength(3);
    const lines = container.querySelectorAll('[data-metric="train_loss"] polyline');
    expect(lines).toHaveLength(1);
    expect(lines[0].getAttribute('points')?.split(' ')).toHaveLength(2);
    fireEvent.click(screen.getByRole('tab', { name: '表格' }));
    expect(container.querySelectorAll('.ml-table tbody tr')).toHaveLength(4);
    expect(container.querySelectorAll('.ml-table tbody tr')[1].textContent).toContain('—');
  });

  it('updates the current training card from the project job snapshot', async () => {
    listProjectTrainingRunsMock.mockResolvedValue({
      training_runs: [{
        tool: 'train', call_id: 'call-gcn', training_run_id: 'job-gcn', session_id: 'session-current', session_title: '当前实验', done: true,
        args: { task: 'node_classification', model: 'GCN', train_config: { epochs: 200 } },
        result: { job_id: 'job-gcn', metrics: { val_acc: .773, best_epoch: 1, trained_epochs: 200 } },
        progress: { epoch: 200, epochs: 200, status: 'done' },
        history: [{ epoch: 8, val_acc: .773 }, { epoch: 200, val_acc: .773 }],
      }],
      has_running: false,
    });
    const messages = [{
      role: 'assistant' as const,
      content: '训练会在后台继续',
      tools: [{ tool: 'train', call_id: 'call-gcn', done: false, args: { task: 'node_classification', model: 'GCN', train_config: { epochs: 200 } }, result: { async: true, job_id: 'job-gcn' }, progress: { epoch: 8, epochs: 200 }, history: [{ epoch: 8, val_acc: .773 }] }],
    }];

    render(<GraphWorkspace data={null} messages={messages} liveTools={[]} projectId="project-1" sessionId="session-current" tab="ML" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);

    await waitFor(() => expect(screen.getByText(/200\/200 轮/)).toBeTruthy());
    expect(document.querySelectorAll('.ml-run-card')).toHaveLength(1);
    expect(screen.getByText('已完成')).toBeTruthy();
    fireEvent.click(screen.getByText('项目对比'));
    expect(screen.getAllByText('当前实验').length).toBeGreaterThan(0);
  });
});

it('highlights neighbors without moving nodes, preserves community colors and supports display controls', async () => {
  const graph = { degree_scope: 'full_graph' as const, nodes: [{ id: 'hub', total_degree: 20 }, { id: 'leaf', total_degree: 1 }, { id: 'other', total_degree: 0 }], edges: [{ source: 'hub', target: 'leaf' }], community: { node_community: { hub: 0, leaf: 1 } } };
  const onSelect = vi.fn();
  const { container, rerender } = render(<GraphWorkspace data={graph} tab="Graph" onTab={vi.fn()} selected={null} onSelect={onSelect} collapsed={false} onToggle={vi.fn()} />);
  const hub = container.querySelector('[data-node-id="hub"]')!;
  const leaf = container.querySelector('[data-node-id="leaf"]')!;
  expect(Number(hub.querySelector('.graph-node-dot')?.getAttribute('r'))).toBeGreaterThan(Number(leaf.querySelector('.graph-node-dot')?.getAttribute('r')));
  const fill = leaf.querySelector('.graph-node-dot')?.getAttribute('fill');
  fireEvent.pointerEnter(hub);
  await waitFor(() => expect(container.querySelectorAll('.graph-link.is-neighbor')).toHaveLength(1));
  expect(container.querySelector('[data-node-id="other"]')?.classList.contains('is-muted')).toBe(true);
  expect(leaf.querySelector('.graph-node-dot')?.getAttribute('fill')).toBe(fill);
  fireEvent.pointerLeave(hub);
  await waitFor(() => expect(container.querySelectorAll('.graph-node.is-muted')).toHaveLength(0));
  fireEvent.click(screen.getByRole('button', { name: '显示设置' }));
  fireEvent.change(screen.getByLabelText('节点大小'), { target: { value: 'uniform' } });
  expect(Number(hub.querySelector('.graph-node-dot')?.getAttribute('r'))).toBe(8);
  expect(container.querySelector('[data-node-id="hub"]')).toBe(hub);
  fireEvent.click(hub);
  expect(onSelect).toHaveBeenCalled();
  rerender(<GraphWorkspace data={graph} tab="Graph" onTab={vi.fn()} selected={graph.nodes[0]} onSelect={onSelect} collapsed={false} onToggle={vi.fn()} />);
  await waitFor(() => expect(hub.classList.contains('is-selected')).toBe(true));
  expect(container.querySelector('[data-node-id="hub"]')).toBe(hub);
});

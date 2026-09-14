import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { GraphWorkspace } from './GraphWorkspace';

const { listProjectTrainingRunsMock } = vi.hoisted(() => ({ listProjectTrainingRunsMock: vi.fn() }));

vi.mock('../api', () => ({
  listProjectTrainingRuns: listProjectTrainingRunsMock,
}));

afterEach(() => {
  cleanup();
  listProjectTrainingRunsMock.mockReset();
});

describe('GraphWorkspace visualization layers', () => {
  it('shows directed edge arrows with a marker unique to each canvas', async () => {
    const graph = { nodes: [{ id: 'a' }, { id: 'b' }], edges: [{ source: 'a', target: 'b' }], directed: true };
    const { container } = render(<><GraphWorkspace data={graph} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} /><GraphWorkspace data={graph} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} embedded /></>);
    await waitFor(() => expect(container.querySelectorAll('marker')).toHaveLength(2));
    const markers = Array.from(container.querySelectorAll('marker'), item => item.id);
    expect(new Set(markers).size).toBe(2);
    expect(Array.from(container.querySelectorAll('.graph-link'), item => item.getAttribute('marker-end'))).toEqual(markers.map(id => `url(#${id})`));
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
    const nodeFills = new Set([...container.querySelectorAll('.graph-node circle:not(.graph-node-halo)')].map(node => node.getAttribute('fill')));
    expect(nodeFills.size).toBe(3);
    expect(container.querySelectorAll('.graph-node-halo[r="13"]')).toHaveLength(4);
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

  it('reports the smart overview node cap instead of the complete graph size', () => {
    const nodes = Array.from({ length: 21951 }, (_, index) => ({ id: `node-${index}` }));
    render(<GraphWorkspace data={{ nodes, edges: [], num_nodes: nodes.length, num_edges: 0 }} tab="Overview" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed={false} onToggle={vi.fn()} />);

    expect(screen.getByText('21,951')).toBeTruthy();
    expect(screen.getByText('800')).toBeTruthy();
    expect(screen.getByText(/稳定生成最多 800 个节点的智能概览/)).toBeTruthy();
  });

  it('distinguishes an upstream partial graph from a locally sampled overview', () => {
    const nodes = Array.from({ length: 200 }, (_, index) => ({ id: `node-${index}` }));
    const { container } = render(<GraphWorkspace data={{ nodes, edges: [], total_nodes: 10000, total_edges: 24000, truncated: true }} tab="Graph" onTab={vi.fn()} selected={null} onSelect={vi.fn()} collapsed onToggle={vi.fn()} />);
    const status = container.querySelector('.graph-overview-status');

    expect(status?.textContent).toContain('上游部分图');
    expect(status?.textContent).toContain('已载入 200 / 10,000 个节点');
    expect(status?.textContent).not.toContain('本地智能概览');
  });

  it('renders relationship type and count labels for a complete schema graph', async () => {
    const { container } = render(<GraphWorkspace
      data={{
        visualization_kind: 'schema',
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
    expect(screen.getByRole('status').textContent).toContain('Graph Schema · 2 种节点类型 · 1 种关系');
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
    const selfLoop = paths.find(path => (path as any).__data__?.type === 'SELF');
    const parallel = paths.filter(path => ['FIRST', 'SECOND'].includes((path as any).__data__?.type));
    expect(selfLoop?.getAttribute('d')).toContain('A');
    expect(parallel.every(path => path.getAttribute('d')?.includes('Q'))).toBe(true);
    expect(new Set(parallel.map(path => path.getAttribute('d'))).size).toBe(2);
    expect([...container.querySelectorAll('.graph-schema-edge-label')].map(label => label.textContent)).toEqual(expect.arrayContaining(['SELF · 3', 'FIRST · 4', 'SECOND · 5']));
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
    expect(screen.getByText('已完成')).toBeTruthy();
    fireEvent.click(screen.getByText('项目对比'));
    expect(screen.getAllByText('当前实验').length).toBeGreaterThan(0);
  });
});

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DatasetPanel } from './DatasetPanel';
import { DrawerShell } from './DrawerShell';
import { GraphWorkspace } from './GraphWorkspace';
import { Sidebar } from './Sidebar';

const noop = () => undefined;
afterEach(cleanup);

const sidebarProps = {
  projects: [],
  sessions: [],
  userId: 'tester',
  dark: false,
  onTheme: noop,
  onMemory: noop,
  onSkills: noop,
  mobileOpen: false,
  onCloseMobile: noop,
  onNew: noop,
  onNewProject: noop,
  onNewInProject: noop,
  onEditProject: noop,
  onDeleteProject: noop,
  onSelectProject: noop,
  onSelect: noop,
  onDelete: noop,
};

const graphProps = {
  data: null,
  tab: 'Graph' as const,
  onTab: noop,
  selected: null,
  onSelect: noop,
};

describe('docked panel controls', () => {
  it('removes the legacy sidebar version label and keeps a functional toggle', () => {
    const onCollapse = vi.fn();
    const { rerender } = render(<Sidebar {...sidebarProps} collapsed={false} onCollapse={onCollapse} />);

    expect(screen.queryByText('iGraph 1.0')).toBeNull();
    expect(screen.getByText('新建任务')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '收起左侧栏' }));
    expect(onCollapse).toHaveBeenCalledOnce();

    rerender(<Sidebar {...sidebarProps} collapsed onCollapse={onCollapse} />);
    const expandLeft = screen.getByRole('button', { name: '展开左侧栏' });
    expect(expandLeft).toBeTruthy();
    expect(expandLeft.querySelector('path')?.getAttribute('d')).toBe('M9 4v16M14 9l3 3-3 3');
    expect(screen.getByText('新建任务').closest('.sidebar-content')?.getAttribute('aria-hidden')).toBe('true');
  });

  it('highlights a selected conversation without highlighting its parent project', () => {
    const projects = [{ project_id: 'project-1', user_id: 'tester', name: '项目一', conversation_count: 1 }];
    const sessions = [{ session_id: 'session-1', project_id: 'project-1', title: '会话一' }];
    const { container, rerender } = render(<Sidebar {...sidebarProps} projects={projects} sessions={sessions} activeId="session-1" collapsed={false} onCollapse={noop} />);

    expect(container.querySelector('.project-row-main small')).toBeNull();
    expect(container.querySelector('.project-row')?.classList.contains('selected')).toBe(false);
    expect(container.querySelector('.project-session-row')?.classList.contains('selected')).toBe(true);

    rerender(<Sidebar {...sidebarProps} projects={projects} sessions={sessions} activeProjectId="project-1" collapsed={false} onCollapse={noop} />);
    expect(container.querySelector('.project-row')?.classList.contains('selected')).toBe(true);
    expect(container.querySelector('.project-session-row')?.classList.contains('selected')).toBe(false);
  });

  it('shows the project name before each recent task title', () => {
    const projects = [{ project_id: 'project-1', user_id: 'tester', name: '项目一', conversation_count: 1 }];
    const sessions = [{ session_id: 'session-1', project_id: 'project-1', title: '会话一' }];
    render(<Sidebar {...sidebarProps} projects={projects} sessions={sessions} collapsed={false} onCollapse={noop} />);

    const recent = screen.getByTitle('项目一 / 会话一');
    expect(recent.querySelector('.recent-project-name')?.textContent).toBe('项目一');
    expect(recent.querySelector('.recent-session-separator')?.textContent).toBe('/');
    expect(recent.querySelector('.recent-session-name')?.textContent).toBe('会话一');
  });

  it('uses one right panel toggle for both expanded and collapsed states', () => {
    const onToggle = vi.fn();
    const onTab = vi.fn();
    const { rerender } = render(<GraphWorkspace {...graphProps} onTab={onTab} collapsed={false} onToggle={onToggle} />);

    expect(screen.getByText('图工作台')).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Graph' }).getAttribute('aria-selected')).toBe('true');
    expect(document.querySelector('.workspace-tab-indicator')).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: 'Artifacts' }));
    expect(onTab).toHaveBeenCalledWith('Artifacts');
    expect(screen.queryByLabelText('关闭图工作台')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '收起右侧栏' }));
    expect(onToggle).toHaveBeenCalledOnce();

    rerender(<GraphWorkspace {...graphProps} onTab={onTab} collapsed onToggle={onToggle} />);
    const expandRight = screen.getByRole('button', { name: '展开右侧栏' });
    expect(expandRight).toBeTruthy();
    expect(expandRight.querySelector('path')?.getAttribute('d')).toBe('M15 4v16M10 9l-3 3 3 3');
    expect(screen.getByText('图工作台').classList.contains('workspace-title')).toBe(true);
    expect(screen.getByText('Overview').closest('.workspace-panel-content')?.getAttribute('aria-hidden')).toBe('true');
  });

  it('degrades graph collections without mounting a canvas and keeps statistics tabs useful', () => {
    const view = {
      status: 'unsupported' as const,
      data: null,
      code: 'NON_SINGLE_GRAPH' as const,
      summary: { num_graphs: 41127, num_tasks: 1, metric: 'roc_auc', split_sizes: { train: 32901, val: 4113, test: 4113 }, node_vocab_size: 382, edge_vocab_size: 8, missing_label_rate: 0 },
    };
    const { rerender, container } = render(<GraphWorkspace {...graphProps} data={null} collapsed={false} onToggle={noop} />);

    expect(screen.getByText('暂无图')).toBeTruthy();
    expect(container.querySelector('.graph-canvas svg')).toBeNull();
    expect(container.querySelector('.graph-toolbar')).toBeNull();
    expect(container.querySelector('.node-inspector')).toBeNull();

    rerender(<GraphWorkspace {...graphProps} data={null} tab="Overview" collapsed={false} onToggle={noop} />);
    expect(screen.getByText('暂无图')).toBeTruthy();

    rerender(<GraphWorkspace {...graphProps} data={null} tab="Data" collapsed={false} onToggle={noop} />);
    expect(screen.getByText('暂无图')).toBeTruthy();
  });

  it('keeps the graph canvas stable across unrelated renders and supports zoom gestures', () => {
    const graphData = {
      nodes: [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }],
      edges: [{ source: 'a', target: 'b' }],
    };
    const { container, rerender } = render(<GraphWorkspace {...graphProps} data={graphData} collapsed={false} onToggle={noop} />);
    const canvas = screen.getByLabelText('图谱画布，可拖动平移并使用滚轮缩放');
    const originalViewport = container.querySelector('.graph-viewport');

    expect(originalViewport).toBeTruthy();
    expect((canvas as unknown as SVGSVGElement & { __zoom?: unknown }).__zoom).toBeTruthy();

    rerender(<GraphWorkspace {...graphProps} data={{ nodes: graphData.nodes.map(node => ({ ...node })), edges: graphData.edges.map(edge => ({ ...edge })) }} messages={[{ id: 'm1', role: 'user', content: '输入变化' }]} selected={{ id: 'a' }} onSelect={() => undefined} collapsed={false} onToggle={noop} />);

    expect(container.querySelector('.graph-viewport')).toBe(originalViewport);
    expect(container.querySelector<SVGCircleElement>('.graph-node circle')?.getAttribute('r')).toBe('13');
  });

  it('keeps a drawer mounted until its exit animation completes', () => {
    const onClose = vi.fn();
    render(<DrawerShell ariaLabel="测试抽屉" onClose={onClose}>{requestClose => <button onClick={requestClose}>关闭测试抽屉</button>}</DrawerShell>);

    const overlay = screen.getByRole('dialog', { name: '测试抽屉' }).parentElement!;
    fireEvent.click(screen.getByRole('button', { name: '关闭测试抽屉' }));
    expect(overlay.classList.contains('is-closing')).toBe(true);
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.animationEnd(overlay);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('renders datasets as compact rows without the internal implementation note', () => {
    render(<DatasetPanel
      datasets={[{ dataset_id: 'dataset-1', project_id: 'project-1', name: 'Cora', original_filename: 'cora.pt', file_type: 'pt', status: 'ready', is_default: true }]}
      activeDatasetId="dataset-1"
      onSelect={noop}
      onUpload={noop}
      onRename={noop}
      onDelete={noop}
      onClose={noop}
    />);

    expect(screen.queryByText('会话共享项目数据源，但各自维护独立的图运行状态。')).toBeNull();
    expect(screen.queryByText('默认')).toBeNull();
    expect(screen.getByText('当前')).toBeTruthy();
    expect(screen.getByRole('button', { name: /^Cora/ }).getAttribute('aria-current')).toBe('true');
    expect(screen.getByRole('button', { name: '重命名 Cora' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '删除 Cora' })).toBeTruthy();
  });
});

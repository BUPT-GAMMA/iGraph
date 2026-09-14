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
  onCreateGraph: noop,
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
  it('keeps create your graph as a permanent first-level destination', () => {
    const onCreateGraph = vi.fn();
    const onCloseMobile = vi.fn();
    const { rerender } = render(<Sidebar {...sidebarProps} onCreateGraph={onCreateGraph} onCloseMobile={onCloseMobile} collapsed={false} onCollapse={noop} />);

    const entry = screen.getByRole('button', { name: 'create your graph' });
    expect(entry.getAttribute('aria-current')).toBeNull();
    fireEvent.click(entry);
    expect(onCreateGraph).toHaveBeenCalledOnce();
    expect(onCloseMobile).toHaveBeenCalledOnce();

    rerender(<Sidebar {...sidebarProps} onCreateGraph={onCreateGraph} createGraphActive collapsed={false} onCollapse={noop} />);
    expect(screen.getByRole('button', { name: 'create your graph' }).getAttribute('aria-current')).toBe('page');
  });

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

  it('opens the login form from the signed-out avatar and dispatches input and submit callbacks', () => {
    const onLoginValueChange = vi.fn();
    const onLogin = vi.fn();
    const { rerender } = render(<Sidebar
      {...sidebarProps}
      userId=""
      loginValue=""
      onLoginValueChange={onLoginValueChange}
      onLogin={onLogin}
      collapsed={false}
      onCollapse={noop}
    />);

    expect(screen.queryByRole('dialog', { name: '账户' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '打开登录' }));
    expect(screen.getByRole('dialog', { name: '账户' })).toBeTruthy();

    fireEvent.change(screen.getByLabelText('用户 ID'), { target: { value: 'alice' } });
    expect(onLoginValueChange).toHaveBeenCalledWith('alice');

    rerender(<Sidebar
      {...sidebarProps}
      userId=""
      loginValue="alice"
      onLoginValueChange={onLoginValueChange}
      onLogin={onLogin}
      collapsed={false}
      onCollapse={noop}
    />);
    fireEvent.click(screen.getByRole('button', { name: '登录' }));
    expect(onLogin).toHaveBeenCalledOnce();
  });

  it('lets a signed-in user choose another account from the avatar menu', () => {
    render(<Sidebar
      {...sidebarProps}
      userId="tester"
      loginValue="tester"
      collapsed={false}
      onCollapse={noop}
    />);

    fireEvent.click(screen.getByRole('button', { name: '账户：tester' }));
    expect(screen.getByText('当前账户')).toBeTruthy();
    expect(screen.queryByLabelText('用户 ID')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '使用其他用户登录' }));
    expect(screen.getByLabelText('用户 ID')).toBeTruthy();
    expect(screen.getByRole('button', { name: '取消' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '登录' })).toBeTruthy();
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

  it('switches between closed and open folder icons with the project tree state', () => {
    const projects = [{ project_id: 'project-1', user_id: 'tester', name: '项目一', conversation_count: 0 }];
    const { container } = render(<Sidebar {...sidebarProps} projects={projects} sessions={[]} collapsed={false} onCollapse={noop} />);

    expect(container.querySelector('.project-row-icon path')?.getAttribute('d')).toContain('M3 6.5');
    fireEvent.click(screen.getByRole('button', { name: '展开 项目一' }));
    expect(container.querySelector('.project-row-icon path')?.getAttribute('d')).toContain('M3 16.5');
  });

  it('opens project actions in a floating menu and dispatches each action', () => {
    const projects = [{ project_id: 'project-1', user_id: 'tester', name: '项目一', conversation_count: 0 }];
    const onEditProject = vi.fn();
    const onDeleteProject = vi.fn();
    render(<Sidebar {...sidebarProps} projects={projects} sessions={[]} collapsed={false} onCollapse={noop} onEditProject={onEditProject} onDeleteProject={onDeleteProject} />);

    const trigger = screen.getByRole('button', { name: '项目一 项目操作' });
    expect(Array.from(trigger.closest('.project-row')!.querySelectorAll('.project-inline-action')).map(button => button.getAttribute('aria-label'))).toEqual([
      '项目一 项目操作',
      '在 项目一 中新建会话',
    ]);
    fireEvent.click(trigger);
    const menu = screen.getByRole('menu', { name: '项目一 项目操作' });
    expect(menu.parentElement).toBe(document.body);
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(screen.getByRole('menuitem', { name: '编辑项目' }));
    expect(onEditProject).toHaveBeenCalledWith(projects[0]);
    expect(screen.queryByRole('menu')).toBeNull();

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole('menuitem', { name: '删除项目' }));
    expect(onDeleteProject).toHaveBeenCalledWith(projects[0]);
  });

  it('lists independent conversations once and keeps project conversations in their project', () => {
    const projects = [{ project_id: 'project-1', user_id: 'tester', name: '项目一', conversation_count: 1 }];
    const sessions = [{ session_id: 'session-1', project_id: 'project-1', title: '会话一' }, { session_id: 'standalone', project_id: null, title: '独立对话' }];
    const { container } = render(<Sidebar {...sidebarProps} projects={projects} sessions={sessions} activeId="session-1" collapsed={false} onCollapse={noop} />);
    expect(container.querySelector('.sidebar-recents')?.textContent).toContain('独立对话');
    expect(container.querySelector('.sidebar-recents')?.textContent).not.toContain('会话一');
    expect(container.querySelector('.project-session-list')?.textContent).toContain('会话一');
    expect(screen.getAllByText('独立对话')).toHaveLength(1);
    expect(screen.getAllByText('会话一')).toHaveLength(1);
  });

  it('creates tasks in the focused project and keeps the recent-task action independent', () => {
    const projects = [{ project_id: 'project-1', user_id: 'tester', name: '项目一', conversation_count: 1 }];
    const projectSession = { session_id: 'session-1', project_id: 'project-1', title: '项目会话' };
    const independentSession = { session_id: 'standalone', project_id: null, title: '独立会话' };
    const onNew = vi.fn();
    const onNewInProject = vi.fn();
    const { rerender } = render(<Sidebar {...sidebarProps} projects={projects} sessions={[]} onNew={onNew} onNewInProject={onNewInProject} collapsed={false} onCollapse={noop} />);

    expect(screen.getByText('暂无任务')).toBeTruthy();
    expect(screen.queryByText('点击上方按钮开始新分析')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '新建独立会话' }));
    fireEvent.click(screen.getByRole('button', { name: '新建任务' }));
    expect(onNew).toHaveBeenCalledTimes(2);
    expect(onNewInProject).not.toHaveBeenCalled();

    rerender(<Sidebar {...sidebarProps} projects={projects} sessions={[projectSession]} activeProjectId="project-1" onNew={onNew} onNewInProject={onNewInProject} collapsed={false} onCollapse={noop} />);
    fireEvent.click(screen.getByRole('button', { name: '新建任务' }));
    expect(onNewInProject).toHaveBeenLastCalledWith('project-1');

    rerender(<Sidebar {...sidebarProps} projects={projects} sessions={[projectSession]} activeId="session-1" onNew={onNew} onNewInProject={onNewInProject} collapsed={false} onCollapse={noop} />);
    fireEvent.click(screen.getByRole('button', { name: '新建任务' }));
    expect(onNewInProject).toHaveBeenLastCalledWith('project-1');

    rerender(<Sidebar {...sidebarProps} projects={projects} sessions={[independentSession]} activeId="standalone" onNew={onNew} onNewInProject={onNewInProject} collapsed={false} onCollapse={noop} />);
    fireEvent.click(screen.getByRole('button', { name: '新建任务' }));
    expect(onNew).toHaveBeenCalledTimes(3);
  });

  it('uses one right panel toggle for both expanded and collapsed states', () => {
    const onToggle = vi.fn();
    const onTab = vi.fn();
    const { rerender } = render(<GraphWorkspace {...graphProps} onTab={onTab} collapsed={false} onToggle={onToggle} />);

    expect(screen.getByText('图工作台')).toBeTruthy();
    expect(screen.getByRole('tab', { name: '图谱' }).getAttribute('aria-selected')).toBe('true');
    expect(document.querySelector('.workspace-tab-indicator')).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: '分析产物' }));
    expect(onTab).toHaveBeenCalledWith('Artifacts');
    expect(screen.queryByLabelText('关闭图工作台')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '收起右侧栏' }));
    expect(onToggle).toHaveBeenCalledOnce();

    rerender(<GraphWorkspace {...graphProps} onTab={onTab} collapsed onToggle={onToggle} />);
    const expandRight = screen.getByRole('button', { name: '展开右侧栏' });
    expect(expandRight).toBeTruthy();
    expect(expandRight.querySelector('path')?.getAttribute('d')).toBe('M15 4v16M10 9l-3 3 3 3');
    expect(screen.getByText('图工作台').classList.contains('workspace-title')).toBe(true);
    expect(screen.getByText('概览').closest('.workspace-panel-content')?.getAttribute('aria-hidden')).toBe('true');
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

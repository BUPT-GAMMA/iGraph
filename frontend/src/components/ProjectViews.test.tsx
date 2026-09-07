import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Dataset, Project } from '../types';
import { DeleteProjectDialog, ProjectDialog } from './ProjectDialogs';
import { ProjectWorkspace } from './ProjectWorkspace';
import { TopBar } from './TopBar';

afterEach(cleanup);

const project: Project = {
  project_id: 'project-1',
  user_id: 'tester',
  name: 'Graph Research',
  description: '图学习实验',
  conversation_count: 2,
};

describe('project dialogs', () => {
  it('validates and trims a new project before saving', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const { container } = render(<ProjectDialog onClose={vi.fn()} onSave={onSave} />);

    fireEvent.submit(container.querySelector('form')!);
    expect(screen.getByText('请输入项目名称')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('项目名称'), { target: { value: '  New Project  ' } });
    fireEvent.change(screen.getByLabelText('描述'), { target: { value: '  Description  ' } });
    fireEvent.submit(container.querySelector('form')!);

    await waitFor(() => expect(onSave).toHaveBeenCalledWith('New Project', 'Description'));
    expect(screen.getByText('保存中…')).toBeTruthy();
  });

  it('shows save failures and allows renaming ordinary projects', async () => {
    const onSave = vi.fn().mockRejectedValue(new Error('名称重复'));
    const existing = { ...project };
    const { container } = render(<ProjectDialog project={existing} onClose={vi.fn()} onSave={onSave} />);

    expect(screen.getByText('编辑项目')).toBeTruthy();
    expect((screen.getByLabelText('项目名称') as HTMLInputElement).disabled).toBe(false);
    fireEvent.submit(container.querySelector('form')!);
    await waitFor(() => expect(screen.getByText('名称重复')).toBeTruthy());
    expect(screen.getByRole('button', { name: '保存' })).toBeTruthy();
  });

  it('confirms the complete deletion scope and retries failures', async () => {
    const onConfirm = vi.fn().mockRejectedValueOnce(new Error('删除失败')).mockResolvedValueOnce(undefined);
    render(<DeleteProjectDialog project={project} onClose={vi.fn()} onConfirm={onConfirm} />);

    expect(screen.getByText(/该项目包含 2 个会话、0 个数据集/)).toBeTruthy();
    expect(screen.getByText(/停止相关任务.*训练模型/)).toBeTruthy();
    expect(screen.queryByRole('radio')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '删除项目及全部内容' }));
    await waitFor(() => expect(screen.getByText('删除失败')).toBeTruthy());
    expect(onConfirm).toHaveBeenNthCalledWith(1);

    fireEvent.click(screen.getByRole('button', { name: '删除项目及全部内容' }));
    await waitFor(() => expect(onConfirm).toHaveBeenNthCalledWith(2));
  });
});

describe('project workspace and top bar', () => {
  it('renders empty and populated datasets and forwards dataset actions', () => {
    const callbacks = {
      onUpload: vi.fn(),
      onSetDefault: vi.fn(),
      onRename: vi.fn(),
      onDelete: vi.fn(),
    };
    const datasets: Dataset[] = [
      { dataset_id: 'dataset-1', project_id: project.project_id, name: 'Cora', original_filename: 'cora.pt', file_type: 'pt', status: 'ready', is_default: true, metadata: { num_nodes: 1000, num_edges: 2000 } },
      { dataset_id: 'dataset-2', project_id: project.project_id, name: 'Notes', original_filename: 'notes.csv', file_type: 'csv', status: 'processing' },
    ];
    const { container, rerender } = render(<ProjectWorkspace project={project} datasets={[]} {...callbacks} />);

    expect(screen.getByText('还没有数据集')).toBeTruthy();
    expect(screen.queryByText('上传第一个数据集后，它会自动成为项目默认数据集。')).toBeNull();
    expect(screen.queryByText('未设置默认数据集')).toBeNull();
    expect(screen.queryByText('项目中的会话可选择这些数据源，并保持各自的运行时状态。')).toBeNull();
    rerender(<ProjectWorkspace project={project} datasets={datasets} {...callbacks} />);
    expect(screen.getByText('1,000 个节点 · 2,000 条边')).toBeTruthy();
    expect(screen.getByText('notes.csv')).toBeTruthy();
    expect(screen.queryByText('已设置默认数据集')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '设为默认' }));
    fireEvent.click(screen.getAllByTitle('重命名')[1]);
    fireEvent.click(screen.getAllByTitle('删除')[1]);
    expect(callbacks.onSetDefault).toHaveBeenCalledWith(datasets[1]);
    expect(callbacks.onRename).toHaveBeenCalledWith(datasets[1]);
    expect(callbacks.onDelete).toHaveBeenCalledWith(datasets[1]);

    const file = new File(['a,b'], 'graph.csv', { type: 'text/csv' });
    fireEvent.change(container.querySelector('input[type="file"]')!, { target: { files: [file] } });
    expect(callbacks.onUpload).toHaveBeenCalledWith(file);
  });

  it('omits project fallback copy and disables actions while busy', () => {
    const defaultProject = { ...project, description: '', conversation_count: 0 };
    const dataset: Dataset = { dataset_id: 'dataset-1', project_id: project.project_id, name: 'Cora', original_filename: 'cora.pt', file_type: 'pt', status: 'ready' };
    render(<ProjectWorkspace project={defaultProject} datasets={[dataset]} busy onUpload={vi.fn()} onSetDefault={vi.fn()} onRename={vi.fn()} onDelete={vi.fn()} />);

    expect(screen.queryByText('集中管理这个项目中的图分析会话。')).toBeNull();
    expect(screen.queryByText('项目工作区')).toBeNull();
    expect((screen.getByRole('button', { name: /添加数据集/ }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '设为默认' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTitle('重命名') as HTMLButtonElement).disabled).toBe(true);
  });

  it('opens the mobile navigation from the top bar', () => {
    const onMenu = vi.fn();
    render(<TopBar onMenu={onMenu} />);
    expect(screen.getByText('iGraph')).toBeTruthy();
    expect(screen.queryByText('图分析工作台')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '打开侧栏' }));
    expect(onMenu).toHaveBeenCalledOnce();
  });
});

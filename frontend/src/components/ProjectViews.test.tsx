import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Project } from '../types';
import { DeleteProjectDialog, ProjectDialog } from './ProjectDialogs';
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
describe('top bar', () => {
  it('opens the mobile navigation from the top bar', () => {
    const onMenu = vi.fn();
    render(<TopBar onMenu={onMenu} />);
    expect(screen.getByText('iGraph')).toBeTruthy();
    expect(screen.queryByText('图分析工作台')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '打开侧栏' }));
    expect(onMenu).toHaveBeenCalledOnce();
  });
});

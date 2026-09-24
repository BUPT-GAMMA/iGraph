import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ManagementPanel } from './ManagementPanel';

const { apiFetchMock } = vi.hoisted(() => ({ apiFetchMock: vi.fn() }));

vi.mock('../api', () => ({ apiFetch: apiFetchMock }));

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  cleanup();
  apiFetchMock.mockReset();
  vi.restoreAllMocks();
});

describe('ManagementPanel', () => {
  it('loads, filters, cancels, and deletes long-term memories', async () => {
    apiFetchMock
      .mockResolvedValueOnce(jsonResponse({
        items: [
          { memory_id: 'memory-1', key: 'Alpha', value: '图分类实验', category: 'fact', scope: 'long_term' },
          { memory_id: 'memory-2', key: 'Beta', value: '节点预测', category: 'preference', scope: 'project' },
        ],
      }))
      .mockResolvedValueOnce(jsonResponse({ success: true }));

    render(<ManagementPanel onClose={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('Alpha')).toBeTruthy());
    expect(screen.getByRole('separator', { name: '调整记忆与技能栏宽度' })).toBeTruthy();
    expect(apiFetchMock).toHaveBeenNthCalledWith(1, '/api/memory?scope=long_term&limit=200');
    fireEvent.change(screen.getByPlaceholderText('搜索记忆内容'), { target: { value: '图分类' } });
    expect(screen.getByText('Alpha')).toBeTruthy();
    expect(screen.queryByText('Beta')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '删除记忆' }));
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '取消' }));
    expect(apiFetchMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: '删除记忆' }));
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '删除' }));
    await waitFor(() => expect(screen.queryByText('Alpha')).toBeNull());
    expect(apiFetchMock).toHaveBeenNthCalledWith(2, '/api/memory/memory-1?scope=long_term', { method: 'DELETE' });
    expect(screen.getByText('暂无匹配的记忆')).toBeTruthy();
  });

  it('reports memory deletion and management loading failures', async () => {
    apiFetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [{ memory_id: 'memory/1', key: 'Important', value: 'Keep', scope: 'team scope' }] }))
      .mockResolvedValueOnce(jsonResponse({ detail: 'denied' }, 500));

    const { unmount } = render(<ManagementPanel onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('Important')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: '删除记忆' }));
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '删除' }));
    await waitFor(() => expect(screen.getByText('记忆删除失败')).toBeTruthy());
    expect(apiFetchMock).toHaveBeenLastCalledWith('/api/memory/memory%2F1?scope=team%20scope', { method: 'DELETE' });

    unmount();
    apiFetchMock.mockResolvedValueOnce(jsonResponse({ detail: '没有权限' }, 403));
    render(<ManagementPanel initialTab="skills" onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('没有权限')).toBeTruthy());
  });

  it('switches to skills, filters entries, opens details, and clears them when returning', async () => {
    apiFetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(jsonResponse({
        skills: [
          { name: 'Graph Audit', description: '检查图结构', source: 'builtin', risk_level: 'low', status: 'ready' },
          { name: 'Train Model', description: '训练模型', source: 'custom' },
        ],
      }))
      .mockResolvedValueOnce(jsonResponse({ skill: { content: '# Graph Audit' } }))
      .mockResolvedValueOnce(jsonResponse({ skill: { content: '# Graph Audit' } }))
      .mockResolvedValueOnce(jsonResponse({ items: [] }));

    render(<ManagementPanel onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('暂无匹配的记忆')).toBeTruthy());
    fireEvent.click(screen.getByRole('tab', { name: /技能/ }));

    await waitFor(() => expect(screen.getByText('Graph Audit')).toBeTruthy());
    expect(apiFetchMock).toHaveBeenNthCalledWith(2, '/api/skills?source=all');
    fireEvent.change(screen.getByPlaceholderText('搜索技能'), { target: { value: 'audit' } });
    expect(screen.queryByText('Train Model')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Graph Audit/ }));
    expect(screen.getByText('内置 · 就绪')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '关闭技能详情' }));
    await waitFor(() => expect(screen.queryByText('内置 · 就绪')).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: /Graph Audit/ }));
    fireEvent.click(screen.getByRole('tab', { name: /记忆/ }));
    await waitFor(() => expect(screen.getByText('暂无匹配的记忆')).toBeTruthy());
    await waitFor(() => expect(screen.queryByText('内置 · 就绪')).toBeNull());
    expect((screen.getByPlaceholderText('搜索记忆内容') as HTMLInputElement).value).toBe('');
  });

  it.each(['md', 'zip'])('uploads %s and deletes uploaded skills while keeping bundled skills protected', async extension => {
    apiFetchMock
      .mockResolvedValueOnce(jsonResponse({ items: [] }))
      .mockResolvedValueOnce(jsonResponse({ skills: [
        { name: 'bundled-skill', description: 'Built in', source: 'bundled' },
        { name: 'pending-skill', description: 'Pending', source: 'review_queue' },
      ] }))
      .mockResolvedValueOnce(jsonResponse({ success: true, name: 'uploaded-skill', source: 'review_queue' }))
      .mockResolvedValueOnce(jsonResponse({ skills: [
        { name: 'bundled-skill', description: 'Built in', source: 'bundled' },
        { name: 'uploaded-skill', description: 'Uploaded', source: 'review_queue' },
        { name: 'pending-skill', description: 'Pending', source: 'review_queue' },
      ] }))
      .mockResolvedValueOnce(jsonResponse({ skill: { content: '# Uploaded' } }))
      .mockResolvedValueOnce(jsonResponse({ success: true }))
      .mockResolvedValueOnce(jsonResponse({ skill: { content: '# Bundled' } }));

    render(<ManagementPanel onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('暂无匹配的记忆')).toBeTruthy());
    fireEvent.click(screen.getByRole('tab', { name: /技能/ }));
    await waitFor(() => expect(screen.getByText('pending-skill')).toBeTruthy());
    fireEvent.change(screen.getByLabelText('上传技能（.md / .zip）'), {
      target: { files: [new File(['skill'], `uploaded-skill.${extension}`, { type: extension === 'md' ? 'text/markdown' : 'application/zip' })] },
    });
    await waitFor(() => expect(screen.getByText('uploaded-skill')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: /uploaded-skill/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: '删除技能' })).toBeTruthy());
    expect(screen.getByRole('button', { name: '删除技能' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '删除技能' }));
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: '删除' }));
    await waitFor(() => expect(screen.queryByText('uploaded-skill')).toBeNull());
    expect(apiFetchMock).toHaveBeenNthCalledWith(6, '/api/skills/uploaded-skill?source=review_queue', { method: 'DELETE' });

    fireEvent.click(screen.getByRole('button', { name: /bundled-skill/ }));
    await waitFor(() => expect(screen.getAllByText('内置').length).toBeGreaterThan(0));
    expect(screen.queryByRole('button', { name: '删除技能' })).toBeNull();
  });
});

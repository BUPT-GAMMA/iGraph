import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { GraphExplorePanel } from './GraphExplorePanel';

afterEach(cleanup);

describe('GraphExplorePanel', () => {
  it('delegates a question to full-dataset tools and requests a visualized result', () => {
    const onQuery = vi.fn();
    render(<GraphExplorePanel schema={false} selectedId="" pending={false} onQuery={onQuery} />);
    fireEvent.change(screen.getByRole('textbox', { name: '图谱问题' }), { target: { value: '图学习相关论文' } });
    fireEvent.click(screen.getByRole('button', { name: '定位并显示' }));
    expect(onQuery).toHaveBeenCalledOnce();
    const prompt = onQuery.mock.calls[0][0];
    expect(prompt).toContain('图学习相关论文');
    expect(prompt).toContain('完整实例图');
    expect(prompt).toContain('search_node');
    expect(prompt).toContain('visualize_subgraph');
    expect(screen.getByRole('status').textContent).toContain('候选确认');
  });

  it('supports exact selected endpoints and weighted path queries without requiring sampled nodes', () => {
    const onQuery = vi.fn();
    render(<GraphExplorePanel schema={false} selectedId="selected-id" pending={false} onQuery={onQuery} />);
    fireEvent.click(screen.getByRole('button', { name: '最短路径' }));
    fireEvent.click(screen.getByRole('button', { name: '将选中节点设为起点' }));
    fireEvent.change(screen.getByRole('textbox', { name: /终点节点/ }), { target: { value: 'node-not-in-preview' } });
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '查找并高亮路径' }));
    const prompt = onQuery.mock.calls[0][0];
    expect(prompt).toContain('"source":"selected-id"');
    expect(prompt).toContain('"target":"node-not-in-preview"');
    expect(prompt).toContain('"use_weight":true');
    expect(prompt).toContain('highlights.paths');
    expect(prompt).toContain('搜索上限');
  });

  it('blocks instance queries in a schema view and during an active run', () => {
    const onQuery = vi.fn();
    const { rerender } = render(<GraphExplorePanel schema pending={false} selectedId="" onQuery={onQuery} />);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true);
    expect(screen.getByText(/当前视图展示节点类型与字段/)).toBeTruthy();
    rerender(<GraphExplorePanel schema={false} pending selectedId="" onQuery={onQuery} />);
    expect((screen.getByRole('button', { name: '正在对话中处理…' }) as HTMLButtonElement).disabled).toBe(true);
    expect(onQuery).not.toHaveBeenCalled();
  });
});

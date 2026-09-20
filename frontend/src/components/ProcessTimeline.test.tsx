import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ProcessTimeline } from './ProcessTimeline';

afterEach(cleanup);

describe('ProcessTimeline', () => {
  it('groups reasoning and tools under one lightweight expandable summary', () => {
    render(<ProcessTimeline streaming thinkingDurationMs={12000} items={[
      { id: 'r1', kind: 'reasoning', sequence: 1, content: '先检查图结构' },
      { id: 't1', kind: 'tool', sequence: 2, tool: { tool: 'degree_analysis', done: true, status: 'completed' } },
    ]} />);

    expect(screen.getByText('思考中 · 12s')).toBeTruthy();
    expect(screen.getByText('先检查图结构')).toBeTruthy();
    expect(screen.getByText('degree_analysis')).toBeTruthy();
    expect(document.querySelectorAll('.process-panel')).toHaveLength(1);
    expect(document.querySelectorAll('.process-item')).toHaveLength(2);
  });
});


it('preserves live training progress after the agent turn has completed', () => {
  render(<ProcessTimeline compact items={[
    { id: 't', kind: 'tool', sequence: 1, tool: { tool: 'train', call_id: 't', done: true, status: 'completed' } },
  ]} tools={[{ tool: 'train', call_id: 't', job_id: 'job', done: false, progress: { epoch: 2, epochs: 10 } }]} />);
  expect(screen.getByRole('status', { name: 'train：轮次 2/10' })).toBeTruthy();
  expect(screen.getByText('1 项运行中')).toBeTruthy();
  expect((document.querySelector('.process-panel') as HTMLDetailsElement).open).toBe(true);
});

it('shows structured failure details without counting failed tools as running', () => {
  render(<ProcessTimeline compact items={[
    { id: 't', kind: 'tool', sequence: 1, tool: { tool: 'workspace_bash', status: 'failed', result: { error: { message: '沙箱不可用' } } } },
  ]} />);
  expect(screen.getByText('沙箱不可用')).toBeTruthy();
  expect(screen.queryByText('1 项运行中')).toBeNull();
});

it.each([['failed', '失败'], ['cancelled', '已取消']])('overrides completed submission with live %s job status', (status, label) => {
  render(<ProcessTimeline items={[{ id: 't', kind: 'tool', sequence: 1, tool: { tool: 'train', call_id: 't', done: true, status: 'completed' } }]}
    tools={[{ tool: 'train', call_id: 't', done: true, result: { job_id: 'job', job_status: status, error: 'Training stopped' } }]} />);
  expect(screen.getByText(label)).toBeTruthy();
  expect(screen.queryByText('完成')).toBeNull();
  expect(screen.queryByText('1 项运行中')).toBeNull();
});


it('shows live reasoning before tools and preserves disclosure choices while deltas arrive', () => {
  const first = { id: 'reasoning:0', kind: 'reasoning' as const, sequence: 0, content: '先检查' };
  const { rerender } = render(<ProcessTimeline compact streaming items={[first]} />);
  const panel = document.querySelector('.process-panel') as HTMLDetailsElement;
  expect(panel.open).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '展开思考' }));
  rerender(<ProcessTimeline compact streaming items={[{ ...first, content: '先检查图结构' }]} />);
  expect(screen.getByRole('button', { name: '收起思考' }).getAttribute('aria-expanded')).toBe('true');
  expect(screen.getByText('先检查图结构')).toBeTruthy();
  expect(screen.getByRole('button', { name: '收起思考' }).getAttribute('title')).toBeNull();
  panel.open = false;
  fireEvent(panel, new Event('toggle'));
  rerender(<ProcessTimeline compact streaming items={[{ ...first, content: '先检查图结构和连通性' },
    { id: 't', kind: 'tool', sequence: 1, tool: { tool: 'get_graph_info' } }]} />);
  expect(panel.open).toBe(false);
  expect(screen.getByRole('button', { name: '收起思考' }).getAttribute('aria-expanded')).toBe('true');
});


it('displays Chinese labels alongside exact tool identifiers and keeps unknown tools readable', () => {
  const names = ['global_metrics', 'visualize_subgraph', 'workspace_bash', 'get_skill_instructions', 'custom_external_tool'];
  const { container } = render(<ProcessTimeline items={names.map((name, sequence) => ({
    id: name, kind: 'tool', sequence, tool: { tool: name, done: true, status: 'completed' },
  }))} />);
  expect([...container.querySelectorAll('.process-tool-name')].map(node => node.textContent)).toEqual([
    '分析全局指标', '可视化子图', '执行工作区命令', '读取技能指引', 'custom_external_tool',
  ]);
  expect([...container.querySelectorAll('.process-tool-code')].map(node => node.textContent)).toEqual(names.slice(0, -1));
  expect(screen.getAllByText('完成')).toHaveLength(5);
});

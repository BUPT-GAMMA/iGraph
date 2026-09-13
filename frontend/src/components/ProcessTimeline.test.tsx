import { cleanup, render, screen } from '@testing-library/react';
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

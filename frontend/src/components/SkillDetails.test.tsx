import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { SkillDetails } from './SkillDetails';
const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('../api', () => ({ apiFetch }));
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
});
afterEach(() => { cleanup(); apiFetch.mockReset(); });
const response = (content: string) => new Response(JSON.stringify({ skill: { content } }));
it('loads complete content and closes the reader', async () => {
  apiFetch.mockResolvedValueOnce(response('# 操作步骤\n\n完整内容末尾\n<script>alert(1)</script>'));
  const onClose = vi.fn();
  render(<SkillDetails skill={{ name: 'graph-helper', source: 'user' }} onClose={onClose} />);
  expect(screen.getByText('正在加载 Skill 内容…')).toBeTruthy();
  await waitFor(() => expect(screen.getByText(/完整内容末尾/)).toBeTruthy());
  expect(document.querySelector('dialog script')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '关闭 Skill 详情' }));
  expect(onClose).toHaveBeenCalledOnce();
});
it('shows a readable error and can retry', async () => {
  apiFetch.mockResolvedValueOnce(new Response(JSON.stringify({ detail: { detail: 'Skill 不存在' } }), { status: 404 }))
    .mockResolvedValueOnce(response('重新读取成功'));
  render(<SkillDetails skill={{ name: 'helper' }} onClose={vi.fn()} />);
  await waitFor(() => expect(screen.getByText('Skill 不存在')).toBeTruthy());
  fireEvent.click(screen.getByRole('button', { name: '重新加载' }));
  await waitFor(() => expect(screen.getByText('重新读取成功')).toBeTruthy());
});
it('does not show an old response after changing the selected skill', async () => {
  let resolveOld!: (value: Response) => void;
  apiFetch.mockReturnValueOnce(new Promise<Response>(resolve => { resolveOld = resolve; }))
    .mockResolvedValueOnce(response('第二个 Skill 正文'));
  const { rerender } = render(<SkillDetails skill={{ name: 'first' }} onClose={vi.fn()} />);
  rerender(<SkillDetails skill={{ name: 'second' }} onClose={vi.fn()} />);
  await waitFor(() => expect(screen.getByText('第二个 Skill 正文')).toBeTruthy());
  resolveOld(response('过期正文'));
  await waitFor(() => expect(screen.queryByText('过期正文')).toBeNull());
});

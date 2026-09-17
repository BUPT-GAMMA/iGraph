import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ChatPane } from './ChatPane';
import type { Message } from '../types';

const noop = () => {};
const props = { value: '独立草稿', model: 'm', models: ['m'], onChange: noop, onModelChange: noop, onDataset: noop,
  onSend: noop, onUpload: noop, onRemoveAttachment: noop, attachments: [], loading: false, uploading: false,
  disabled: false, hasGraph: false, onOpenGraph: noop, interaction: { kind: 'composer' as const } };
const messages: Message[] = [
  { id: 'u1', run_id: 'r1', role: 'user', content: 'Earlier question' },
  { id: 'a1', run_id: 'r1', role: 'assistant', content: 'Earlier answer', run_status: 'completed' },
  { id: 'u2', run_id: 'r2', role: 'user', content: 'Latest question', attachments: [{ file_token: 't', filename: 'graph.csv' }] },
  { id: 'a2', run_id: 'r2', role: 'assistant', content: 'Latest answer', run_status: 'completed' },
];
afterEach(cleanup);

it.each(['completed', 'cancelled'])('edits only the latest %s turn and preserves attachments and the composer', status => {
  const onEditLatest = vi.fn();
  const { container } = render(<ChatPane {...props} onEditLatest={onEditLatest} messages={[...messages.slice(0, -1), { ...messages[3], run_status: status }]} />);
  expect(screen.getAllByRole('button', { name: '修改提示词' })).toHaveLength(1);
  const row = container.querySelectorAll<HTMLElement>('.message-row.user')[1];
  fireEvent.click(within(row).getByRole('button', { name: '修改提示词' }));
  const editor = screen.getByRole('textbox', { name: '修改本轮提示词' });
  expect((editor as HTMLTextAreaElement).value).toBe('Latest question');
  expect(document.activeElement).toBe(editor);
  expect((editor as HTMLTextAreaElement).selectionStart).toBe('Latest question'.length);
  expect((editor as HTMLTextAreaElement).selectionEnd).toBe('Latest question'.length);
  expect(within(row).getByText('graph.csv')).toBeTruthy();
  fireEvent.change(editor, { target: { value: '  New question\nWith detail  ' } });
  fireEvent.click(screen.getByRole('button', { name: '发送修改后的提示词' }));
  expect(onEditLatest).toHaveBeenCalledWith(messages[2], 'r2', 'New question\nWith detail');
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('独立草稿');
});

it('cancels local edits and prevents blank or IME submissions', () => {
  const onEditLatest = vi.fn();
  render(<ChatPane {...props} messages={messages} onEditLatest={onEditLatest} />);
  fireEvent.click(screen.getByRole('button', { name: '修改提示词' }));
  const editor = screen.getByRole('textbox', { name: '修改本轮提示词' });
  fireEvent.keyDown(editor, { key: 'Enter', ctrlKey: true, isComposing: true });
  expect(onEditLatest).not.toHaveBeenCalled();
  fireEvent.change(editor, { target: { value: '   ' } });
  expect((screen.getByRole('button', { name: '发送修改后的提示词' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '取消' }));
  expect(screen.queryByRole('textbox', { name: '修改本轮提示词' })).toBeNull();
  expect(screen.getByText('Latest question')).toBeTruthy();
  expect(onEditLatest).not.toHaveBeenCalled();
});

it('hides editing while streaming, stopping, uploading or awaiting approval', () => {
  const onEditLatest = vi.fn();
  const { rerender } = render(<ChatPane {...props} messages={messages} onEditLatest={onEditLatest} loading />);
  expect(screen.queryByRole('button', { name: '修改提示词' })).toBeNull();
  for (const gate of [{ sendBlocked: true }, { uploading: true }, { disabled: true }]) {
    rerender(<ChatPane {...props} {...gate} messages={messages} onEditLatest={onEditLatest} />);
    expect(screen.queryByRole('button', { name: '修改提示词' })).toBeNull();
  }
  for (const run_status of ['streaming', 'paused', 'failed']) {
    rerender(<ChatPane {...props} messages={[...messages.slice(0, -1), { ...messages[3], run_status }]} onEditLatest={onEditLatest} />);
    expect(screen.queryByRole('button', { name: '修改提示词' })).toBeNull();
  }
});

it('closes the editor when the conversation changes', () => {
  const { rerender } = render(<ChatPane {...props} messages={messages} onEditLatest={noop} conversationKey="A" />);
  fireEvent.click(screen.getByRole('button', { name: '修改提示词' }));
  rerender(<ChatPane {...props} messages={messages} onEditLatest={noop} conversationKey="B" />);
  expect(screen.queryByRole('textbox', { name: '修改本轮提示词' })).toBeNull();
});

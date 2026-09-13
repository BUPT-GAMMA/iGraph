import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Composer } from './Composer';
import { ChatPane } from './ChatPane';
import { Sidebar } from './Sidebar';
import { ManagementPanel } from './ManagementPanel';
import { apiFetch, setUserId } from '../api';
import { answerVersions } from '../answerVersions';
import type { Message } from '../types';
vi.mock('../api', async () => ({ ...await vi.importActual('../api'), apiFetch: vi.fn() }));
const noop = () => {};
const composer = { value: '', model: 'model', models: ['model'], onChange: noop, onModelChange: noop, onDataset: noop,
  onSend: noop, onUpload: noop, onRemoveAttachment: noop, attachments: [], loading: false, uploading: false, disabled: false };
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
beforeEach(() => { localStorage.clear(); setUserId('a'); });

it('does not submit IME candidate confirmation or Enter during an upload', () => {
  const onSend = vi.fn();
  const { rerender } = render(<Composer {...composer} onSend={onSend} />);
  const input = screen.getByRole('textbox');
  fireEvent.compositionStart(input); fireEvent.keyDown(input, { key: 'Enter' });
  fireEvent.compositionEnd(input); fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
  fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 });
  expect(onSend).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: 'Enter' }); expect(onSend).toHaveBeenCalledOnce();
  rerender(<Composer {...composer} onSend={onSend} uploading />);
  fireEvent.keyDown(input, { key: 'Enter' }); expect(onSend).toHaveBeenCalledOnce();
});
it('shows confirming at 100%, allows removal, and exposes failed-file retry', () => {
  const onRemoveAttachment = vi.fn(), onRetryAttachment = vi.fn();
  const attachment = { localId: 'x', filename: 'x.csv', file: new File(['x'], 'x.csv'), status: 'uploading' as const, progress: 100 };
  const { rerender } = render(<Composer {...composer} uploading attachments={[attachment]} onRemoveAttachment={onRemoveAttachment} />);
  expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('100');
  expect(screen.getByText('正在确认')).toBeTruthy();
  expect((screen.getByRole('button', { name: '发送' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '移除 x.csv' })); expect(onRemoveAttachment).toHaveBeenCalledWith('x');
  rerender(<Composer {...composer} attachments={[{ ...attachment, status: 'failed' }]} onRetryAttachment={onRetryAttachment} />);
  fireEvent.click(screen.getByRole('button', { name: '重试 x.csv' })); expect(onRetryAttachment).toHaveBeenCalledWith('x');
});
const sidebar = { projects: [], sessions: [{ session_id: '1', title: 'One' }, { session_id: '2', title: 'Two' }], userId: 'a', dark: false,
  onTheme: noop, onMemory: noop, onSkills: noop, mobileOpen: false, onCloseMobile: noop, onNew: noop, onNewProject: noop,
  onNewInProject: noop, onEditProject: noop, onDeleteProject: noop, onSelectProject: noop, onSelect: noop, onDelete: noop, collapsed: false, onCollapse: noop };
it('pins inside the existing group, persists, and separates accounts', () => {
  const { container, rerender } = render(<Sidebar {...sidebar} />);
  fireEvent.click(screen.getByRole('button', { name: '置顶 Two' }));
  expect(container.querySelector('.recent-session-name')?.textContent).toBe('Two');
  setUserId('b'); rerender(<Sidebar {...sidebar} userId="b" />);
  expect(container.querySelector('.recent-session-name')?.textContent).toBe('One');
  setUserId('a'); rerender(<Sidebar {...sidebar} />);
  expect(container.querySelector('.recent-session-name')?.textContent).toBe('Two');
});
it('ignores damaged pin data and does not move project sessions into recents', () => {
  localStorage.setItem('igraph-pinned-sessions:v1:a', '{invalid');
  const { container } = render(<Sidebar {...sidebar} activeProjectId="p" projects={[{ project_id: 'p', user_id: 'a', name: 'Project' }]}
    sessions={[...sidebar.sessions, { session_id: '3', title: 'Project task', project_id: 'p' }]} />);
  fireEvent.click(screen.getByRole('button', { name: '置顶 Project task' }));
  expect(container.querySelector('.sidebar-recents')?.textContent).not.toContain('Project task');
});
it('paginates skills and searches across all pages; sidebar selection changes tabs', async () => {
  vi.mocked(apiFetch).mockImplementation(async path => new Response(JSON.stringify(String(path).includes('memory')
    ? { items: [] } : { skills: Array.from({ length: 12 }, (_, i) => ({ name: 'Skill-' + i, description: 'Details' })) })));
  const { container, rerender } = render(<ManagementPanel initialTab="skills" onClose={noop} />);
  await waitFor(() => expect(container.querySelectorAll('.skill-row')).toHaveLength(9));
  fireEvent.click(screen.getByRole('button', { name: '下一页' }));
  expect(container.querySelectorAll('.skill-row')).toHaveLength(3);
  fireEvent.change(screen.getByPlaceholderText('搜索技能'), { target: { value: 'Skill-0' } });
  expect(container.querySelectorAll('.skill-row')).toHaveLength(1);
  rerender(<ManagementPanel initialTab="memory" onClose={noop} />);
  await screen.findByPlaceholderText('搜索记忆内容');
});
it('switches only explicit answer versions and copies the selected answer', async () => {
  const copy = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } });
  const first: Message = { id: 'a1', run_id: 'r1', role: 'assistant', content: 'First', tools: [{ tool: 'first_tool', done: true }] };
  const last: Message = { id: 'a2', run_id: 'r2', regenerate_of: 'r1', role: 'assistant', content: 'Second', tools: [{ tool: 'second_tool', done: true }] };
  const { container } = render(<ChatPane {...composer} hasGraph={false} onOpenGraph={noop} interaction={{ kind: 'composer' }} messages={[first, last]} />);
  fireEvent.click(screen.getByRole('button', { name: '上一个回答版本' }));
  const card = container.querySelectorAll('.message-row')[1] as HTMLElement;
  expect(card.querySelector('.message-content')?.textContent).toBe('First');
  expect(card.textContent).toContain('first_tool'); expect(card.textContent).not.toContain('second_tool');
  fireEvent.click(within(card).getByRole('button', { name: '复制' }));
  await waitFor(() => expect(copy).toHaveBeenCalledWith('First'));
  expect(answerVersions({ ...last, regenerate_of: null }, [first, last])).toHaveLength(1);
});
it('passes attachments with edit and shows them before message text', () => {
  const onEditMessage = vi.fn();
  const message: Message = { id: 'u', role: 'user', content: 'Question', attachments: [{ file_token: 't', filename: 'data.csv' }] };
  const { container } = render(<ChatPane {...composer} hasGraph={false} onOpenGraph={noop} interaction={{ kind: 'composer' }}
    messages={[message]} onEditMessage={onEditMessage} />);
  fireEvent.click(screen.getByRole('button', { name: '编辑并重发' })); expect(onEditMessage).toHaveBeenCalledWith(message);
  const body = container.querySelector('.message-body')!;
  const children = Array.from(body.children);
  expect(children.indexOf(body.querySelector('.message-attachments')!)).toBeLessThan(children.indexOf(body.querySelector('.message-content')!));
});

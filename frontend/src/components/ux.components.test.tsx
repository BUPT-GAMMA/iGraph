import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });
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
  onTheme: noop, onMemory: noop, onSkills: noop, onCreateGraph: noop, mobileOpen: false, onCloseMobile: noop, onNew: noop, onNewProject: noop,
  onNewInProject: noop, onProjectDatasets: noop, onEditProject: noop, onDeleteProject: noop, onSelect: noop, onDelete: noop, collapsed: false, onCollapse: noop };
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
  await waitFor(() => expect(within(card).getByRole('button', { name: '已复制' })).toBeTruthy());
  expect(copy).toHaveBeenCalledWith('First');
  fireEvent.click(screen.getByRole('button', { name: '下一个回答版本' }));
  expect(within(card).queryByRole('button', { name: '已复制' })).toBeNull();
  fireEvent.click(within(card).getByRole('button', { name: '复制' }));
  await waitFor(() => expect(copy).toHaveBeenLastCalledWith('Second'));
  expect(answerVersions({ ...last, regenerate_of: null }, [first, last])).toHaveLength(1);
});
it('keeps attachments above the user bubble and actions outside it', () => {
  const message: Message = { id: 'u', role: 'user', content: 'Question', attachments: [{ file_token: 't', filename: 'data.csv' }] };
  const { container } = render(<ChatPane {...composer} hasGraph={false} onOpenGraph={noop} interaction={{ kind: 'composer' }}
    messages={[message]} />);
  expect(screen.queryByRole('button', { name: '编辑并重发' })).toBeNull();
  const bubble = container.querySelector('.message-bubble')!;
  expect(bubble.contains(screen.getByRole('button', { name: '复制' }))).toBe(false);
  // Attachment chips are a sibling above the bubble, not part of the sent text.
  const attachments = container.querySelector('.message-attachments')!;
  expect(bubble.contains(attachments)).toBe(false);
  const body = container.querySelector('.message-body')!;
  const children = Array.from(body.children);
  expect(children.indexOf(attachments)).toBeLessThan(children.indexOf(bubble));
});

it('renders agent deliverables as downloadable chips below the assistant message', () => {
  const message: Message = { id: 'a', role: 'assistant', content: 'Report ready',
    artifacts: [{ path: 'Derived/report.csv', filename: 'report.csv', size: 2048 }] };
  const { container } = render(<ChatPane {...composer} hasGraph={false} onOpenGraph={noop} interaction={{ kind: 'composer' }}
    artifactSessionId="S" messages={[message]} />);
  const chip = screen.getByRole('button', { name: '下载 report.csv' });
  expect(chip).toBeTruthy();
  expect(chip.textContent).toContain('2.0 KB');
  const body = container.querySelector('.message-body')!;
  const children = Array.from(body.children);
  const artifacts = container.querySelector('.message-artifacts')!;
  expect(children.indexOf(artifacts)).toBeGreaterThan(children.indexOf(container.querySelector('.message-content')!));
});

it('shows a temporary copy check only on the copied message', async () => {
  vi.useFakeTimers();
  const copy = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } });
  const { container } = render(<ChatPane {...composer} hasGraph={false} onOpenGraph={noop} interaction={{ kind: 'composer' }} messages={[
    { id: 'u', role: 'user', content: 'Question' },
    { id: 'a', role: 'assistant', content: '**Answer**\nDetails' },
  ]} />);
  const [user, assistant] = Array.from(container.querySelectorAll<HTMLElement>('.message-row'));
  await act(async () => { fireEvent.click(within(assistant).getByRole('button', { name: '复制' })); });
  expect(copy).toHaveBeenCalledWith('**Answer**\nDetails');
  const copied = within(assistant).getByRole('button', { name: '已复制' });
  expect(copied.textContent).toBe('');
  expect(copied.querySelector('svg')).not.toBeNull();
  expect(within(user).getByRole('button', { name: '复制' })).toBeTruthy();
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(within(assistant).getByRole('button', { name: '复制' })).toBeTruthy();
});

it('shows the assistant copy action only after the answer is complete', () => {
  const user: Message = { id: 'u', role: 'user', content: 'Question' };
  const partial: Message = { id: 'a', role: 'assistant', content: 'Partial answer', streaming: true, run_status: 'streaming' };
  const props = { ...composer, hasGraph: false, onOpenGraph: noop, interaction: { kind: 'composer' as const } };
  const { container, rerender } = render(<ChatPane {...props} loading messages={[user, partial]} />);
  let [userRow, assistantRow] = Array.from(container.querySelectorAll<HTMLElement>('.message-row'));
  expect(within(userRow).getByRole('button', { name: '复制' })).toBeTruthy();
  expect(within(assistantRow).queryByRole('button', { name: '复制' })).toBeNull();

  rerender(<ChatPane {...props} messages={[user, { ...partial, streaming: false, run_status: 'awaiting_feedback' }]} />);
  assistantRow = container.querySelectorAll<HTMLElement>('.message-row')[1];
  expect(within(assistantRow).queryByRole('button', { name: '复制' })).toBeNull();

  rerender(<ChatPane {...props} messages={[user, { ...partial, streaming: false, run_status: 'completed', content: 'Complete answer' }]} />);
  assistantRow = container.querySelectorAll<HTMLElement>('.message-row')[1];
  expect(within(assistantRow).getByRole('button', { name: '复制' })).toBeTruthy();
});

it('allows retry after a clipboard failure without falsely reporting success', async () => {
  const copy = vi.fn().mockRejectedValueOnce(new Error('Clipboard denied')).mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } });
  render(<ChatPane {...composer} hasGraph={false} onOpenGraph={noop} interaction={{ kind: 'composer' }}
    messages={[{ id: 'a', role: 'assistant', content: 'Answer' }]} />);
  fireEvent.click(screen.getByRole('button', { name: '复制' }));
  expect((await screen.findByRole('alert')).textContent).toContain('复制失败');
  expect(screen.queryByRole('button', { name: '已复制' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '复制' }));
  await screen.findByRole('button', { name: '已复制' });
  expect(screen.queryByRole('alert')).toBeNull();
});

it('ignores a pending copy result after the displayed message changes', async () => {
  let finishCopy!: () => void;
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
    writeText: () => new Promise<void>(resolve => { finishCopy = resolve; }),
  } });
  const props = { ...composer, hasGraph: false, onOpenGraph: noop, interaction: { kind: 'composer' as const } };
  const { rerender } = render(<ChatPane {...props} messages={[{ id: 'a', role: 'assistant', content: 'Earlier content' }]} />);
  fireEvent.click(screen.getByRole('button', { name: '复制' }));
  rerender(<ChatPane {...props} messages={[{ id: 'a', role: 'assistant', content: 'Current content' }]} />);
  await act(async () => { finishCopy(); });
  expect(screen.queryByRole('button', { name: '已复制' })).toBeNull();
  expect(screen.getByRole('button', { name: '复制' })).toBeTruthy();
});

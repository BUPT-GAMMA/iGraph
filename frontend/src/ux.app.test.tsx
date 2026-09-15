import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
vi.mock('react-dom/client', () => ({ createRoot: () => ({ render: () => {} }) }));
vi.mock('./components/GraphWorkspace', () => ({ GraphWorkspace: () => null }));
vi.mock('./api', async () => ({ ...await vi.importActual('./api'),
  validateUser: vi.fn(), createProject: vi.fn(), listProjectConversations: vi.fn(), listProjects: vi.fn(), listSessions: vi.fn(), listCustomModels: vi.fn(),
  listProjectDatasets: vi.fn(), activateSession: vi.fn(), getCapabilities: vi.fn(), createSession: vi.fn(),
  uploadProjectDataset: vi.fn(), uploadFile: vi.fn(), streamChat: vi.fn(), visualizeGraph: vi.fn(), getRunControl: vi.fn(),
}));
import App from './main';
import { USER_KEY, activateSession, createProject, createSession, listProjectConversations, getCapabilities, getRunControl, listCustomModels, listProjectDatasets,
  listProjects, listSessions, streamChat, uploadFile, uploadProjectDataset, validateUser, visualizeGraph } from './api';
import { composerStorageKey, readComposerDraft, writeComposerDraft } from './composerDrafts';
const placeholder = '今天帮你分析什么？粘贴边列表，或直接用自然语言提问';
beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear(); sessionStorage.clear(); localStorage.setItem(USER_KEY, 'ux-user');
  window.history.replaceState({}, '', '/?session=A');
  vi.mocked(validateUser).mockResolvedValue({ valid: true, default_model: 'qwen3.6-plus' });
  vi.mocked(listSessions).mockResolvedValue({ sessions: [{ session_id: 'A', title: 'Session A' }] });
  vi.mocked(listProjects).mockResolvedValue({ projects: [] }); vi.mocked(listCustomModels).mockResolvedValue({ models: [] });
  vi.mocked(listProjectDatasets).mockResolvedValue({ project_id: null, datasets: [] });
  vi.mocked(activateSession).mockResolvedValue({ model: 'qwen3.6-plus', has_graph: false, messages: [], active_run: null });
  vi.mocked(getCapabilities).mockResolvedValue({ run_cancel_v1: false, job_cancel_v1: false });
  vi.mocked(visualizeGraph).mockRejectedValue(new Error('no graph'));
  vi.mocked(getRunControl).mockResolvedValue({ run_id: null, start_request_id: null, registration_status: 'not_seen',
    cancel_status: 'none', execution_stopped: true, terminal_persisted: true, can_send: true, revision: 0, active_run: null });
  vi.mocked(createSession).mockResolvedValue({ success: true, session_id: 'created', model: 'qwen3.6-plus', project_id: null });
  vi.mocked(uploadFile).mockResolvedValue({ success: true, filename: 'data.pt', file_token: 'token' });
  vi.mocked(streamChat).mockImplementation(async (_s, _m, _a, _h, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true });
  }));
});
afterEach(cleanup);
async function mount() { const view = render(<App />); await screen.findByPlaceholderText(placeholder); return view; }
function choose(container: HTMLElement, filename = 'data.pt') {
  fireEvent.change(container.querySelector('input[type="file"][accept*=".sql"]')!, { target: { files: [new File(['data'], filename)] } });
}
it('starts uploading immediately, waits for confirmation, then sends its returned token', async () => {
  let finish!: (value: any) => void;
  vi.mocked(uploadFile).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const { container } = await mount(); choose(container);
  await waitFor(() => expect(uploadFile).toHaveBeenCalledOnce());
  const input = screen.getByPlaceholderText(placeholder);
  fireEvent.change(input, { target: { value: '分析附件' } });
  act(() => vi.mocked(uploadFile).mock.calls[0][3]?.(100));
  expect(screen.getByText('正在确认')).toBeTruthy();
  fireEvent.keyDown(input, { key: 'Enter' }); expect(streamChat).not.toHaveBeenCalled();
  await act(async () => finish({ success: true, filename: 'data.pt', file_token: 'server-token' }));
  fireEvent.keyDown(input, { key: 'Enter' });
  await waitFor(() => expect(streamChat).toHaveBeenCalledOnce());
  expect(vi.mocked(streamChat).mock.calls[0].slice(0, 3)).toEqual(['A', '分析附件', [{ filename: 'data.pt', file_token: 'server-token' }]]);
});
it('retries a failed file without requiring reselection', async () => {
  vi.mocked(uploadFile).mockRejectedValueOnce(new Error('网络中断')).mockResolvedValueOnce({ success: true, filename: 'data.pt', file_token: 'retried' });
  const { container } = await mount(); choose(container);
  fireEvent.click(await screen.findByRole('button', { name: '重试 data.pt' }));
  await waitFor(() => expect(uploadFile).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByRole('button', { name: '重试 data.pt' })).toBeNull());
  expect(vi.mocked(uploadFile).mock.calls[0][1]).toBe(vi.mocked(uploadFile).mock.calls[1][1]);
});
it('removing a pending file aborts its upload and ignores late success', async () => {
  let finish!: (value: any) => void;
  vi.mocked(uploadFile).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const { container } = await mount(); choose(container);
  await waitFor(() => expect(uploadFile).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole('button', { name: '移除 data.pt' }));
  expect(vi.mocked(uploadFile).mock.calls[0][2]?.aborted).toBe(true);
  await act(async () => finish({ success: true, filename: 'data.pt', file_token: 'late' }));
  expect(screen.queryByRole('button', { name: '移除 data.pt' })).toBeNull();
});
it('aborts account A uploads and rejects their late result after switching accounts', async () => {
  let finish!: (value: any) => void;
  vi.mocked(uploadFile).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const { container } = await mount(); choose(container, 'private.pt');
  await waitFor(() => expect(uploadFile).toHaveBeenCalledOnce());
  act(() => {
    localStorage.setItem(USER_KEY, 'next-account');
    window.dispatchEvent(new StorageEvent('storage', { key: USER_KEY, storageArea: localStorage }));
  });
  await screen.findByRole('button', { name: '账户：next-account' });
  expect(vi.mocked(uploadFile).mock.calls[0][2]?.aborted).toBe(true);
  await act(async () => finish({ success: true, filename: 'private.pt', file_token: 'private-token' }));
  expect(container.textContent).not.toContain('private.pt');
});
it('uploads a new-task file before the first message without starting an agent run', async () => {
  window.history.replaceState({}, '', '/?new=1');
  const { container } = render(<App />);
  await screen.findByText('Session A');
  fireEvent.click(screen.getByRole('button', { name: '新建独立会话' }));
  await screen.findByPlaceholderText(placeholder); choose(container);
  await waitFor(() => expect(createSession).toHaveBeenCalledOnce());
  await waitFor(() => expect(uploadFile).toHaveBeenCalledOnce());
  expect(vi.mocked(uploadFile).mock.calls[0][0]).toBe('created');
  expect(streamChat).not.toHaveBeenCalled();
  const input = screen.getByPlaceholderText(placeholder);
  fireEvent.change(input, { target: { value: '开始分析' } });
  await waitFor(() => expect((screen.getByRole('button', { name: '发送' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.keyDown(input, { key: 'Enter' });
  await waitFor(() => expect(streamChat).toHaveBeenCalledOnce());
  expect(vi.mocked(streamChat).mock.calls[0][2]).toEqual([{ filename: 'data.pt', file_token: 'token' }]);
  expect(uploadFile).toHaveBeenCalledOnce();
});
it('restores an ordinary composer draft and attachments after remount, then clears it on send', async () => {
  const first = await mount();
  fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value: '未发送草稿' } });
  choose(first.container);
  await waitFor(() => expect(readComposerDraft('ux-user', 'A')?.attachments).toHaveLength(1));
  first.unmount();
  await mount();
  expect((screen.getByPlaceholderText(placeholder) as HTMLTextAreaElement).value).toBe('未发送草稿');
  expect(screen.getByRole('button', { name: '移除 data.pt' })).toBeTruthy();
  fireEvent.keyDown(screen.getByPlaceholderText(placeholder), { key: 'Enter' });
  await waitFor(() => expect(streamChat).toHaveBeenCalledOnce());
  await waitFor(() => expect(localStorage.getItem(composerStorageKey('ux-user', 'A'))).toBeNull());
});
it('restores unfinished files as needing reselection and isolates stored drafts', async () => {
  writeComposerDraft('ux-user', 'A', { draft: '保留内容', attachments: [], pendingAttachments: [
    { localId: 'file', filename: 'pending.pt', file: new File(['data'], 'pending.pt'), status: 'uploading', progress: 50 },
  ] });
  expect(localStorage.getItem(composerStorageKey('ux-user', 'A'))).not.toContain('progress');
  expect(readComposerDraft('other-user', 'A')).toBeNull();
  await mount();
  expect(screen.getByText('需重新选择')).toBeTruthy();
  expect(uploadFile).not.toHaveBeenCalled();
});
it('copying history preserves the current draft and sends only its new attachments', async () => {
  vi.mocked(activateSession).mockResolvedValue({ model: 'qwen3.6-plus', has_graph: false, active_run: null, messages: [
    { id: 'user:r', role: 'user', content: '原问题', attachments: [{ file_token: 'original', filename: 'original.csv' }] },
    { id: 'assistant:r', role: 'assistant', content: '原回复', run_id: 'r' },
  ] });
  const copy = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } });
  const { container } = await mount();
  fireEvent.change(screen.getByPlaceholderText(placeholder), { target: { value: '新的问题' } });
  choose(container);
  await screen.findByRole('button', { name: '移除 data.pt' });
  await waitFor(() => expect((screen.getByRole('button', { name: '发送' }) as HTMLButtonElement).disabled).toBe(false));
  expect(screen.queryByRole('button', { name: '编辑并重发' })).toBeNull();
  expect(screen.queryByRole('button', { name: '重新生成' })).toBeNull();
  fireEvent.click(screen.getAllByRole('button', { name: '复制' })[0]);
  await waitFor(() => expect(copy).toHaveBeenCalledWith('原问题'));
  expect((screen.getByPlaceholderText(placeholder) as HTMLTextAreaElement).value).toBe('新的问题');
  expect(screen.queryByRole('button', { name: '移除 original.csv' })).toBeNull();
  expect(streamChat).not.toHaveBeenCalled();
  fireEvent.keyDown(screen.getByPlaceholderText(placeholder), { key: 'Enter' });
  await waitFor(() => expect(streamChat).toHaveBeenCalledOnce());
  expect(vi.mocked(streamChat).mock.calls[0].slice(0, 3)).toEqual(['A', '新的问题', [{ filename: 'data.pt', file_token: 'token' }]]);
});

it('retains process events and regeneration links when loading history', async () => {
  vi.mocked(activateSession).mockResolvedValue({ model: 'qwen3.6-plus', has_graph: false, active_run: null, messages: [
    { id: 'a1', run_id: 'r1', role: 'assistant', content: '早先回复', processItems: [{ id: 'old', kind: 'tool', sequence: 1, tool: { tool: 'old_tool', done: true } }] },
    { id: 'a2', run_id: 'r2', role: 'assistant', content: '最新回复', regenerate_of: 'r1', processItems: [{ id: 'new', kind: 'tool', sequence: 1, tool: { tool: 'new_tool', done: true } }] },
  ] });
  const { container } = await mount();
  expect(screen.getByRole('button', { name: '上一个回答版本' })).toBeTruthy();
  expect(container.querySelectorAll('.process-panel')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: '上一个回答版本' }));
  expect(container.querySelectorAll('.message-content')[1].textContent).toBe('早先回复');
  expect(container.querySelectorAll('.process-panel')[1].textContent).toContain('old_tool');
});

it('opens a project draft immediately after creation without materializing a conversation', async () => {
  vi.mocked(createProject).mockResolvedValue({ success: true, project: {
    project_id: 'new-project', user_id: 'ux-user', name: '新项目',
  } });
  const { container } = await mount();
  fireEvent.click(screen.getByRole('button', { name: '创建项目' }));
  fireEvent.change(screen.getByLabelText('项目名称'), { target: { value: '新项目' } });
  fireEvent.submit(container.querySelector('.project-dialog')!);
  await waitFor(() => expect(Object.fromEntries(new URLSearchParams(window.location.search))).toEqual({ new: '1', project: 'new-project' }));
  expect(screen.getByRole('heading', { name: '开始在 新项目 中图分析' })).toBeTruthy();
  expect(screen.getByPlaceholderText(placeholder)).toBe(document.activeElement);
  expect(container.querySelector('.project-workspace')).toBeNull();
  expect(screen.getByRole('button', { name: '新项目' }).getAttribute('aria-expanded')).toBe('true');
  expect(createSession).not.toHaveBeenCalled();
  expect(listProjectConversations).not.toHaveBeenCalled();
});

it('toggling a project keeps the current conversation, draft and URL', async () => {
  vi.mocked(listProjects).mockResolvedValue({ projects: [{ project_id: 'p', user_id: 'ux-user', name: '项目一' }] });
  vi.mocked(listSessions).mockResolvedValue({ sessions: [
    { session_id: 'A', title: 'Session A' }, { session_id: 'B', project_id: 'p', title: '项目会话' },
  ] });
  const { container } = await mount();
  const input = screen.getByPlaceholderText(placeholder);
  fireEvent.change(input, { target: { value: '尚未发送的问题' } });
  const project = screen.getByRole('button', { name: '项目一' });
  expect(container.querySelector('.project-disclosure')).toBeNull();
  fireEvent.click(project);
  expect(project.getAttribute('aria-expanded')).toBe('true');
  expect(screen.getByRole('button', { name: '项目会话' })).toBeTruthy();
  fireEvent.click(project);
  expect(project.getAttribute('aria-expanded')).toBe('false');
  expect(screen.queryByRole('button', { name: '项目会话' })).toBeNull();
  expect(window.location.search).toBe('?session=A');
  expect((input as HTMLTextAreaElement).value).toBe('尚未发送的问题');
  expect(listProjectConversations).not.toHaveBeenCalled();
  expect(createSession).not.toHaveBeenCalled();
});

it('opens legacy project URLs as project drafts and replaces the obsolete route', async () => {
  window.history.replaceState({}, '', '/?project=p');
  vi.mocked(listProjects).mockResolvedValue({ projects: [{ project_id: 'p', user_id: 'ux-user', name: '项目一' }] });
  vi.mocked(listProjectConversations).mockResolvedValue({ conversations: [] });
  vi.mocked(listProjectDatasets).mockResolvedValue({ project_id: 'p', datasets: [], default_dataset_id: null });
  const { container } = await mount();
  await waitFor(() => expect(Object.fromEntries(new URLSearchParams(window.location.search))).toEqual({ new: '1', project: 'p' }));
  expect(screen.getByRole('heading', { name: '开始在 项目一 中图分析' })).toBeTruthy();
  expect(container.querySelector('.project-workspace')).toBeNull();
  expect(createSession).not.toHaveBeenCalled();
});

it('opens the chosen project datasets without navigating, uploads there, and selects into its draft', async () => {
  vi.mocked(listProjects).mockResolvedValue({ projects: [{ project_id: 'p', user_id: 'ux-user', name: '项目一' }] });
  const dataset = { dataset_id: 'p-data', project_id: 'p', name: '项目图', original_filename: 'p.csv', file_type: 'csv', status: 'ready' };
  vi.mocked(listProjectDatasets).mockImplementation(async projectId => ({ project_id: projectId, datasets: projectId === 'p' ? [dataset] : [] }));
  vi.mocked(uploadProjectDataset).mockResolvedValue({ success: true, dataset: { ...dataset, dataset_id: 'p-upload', name: '新图' }, default_dataset_id: 'p-data' });
  await mount();
  const input = screen.getByPlaceholderText(placeholder);
  fireEvent.change(input, { target: { value: '当前会话的草稿' } });
  fireEvent.click(screen.getByRole('button', { name: '项目一 项目数据集' }));
  const panel = await screen.findByRole('dialog', { name: '项目数据集' });
  await within(panel).findByText('项目图');
  expect(listProjectDatasets).toHaveBeenLastCalledWith('p');
  expect(window.location.search).toBe('?session=A');
  expect((input as HTMLTextAreaElement).value).toBe('当前会话的草稿');
  fireEvent.change(panel.querySelector('input[type="file"]')!, { target: { files: [new File(['a,b'], 'new.csv')] } });
  await within(panel).findByText('新图');
  expect(vi.mocked(uploadProjectDataset).mock.calls[0][0]).toBe('p');
  expect(vi.mocked(uploadProjectDataset).mock.calls[0][2]).toBeUndefined();
  expect(within(panel).getByText('项目图')).toBeTruthy();
  fireEvent.click(within(panel).getByRole('button', { name: /^项目图/ }));
  await screen.findByRole('heading', { name: '开始在 项目一 中图分析' });
  expect(screen.queryByRole('dialog', { name: '项目数据集' })).toBeNull();
  expect(screen.getByRole('button', { name: '项目图' })).toBeTruthy();
  expect(createSession).not.toHaveBeenCalled();
});

it('the composer dataset button still loads the current conversation scope', async () => {
  await mount();
  fireEvent.click(screen.getByRole('button', { name: '数据集' }));
  await screen.findByRole('dialog', { name: '个人数据集' });
  expect(listProjectDatasets).toHaveBeenLastCalledWith(null);
});

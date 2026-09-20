import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ChatPane } from './ChatPane';
import { Composer } from './Composer';

const noop = () => undefined;
afterEach(cleanup);

const baseChatProps = {
  messages: [],
  value: '',
  model: 'model-a',
  models: ['model-a'],
  onChange: noop,
  onModelChange: noop,
  onDataset: noop,
  onSend: noop,
  onUpload: noop,
  onRemoveAttachment: noop,
  attachments: [],
  loading: false,
  uploading: false,
  disabled: false,
  hasGraph: false,
  onOpenGraph: noop,
  interaction: { kind: 'composer' } as const,
};

const approval = {
  kind: 'approval' as const,
  phase: 'awaiting' as const,
  feedback: {
    run_id: 'run-1', request_id: 'run-1', call_id: 'call-1', tool_name: 'train',
    proposed_args: { epochs: 2 }, description: '确认训练任务',
  },
};

describe('ConversationInputArea', () => {
  it.each([
    'check_status(job_id="job-7")',
    'resume(run_id="r1", session_id="s1")',
    'print("test \t.")',
    '结果（job_id: lg_test）。',
    'pending(request_id="partial',
  ])('preserves code and incomplete streamed parameters: %s', async code => {
    const copy = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } });
    const content = `\`\`\`python\n${code}\n\`\`\``;
    const { container } = render(<ChatPane {...baseChatProps} messages={[
      { id: 'a', role: 'assistant', content, run_status: 'completed' },
    ]} />);
    expect(container.querySelector('pre code')?.textContent).toBe(`${code}\n`);
    fireEvent.click(screen.getByRole('button', { name: '复制' }));
    await waitFor(() => expect(copy).toHaveBeenCalledWith(content));
  });

  it('preserves inline code and an unfinished fenced code block while streaming', () => {
    const content = 'Call `check_status(job_id="job-7")` first.\n\n```python\nresume(run_id="partial';
    const { container } = render(<ChatPane {...baseChatProps} loading messages={[
      { id: 'a', role: 'assistant', content, streaming: true },
    ]} />);
    expect(container.querySelector('p code')?.textContent).toBe('check_status(job_id="job-7")');
    expect(container.querySelector('pre code')?.textContent).toBe('resume(run_id="partial\n');
  });

  it('uses a project-aware heading only for project drafts', () => {
    const { rerender } = render(<ChatPane {...baseChatProps} />);
    expect(screen.getByRole('heading', { name: '开始图分析' })).toBeTruthy();

    rerender(<ChatPane {...baseChatProps} emptyProjectName="test" />);
    expect(screen.getByRole('heading', { name: '开始在 test 中图分析' })).toBeTruthy();
    expect(screen.getByText('test').classList.contains('empty-project-name')).toBe(true);
    expect(screen.queryByRole('heading', { name: '开始图分析' })).toBeNull();
  });

  it('renders streaming run tools once above the assistant content', () => {
    const { container } = render(<ChatPane
      {...baseChatProps}
      loading
      messages={[
        { id: 'user-1', role: 'user', content: '分析这个图' },
        { id: 'assistant-1', role: 'assistant', content: '正在生成回答', streaming: true, tools: [{ tool: 'stale_tool', done: true }] },
      ]}
      runTools={[
        { tool: 'degree_analysis', call_id: 'tool-1', done: true },
        { tool: 'community_detection', call_id: 'tool-2', done: false },
      ]}
    />);

    const assistantBody = container.querySelector('.message-row.assistant .message-body')!;
    const toolLists = container.querySelectorAll('.tool-list');
    const toolList = toolLists[0];
    const messageContent = assistantBody.querySelector('.message-content')!;

    expect(toolLists).toHaveLength(1);
    expect(container.querySelector('.live-analysis-card')).toBeNull();
    expect(toolList.querySelectorAll('.tool-row')).toHaveLength(2);
    expect(toolList.textContent).toContain('degree_analysis');
    expect(toolList.textContent).toContain('community_detection');
    expect(toolList.textContent).not.toContain('stale_tool');
    expect(Array.from(assistantBody.children).indexOf(toolList)).toBeLessThan(Array.from(assistantBody.children).indexOf(messageContent));
  });

  it('omits the user initial avatar while keeping the assistant mark', () => {
    const { container } = render(<ChatPane
      {...baseChatProps}
      messages={[
        { id: 'user-1', role: 'user', content: '分析这个图' },
        { id: 'assistant-1', role: 'assistant', content: '分析完成' },
      ]}
    />);

    expect(container.querySelector('.message-row.user .message-avatar')).toBeNull();
    expect(container.querySelector('.message-row.assistant .message-avatar')).not.toBeNull();
  });

  it('keeps normal Composer behavior outside feedback states', () => {
    const onChange = vi.fn();
    const onSend = vi.fn();
    render(<Composer
      value="next question"
      model="model-a"
      models={['model-a', 'model-b']}
      onChange={onChange}
      onModelChange={noop}
      onDataset={noop}
      onSend={onSend}
      onUpload={noop}
      onRemoveAttachment={noop}
      attachments={[]}
      loading={false}
      uploading={false}
      disabled={false}
    />);

    const textarea = screen.getByPlaceholderText('今天帮你分析什么？粘贴边列表，或直接用自然语言提问') as HTMLTextAreaElement;
    expect(textarea.disabled).toBe(false);
    fireEvent.change(textarea, { target: { value: 'edited draft' } });
    fireEvent.keyDown(textarea, { key: 'Enter' });
    expect(onSend).toHaveBeenCalledOnce();
    expect((screen.getByLabelText('发送') as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByLabelText('选择模型') as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByLabelText('上传图文件') as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByText(/支持上传 \.txt/)).toBeNull();
    expect(screen.queryByText('Shift + Enter 换行')).toBeNull();
  });

  it('opens custom model configuration in a separate dialog', async () => {
    const onSaveCustomModel = vi.fn().mockResolvedValue(undefined);
    render(<Composer
      value=""
      model="model-a"
      models={['model-a', 'model-b']}
      onChange={noop}
      onModelChange={noop}
      onDataset={noop}
      onSend={noop}
      onUpload={noop}
      onRemoveAttachment={noop}
      attachments={[]}
      loading={false}
      uploading={false}
      disabled={false}
      onSaveCustomModel={onSaveCustomModel}
    />);

    fireEvent.click(screen.getByRole('button', { name: '选择模型' }));
    fireEvent.click(screen.getByRole('button', { name: '配置自定义模型' }));

    expect(screen.queryByRole('group', { name: '可用模型' })).toBeNull();
    expect(screen.getByRole('dialog', { name: '配置自定义模型' })).toBeTruthy();
    fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'llama-3.1-8b' } });
    fireEvent.change(screen.getByLabelText('接口地址'), { target: { value: 'https://api.example.com/v1' } });
    fireEvent.change(screen.getByLabelText('访问密钥'), { target: { value: 'test-key' } });
    fireEvent.click(screen.getByRole('button', { name: '保存模型' }));

    await waitFor(() => expect(onSaveCustomModel).toHaveBeenCalledWith({
      display_name: 'llama-3.1-8b',
      model_id: 'llama-3.1-8b',
      base_url: 'https://api.example.com/v1',
      api_key: 'test-key',
    }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '配置自定义模型' })).toBeNull());
  });

  it('shows one branded representative per built-in model company', () => {
    render(<Composer
      value=""
      model="qwen3.6-plus"
      models={[
        'qwen3.6-plus',
        'claude-opus-4-6',
        'claude-sonnet-4-6',
        'deepseek-v4-flash',
        'deepseek-v4-lite',
        'deepseek-v4-pro',
        'doubao-seed-2-0-pro-260215',
        'gpt-5-2',
        'gpt-5-4',
        'qwen3.7-max',
      ]}
      onChange={noop}
      onModelChange={noop}
      onDataset={noop}
      onSend={noop}
      onUpload={noop}
      onRemoveAttachment={noop}
      attachments={[]}
      loading={false}
      uploading={false}
      disabled={false}
    />);

    const trigger = screen.getByRole('button', { name: '选择模型' });
    expect(trigger.querySelector('[data-model-provider="qwen"]')).toBeTruthy();
    fireEvent.click(trigger);

    expect(screen.getByText('模型')).toBeTruthy();
    expect(screen.queryByText(/每家精选一款/)).toBeNull();
    const modelGroup = screen.getByRole('group', { name: '可用模型' });
    expect(within(modelGroup).getAllByRole('button')).toHaveLength(10);
    expect(within(modelGroup).getByRole('button', { name: 'qwen3.6-plus' }).getAttribute('aria-pressed')).toBe('true');
    expect(within(modelGroup).getByRole('button', { name: 'qwen3.7-max' })).toBeTruthy();
    expect(within(modelGroup).getByRole('button', { name: 'claude-opus-4-6' }).querySelector('[data-model-provider="anthropic"]')).toBeTruthy();
    expect(within(modelGroup).getByRole('button', { name: 'claude-sonnet-4-6' })).toBeTruthy();
    expect(within(modelGroup).getByRole('button', { name: 'deepseek-v4-pro' }).querySelector('[data-model-provider="deepseek"]')).toBeTruthy();
    expect(within(modelGroup).getByRole('button', { name: 'deepseek-v4-flash' })).toBeTruthy();
    expect(within(modelGroup).getByRole('button', { name: 'deepseek-v4-lite' })).toBeTruthy();
    expect(within(modelGroup).getByRole('button', { name: 'doubao-seed-2-0-pro-260215' }).querySelector('[data-model-provider="doubao"]')).toBeTruthy();
    expect(within(modelGroup).getByRole('button', { name: 'gpt-5-4' }).querySelector('[data-model-provider="openai"]')).toBeTruthy();
    expect(within(modelGroup).getByRole('button', { name: 'gpt-5-2' })).toBeTruthy();
  });

  it('terminates despite upload and draft gates, and allows idempotent retry when unconfirmed', () => {
    const onCancel = vi.fn();
    const props = {
      value: '', model: 'model-a', models: ['model-a'], onChange: noop,
      onModelChange: noop, onDataset: noop, onSend: noop, onUpload: noop,
      onRemoveAttachment: noop, attachments: [], uploading: true, disabled: true,
      sendBlocked: true, sendBlockedReason: '当前会话已有活动任务',
      canCancel: true, onCancel,
    };
    const { rerender } = render(<Composer {...props} loading />);

    const pause = screen.getByRole('button', { name: '终止当前任务' }) as HTMLButtonElement;
    expect(pause.disabled).toBe(false);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true);
    fireEvent.click(pause);
    expect(onCancel).toHaveBeenCalledOnce();

    rerender(<Composer {...props} loading cancellation="stopping" />);
    expect((screen.getByRole('button', { name: '终止当前任务' }) as HTMLButtonElement).disabled).toBe(true);
    rerender(<Composer {...props} loading cancellation="unconfirmed" />);
    expect(screen.getByRole('status').textContent).toContain('停止请求尚未确认');
    fireEvent.click(screen.getByRole('button', { name: '终止当前任务' }));
    expect(onCancel).toHaveBeenCalledTimes(2);
  });

  it('shows the custom model ID instead of its display name', () => {
    render(<Composer
      value=""
      model="Deepseek-v4-pro"
      models={['model-a', 'Deepseek-v4-pro']}
      customModels={[{ model_id: 'Deepseek-v4-pro', display_name: 'iGraph', base_url: 'https://api.deepseek.com' }]}
      onChange={noop}
      onModelChange={noop}
      onDataset={noop}
      onSend={noop}
      onUpload={noop}
      onRemoveAttachment={noop}
      attachments={[]}
      loading={false}
      uploading={false}
      disabled={false}
    />);

    expect(screen.getByRole('button', { name: '选择模型' }).textContent).toContain('Deepseek-v4-pro');
    expect(screen.getByRole('button', { name: '选择模型' }).textContent).not.toContain('iGraph');
    fireEvent.click(screen.getByRole('button', { name: '选择模型' }));
    expect(screen.getByRole('button', { name: 'Deepseek-v4-pro' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'iGraph' })).toBeNull();
  });

  it('replaces Composer with approval controls and preserves a hidden draft and attachments', () => {
    const onFeedback = vi.fn();
    render(<ChatPane
      {...baseChatProps}
      value="next question"
      attachments={[{ file_token: 'file-1', filename: 'graph.csv' }]}
      onFeedback={onFeedback}
      interaction={approval}
    />);

    expect(screen.getByText('助手请求确认')).toBeTruthy();
    expect(screen.getByText('草稿已保存 · 1 个附件已保存')).toBeTruthy();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByLabelText('发送')).toBeNull();
    expect(screen.queryByLabelText('上传图文件')).toBeNull();
    expect(screen.queryByLabelText('选择模型')).toBeNull();
    expect(screen.queryByText('检查图结构')).toBeNull();

    const approve = screen.getByRole('button', { name: '批准并执行' });
    fireEvent.keyDown(approve, { key: 'Enter' });
    expect(onFeedback).not.toHaveBeenCalled();
    fireEvent.click(approve);
    expect(onFeedback).toHaveBeenCalledOnce();
    expect(onFeedback).toHaveBeenCalledWith('approve');
  });

  it('keeps the approval panel visible and disables both decisions while submitting', () => {
    const onFeedback = vi.fn();
    render(<ChatPane {...baseChatProps} onFeedback={onFeedback} interaction={{ ...approval, phase: 'submitting' }} />);

    expect(screen.getByText('正在提交确认')).toBeTruthy();
    const buttons = screen.getAllByRole('button').filter(button => button.textContent?.includes('提交')) as HTMLButtonElement[];
    expect(buttons).toHaveLength(2);
    expect(buttons.every(button => button.disabled)).toBe(true);
    buttons.forEach(button => fireEvent.click(button));
    expect(onFeedback).not.toHaveBeenCalled();
  });

  it('shows a safe syncing panel instead of Composer when feedback details are missing', () => {
    render(<ChatPane {...baseChatProps} value="saved draft" uploading interaction={{ kind: 'syncing_feedback', reason: 'missing_feedback' }} />);

    expect(screen.getByText('正在同步确认信息')).toBeTruthy();
    expect(screen.getByText('草稿已保存 · 附件上传中…')).toBeTruthy();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.queryByLabelText('发送')).toBeNull();
  });

  it('restores the same controlled draft after approval mode ends', () => {
    const { rerender } = render(<ChatPane {...baseChatProps} value="preserved draft" interaction={approval} />);
    expect(screen.queryByRole('textbox')).toBeNull();

    rerender(<ChatPane {...baseChatProps} value="preserved draft" interaction={{ kind: 'composer' }} />);
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('preserved draft');
  });

  it('preserves authored explanations while keeping structured runtime IDs out of the body', () => {
    const { container } = render(<ChatPane
      {...baseChatProps}
      messages={[
        { id: 'user-1', role: 'user', content: '请解释 graph_version: 1' },
        {
          id: 'assistant-1',
          role: 'assistant',
          run_id: 'internal-run-not-part-of-content',
          content: '这是一个非常大的图，当前已通过分区快照方式加载（graph_version: 1）。分区数量（16 个）保持可见。',
        },
      ]}
    />);

    const userRow = container.querySelector('.message-row.user');
    const assistantRow = container.querySelector('.message-row.assistant');
    expect(userRow?.textContent).toContain('graph_version: 1');
    expect(assistantRow?.textContent).toContain('当前已通过分区快照方式加载（graph_version: 1）。');
    expect(assistantRow?.textContent).toContain('分区数量（16 个）保持可见。');
    expect(assistantRow?.textContent).not.toContain('internal-run-not-part-of-content');
  });

  it('shows a jump-to-latest button without interrupting a user who scrolled up', () => {
    const initialMessages = [
      { id: 'user-1', role: 'user' as const, content: 'question' },
      { id: 'assistant-1', role: 'assistant' as const, content: 'first chunk', streaming: true },
    ];
    const { container, rerender } = render(<ChatPane {...baseChatProps} conversationKey="session-a" messages={initialMessages} />);
    const scroll = container.querySelector('.chat-scroll') as HTMLDivElement;
    Object.defineProperties(scroll, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 300 },
      scrollTop: { configurable: true, writable: true, value: 120 },
    });
    const scrollTo = vi.fn((options: ScrollToOptions) => { scroll.scrollTop = Number(options.top); });
    Object.defineProperty(scroll, 'scrollTo', { configurable: true, value: scrollTo });

    fireEvent.scroll(scroll);
    expect(screen.getByRole('button', { name: '滚动到最新消息' })).toBeTruthy();

    rerender(<ChatPane {...baseChatProps} conversationKey="session-a" messages={[initialMessages[0], { ...initialMessages[1], content: 'first chunk\nsecond chunk' }]} />);
    expect(scroll.scrollTop).toBe(120);
    expect(screen.getByRole('button', { name: '滚动到最新消息' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '滚动到最新消息' }));
    expect(scrollTo).toHaveBeenCalledWith({ top: 1000, behavior: 'smooth' });
    expect(screen.queryByRole('button', { name: '滚动到最新消息' })).toBeNull();
  });

  it.each(['completed', 'cancelled'])('preserves reading position when a %s snapshot replaces temporary IDs', (status) => {
    const messages = [
      { id: 'user:local-generation', role: 'user' as const, content: 'question', run_id: 'run-1' },
      { id: 'assistant:local-generation', role: 'assistant' as const, content: 'partial answer', run_id: 'run-1', streaming: true },
    ];
    const { container, rerender } = render(<ChatPane {...baseChatProps} loading messages={messages} />);
    const scroll = container.querySelector('.chat-scroll') as HTMLDivElement;
    Object.defineProperties(scroll, {
      scrollHeight: { configurable: true, value: 1800 },
      clientHeight: { configurable: true, value: 300 },
      scrollTop: { configurable: true, writable: true, value: 120 },
    });
    fireEvent.scroll(scroll);
    rerender(<ChatPane {...baseChatProps} loading={false} messages={[
      { ...messages[0], id: 'user:run-1' },
      { ...messages[1], id: 'assistant:run-1', streaming: false, run_status: status,
        content: status === 'completed' ? 'complete answer' : '已停止本轮对话' },
    ]} />);
    expect(scroll.scrollTop).toBe(120);
    expect(screen.getByRole('button', { name: '滚动到最新消息' })).toBeTruthy();
  });

  it('keeps following completion when already at the bottom', () => {
    const messages = [{ id: 'user:local', role: 'user' as const, content: 'question' }];
    const { container, rerender } = render(<ChatPane {...baseChatProps} messages={messages} loading />);
    const scroll = container.querySelector('.chat-scroll') as HTMLDivElement;
    Object.defineProperties(scroll, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 300 },
      scrollTop: { configurable: true, writable: true, value: 700 },
    });
    fireEvent.scroll(scroll);
    Object.defineProperty(scroll, 'scrollHeight', { configurable: true, value: 1500 });
    rerender(<ChatPane {...baseChatProps} messages={[
      { ...messages[0], id: 'user:run' }, { id: 'assistant:run', role: 'assistant', content: 'done' },
    ]} />);
    expect(scroll.scrollTop).toBe(1500);
  });

  it('resumes following on an explicit send, even for an identical prompt', () => {
    const onSend = vi.fn();
    const messages = [{ id: 'user:old', role: 'user' as const, content: 'question' }];
    const { container, rerender } = render(<ChatPane {...baseChatProps} messages={messages} value="question" onSend={onSend} />);
    const scroll = container.querySelector('.chat-scroll') as HTMLDivElement;
    Object.defineProperties(scroll, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 300 },
      scrollTop: { configurable: true, writable: true, value: 120 },
    });
    fireEvent.scroll(scroll);
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(scroll.scrollTop).toBe(1000);
    Object.defineProperty(scroll, 'scrollHeight', { configurable: true, value: 1500 });
    rerender(<ChatPane {...baseChatProps} messages={[...messages,
      { id: 'user:new', role: 'user', content: 'question' },
    ]} loading />);
    expect(scroll.scrollTop).toBe(1500);
  });

  it('jumps to the bottom when switching conversations', () => {
    const messages = [
      { id: 'user-1', role: 'user' as const, content: 'question' },
      { id: 'assistant-1', role: 'assistant' as const, content: 'answer' },
    ];
    const { container, rerender } = render(<ChatPane {...baseChatProps} conversationKey="session-a" messages={messages} />);
    const scroll = container.querySelector('.chat-scroll') as HTMLDivElement;
    Object.defineProperties(scroll, {
      scrollHeight: { configurable: true, value: 1200 },
      clientHeight: { configurable: true, value: 300 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    });

    rerender(<ChatPane {...baseChatProps} conversationKey="session-b" messages={messages.map(message => ({ ...message, id: `b-${message.id}` }))} />);
    expect(scroll.scrollTop).toBe(1200);
    expect(screen.queryByRole('button', { name: '滚动到最新消息' })).toBeNull();
  });
});


it('uploads a dropped file through the existing handler and respects the disabled gate', () => {
  const onUpload = vi.fn();
  const file = new File(['print(1)'], 'inspect.py', { type: 'text/plain' });
  const { container, rerender } = render(<Composer {...baseChatProps} onUpload={onUpload} />);
  fireEvent.drop(container.querySelector('.composer-wrap')!, { dataTransfer: { files: [file], types: ['Files'] } });
  expect(onUpload).toHaveBeenCalledWith(file);
  rerender(<Composer {...baseChatProps} onUpload={onUpload} disabled />);
  fireEvent.drop(container.querySelector('.composer-wrap')!, { dataTransfer: { files: [file], types: ['Files'] } });
  expect(onUpload).toHaveBeenCalledTimes(1);
});

it('offers copy without resend actions or changes to the current draft', async () => {
  const copy = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } });
  const onSend = vi.fn();
  const onChange = vi.fn();
  render(<ChatPane {...baseChatProps} value="unsent draft" onSend={onSend} onChange={onChange} messages={[
    { id: 'u', role: 'user', content: 'inspect', attachments: [{ filename: 'a.csv', file_token: 'a' }] },
    { id: 'a', role: 'assistant', content: 'result', run_id: 'run-a' },
  ]} />);
  expect(screen.queryByRole('button', { name: '重新生成' })).toBeNull();
  expect(screen.queryByRole('button', { name: '编辑并重发' })).toBeNull();
  fireEvent.click(screen.getAllByRole('button', { name: '复制' })[0]);
  await waitFor(() => expect(copy).toHaveBeenCalledWith('inspect'));
  expect(onSend).not.toHaveBeenCalled();
  expect(onChange).not.toHaveBeenCalled();
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('unsent draft');
});

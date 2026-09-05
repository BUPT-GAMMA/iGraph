import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
    expect((screen.getByLabelText('选择模型') as HTMLSelectElement).disabled).toBe(false);
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

    expect(screen.queryByRole('listbox', { name: '可用模型' })).toBeNull();
    expect(screen.getByRole('dialog', { name: '配置自定义模型' })).toBeTruthy();
    fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'llama-3.1-8b' } });
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'https://api.example.com/v1' } });
    fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'test-key' } });
    fireEvent.click(screen.getByRole('button', { name: '保存模型' }));

    await waitFor(() => expect(onSaveCustomModel).toHaveBeenCalledWith({
      display_name: 'llama-3.1-8b',
      model_id: 'llama-3.1-8b',
      base_url: 'https://api.example.com/v1',
      api_key: 'test-key',
    }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '配置自定义模型' })).toBeNull());
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
    expect(screen.getByRole('option', { name: 'Deepseek-v4-pro' })).toBeTruthy();
    expect(screen.queryByRole('option', { name: 'iGraph' })).toBeNull();
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

    expect(screen.getByText('Agent 请求确认')).toBeTruthy();
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

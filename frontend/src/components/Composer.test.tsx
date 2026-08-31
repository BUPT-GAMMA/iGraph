import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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
    expect(screen.queryByText('检查图谱结构')).toBeNull();

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
});

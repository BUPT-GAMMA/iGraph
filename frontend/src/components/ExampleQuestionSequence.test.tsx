import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ChatPane, type ChatPaneProps } from './ChatPane';
import { I18nProvider, useI18n } from '../i18n';
import type { Message } from '../types';
import type { UserCase } from '../userCases';

afterEach(cleanup);
const novel: UserCase = { id: 'novel', name: '小说', filename: 'novel.npz', available: true, description: '',
  questions: ['小说问题一？', '小说问题二？', '小说问题三？', '小说问题四？'],
  translations: { en: { name: 'Novel', description: '', questions: ['Novel one?', 'Novel two?', 'Novel three?', 'Novel four?'] } } };
const finance: UserCase = { id: 'finance', name: '金融', filename: 'finance.npz', available: true, description: '',
  questions: ['金融问题一？', '金融问题二？', '金融问题三？'] };
const noop = () => undefined;
function turn(question: string, status = 'completed'): Message[] {
  return [{ role: 'user', content: question, run_id: question }, { role: 'assistant', content: 'answer', run_id: question, run_status: status, streaming: status === 'running' }];
}
function LanguageControl() {
  const { setLanguage } = useI18n();
  return <><button onClick={() => setLanguage('en')}>English</button><button onClick={() => setLanguage('zh')}>Chinese</button></>;
}
function Harness({ sent = noop, ...overrides }: Partial<ChatPaneProps> & { sent?: (value: string) => void }) {
  const [value, setValue] = useState('');
  return <I18nProvider initialLanguage="zh"><LanguageControl /><ChatPane
    messages={[]} model="test" models={['test']} value={value} onChange={setValue}
    onSend={() => { sent(value); setValue(''); }} onModelChange={noop} onDataset={noop}
    onUpload={noop} onRemoveAttachment={noop} attachments={[]} loading={false} uploading={false}
    disabled={false} onOpenGraph={noop} interaction={{ kind: 'composer' }} exampleCase={novel} {...overrides}
  /></I18nProvider>;
}
const textbox = () => screen.getByRole('textbox') as HTMLTextAreaElement;

describe('example question progression in the actual chat composer', () => {
  it('fills each next question with Tab without sending, then stops after the last completed answer', () => {
    const sent = vi.fn();
    let messages = turn(novel.questions[0]);
    const { rerender } = render(<Harness messages={messages} sent={sent} />);
    for (let index = 1; index < novel.questions.length; index++) {
      expect(textbox().value).toBe('');
      expect(textbox().placeholder).toBe(novel.questions[index]);
      expect(screen.getByRole('button', { name: '填入下一示例问题' }).textContent).toContain(`${index + 1} / 4`);
      expect(fireEvent.keyDown(textbox(), { key: 'Tab' })).toBe(false);
      expect(textbox().value).toBe(novel.questions[index]);
      expect(sent).toHaveBeenCalledTimes(index - 1);
      expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
      fireEvent.keyDown(textbox(), { key: 'Enter' });
      expect(sent).toHaveBeenLastCalledWith(novel.questions[index]);
      messages = [...messages, ...turn(novel.questions[index])];
      rerender(<Harness messages={messages} sent={sent} />);
    }
    expect(textbox().placeholder).not.toBe(novel.questions[0]);
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
    expect(fireEvent.keyDown(textbox(), { key: 'Tab' })).toBe(true);
  });

  it.each(['running', 'failed', 'cancelled'])('never advances a %s answer or an unfinished prompt', status => {
    const { rerender } = render(<Harness messages={turn(novel.questions[0], status)} />);
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
    rerender(<Harness messages={[turn(novel.questions[0])[0]]} />);
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
  });

  it('switches the hint language without losing progress and keeps accepted drafts untouched', () => {
    render(<Harness messages={turn(novel.questions[0])} />);
    fireEvent.click(screen.getByText('English'));
    expect(textbox().placeholder).toBe('Novel two?');
    fireEvent.keyDown(textbox(), { key: 'Tab' });
    expect(textbox().value).toBe('Novel two?');
    fireEvent.click(screen.getByText('Chinese'));
    expect(textbox().value).toBe('Novel two?');
    fireEvent.change(textbox(), { target: { value: '' } });
    expect(textbox().placeholder).toBe(novel.questions[1]);
  });

  it('recognizes English history after reload and scopes hints to the selected example', () => {
    const { rerender } = render(<Harness conversationKey="old-novel" messages={turn('Novel one?')} />);
    expect(textbox().placeholder).toBe(novel.questions[1]);
    rerender(<Harness exampleCase={finance} messages={turn('Novel one?')} />);
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
    rerender(<Harness exampleCase={finance} messages={turn(finance.questions[0])} />);
    expect(textbox().placeholder).toBe(finance.questions[1]);
    rerender(<Harness exampleCase={finance} messages={turn(finance.questions[2])} />);
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
    rerender(<Harness exampleCase={undefined} messages={turn(novel.questions[0])} />);
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
    rerender(<Harness conversationKey="new-conversation" messages={[]} />);
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
  });

  it('respects typed drafts, modifier keys, IME, and ordinary unrelated questions', () => {
    const { rerender } = render(<Harness messages={turn(novel.questions[0])} />);
    for (const key of ['shiftKey', 'ctrlKey', 'altKey', 'metaKey']) {
      expect(fireEvent.keyDown(textbox(), { key: 'Tab', [key]: true })).toBe(true);
      expect(textbox().value).toBe('');
    }
    fireEvent.compositionStart(textbox());
    expect(fireEvent.keyDown(textbox(), { key: 'Tab' })).toBe(true);
    expect(textbox().value).toBe('');
    fireEvent.compositionEnd(textbox());
    fireEvent.change(textbox(), { target: { value: '自己的追问' } });
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
    expect(fireEvent.keyDown(textbox(), { key: 'Tab' })).toBe(true);
    expect(textbox().value).toBe('自己的追问');
    fireEvent.change(textbox(), { target: { value: '' } });
    rerender(<Harness messages={[...turn(novel.questions[0]), ...turn('解释一下刚才的答案')]} />);
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
  });

  it('hides hints during generation, blocked runs, attachments, and inline prompt editing', () => {
    const props = { messages: turn(novel.questions[0]) };
    const { rerender } = render(<Harness {...props} loading />);
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
    rerender(<Harness {...props} sendBlocked />);
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
    rerender(<Harness {...props} attachments={[{ file_token: 'test', filename: 'input.csv' }]} />);
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
    rerender(<Harness {...props} onEditLatest={noop} />);
    fireEvent.click(screen.getByRole('button', { name: '修改提示词' }));
    expect(screen.queryByRole('button', { name: '填入下一示例问题' })).toBeNull();
  });

  it('lets touch and mouse users fill the same question without auto-sending', () => {
    const sent = vi.fn();
    render(<Harness messages={turn(novel.questions[0])} sent={sent} />);
    fireEvent.click(screen.getByRole('button', { name: '填入下一示例问题' }));
    expect(textbox().value).toBe(novel.questions[1]);
    expect(document.activeElement).toBe(textbox());
    expect(sent).not.toHaveBeenCalled();
  });
});

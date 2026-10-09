import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { I18nProvider } from '../i18n';
import { getContextWindow, type ContextWindowUsage } from '../api';
import { ContextWindow } from './ContextWindow';

vi.mock('../api', () => ({ getContextWindow: vi.fn() }));
const model = { config_id: 'model-a', revision: 2, context_window: 10000, max_output_tokens: 1000 };
const usage: ContextWindowUsage = { basis: 'last_request_estimate', config_id: 'model-a', model_revision: 2,
  context_window: 10000, input_tokens: 6000, tool_tokens: 1000, output_reserve: 1000,
  safety_reserve: 1000, remaining_tokens: 2000, remaining_ratio: .2, history_compacted: false, updated_at: 1 };
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.useRealTimers(); });

it.each(['zh', 'en'] as const)('shows reserved-space-aware usage in %s', async language => {
  vi.mocked(getContextWindow).mockResolvedValue({ context_window_usage: usage });
  render(<I18nProvider initialLanguage={language}><ContextWindow sessionId="A" model={model} loading={false} /></I18nProvider>);
  await waitFor(() => expect(screen.getByRole('meter').getAttribute('value')).toBe('2000'));
  expect(screen.getByText(language === 'zh' ? '约剩 2,000 tokens（20.0%）' : '≈ 2,000 tokens left (20.0%)')).toBeTruthy();
  expect(screen.getByText(language === 'zh' ? '回复预留' : 'Reserved for reply')).toBeTruthy();
});

it('does not invent a remaining value for drafts or snapshots from a different model', async () => {
  vi.mocked(getContextWindow).mockResolvedValue({ context_window_usage: { ...usage, config_id: 'other' } });
  const view = render(<I18nProvider initialLanguage="zh"><ContextWindow model={model} loading={false} /></I18nProvider>);
  expect(screen.getByText('等待估算')).toBeTruthy();
  expect(getContextWindow).not.toHaveBeenCalled();
  view.rerender(<I18nProvider initialLanguage="zh"><ContextWindow sessionId="A" model={model} loading={false} /></I18nProvider>);
  await waitFor(() => expect(getContextWindow).toHaveBeenCalled());
  expect(screen.queryByRole('meter')).toBeNull();
});

it('polls during a run and discards stale results when the session changes', async () => {
  vi.useFakeTimers();
  let finishOld!: (value: { context_window_usage: ContextWindowUsage }) => void;
  vi.mocked(getContextWindow).mockImplementation(id => id === 'A'
    ? new Promise(resolve => { finishOld = resolve; })
    : Promise.resolve({ context_window_usage: { ...usage, remaining_tokens: 400 } }));
  const view = render(<I18nProvider initialLanguage="zh"><ContextWindow sessionId="A" model={model} loading /></I18nProvider>);
  view.rerender(<I18nProvider initialLanguage="zh"><ContextWindow sessionId="B" model={model} loading /></I18nProvider>);
  await act(async () => { finishOld({ context_window_usage: usage }); });
  expect(screen.getByRole('meter').getAttribute('value')).toBe('400');
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(getContextWindow).toHaveBeenCalledTimes(3);
  expect(screen.getByText(/可用上下文较少/)).toBeTruthy();
});

it('does not show zero or an old value after a request failure', async () => {
  vi.mocked(getContextWindow).mockRejectedValue(new Error('offline'));
  render(<I18nProvider initialLanguage="zh"><ContextWindow sessionId="A" model={model} loading={false} /></I18nProvider>);
  await screen.findByText('用量暂不可用');
  expect(screen.queryByRole('meter')).toBeNull();
});

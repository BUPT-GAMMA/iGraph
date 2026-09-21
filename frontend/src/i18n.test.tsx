import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { TopBar } from './components/TopBar';
import { I18nProvider, LANGUAGE_STORAGE_KEY, resolveLanguage, useI18n } from './i18n';

function Probe() {
  const { t } = useI18n();
  return <p>{t('English interface', '中文界面')}</p>;
}

describe('interface language', () => {
  afterEach(() => {
    cleanup();
    localStorage.removeItem(LANGUAGE_STORAGE_KEY);
    document.documentElement.lang = 'zh-CN';
    document.title = '';
  });

  it('uses English for a new production browser', () => {
    expect(resolveLanguage(null)).toBe('en');
    expect(resolveLanguage('zh')).toBe('zh');
  });

  it('switches the rendered copy, document language, and persisted preference', async () => {
    render(<I18nProvider initialLanguage="en"><TopBar onMenu={() => undefined} onHome={() => undefined} /><Probe /></I18nProvider>);

    expect(screen.getByText('English interface')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Go to home' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Open sidebar' })).toBeTruthy();
    await waitFor(() => expect(document.documentElement.lang).toBe('en'));
    expect(document.title).toBe('iGraph · Graph Intelligence Workspace');

    fireEvent.click(screen.getByRole('button', { name: '中文' }));

    expect(screen.getByText('中文界面')).toBeTruthy();
    expect(screen.getByRole('button', { name: '打开侧栏' })).toBeTruthy();
    expect(localStorage.getItem(LANGUAGE_STORAGE_KEY)).toBe('zh');
    await waitFor(() => expect(document.documentElement.lang).toBe('zh-CN'));
    expect(document.title).toBe('iGraph · 图分析工作台');
  });

  it('synchronizes a language change from another browser tab', async () => {
    render(<I18nProvider initialLanguage="en"><Probe /></I18nProvider>);

    fireEvent(window, new StorageEvent('storage', { key: LANGUAGE_STORAGE_KEY, newValue: 'zh' }));

    expect(screen.getByText('中文界面')).toBeTruthy();
    await waitFor(() => expect(document.documentElement.lang).toBe('zh-CN'));
    expect(document.title).toBe('iGraph · 图分析工作台');
  });
});

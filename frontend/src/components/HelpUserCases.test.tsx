import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { I18nProvider, useI18n } from '../i18n';
import { UserCaseContext, type UserCase } from '../userCases';
import { HelpPage } from './HelpPage';

const examples: UserCase[] = [
  ['finance_risk', '金融股权穿透分析', 'Ownership Look-Through Analysis'],
  ['borylation', '化学分子性质分析', 'Chemical Molecular Property Analysis'],
  ['tianlongbabu', '文学人物关系网络', 'Literary Character Network'],
].map(([id, name, englishName]) => ({
  id, name, filename: `${id}.npz`, available: true, description: `${name}的数据来源`,
  questions: ['核查图中记录', '追踪关系', '标注位点', '分析结果'], question_titles: ['数据核查', '关系追踪', '位点识别', '结果分析'],
  translations: { en: { name: englishName, description: `Source data for ${englishName}`,
    questions: ['Inspect graph records', 'Trace relationships', 'Mark sites', 'Analyze results'],
    question_titles: ['Data checks', 'Relationships', 'Sites', 'Results'] } },
}));

beforeEach(() => { vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function LanguageSwitch() {
  const { toggleLanguage } = useI18n();
  return <button onClick={toggleLanguage}>Switch language</button>;
}

function renderHelp(cases = examples) {
  return render(<I18nProvider initialLanguage="zh"><UserCaseContext.Provider value={cases}><LanguageSwitch /><HelpPage /></UserCaseContext.Provider></I18nProvider>);
}

describe('Help user cases', () => {
  it('opens alongside the existing catalogs and loads videos only on demand', () => {
    const { container } = renderHelp();
    expect(container.querySelectorAll('video')).toHaveLength(0);
    fireEvent.click(screen.getByRole('tab', { name: '用户案例' }));
    expect(screen.getByRole('tab', { name: '用户案例' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.queryByRole('searchbox')).toBeNull();
    const panel = screen.getByRole('tabpanel', { name: '用户案例' });
    expect(within(panel).getAllByRole('article')).toHaveLength(3);
    const files = ['financial-risk', 'molecular-graph', 'demi-gods-and-semi-devils'];
    for (const [index, video] of Array.from(panel.querySelectorAll('video')).entries()) {
      expect(video.controls).toBe(true);
      expect(video.playsInline).toBe(true);
      expect(video.preload).toBe('none');
      expect(video.autoplay).toBe(false);
      expect(video.querySelector('source')?.getAttribute('src')).toBe(`/media/user-cases/${files[index]}.mp4`);
      expect(video.getAttribute('poster')).toBe(`/media/user-cases/${files[index]}.jpg`);
      const article = video.closest('article')!;
      expect(within(article).getByRole('heading', { name: examples[index].name })).toBeTruthy();
      expect(within(article).getAllByRole('listitem')).toHaveLength(4);
    }
    fireEvent.click(screen.getByRole('tab', { name: '使用流程' }));
    expect(container.querySelectorAll('video')).toHaveLength(0);
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(3);
  });

  it('uses current catalog questions and follows interface language without changing the recordings', () => {
    const updated = examples.map(item => ({ ...item, questions: item.questions.map((question, index) => index === 2 ? '只标注当前图中的位点' : question) }));
    renderHelp(updated);
    fireEvent.click(screen.getByRole('tab', { name: '用户案例' }));
    expect(screen.getAllByText('只标注当前图中的位点')).toHaveLength(3);
    const source = document.querySelector('video source')?.getAttribute('src');
    fireEvent.click(screen.getByRole('button', { name: 'Switch language' }));
    const panel = screen.getByRole('tabpanel', { name: 'User cases' });
    expect(panel.textContent).not.toMatch(/[\u3400-\u9fff]/);
    for (const example of examples) expect(within(panel).getByRole('heading', { name: example.translations!.en!.name })).toBeTruthy();
    expect(within(panel).getAllByText('Mark sites')).toHaveLength(3);
    expect(document.querySelector('video source')?.getAttribute('src')).toBe(source);
    expect(HTMLMediaElement.prototype.pause).not.toHaveBeenCalled();
  });

  it('pauses the previous walkthrough when a different one starts playing', () => {
    const { container, unmount } = renderHelp();
    fireEvent.click(screen.getByRole('tab', { name: '用户案例' }));
    const [first, second] = Array.from(container.querySelectorAll('video'));
    Object.defineProperty(first, 'paused', { configurable: true, value: false });
    fireEvent.play(second);
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledOnce();
    expect(vi.mocked(HTMLMediaElement.prototype.pause).mock.instances[0]).toBe(first);
    unmount();
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledTimes(4);
  });

  it('keeps an accessible direct video link available on playback errors', () => {
    const { container } = renderHelp();
    fireEvent.click(screen.getByRole('tab', { name: '用户案例' }));
    fireEvent.error(container.querySelector('video source')!);
    expect(screen.getByRole('alert').textContent).toContain('视频暂时无法加载');
    expect(screen.getByRole('link', { name: '打开视频：金融股权穿透分析' }).getAttribute('href')).toBe('/media/user-cases/financial-risk.mp4');
  });
});

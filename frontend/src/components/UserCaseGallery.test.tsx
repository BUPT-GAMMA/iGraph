import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { UserCaseGallery } from './UserCaseGallery';
import { I18nProvider } from '../i18n';
import { TopBar } from './TopBar';
afterEach(cleanup);
it('switches both examples and sends the visible localized question without reloading', () => {
  const onSelect = vi.fn();
  const cases = ['finance', 'tianlongbabu'].map(id => ({ id, name: `${id} 中文示例`, filename: `${id}.npz`, description: '中文介绍',
    questions: [`${id} 中文问题`], available: true, translations: { en: { name: `${id} example`, description: 'English description', questions: [`Inspect ${id}`] } } }));
  render(<I18nProvider initialLanguage="zh"><TopBar onMenu={vi.fn()} onHome={vi.fn()} /><UserCaseGallery cases={cases} busy={false} onSelect={onSelect} /></I18nProvider>);
  fireEvent.click(screen.getByRole('button', { name: 'EN' }));
  for (const id of ['finance', 'tianlongbabu']) {
    expect(screen.getByRole('heading', { name: `${id} example` })).toBeTruthy();
    expect(screen.queryByText(`${id} 中文示例`)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: `Inspect ${id}` }));
    expect(onSelect).toHaveBeenLastCalledWith(id, `Inspect ${id}`);
  }
  expect(screen.getAllByText('English description')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: '中文' }));
  expect(screen.getAllByText('中文介绍')).toHaveLength(2);
  for (const id of ['finance', 'tianlongbabu']) {
    expect(screen.getByRole('heading', { name: `${id} 中文示例` })).toBeTruthy();
    expect(screen.getByRole('button', { name: `${id} 中文问题` })).toBeTruthy();
  }
});
it('opens either example and submits the selected question', () => {
  const onSelect = vi.fn();
  render(<UserCaseGallery busy={false} onSelect={onSelect} cases={[
    { id:'finance', name:'金融股权示例', filename:'finance-ownership.npz', description:'预置金融图', questions:['谁是第一大股东？'], available:true },
    { id:'tianlongbabu', name:'天龙八部示例', filename:'tianlongbabu-characters.npz', description:'预置小说图', questions:['人物关系有哪些？'], available:true },
  ]} />);
  fireEvent.click(screen.getByRole('button', { name:'谁是第一大股东？' }));
  expect(onSelect).toHaveBeenLastCalledWith('finance', '谁是第一大股东？');
  fireEvent.click(screen.getByRole('button', { name:'人物关系有哪些？' }));
  expect(onSelect).toHaveBeenLastCalledWith('tianlongbabu', '人物关系有哪些？');
});
it('does not allow opening a graph that is not installed', () => {
  render(<UserCaseGallery busy={false} onSelect={vi.fn()} cases={[{ id:'novel', name:'小说', filename:'novel.npz', description:'', questions:['问题'], available:false }]} />);
  expect(screen.getByRole('button', { name:'尚未安装' }).matches(':disabled')).toBe(true);
  expect(screen.getByRole('button', { name:'问题' }).matches(':disabled')).toBe(true);
});

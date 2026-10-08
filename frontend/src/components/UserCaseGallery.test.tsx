import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { UserCaseGallery } from './UserCaseGallery';
afterEach(cleanup);
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

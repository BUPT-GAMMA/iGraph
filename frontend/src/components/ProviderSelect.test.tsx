import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ProviderSelect } from './ProviderSelect';
beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);
it('searches provider names and selects with the keyboard', async () => {
  const onChange = vi.fn();
  const u = userEvent.setup();
  render(<ProviderSelect label="提供商" value="custom" options={[
    {value:'custom',label:'自定义提供商',icon:'custom'},
    {value:'anthropic',label:'Anthropic',icon:'anthropic'},
    {value:'google',label:'Google Gemini',icon:'google'},
  ]} onChange={onChange} />);
  await u.click(screen.getByRole('button',{name:'提供商'}));
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'Gemini' } });
  expect(screen.getAllByRole('option')).toHaveLength(1);
  await u.keyboard('{Enter}');
  expect(onChange).toHaveBeenCalledWith('google');
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
}, 15000);

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { useActionDialog } from './useActionDialog';

afterEach(cleanup);
function Harness({ scope = 'one', onResult }: { scope?: string; onResult: (result: string | boolean | null) => void }) {
  const { askConfirm, askPrompt, actionDialog } = useActionDialog(scope);
  return <>{actionDialog}<button onClick={async () => onResult(await askPrompt('数据集名称', '原名称'))}>重命名</button><button onClick={async () => onResult(await askConfirm('删除数据集？'))}>移除</button></>;
}

it('submits an edited name and completes the animated close', async () => {
  const result = vi.fn();
  render(<Harness onResult={result} />);
  fireEvent.click(screen.getByText('重命名'));
  const input = screen.getByRole('textbox', { name: '数据集名称' });
  expect((input as HTMLInputElement).value).toBe('原名称');
  fireEvent.change(input, { target: { value: '  新名称  ' } });
  fireEvent.submit(input.closest('form')!);
  await waitFor(() => expect(result).toHaveBeenCalledWith('新名称'));
  await waitFor(() => expect(document.querySelector('.action-dialog')).toBeNull());
});

it('cancels Escape and any pending answer when the account changes', async () => {
  const result = vi.fn();
  const view = render(<Harness onResult={result} />);
  fireEvent.click(screen.getByText('移除'));
  fireEvent(screen.getByRole('alertdialog'), new Event('cancel', { cancelable: true }));
  await waitFor(() => expect(result).toHaveBeenCalledWith(false));
  result.mockClear();
  fireEvent.click(screen.getByText('重命名'));
  view.rerender(<Harness scope="two" onResult={result} />);
  await waitFor(() => expect(result).toHaveBeenCalledWith(null));
  expect(screen.queryByRole('textbox')).toBeNull();
});

it('cancels pending deletion when the owning panel unmounts', async () => {
  const result = vi.fn();
  const view = render(<Harness onResult={result} />);
  fireEvent.click(screen.getByText('移除'));
  await act(async () => view.unmount());
  expect(result).toHaveBeenCalledExactlyOnceWith(false);
  expect(document.querySelector('.action-dialog')).toBeNull();
});

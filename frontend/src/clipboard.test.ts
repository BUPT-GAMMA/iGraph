import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { copyText } from './clipboard';

function setClipboard(value: unknown) {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value });
}

function setExecCommand(value: unknown) {
  Object.defineProperty(document, 'execCommand', { configurable: true, value });
}

describe('copyText', () => {
  let button: HTMLButtonElement;

  beforeEach(() => {
    button = document.createElement('button');
    document.body.appendChild(button);
    button.focus();
  });

  afterEach(() => {
    button.remove();
    delete (navigator as { clipboard?: unknown }).clipboard;
    delete (document as { execCommand?: unknown }).execCommand;
  });

  it('uses the async clipboard API in secure contexts', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const execCommand = vi.fn(() => true);
    setClipboard({ writeText });
    setExecCommand(execCommand);
    await copyText('node-42');
    expect(writeText).toHaveBeenCalledWith('node-42');
    expect(execCommand).not.toHaveBeenCalled();
  });

  it('falls back to execCommand on plain HTTP where navigator.clipboard is absent', async () => {
    setClipboard(undefined);
    let selected = '';
    const execCommand = vi.fn(() => {
      const textarea = document.activeElement as HTMLTextAreaElement;
      selected = textarea.value.slice(textarea.selectionStart, textarea.selectionEnd);
      return true;
    });
    setExecCommand(execCommand);
    await copyText('多行\n回答');
    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(selected).toBe('多行\n回答');
    expect(document.querySelector('textarea')).toBeNull();
    expect(document.activeElement).toBe(button);
  });

  it('rejects when the fallback copy fails', async () => {
    setClipboard(undefined);
    setExecCommand(vi.fn(() => false));
    await expect(copyText('Answer')).rejects.toThrow('Clipboard copy failed');
    setExecCommand(vi.fn(() => { throw new Error('SecurityError'); }));
    await expect(copyText('Answer')).rejects.toThrow('SecurityError');
    setExecCommand(undefined);
    await expect(copyText('Answer')).rejects.toThrow('Clipboard copy failed');
    expect(document.querySelector('textarea')).toBeNull();
    expect(document.activeElement).toBe(button);
  });
});

/**
 * Copy text to the system clipboard.
 *
 * navigator.clipboard only exists in secure contexts (HTTPS or localhost), so a
 * deployment opened over plain HTTP by IP or hostname has no async clipboard.
 * Fall back to selecting a temporary textarea and running execCommand('copy'),
 * which browsers still honour there during a user gesture. Rejects when the
 * text could not be copied so callers can surface their error state.
 */
export async function copyText(text: string): Promise<void> {
  const clipboard = globalThis.navigator?.clipboard;
  if (typeof clipboard?.writeText === 'function') return clipboard.writeText(text);
  copyWithExecCommand(text);
}

function copyWithExecCommand(text: string) {
  const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.setAttribute('aria-hidden', 'true');
  Object.assign(textarea.style, { position: 'fixed', top: '0', left: '0', opacity: '0', pointerEvents: 'none' });
  document.body.appendChild(textarea);
  let copied = false;
  try {
    textarea.focus({ preventScroll: true });
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    copied = typeof document.execCommand === 'function' && document.execCommand('copy');
  } finally {
    textarea.remove();
    previousFocus?.focus({ preventScroll: true });
  }
  if (!copied) throw new Error('Clipboard copy failed');
}

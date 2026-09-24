import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { createPortal } from 'react-dom';
import { useReducedMotion } from 'motion/react';
import { Popup, PopupBackdrop, PopupPresence } from './PopupMotion';

vi.mock('motion/react', async () => ({ ...await vi.importActual('motion/react'), useReducedMotion: vi.fn(() => false) }));
afterEach(() => { cleanup(); vi.mocked(useReducedMotion).mockReturnValue(false); });

it('reverses an interrupted exit without leaving duplicate or inert windows', async () => {
  const panel = <Popup.div role="dialog" aria-label="窗口">内容</Popup.div>;
  const view = render(<PopupPresence>{panel}</PopupPresence>);
  const element = screen.getByRole('dialog');
  view.rerender(<PopupPresence>{null}</PopupPresence>);
  expect(element.isConnected).toBe(true);
  expect(element.getAttribute('aria-hidden')).toBe('true');
  view.rerender(<PopupPresence>{panel}</PopupPresence>);
  expect(screen.getAllByRole('dialog')).toHaveLength(1);
  expect(screen.getByRole('dialog')).toBe(element);
  expect(element.hasAttribute('inert')).toBe(false);
  await waitFor(() => expect(element.style.opacity).toBe('1'));
  expect(element.getAttribute('data-popup-state')).toBe('open');
});

it('finishes nested portal exits before removing their parent', async () => {
  function Confirmation() { return createPortal(<Popup.div role="alertdialog">确认</Popup.div>, document.body); }
  function Window() {
    return <PopupBackdrop data-testid="backdrop"><Popup.div role="dialog" aria-label="父窗口">
      <PopupPresence><Confirmation /></PopupPresence>
    </Popup.div></PopupBackdrop>;
  }
  const view = render(<PopupPresence><Window /></PopupPresence>);
  const panel = screen.getByRole('dialog');
  const confirmation = screen.getByRole('alertdialog');
  view.rerender(<PopupPresence>{null}</PopupPresence>);
  expect(screen.queryByRole('dialog')).toBeNull();
  await waitFor(() => expect(confirmation.hasAttribute('inert')).toBe(true));
  await waitFor(() => expect(panel.isConnected || confirmation.isConnected).toBe(false));
  expect(screen.queryByTestId('backdrop')).toBeNull();
});

it('shows reduced-motion windows immediately without vertical displacement', async () => {
  vi.mocked(useReducedMotion).mockReturnValue(true);
  const view = render(<PopupPresence><Popup.div role="dialog">内容</Popup.div></PopupPresence>);
  const panel = screen.getByRole('dialog');
  expect(panel.style.opacity).toBe('1');
  expect(panel.style.transform).toBe('none');
  view.rerender(<PopupPresence>{null}</PopupPresence>);
  await waitFor(() => expect(panel.isConnected).toBe(false));
});

import type { ReactNode } from 'react';
import { Popup, PopupBackdrop } from './PopupMotion';

type Props = {
  ariaLabel: string;
  className?: string;
  children: (requestClose: () => void) => ReactNode;
  onClose: () => void;
};

export function DrawerShell({ ariaLabel, className = '', children, onClose }: Props) {
  return <PopupBackdrop className="overlay drawer-overlay" onClick={onClose}>
    <Popup.section
      className={`drawer ${className}`.trim()}
      role="dialog"
      aria-label={ariaLabel}
      aria-modal="true"
      onClick={event => event.stopPropagation()}
    >
      {children(onClose)}
    </Popup.section>
  </PopupBackdrop>;
}

import { useEffect, useState, type ReactNode } from 'react';

type Props = {
  ariaLabel: string;
  className?: string;
  children: (requestClose: () => void) => ReactNode;
  onClose: () => void;
};

export function DrawerShell({ ariaLabel, className = '', children, onClose }: Props) {
  const [closing, setClosing] = useState(false);

  function requestClose() {
    setClosing(true);
  }

  useEffect(() => {
    if (!closing) return;
    const fallback = window.setTimeout(onClose, 320);
    return () => window.clearTimeout(fallback);
  }, [closing, onClose]);

  return <div
    className={`overlay drawer-overlay ${closing ? 'is-closing' : ''}`}
    onClick={requestClose}
    onAnimationEnd={event => {
      if (closing && event.target === event.currentTarget) onClose();
    }}
  >
    <section
      className={`drawer ${className}`.trim()}
      role="dialog"
      aria-label={ariaLabel}
      aria-modal="true"
      onClick={event => event.stopPropagation()}
    >
      {children(requestClose)}
    </section>
  </div>;
}

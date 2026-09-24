import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react';

type ResizeOptions = {
  defaultWidth: number;
  label: string;
  storageKey: string;
  minimumWidth?: number;
  maximumWidth?: number;
};

type Props = {
  ariaLabel: string;
  className?: string;
  children: (requestClose: () => void) => ReactNode;
  onClose: () => void;
  resize?: ResizeOptions;
};

const DRAWER_VIEWPORT_GAP = 32;

function clampWidth(value: number, minimum: number, maximum: number) {
  const viewportWidth = typeof window === 'undefined' ? maximum : window.innerWidth - DRAWER_VIEWPORT_GAP;
  const upperBound = Math.max(minimum, Math.min(maximum, viewportWidth));
  return Math.min(Math.max(value, minimum), upperBound);
}

function initialWidth(options: ResizeOptions | undefined, minimum: number, maximum: number) {
  if (!options) return 0;
  try {
    const stored = Number(localStorage.getItem(options.storageKey));
    if (Number.isFinite(stored) && stored > 0) return clampWidth(stored, minimum, maximum);
  } catch { /* Width persistence is optional. */ }
  return clampWidth(options.defaultWidth, minimum, maximum);
}

export function DrawerShell({ ariaLabel, className = '', children, onClose, resize }: Props) {
  const [closing, setClosing] = useState(false);
  const minimumWidth = resize?.minimumWidth ?? 360;
  const maximumWidth = resize?.maximumWidth ?? 1200;
  const resizeEnabled = Boolean(resize);
  const [width, setWidth] = useState(() => initialWidth(resize, minimumWidth, maximumWidth));
  const resizeRef = useRef<{ startX: number; startWidth: number } | null>(null);

  function requestClose() {
    setClosing(true);
  }

  function updateWidth(value: number) {
    setWidth(clampWidth(value, minimumWidth, maximumWidth));
  }

  function beginResize(event: ReactPointerEvent<HTMLDivElement>) {
    if (!resizeEnabled || event.button !== 0 || window.innerWidth <= 720) return;
    event.preventDefault();
    resizeRef.current = { startX: event.clientX, startWidth: width };
    document.body.classList.add('is-resizing-drawer');
  }

  function resizeWithKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (!resize) return;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      updateWidth(width + (event.key === 'ArrowLeft' ? 16 : -16));
    } else if (event.key === 'Home') {
      event.preventDefault();
      updateWidth(minimumWidth);
    } else if (event.key === 'End') {
      event.preventDefault();
      updateWidth(maximumWidth);
    }
  }

  useEffect(() => {
    if (!closing) return;
    const fallback = window.setTimeout(onClose, 320);
    return () => window.clearTimeout(fallback);
  }, [closing, onClose]);

  useEffect(() => {
    if (!resizeEnabled) return;
    function move(event: PointerEvent) {
      const current = resizeRef.current;
      if (current) updateWidth(current.startWidth + current.startX - event.clientX);
    }
    function finish() {
      resizeRef.current = null;
      document.body.classList.remove('is-resizing-drawer');
    }
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
      document.body.classList.remove('is-resizing-drawer');
    };
  }, [resizeEnabled, minimumWidth, maximumWidth]);

  useEffect(() => {
    if (!resizeEnabled) return;
    function fitToViewport() { setWidth(current => clampWidth(current, minimumWidth, maximumWidth)); }
    window.addEventListener('resize', fitToViewport);
    return () => window.removeEventListener('resize', fitToViewport);
  }, [resizeEnabled, minimumWidth, maximumWidth]);

  useEffect(() => {
    if (!resize?.storageKey) return;
    try { localStorage.setItem(resize.storageKey, String(Math.round(width))); } catch { /* Width persistence is optional. */ }
  }, [resize?.storageKey, width]);

  return <div
    className={`overlay drawer-overlay ${closing ? 'is-closing' : ''}`}
    onClick={requestClose}
    onAnimationEnd={event => {
      if (closing && event.target === event.currentTarget) onClose();
    }}
  >
    <section
      className={`drawer ${className} ${resizeEnabled ? 'is-resizable' : ''}`.trim()}
      role="dialog"
      aria-label={ariaLabel}
      aria-modal="true"
      onClick={event => event.stopPropagation()}
      style={resizeEnabled ? { width } : undefined}
    >
      {resize && <div
        className="drawer-resize-handle"
        role="separator"
        aria-label={resize.label}
        aria-orientation="vertical"
        aria-valuemin={minimumWidth}
        aria-valuemax={clampWidth(maximumWidth, minimumWidth, maximumWidth)}
        aria-valuenow={Math.round(width)}
        tabIndex={0}
        title={`${resize.label}；双击恢复默认宽度`}
        onPointerDown={beginResize}
        onKeyDown={resizeWithKeyboard}
        onDoubleClick={() => updateWidth(resize.defaultWidth)}
      ><span /></div>}
      {children(requestClose)}
    </section>
  </div>;
}

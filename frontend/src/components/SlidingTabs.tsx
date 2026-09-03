import { useEffect, useLayoutEffect, useRef, type ReactNode } from 'react';

export type SlidingTabOption<T extends string> = {
  value: T;
  label: ReactNode;
  disabled?: boolean;
};

type Props<T extends string> = {
  ariaLabel: string;
  className: string;
  indicatorClassName?: string;
  options: SlidingTabOption<T>[];
  value: T;
  onChange: (value: T) => void;
};

export function SlidingTabs<T extends string>({ ariaLabel, className, indicatorClassName = '', options, value, onChange }: Props<T>) {
  const tabsRef = useRef<HTMLDivElement>(null);
  const indicatorRef = useRef<HTMLSpanElement>(null);

  function positionIndicator() {
    const activeButton = Array.from(tabsRef.current?.querySelectorAll<HTMLButtonElement>(':scope > button') || [])
      .find(button => button.dataset.tab === value);
    const indicator = indicatorRef.current;
    if (!activeButton || !indicator) return;
    indicator.style.width = `${activeButton.offsetWidth}px`;
    indicator.style.transform = `translateX(${activeButton.offsetLeft}px)`;
    indicator.style.opacity = '1';
  }

  useLayoutEffect(positionIndicator);
  useEffect(() => {
    window.addEventListener('resize', positionIndicator);
    return () => window.removeEventListener('resize', positionIndicator);
  });

  return <div ref={tabsRef} className={`sliding-tabs ${className}`} role="tablist" aria-label={ariaLabel}>
    {options.map(option => <button
      key={option.value}
      data-tab={option.value}
      role="tab"
      aria-selected={value === option.value}
      className={value === option.value ? 'active' : ''}
      disabled={option.disabled}
      onClick={() => onChange(option.value)}
    >{option.label}</button>)}
    <span ref={indicatorRef} className={`sliding-tab-indicator ${indicatorClassName}`.trim()} aria-hidden="true" />
  </div>;
}

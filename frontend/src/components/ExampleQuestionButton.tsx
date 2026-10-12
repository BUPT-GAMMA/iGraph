import { useEffect, useId, useRef, useState } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { useI18n } from '../i18n';
import './ExampleQuestionButton.css';

type Props = { title: string; question: string; disabled?: boolean; onSelect: () => void };

/** The help control is a sibling, so reading a prompt never starts an analysis. */
export function ExampleQuestionButton({ title, question, disabled = false, onSelect }: Props) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const contentId = useId();
  const cancelClose = () => clearTimeout(timer.current);
  const show = () => { cancelClose(); setOpen(true); };
  const hideSoon = () => { cancelClose(); timer.current = setTimeout(() => setOpen(false), 160); };
  useEffect(() => () => clearTimeout(timer.current), []);

  return <div className="example-question-button" data-disabled={disabled || undefined}>
    <button type="button" className="example-question-action" disabled={disabled} onClick={onSelect}>{title}</button>
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button type="button" className="example-question-help" aria-label={t('View full question: ', '查看完整问题：') + title}
          aria-describedby={open ? contentId : undefined}
          onPointerEnter={show} onPointerLeave={hideSoon} onFocus={show} onBlur={hideSoon}
          onClick={event => { event.preventDefault(); show(); }}>
          <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.35" aria-hidden="true">
            <circle cx="10" cy="10" r="7.1" />
            <path d="M7.9 7.7a2.1 2.1 0 1 1 3.2 1.8c-.8.5-1.1.9-1.1 1.8" strokeLinecap="round" />
            <circle cx="10" cy="13.7" r=".7" fill="currentColor" stroke="none" />
          </svg>
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content id={contentId} role="tooltip" className="example-question-tooltip" side="bottom" align="end" sideOffset={8} collisionPadding={16}
          onOpenAutoFocus={event => event.preventDefault()} onCloseAutoFocus={event => event.preventDefault()}
          onPointerEnter={cancelClose} onPointerLeave={hideSoon}>
          <strong>{title}</strong><p>{question}</p>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  </div>;
}

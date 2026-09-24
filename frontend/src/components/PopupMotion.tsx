import { forwardRef, type PropsWithChildren } from 'react';
import { AnimatePresence, motion, useIsPresent, useReducedMotion, type HTMLMotionProps } from 'motion/react';
import './PopupMotion.css';

// Cherry Studio's directional dialog: 16px rise, 99% scale, 260ms in / 200ms out.
const enterEase = [0.16, 1, 0.3, 1] as const;
const exitEase = [0.4, 0, 1, 1] as const;

export function PopupPresence({ children }: PropsWithChildren) {
  return <AnimatePresence propagate>{children}</AnimatePresence>;
}

function usePopupMotion(kind: 'panel' | 'popover' | 'backdrop') {
  const present = useIsPresent();
  const reduce = useReducedMotion();
  const offset = reduce ? 0 : kind === 'popover' ? 8 : 16;
  // Independent translate keeps a popover's horizontal viewport correction intact.
  const from = kind === 'backdrop' ? { opacity: 0 }
    : kind === 'popover' ? { opacity: 0, translate: `0px ${offset}px` }
      : { opacity: 0, y: offset, scale: reduce ? 1 : 0.99 };
  const to = kind === 'backdrop' ? { opacity: 1 }
    : kind === 'popover' ? { opacity: 1, translate: '0px 0px' }
      : { opacity: 1, y: 0, scale: 1 };
  return {
    initial: reduce ? false as const : from,
    animate: { ...to, transition: { duration: reduce ? 0 : kind === 'popover' ? 0.18 : kind === 'backdrop' ? 0.22 : 0.26, ease: enterEase } },
    exit: { ...from, transition: { duration: reduce ? 0 : kind === 'popover' ? 0.14 : 0.2, ease: exitEase } },
    'data-popup-kind': kind,
    'data-popup-state': present ? 'open' : 'closed',
    'aria-hidden': present ? undefined : true,
    // Retained exit surfaces cannot receive focus or trigger duplicate actions.
    inert: present ? undefined : '',
  };
}

type DivProps = HTMLMotionProps<'div'> & { variant?: 'panel' | 'popover' };
export const PopupBackdrop = forwardRef<HTMLDivElement, HTMLMotionProps<'div'>>(function PopupBackdrop(props, ref) {
  return <motion.div {...props} {...usePopupMotion('backdrop')} ref={ref} />;
});
const Div = forwardRef<HTMLDivElement, DivProps>(function PopupDiv({ variant = 'panel', ...props }, ref) {
  return <motion.div {...props} {...usePopupMotion(variant)} ref={ref} />;
});
const Section = forwardRef<HTMLElement, HTMLMotionProps<'section'>>(function PopupSection(props, ref) {
  return <motion.section {...props} {...usePopupMotion('panel')} ref={ref} />;
});
const Form = forwardRef<HTMLFormElement, HTMLMotionProps<'form'>>(function PopupForm(props, ref) {
  return <motion.form {...props} {...usePopupMotion('panel')} ref={ref} />;
});
const Dialog = forwardRef<HTMLDialogElement, HTMLMotionProps<'dialog'>>(function PopupDialog(props, ref) {
  return <motion.dialog {...props} {...usePopupMotion('panel')} ref={ref} />;
});
export const Popup = { div: Div, section: Section, form: Form, dialog: Dialog };

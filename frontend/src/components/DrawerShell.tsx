import type { ReactNode } from 'react';
import { motion, useIsPresent, useReducedMotion } from 'motion/react';

type Props = {
  ariaLabel: string;
  className?: string;
  children: (requestClose: () => void) => ReactNode;
  onClose: () => void;
};

export function DrawerShell({ ariaLabel, className = '', children, onClose }: Props) {
  const present = useIsPresent();
  const reduce = useReducedMotion();
  const availability = { 'aria-hidden': present ? undefined : true, inert: present ? undefined : '' };
  const enter = { duration: reduce ? 0 : 0.24, ease: [0.2, 0.8, 0.2, 1] as const };
  const exit = { ...enter, duration: reduce ? 0 : 0.16 };

  // Side panels slide along the viewport edge; only nested dialogs float upward.
  return <motion.div className="overlay drawer-overlay" onClick={onClose} style={{ overflow: 'hidden' }}
    initial={reduce ? false : { backgroundColor: 'rgba(30,30,30,0)' }}
    animate={{ backgroundColor: 'rgba(30,30,30,.16)', transition: enter }}
    exit={{ backgroundColor: 'rgba(30,30,30,0)', transition: exit }}
    {...availability}
  >
    <motion.section
      className={`drawer ${className}`.trim()}
      role="dialog"
      aria-label={ariaLabel}
      aria-modal="true"
      data-popup-kind="drawer"
      data-popup-state={present ? 'open' : 'closed'}
      {...availability}
      initial={reduce ? false : { x: '100%' }}
      animate={{ x: 0, transition: enter }}
      exit={{ x: reduce ? 0 : '100%', transition: exit }}
      onClick={event => event.stopPropagation()}
    >
      {children(onClose)}
    </motion.section>
  </motion.div>;
}

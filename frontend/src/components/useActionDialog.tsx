import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useIsPresent } from 'motion/react';
import { useI18n } from '../i18n';
import { Icon } from './Icon';
import { Popup, PopupPresence } from './PopupMotion';

type Question = { id: number; title: string; initial?: string };
type Answer = string | boolean | null;

function ActionDialog({ question, onAnswer }: { question: Question; onAnswer: (answer: Answer) => void }) {
  const { t } = useI18n();
  const titleId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const [value, setValue] = useState(question.initial || '');
  const prompt = question.initial !== undefined;
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement as HTMLElement | null;
    if (typeof element?.showModal === 'function') element.showModal();
    else element?.setAttribute('open', '');
    return () => {
      if (typeof element?.close === 'function') element.close();
      else element?.removeAttribute('open');
      if (previous?.isConnected && !previous.closest('[inert]') && !document.activeElement?.closest('dialog[open], [data-popup-state="open"][aria-modal="true"]')) previous.focus();
    };
  }, []);
  return createPortal(<Popup.dialog ref={dialog} className="project-dialog action-dialog" role={prompt ? 'dialog' : 'alertdialog'} aria-modal="true" aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); event.stopPropagation(); onAnswer(null); }}>
    <form onSubmit={event => { event.preventDefault(); if (!prompt || value.trim()) onAnswer(prompt ? value.trim() : true); }}>
      <div className="dialog-head"><div><h2 id={titleId}>{question.title}</h2></div><button type="button" className="dialog-close" aria-label={t('Close', '关闭')} onClick={() => onAnswer(null)}><Icon name="close" size={17} /></button></div>
      {prompt && <input type="text" aria-labelledby={titleId} autoFocus value={value} onChange={event => setValue(event.target.value)} />}
      <div className="dialog-actions"><button type="button" className="secondary-command" autoFocus={!prompt} onClick={() => onAnswer(null)}>{t('Cancel', '取消')}</button><button type="submit" className={`primary-command${prompt ? '' : ' destructive-command'}`} disabled={prompt && !value.trim()}>{prompt ? t('Save', '保存') : t('Delete', '删除')}</button></div>
    </form>
  </Popup.dialog>, document.body);
}

/** Keeps native top-layer focus handling while allowing the shared exit animation. */
export function useActionDialog(scope?: string) {
  const [question, setQuestion] = useState<Question | null>(null);
  const pending = useRef<((answer: Answer) => void) | null>(null);
  const sequence = useRef(0);
  const present = useIsPresent();
  const answer = useCallback((value: Answer) => {
    const resolve = pending.current;
    pending.current = null;
    setQuestion(null);
    resolve?.(value);
  }, []);
  useEffect(() => {
    answer(null);
    return () => { pending.current?.(null); pending.current = null; };
  }, [scope, present, answer]);
  const ask = useCallback((title: string, initial?: string) => new Promise<Answer>(resolve => {
    pending.current?.(null);
    pending.current = resolve;
    setQuestion({ id: ++sequence.current, title, initial });
  }), []);
  const askConfirm = useCallback(async (title: string) => (await ask(title)) === true, [ask]);
  const askPrompt = useCallback(async (title: string, initial: string) => {
    const result = await ask(title, initial);
    return typeof result === 'string' ? result : null;
  }, [ask]);
  return { askConfirm, askPrompt, actionDialog: <PopupPresence>{question && <ActionDialog key={question.id} question={question} onAnswer={answer} />}</PopupPresence> };
}

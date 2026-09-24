import { Popup } from './PopupMotion';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { apiFetch } from '../api';
import { Icon } from './Icon';
import { useI18n, type Translate } from '../i18n';

type Props = {
  skill: { name?: string; source?: string };
  onClose: () => void;
  children?: ReactNode;
  summary?: string;
  closeLabel?: string;
};

function errorMessage(value: unknown, t: Translate): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    const payload = value as Record<string, unknown>;
    return errorMessage(payload.detail ?? payload.error ?? payload.message, t);
  }
  return t('Failed to load skill content. Try again.', 'Skill 内容读取失败，请重试');
}

export function SkillDetails({ skill, onClose, children, summary, closeLabel }: Props) {
  const { t } = useI18n();
  const dialog = useRef<HTMLDialogElement>(null);
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement as HTMLElement | null;
    if (element) {
      if (typeof element.showModal === 'function') element.showModal();
      else element.setAttribute('open', '');
    }
    return () => {
      if (element && typeof element.close === 'function') element.close(); else element?.removeAttribute('open');
      if (previous?.isConnected && !previous.closest('[inert]') && !document.activeElement?.closest('dialog[open], [data-popup-state="open"][aria-modal="true"]')) previous.focus();
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setLoading(true); setContent(''); setError('');
    async function load() {
      try {
        const query = ['bundled', 'user', 'review_queue'].includes(skill.source || '')
          ? `?source=${encodeURIComponent(skill.source!)}` : '';
        const response = await apiFetch(`/api/skills/${encodeURIComponent(skill.name || '')}${query}`, { signal: controller.signal });
        const data = await response.json();
        if (!response.ok) throw new Error(errorMessage(data, t));
        if (typeof data.skill?.content !== 'string') throw new Error(t('The skill response is invalid. Try again.', 'Skill 返回内容无效，请重试'));
        if (active) setContent(data.skill.content);
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : t('Failed to load skill content. Try again.', 'Skill 内容读取失败，请重试'));
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => { active = false; controller.abort(); };
  }, [skill.name, skill.source, attempt, t]);

  const effectiveCloseLabel = closeLabel || t('Close skill details', '关闭 Skill 详情');

  return createPortal(
    <Popup.dialog ref={dialog} className="skill-reader" aria-labelledby="skill-reader-title" onCancel={event => { event.preventDefault(); onClose(); }}>
      <header className="skill-reader-head">
        <div><small>SKILL.md</small><h2 id="skill-reader-title">{skill.name || t('Skill details', 'Skill 详情')}</h2></div>
        <button type="button" className="plain-icon" aria-label={effectiveCloseLabel} title={effectiveCloseLabel} onClick={onClose} autoFocus><Icon name="close" size={20} /></button>
      </header>
      <div className="skill-reader-body" aria-busy={loading}>
        {summary && <p>{summary}</p>}
        {loading ? <p role="status">{t('Loading skill content…', '正在加载 Skill 内容…')}</p> : error ? <div role="alert"><p>{error}</p><button type="button" onClick={() => setAttempt(value => value + 1)}>{t('Reload', '重新加载')}</button></div> : <pre>{content || t('This skill has no content.', '此 Skill 暂无正文')}</pre>}
      </div>
      {children ? <footer className="skill-reader-actions">{children}</footer> : null}
    </Popup.dialog>, document.body,
  );
}

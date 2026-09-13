import { useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { apiFetch } from '../api';
import { Icon } from './Icon';

type Props = {
  skill: { name?: string; source?: string };
  onClose: () => void;
  children?: ReactNode;
  summary?: string;
  closeLabel?: string;
};

function errorMessage(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    const payload = value as Record<string, unknown>;
    return errorMessage(payload.detail ?? payload.error ?? payload.message);
  }
  return 'Skill 内容读取失败，请重试';
}

export function SkillDetails({ skill, onClose, children, summary, closeLabel = '关闭 Skill 详情' }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [content, setContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const element = dialog.current;
    if (element) {
      if (typeof element.showModal === 'function') element.showModal();
      else element.setAttribute('open', '');
    }
    return () => { if (element && typeof element.close === 'function') element.close(); else element?.removeAttribute('open'); };
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
        if (!response.ok) throw new Error(errorMessage(data));
        if (typeof data.skill?.content !== 'string') throw new Error('Skill 返回内容无效，请重试');
        if (active) setContent(data.skill.content);
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : 'Skill 内容读取失败，请重试');
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => { active = false; controller.abort(); };
  }, [skill.name, skill.source, attempt]);

  return createPortal(
    <dialog ref={dialog} className="skill-reader" aria-labelledby="skill-reader-title" onCancel={event => { event.preventDefault(); onClose(); }}>
      <header className="skill-reader-head">
        <div><small>SKILL.md</small><h2 id="skill-reader-title">{skill.name || 'Skill 详情'}</h2></div>
        <button type="button" className="plain-icon" aria-label={closeLabel} onClick={onClose} autoFocus><Icon name="close" size={20} /></button>
      </header>
      <div className="skill-reader-body" aria-busy={loading}>
        {summary && <p>{summary}</p>}
        {loading ? <p role="status">正在加载 Skill 内容…</p> : error ? <div role="alert"><p>{error}</p><button type="button" onClick={() => setAttempt(value => value + 1)}>重新加载</button></div> : <pre>{content || '此 Skill 暂无正文'}</pre>}
      </div>
      {children ? <footer className="skill-reader-actions">{children}</footer> : null}
    </dialog>, document.body,
  );
}

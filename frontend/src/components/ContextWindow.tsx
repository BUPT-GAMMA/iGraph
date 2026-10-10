import { useEffect, useId, useRef, useState } from 'react';
import { getContextWindow, type ContextWindowUsage, type ModelConfiguration } from '../api';
import { useI18n } from '../i18n';
import { Icon } from './Icon';
import './ContextWindow.css';

export type ContextModel = Pick<ModelConfiguration, 'config_id' | 'revision' | 'context_window' | 'max_output_tokens'>;

export function ContextWindow({ sessionId, model, loading }: { sessionId?: string; model?: ContextModel; loading: boolean }) {
  const { t } = useI18n();
  const identity = `${sessionId || 'draft'}:${model?.config_id}:${model?.revision}`;
  const panelId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [openIdentity, setOpenIdentity] = useState<string | null>(null);
  const open = openIdentity === identity;
  useEffect(() => {
    if (!open) return;
    panelRef.current?.focus({ preventScroll: true });
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpenIdentity(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setOpenIdentity(null); triggerRef.current?.focus(); }
    };
    const closeOnBlur = (event: FocusEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpenIdentity(null);
    };
    document.addEventListener('pointerdown', closeOutside);
    document.addEventListener('keydown', closeOnEscape);
    document.addEventListener('focusin', closeOnBlur);
    return () => {
      document.removeEventListener('pointerdown', closeOutside);
      document.removeEventListener('keydown', closeOnEscape);
      document.removeEventListener('focusin', closeOnBlur);
    };
  }, [open]);
  const [state, setState] = useState<{ identity: string; usage: ContextWindowUsage | null; error?: boolean }>({ identity, usage: null });
  useEffect(() => {
    if (!sessionId || !model) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = async () => {
      try {
        const response = await getContextWindow(sessionId, controller.signal);
        if (!controller.signal.aborted) setState({ identity, usage: response.context_window_usage });
      } catch {
        if (!controller.signal.aborted) setState({ identity, usage: null, error: true });
      } finally {
        if (loading && !controller.signal.aborted) timer = setTimeout(refresh, 5000);
      }
    };
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [identity, loading]);

  const candidate = state.identity === identity ? state.usage : null;
  const usage = candidate && ['last_request_estimate', 'provider_usage'].includes(candidate.basis) && candidate.config_id === model?.config_id
    && (candidate.model_revision == null || candidate.model_revision === model.revision)
    && candidate.context_window === model?.context_window
    && [candidate.context_window, candidate.remaining_tokens, candidate.input_tokens, candidate.output_reserve, candidate.safety_reserve, candidate.tool_tokens].every(value => Number.isFinite(value) && value >= 0)
    && candidate.context_window > 0 ? candidate : null;
  const format = (value: number) => Math.round(value).toLocaleString('en-US');
  const remaining = usage ? Math.min(usage.remaining_tokens, usage.context_window) : 0;
  const percent = usage ? remaining / usage.context_window * 100 : 0;
  const unavailable = state.identity === identity && state.error;
  const status = usage ? t(`${percent.toFixed(1)}% left`, `剩余 ${percent.toFixed(1)}%`)
    : unavailable ? t('Usage unavailable', '用量暂不可用') : t('Awaiting estimate', '等待估算');
  const closePanel = () => { setOpenIdentity(null); triggerRef.current?.focus(); };
  return <div ref={rootRef} className={`context-window ${usage && percent < 10 ? 'context-window-low' : ''}`}>
    <button ref={triggerRef} type="button" className="context-window-trigger" aria-expanded={open} aria-controls={panelId} aria-haspopup="dialog"
      aria-label={t(`Context usage: ${status}`, `上下文用量：${status}`)}
      title={usage ? t(`${format(remaining)} tokens available · View usage`, `可用 ${format(remaining)} tokens · 查看用量`) : t('View context usage', '查看上下文用量')}
      onClick={() => setOpenIdentity(open ? null : identity)}>
      <span className="context-window-label"><Icon name="pulse" size={13} />{t('Context', '上下文')}</span>
      {usage && <span className="context-window-track" aria-hidden="true"><span style={{ width: `${percent}%` }} /></span>}
      <span className="context-window-status">{status}</span>
      <span className={`context-window-chevron ${open ? 'is-open' : ''}`}><Icon name="chevron" size={12} /></span>
    </button>
    {open && <div ref={panelRef} id={panelId} className="context-window-details" role="dialog" aria-label={t('Context usage details', '上下文用量明细')} tabIndex={-1}>
      <div className="context-window-head">
        <strong>{t('Context window', '上下文窗口')}</strong>
        {usage && <span className="context-window-source">{usage.basis === 'provider_usage' ? t('Reported', '接口统计') : t('Estimated', '估算')}</span>}
        <button type="button" className="context-window-close" onClick={closePanel} aria-label={t('Close context details', '关闭上下文明细')}><Icon name="close" size={14} /></button>
      </div>
      {usage ? <>
        <div className="context-window-amount"><strong>{format(remaining)}</strong><span>{t('tokens available', 'tokens 可用')}</span></div>
        <meter min={0} max={usage.context_window} value={remaining} aria-label={t('Remaining context tokens', '剩余上下文 Token')} />
        <div className="context-window-capacity"><span>{t('Total capacity', '总窗口')} {format(usage.context_window)}</span><span>{status}</span></div>
        <dl>
        <div><dt>{t('Request input (includes tools)', '请求输入（含工具）')}</dt><dd>{format(usage.input_tokens)}</dd></div>
        <div className="context-window-tool-count"><dt>{t('Tool definitions · estimated', '其中工具定义 · 估算')}</dt><dd>{format(usage.tool_tokens)}</dd></div>
        <div><dt>{t('Reserved for reply', '回复预留')}</dt><dd>{format(usage.output_reserve)}</dd></div>
        <div><dt>{t('Safety margin', '安全余量')}</dt><dd>{format(usage.safety_reserve)}</dd></div>
        </dl>
        <p className="context-window-note">{usage.basis === 'provider_usage'
          ? t('Based on the latest input reported by the provider, with reply space reserved. Excludes the unsent draft; not cumulative usage.', '按接口最近一次输入统计，已预留回复空间。不含未发送内容，非累计用量。')
          : t('Estimated from the latest request, with reply space reserved. Excludes the unsent draft; not cumulative usage.', '按最近一次请求估算，已预留回复空间。不含未发送内容，非累计用量。')}</p>
      </> : <>
        {model && <p className="context-window-empty-capacity">{t('Total capacity', '总窗口')} <strong>{format(model.context_window)}</strong> tokens</p>}
        <p className="context-window-note">{unavailable ? t('Usage could not be loaded. It will refresh on your next message.', '暂时无法读取用量，发送下一条消息时会重新更新。')
          : t('Usage appears after a message is sent. Older conversations may not have a saved estimate.', '发送消息后显示用量，旧会话可能没有已保存的统计。')}</p>
      </>}
      {usage?.history_compacted && <p className="context-window-compacted"><Icon name="check" size={12} />{t('Earlier history compacted', '较早历史已压缩')}</p>}
      {usage && percent < 10 && <p className="context-window-warning">{t('Little context remains. Start a new task or choose a larger window.', '可用上下文较少，建议新建任务或选择更大窗口。')}</p>}
    </div>}
  </div>;
}

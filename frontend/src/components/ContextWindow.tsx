import { useEffect, useState } from 'react';
import { getContextWindow, type ContextWindowUsage, type ModelConfiguration } from '../api';
import { useI18n } from '../i18n';
import './ContextWindow.css';

export type ContextModel = Pick<ModelConfiguration, 'config_id' | 'revision' | 'context_window' | 'max_output_tokens'>;

export function ContextWindow({ sessionId, model, loading }: { sessionId?: string; model?: ContextModel; loading: boolean }) {
  const { t } = useI18n();
  const identity = `${sessionId || 'draft'}:${model?.config_id}:${model?.revision}`;
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
  return <details className={`context-window ${usage && percent < 10 ? 'context-window-low' : ''}`}>
    <summary>
      <span>{t('LLM context', 'LLM 上下文')}</span>
      <strong>{usage ? t(`≈ ${format(remaining)} tokens left (${percent.toFixed(1)}%)`, `约剩 ${format(remaining)} tokens（${percent.toFixed(1)}%）`)
        : unavailable ? t('Usage unavailable', '用量暂不可用') : t('Awaiting estimate', '等待估算')}</strong>
      {usage && <meter min={0} max={usage.context_window} value={remaining} aria-label={t('Remaining context tokens', '剩余上下文 Token')} />}
    </summary>
    <div className="context-window-details">
      <p>{usage?.basis === 'provider_usage'
        ? t('Calculated from the provider’s reported input for the latest request, including conversation, system instructions and tools. Output space and a safety margin are reserved. This is not cumulative billing usage; the unsent draft is not included.', '按接口返回的最近一次请求输入用量计算，包含对话、系统指令及工具，已预留回复空间和安全余量。此值不是累计计费用量，也不包含尚未发送的草稿。')
        : t('Estimated from the latest model request, including conversation, system instructions and tools. Output space and a safety margin are reserved. This is not cumulative billing usage; the unsent draft is not included.', '按最近一次模型请求估算，包含对话、系统指令及工具，已预留回复空间和安全余量。此值不是累计计费用量，也不包含尚未发送的草稿。')}</p>
      {model && <p>{t('Configured window', '配置的窗口')}：{format(model.context_window)} tokens</p>}
      {usage ? <dl>
        <div><dt>{t('Request input (includes tools)', '请求输入（含工具）')}</dt><dd>{format(usage.input_tokens)}</dd></div>
        <div><dt>{t('Tool definitions (estimated)', '工具定义（估算）')}</dt><dd>{format(usage.tool_tokens)}</dd></div>
        <div><dt>{t('Reserved for reply', '回复预留')}</dt><dd>{format(usage.output_reserve)}</dd></div>
        <div><dt>{t('Safety margin', '安全余量')}</dt><dd>{format(usage.safety_reserve)}</dd></div>
      </dl> : <p>{t('Send a message to measure the selected model’s request. Older conversations may not have a saved estimate.', '发送消息后会统计所选模型的请求。旧会话可能没有已保存的估算值。')}</p>}
      {usage?.history_compacted && <p>{t('Earlier history was compacted for this request.', '本次请求已压缩较早的会话历史。')}</p>}
      {usage && percent < 10 && <p>{t('Little context remains. Consider a new task or a model with a larger window.', '可用上下文较少，可新建任务或选择更大窗口的模型。')}</p>}
    </div>
  </details>;
}

import { useId, useState } from 'react';
import type { ProcessItem, ToolCall } from '../types';
import { Icon } from './Icon';
import { useI18n, type Translate } from '../i18n';

import labels from '../toolLabels.json';

const toolLabels: Readonly<Record<string, string>> = labels;

function toolStatus(tool: ToolCall | undefined, t: Translate) {
  if (tool?.status === 'failed') return { label: t('Failed', '失败'), className: 'is-error' };
  if (tool?.status === 'cancelled') return { label: t('Cancelled', '已取消'), className: 'is-cancelled' };
  if (tool?.done || tool?.status === 'completed') return { label: t('Complete', '完成'), className: 'is-complete' };
  return { label: t('Running', '运行中'), className: 'is-running' };
}

function formatError(error: unknown, t: Translate): string {
  if (error && typeof error === 'object' && 'message' in error) return String(error.message).split('\n')[0];
  return typeof error === 'string' ? error.split('\n')[0] : t('Tool execution failed', '工具执行失败');
}

function safeError(tool: ToolCall, t: Translate) {
  if (tool.status !== 'failed') return '';
  const result = tool.result;
  if (result && typeof result === 'object' && 'error' in result) return formatError((result as { error?: unknown }).error, t);
  return t('Tool execution failed', '工具执行失败');
}

function englishToolLabel(name: string) {
  return name.split('_').filter(Boolean).map(part => {
    const lower = part.toLowerCase();
    if (lower === 'sql') return 'SQL';
    if (lower === 'csv') return 'CSV';
    if (lower === 'id') return 'ID';
    if (lower === 'ml') return 'ML';
    return part.charAt(0).toUpperCase() + part.slice(1);
  }).join(' ');
}

function ProcessText({ item }: { item: ProcessItem }) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);
  const contentId = useId();
  const reasoning = item.kind === 'reasoning';
  const label = reasoning ? t('Reasoning', '思考') : t('Note', '说明');
  const content = item.content || (reasoning ? t('Analyzing context…', '正在分析上下文…') : '');
  return <button type="button"
    className={`process-item process-disclosure ${reasoning ? 'process-reasoning-item' : 'process-text-item'}${expanded ? ' is-expanded' : ''}`}
    aria-label={expanded ? t(`Collapse ${label}`, `收起${label}`) : t(`Expand ${label}`, `展开${label}`)}
    aria-expanded={expanded} aria-controls={contentId}
    onClick={() => setExpanded(value => !value)}>
    <span className="process-kind"><Icon name={reasoning ? 'spark' : 'message'} size={13} />{label}</span>
    <span className="process-copy">
      <span className="process-preview" aria-hidden="true" data-preview={content} />
      <span className="process-expanded" id={contentId} aria-hidden={!expanded}>
        <span className="process-clip"><span className="process-full-text">{content}</span></span>
      </span>
    </span>
    <span className="process-disclosure-icon"><Icon name="chevron" size={13} /></span>
  </button>;
}

export function ProcessTimeline({ items = [], tools: liveTools = [], compact = false, streaming = false, thinkingDurationMs }: { items?: ProcessItem[]; tools?: ToolCall[]; compact?: boolean; streaming?: boolean; thinkingDurationMs?: number }) {
  const { language, t } = useI18n();
  const [expanded, setExpanded] = useState<boolean | null>(null);
  if (!items.length) return null;
  const ordered = items.map(item => {
    if (item.kind !== 'tool') return item;
    const live = liveTools.find(tool => tool.call_id && tool.call_id === item.tool?.call_id);
    if (!live) return item;
    // Graph jobs can continue after the agent turn; dev's job state remains authoritative.
    const result = live.result && typeof live.result === 'object' ? live.result as Record<string, unknown> : {};
    const jobId = live.job_id || result.job_id || live.progress?.job_id;
    const failed = result.success === false || result.status === 'failed' || live.progress?.kind === 'job_failed';
    const cancelled = result.status === 'cancelled' || live.progress?.kind === 'job_cancelled';
    const status: ToolCall['status'] = cancelled ? 'cancelled' : failed ? 'failed' : live.done ? 'completed' : 'running';
    return { ...item, tool: { ...item.tool, progress: live.progress || item.tool?.progress,
      ...(jobId ? { ...live, status } : {}) } };
  }).sort((a, b) => a.sequence - b.sequence);
  const tools = ordered.filter(item => item.kind === 'tool');
  const running = tools.filter(item => !item.tool?.done && !['completed', 'failed', 'cancelled'].includes(item.tool?.status || '')).length;
  const label = running
    ? t(`${running} running`, `${running} 项运行中`)
    : t(`${ordered.length} item${ordered.length === 1 ? '' : 's'}`, `${ordered.length} 项`);
  const seconds = thinkingDurationMs === undefined ? undefined : Math.max(0, Math.round(thinkingDurationMs / 1000));
  const heading = seconds === undefined ? (streaming ? t('Running', '正在执行') : t('Process', '执行过程'))
    : streaming ? t(`Thinking · ${seconds}s`, `思考中 · ${seconds}s`) : t(`Thought for ${seconds}s`, `思考了 ${seconds}s`);
  return <details className={`process-panel ${compact ? 'is-compact' : ''}`} open={expanded ?? (compact && (streaming || running > 0))}
    onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><Icon name="chevron" size={13} /><span>{heading}</span><span className="process-count">{label}</span></summary>
    <div className="process-timeline">
    {ordered.map(item => {
      if (item.kind === 'text' || item.kind === 'reasoning') return <ProcessText key={item.id} item={item} />;
      const tool = item.tool;
      const status = toolStatus(tool, t);
      const epoch = Number(tool?.progress?.epoch);
      const epochs = Number(tool?.progress?.epochs);
      const progress = Number.isFinite(epoch) && Number.isFinite(epochs) && epochs > 0
        ? t(`Epoch ${epoch}/${epochs}`, `轮次 ${epoch}/${epochs}`) : tool?.progress?.message;
      const detail = safeError(tool || {}, t) || String(progress || '');
      const summary = detail || status.label;
      const name = tool?.tool || 'graph_tool';
      const displayName = language === 'zh' ? toolLabels[name] || name : englishToolLabel(name);
      return <div className={`process-item process-tool-item ${status.className}`} key={item.id} role="status" aria-label={(tool?.tool || 'graph_tool') + t(': ', '：') + summary}>
        <span className="process-kind"><Icon name="tool" size={13} />{t('Tool', '工具')}</span>
        <div className="process-tool-body">
          <span className="process-tool-name" title={name}>{displayName}</span>
          {displayName !== name ? <span className="process-tool-code" title={name}>{name}</span> : null}
          {detail ? <span className="process-tool-detail">{detail}</span> : null}
        </div>
        <span className="process-status">
          {status.className === 'is-running' ? <span className="loader" aria-hidden="true" /> : <Icon name={status.className === 'is-complete' ? 'check' : 'close'} size={13} />}
          {status.label}
        </span>
      </div>;
    })}
    </div>
  </details>;
}
